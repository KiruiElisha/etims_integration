"""
Seed data.

Two separate jobs, deliberately kept apart:

* :func:`import_item_classifications` loads the **real** KRA classification list
  from a CSV you supply. The official list runs to thousands of codes and is
  published as a spreadsheet, so this app ships almost none of it -- inventing
  plausible-looking codes would be worse than shipping none, because a wrong
  classification is accepted by the device and misdeclares the goods to KRA.
  Only the two generic codes that appear in the vendor documentation are seeded
  by ``install.py``.

* :func:`create_demo_items` builds a set of fully-configured test items, one per
  VAT band, so a new site can be exercised end to end without hand-filling KRA
  codes on real stock first. They are clearly named and can be deleted.

* :func:`load_demo_catalogue` loads the shipped 479-item PLU sheet
  (``data/demo_catalogue.csv``), which is a real vendor export. It is the
  *volume* fixture, not the tax fixture: every row on it is standard-rated, so
  it exercises PLU batching, item search and list performance, while
  :func:`create_demo_items` remains the one that covers the VAT bands. The two
  are independent and use different item-code prefixes, so loading or deleting
  one never disturbs the other.
"""

import csv
import os

import frappe
from frappe import _
from frappe.utils import flt

DEMO_PREFIX = "ETIMS-DEMO-"

# One item per band, so a single test invoice can exercise the whole tax mapping
# path -- including the two bands that are easiest to get wrong, Exempt and
# Non-VAT, which look identical at 0% and are not interchangeable.
DEMO_ITEMS = [
	("VAT16", "eTIMS Demo - Standard Rated 16%", "B-16.00%", 1, "02Finished Product"),
	("ZERO", "eTIMS Demo - Zero Rated", "C-0%", 1, "02Finished Product"),
	("EXEMPT", "eTIMS Demo - Exempt", "A-Exempt", 1, "02Finished Product"),
	("NONVAT", "eTIMS Demo - Non VAT", "D-Non-VAT", 1, "02Finished Product"),
	("TOURISM", "eTIMS Demo - Tourism 8%", "E-8%", 1, "02Finished Product"),
	("SERVICE", "eTIMS Demo - Service", "B-16.00%", 0, "03Service without stock"),
]

DEMO_RATE = 580.0
DEMO_CLASSIFICATION = "99010000"
DEMO_PACKAGE_UNIT = "BG"
DEMO_QUANTITY_UNIT = "U"


@frappe.whitelist()
def create_demo_items(item_group=None, uom="Nos"):
	"""
	Create the demo items, skipping any that already exist.

	Everything the device requires is filled in, so they are registrable the
	moment they are created -- which is the point: it separates "my eTIMS setup
	is wrong" from "this particular item is not configured".
	"""
	frappe.only_for("System Manager")

	item_group = item_group or _default_item_group()
	_require_masters()

	created, skipped = [], []
	for suffix, name, band, is_stock, product_type in DEMO_ITEMS:
		code = f"{DEMO_PREFIX}{suffix}"
		if frappe.db.exists("Item", code):
			skipped.append(code)
			continue

		frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": name,
				"item_group": item_group,
				"stock_uom": uom,
				"is_stock_item": is_stock,
				"is_sales_item": 1,
				"is_purchase_item": 0,
				"standard_rate": DEMO_RATE,
				"description": "Created by the eTIMS app for testing. Safe to delete.",
				"custom_etims_item_class_code": DEMO_CLASSIFICATION,
				"custom_etims_tax_type": band,
				"custom_etims_product_type": product_type,
				"custom_etims_origin_country": "Kenya",
				"custom_etims_package_unit": DEMO_PACKAGE_UNIT,
				"custom_etims_quantity_unit": DEMO_QUANTITY_UNIT,
			}
		).insert(ignore_permissions=True)
		created.append(code)

	return {
		"created": created,
		"skipped": skipped,
		"next": _("Register them from the Item list (Actions > Register with eTIMS), then invoice one of each."),
	}


def _default_item_group():
	for candidate in ("Products", "All Item Groups"):
		if frappe.db.exists("Item Group", candidate):
			return candidate
	group = frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", limit=1)
	if not group:
		frappe.throw(_("No Item Group exists on this site."))
	return group[0]


def _require_masters():
	"""
	Fail early and clearly. Without the code masters the items would be created
	with dangling links and fail at registration instead, which is a much longer
	way round to the same answer.
	"""
	missing = [
		f"{doctype} {name}"
		for doctype, name in (
			("ETIMS Item Classification", DEMO_CLASSIFICATION),
			("ETIMS Packaging Unit", DEMO_PACKAGE_UNIT),
			("ETIMS Quantity Unit", DEMO_QUANTITY_UNIT),
		)
		if not frappe.db.exists(doctype, name)
	]
	if missing:
		frappe.throw(
			_("KRA code masters are not seeded ({0}). Run `bench --site <site> migrate` first.").format(
				", ".join(missing)
			)
		)


