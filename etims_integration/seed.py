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
"""

import csv
import os

import frappe
from frappe import _

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
