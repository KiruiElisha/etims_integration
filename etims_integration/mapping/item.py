"""
Item -> PLU record.

eTIMS refuses any invoice line whose item is not already registered on the device
(E337), so this mapping is not an optional extra: nothing can be fiscalised until
it has run. It is also where most first-deployment pain lives, because KRA
demands five classification codes per item that ERPNext has no native field for.

The device treats ``plu_name`` and ``barcode`` as unique keys and rejects
duplicates (E353), so registration is idempotent on the PLU number we assign and
keep on the Item.
"""

import hashlib
import json
from decimal import Decimal

import frappe
from frappe import _

from etims_integration.comstore.schema import PLUItem
from etims_integration.mapping import sanitize

ZERO = Decimal("0")

PRODUCT_TYPES = {
	"01": "01Raw Material",
	"02": "02Finished Product",
	"03": "03Service without stock",
}

REQUIRED_FIELDS = (
	("custom_etims_item_class_code", "eTIMS Item Classification"),
	("custom_etims_package_unit", "eTIMS Packaging Unit"),
	("custom_etims_quantity_unit", "eTIMS Quantity Unit"),
	("custom_etims_tax_type", "eTIMS Tax Type"),
)


class ItemNotConfigured(frappe.ValidationError):
	pass


def _dec(value):
	return Decimal(str(value or 0))


def _code_value(doctype, name):
	"""
	The device wants ``CODE-Code Name`` (``BG-Bag``, ``U-Pieces/item [Number]``),
	which is how the KRA master lists render. The masters store both halves so the
	joined form is always spelled the device's way rather than a user's.
	"""
	if not name:
		return ""
	row = frappe.get_cached_value(doctype, name, ["code", "code_name"], as_dict=True)
	if not row:
		return ""
	return f"{row.code}-{row.code_name}" if row.code_name else row.code


def origin_country_code(country):
	"""
	``KE-KENYA``. Derived from ERPNext's own Country master rather than duplicating
	a country list: ``code`` is the ISO-2 and the name supplies the label.
	"""
	if not country:
		return ""
	code = frappe.get_cached_value("Country", country, "code")
	if not code:
		return ""
	return f"{code.upper()}-{country.upper()}"


def missing_configuration(item):
	"""Which eTIMS fields the item still lacks. Empty means it can be registered."""
	return [label for fieldname, label in REQUIRED_FIELDS if not item.get(fieldname)]


def build(item, plu_no, unit_price=None, change_qty=ZERO, stocks=ZERO):
	"""
	Item document -> :class:`PLUItem`.

	``change_qty`` is a *delta* the device applies to its own stock, not an
	absolute -- the doc notes a negative value reduces it. Stock pushes are
	therefore computed by the caller against what was last sent, never from the
	current ERPNext balance alone.
	"""
	missing = missing_configuration(item)
	if missing:
		frappe.throw(
			_("Item {0} cannot be registered with eTIMS until these are set: {1}.").format(
				item.name, ", ".join(missing)
			),
			exc=ItemNotConfigured,
		)

	return PLUItem(
		plu_no=str(plu_no),
		barcode=str(plu_no),
		plu_name=sanitize.item_name(item.item_name or item.name, fallback=item.name),
		unit_price=_dec(unit_price if unit_price is not None else item.get("standard_rate")),
		item_class_code=item.custom_etims_item_class_code,
		package_unit=_code_value("ETIMS Packaging Unit", item.custom_etims_package_unit),
		quantity_unit=_code_value("ETIMS Quantity Unit", item.custom_etims_quantity_unit),
		origin_country=origin_country_code(item.get("custom_etims_origin_country") or item.get("country_of_origin")),
		tax_type=item.custom_etims_tax_type,
		product_type=PRODUCT_TYPES.get(
			(item.get("custom_etims_product_type") or "").strip()[:2],
			"03Service without stock" if not item.get("is_stock_item") else "02Finished Product",
		),
		batch_no="",
		additional_info="",
		safety_qty=_dec(item.get("safety_stock")),
		insurance_applicable=False,
		change_qty=_dec(change_qty),
		stocks=_dec(stocks),
		active=not item.get("disabled"),
	)


# Fields whose change means the device's copy is stale. Deliberately excludes
# stock: stock moves constantly and rides its own delta path, so including it
# here would re-register the whole catalogue every time anything sold.
TRACKED_FIELDS = (
	"item_name",
	"standard_rate",
	"custom_etims_item_class_code",
	"custom_etims_package_unit",
	"custom_etims_quantity_unit",
	"custom_etims_origin_country",
	"custom_etims_tax_type",
	"custom_etims_product_type",
	"disabled",
)


def fingerprint(item):
	"""
	A stable hash of everything the device stores about an item, so a sync can
	skip items that have not actually changed. Cheaper and far less risky than
	re-uploading the catalogue, which invites E353 on every run.
	"""
	payload = {field: str(item.get(field) or "") for field in TRACKED_FIELDS}
	return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