@frappe.whitelist()
def delete_demo_items():
	"""Remove the demo items, provided nothing has been invoiced against them."""
	frappe.only_for("System Manager")

	deleted, kept = [], []
	for suffix, *_rest in DEMO_ITEMS:
		code = f"{DEMO_PREFIX}{suffix}"
		if not frappe.db.exists("Item", code):
			continue
		try:
			frappe.delete_doc("Item", code, ignore_permissions=True)
			deleted.append(code)
		except frappe.LinkExistsError:
			# Linked to a transaction. Deleting would orphan it; that is a refusal,
			# not a failure.
			kept.append(code)

	return {"deleted": deleted, "kept_because_in_use": kept}


# ------------------------------------------------------- classification import


@frappe.whitelist()
def import_item_classifications(file_path, code_column="code", description_column="description"):
	"""
	Load the official KRA classification codes from a CSV.

	Export the KRA sheet to CSV and point this at it::

	    bench --site <site> execute \\
	        etims_integration.seed.import_item_classifications \\
	        --args "['/path/to/kra_item_classes.csv']"

	Existing codes are left alone rather than overwritten -- a site may well have
	corrected a description, and an import should not undo that.

	Source spreadsheet:
	https://docs.google.com/spreadsheets/d/1g3Xm0g6rgLNVp8h5paTednaBRkfzppnTulqg2OY5xNI
	"""
	frappe.only_for("System Manager")

	if not os.path.isfile(file_path):
		frappe.throw(_("No such file: {0}").format(file_path))

	created = skipped = 0
	problems = []

	with open(file_path, newline="", encoding="utf-8-sig") as handle:
		reader = csv.DictReader(handle)
		if code_column not in (reader.fieldnames or []):
			frappe.throw(
				_("The CSV has no '{0}' column. Columns found: {1}").format(
					code_column, ", ".join(reader.fieldnames or [])
				)
			)

		for index, row in enumerate(reader, start=2):
			code = (row.get(code_column) or "").strip()
			if not code:
				continue
			if frappe.db.exists("ETIMS Item Classification", code):
				skipped += 1
				continue
			try:
				frappe.get_doc(
					{
						"doctype": "ETIMS Item Classification",
						"code": code,
						"description": (row.get(description_column) or "").strip()[:140],
					}
				).insert(ignore_permissions=True)
				created += 1
			except Exception as e:
				problems.append(f"row {index} ({code}): {e}")

	return {
		"created": created,
		"skipped_existing": skipped,
		"problems": problems[:20],
		"total": frappe.db.count("ETIMS Item Classification"),
	}


# ----------------------------------------------------------- demo catalogue

DEMO_CATALOGUE_PREFIX = "ETIMS-DEMO-PLU-"
CATALOGUE_FILE = "demo_catalogue.csv"

# Columns the loader actually reads. The shipped file carries the vendor's full
# PLU layout so a site can drop in its own export unchanged, but only these are
# needed to build an Item.
CATALOGUE_COLUMNS = (
	"PLUNo",
	"PLUName",
	"UnitPrice",
	"item_ClsCode",
	"pkgUnitCd",
	"qtyUnitCd",
	"OrgnNatCd",
	"TaxType",
	"TypeCode",
)


def _catalogue_path():
	return os.path.join(frappe.get_app_path("etims_integration"), "data", CATALOGUE_FILE)


def _code_of(value):
	"""
	The PLU sheet writes a coded field as ``<code>-<label>`` -- ``BG-Bag``,
	``U-Pieces/item [Number]``, ``KE-KENYA`` -- but the masters are keyed on the
	code alone.

	Not used for TaxType or TypeCode: those are Select fields whose stored value
	*is* the full string ("B-16.00%", "02Finished Product"), and splitting them
	would quietly turn every item Exempt-shaped.
	"""
	return (value or "").split("-", 1)[0].strip()


def _read_catalogue(limit=None):
	path = _catalogue_path()
	if not os.path.isfile(path):
		frappe.throw(_("The demo catalogue is missing from the app: {0}").format(path))

	with open(path, newline="", encoding="utf-8-sig") as handle:
		reader = csv.DictReader(handle)
		missing = [c for c in CATALOGUE_COLUMNS if c not in (reader.fieldnames or [])]
		if missing:
			frappe.throw(_("The catalogue CSV is missing columns: {0}").format(", ".join(missing)))

		rows = [r for r in reader if (r.get("PLUNo") or "").strip()]

	return rows[: int(limit)] if limit else rows


def _require_catalogue_masters(rows):
	"""
	Check the codes the file actually uses, once, before inserting anything.

	The alternative is 479 individual link-validation failures, which reports the
	same single missing master 479 times and leaves a half-loaded catalogue behind.
	"""
	wanted = set()
	for row in rows:
		wanted.add(("ETIMS Item Classification", (row.get("item_ClsCode") or "").strip()))
		wanted.add(("ETIMS Packaging Unit", _code_of(row.get("pkgUnitCd"))))
		wanted.add(("ETIMS Quantity Unit", _code_of(row.get("qtyUnitCd"))))

	missing = sorted(
		f"{doctype} {name}"
		for doctype, name in wanted
		if name and not frappe.db.exists(doctype, name)
	)
	if missing:
		frappe.throw(
			_("KRA code masters are not seeded ({0}). Run `bench --site <site> migrate` first.").format(
				", ".join(missing)
			)
		)


def _country_names(iso_codes):
	"""
	``OrgnNatCd`` is an ISO alpha-2 code but the Item field links to Country by
	name, so the codes are resolved once per run into ``{code: name}``.

	Resolved per run rather than cached on the module: a worker process outlives
	the request, and a cached miss would keep reporting a Country as missing after
	someone had added it and re-run.

	A code with no matching Country is absent from the result, which the caller
	reports as a per-row problem rather than guessing a country.
	"""
	names = {}
	for code in {(c or "").strip().lower() for c in iso_codes} - {""}:
		name = frappe.db.get_value("Country", {"code": code}, "name")
		if name:
			names[code] = name
	return names


@frappe.whitelist()
def load_demo_catalogue(limit=None, item_group=None, uom="Nos"):
	"""
	Create Items from the shipped PLU sheet, skipping any that already exist.

	::

	    bench --site <site> execute etims_integration.seed.load_demo_catalogue
	    bench --site <site> execute etims_integration.seed.load_demo_catalogue --kwargs "{'limit': 25}"

	``limit`` takes the first N rows. 479 items is a lot to look at when all you
	wanted was something to put on a test invoice, and a partial load is topped up
	by re-running without it.
	"""
	frappe.only_for("System Manager")

	rows = _read_catalogue(limit)
	_require_catalogue_masters(rows)
	item_group = item_group or _default_item_group()
	countries = _country_names(_code_of(r.get("OrgnNatCd")) for r in rows)

	created = skipped = 0
	problems = []

	for index, row in enumerate(rows, start=2):
		plu_no = (row.get("PLUNo") or "").strip()
		code = f"{DEMO_CATALOGUE_PREFIX}{plu_no.zfill(4)}"

		if frappe.db.exists("Item", code):
			skipped += 1
			continue

		country = countries.get(_code_of(row.get("OrgnNatCd")).lower())
		if not country:
			problems.append(f"row {index} ({code}): no Country matches '{row.get('OrgnNatCd')}'")
			continue

		try:
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": (row.get("PLUName") or code).strip()[:140],
					"item_group": item_group,
					"stock_uom": uom,
					"is_stock_item": 1,
					"is_sales_item": 1,
					"is_purchase_item": 0,
					"standard_rate": flt(row.get("UnitPrice")),
					"description": "Loaded by the eTIMS app from the demo catalogue. Safe to delete.",
					"custom_etims_item_class_code": (row.get("item_ClsCode") or "").strip(),
					"custom_etims_tax_type": (row.get("TaxType") or "").strip(),
					"custom_etims_product_type": (row.get("TypeCode") or "").strip(),
					"custom_etims_origin_country": country,
					"custom_etims_package_unit": _code_of(row.get("pkgUnitCd")),
					"custom_etims_quantity_unit": _code_of(row.get("qtyUnitCd")),
				}
			).insert(ignore_permissions=True)
			created += 1
		except Exception as e:
			problems.append(f"row {index} ({code}): {e}")

		# Commit in batches. A single transaction spanning 479 inserts holds locks
		# for the whole run and loses everything if one row at the end throws.
		if created and created % 50 == 0:
			frappe.db.commit()

	frappe.db.commit()

	return {
		"created": created,
		"skipped_existing": skipped,
		"problems": problems[:20],
		"problem_count": len(problems),
		"total_on_site": frappe.db.count("Item", {"item_code": ["like", f"{DEMO_CATALOGUE_PREFIX}%"]}),
		"next": _(
			"Register them from the Item list (Actions > Register with eTIMS), "
			"then upload the PLU data to the device."
		),
	}


@frappe.whitelist()
def delete_demo_catalogue():
	"""
	Remove every catalogue item, except any that a transaction now points at.

	::

	    bench --site <site> execute etims_integration.seed.delete_demo_catalogue

	Matches on the item-code prefix rather than re-reading the CSV, so items left
	behind by an older version of the file are cleaned up too.
	"""
	frappe.only_for("System Manager")

	codes = frappe.get_all(
		"Item", filters={"item_code": ["like", f"{DEMO_CATALOGUE_PREFIX}%"]}, pluck="name"
	)

	deleted = 0
	kept, problems = [], []

	for code in codes:
		try:
			frappe.delete_doc("Item", code, ignore_permissions=True)
			deleted += 1
		except frappe.LinkExistsError:
			# Invoiced, or carrying stock. Deleting would orphan the transaction;
			# that is a refusal, not a failure.
			kept.append(code)
		except Exception as e:
			problems.append(f"{code}: {e}")

		if deleted and deleted % 50 == 0:
			frappe.db.commit()

	frappe.db.commit()

	return {
		"deleted": deleted,
		"kept_because_in_use": kept[:20],
		"kept_count": len(kept),
		"problems": problems[:20],
		"remaining": frappe.db.count("Item", {"item_code": ["like", f"{DEMO_CATALOGUE_PREFIX}%"]}),
	}
