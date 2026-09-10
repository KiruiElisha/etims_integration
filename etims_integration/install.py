"""
Installation: custom fields and KRA code masters.

Custom fields are created in code rather than shipped as fixtures. Fixtures for
fields on *standard* doctypes are a recurring source of migrate conflicts when
two apps touch the same doctype, and they cannot express "only if absent".
``create_custom_fields`` is idempotent, so this runs safely on install and on
every migrate.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

BANDS = "\nA-Exempt\nB-16.00%\nC-0%\nD-Non-VAT\nE-8%"
PRODUCT_TYPES = "\n01Raw Material\n02Finished Product\n03Service without stock"
REFUND_REASONS = (
	"\n01-Missing Quantity\n02-Missing Data\n03-Damaged/Wasted\n04-Raw Material\n05-Shortage\n06-Refund"
)

# eTIMS has no "price adjustment" concept: a credit note is only ever a price and
# a quantity. The distinction is ours, and it decides how the payload is encoded.
ADJUSTMENT_TYPES = "\nGoods Return\nPrice Adjustment"

# Mirrors ETIMS Transmission.status one-for-one. Anything the Transmission can
# be, the invoice must be able to say it is -- a status the invoice cannot hold
# is a status that silently fails to mirror and leaves the two disagreeing.
ETIMS_STATUSES = "\nNot Sent\nQueued\nSending\nSigned\nFailed\nBlocked\nCancelled"


CUSTOM_FIELDS = {
	"Sales Invoice": [
		{
			"fieldname": "custom_etims_section",
			"label": "eTIMS",
			"fieldtype": "Section Break",
			"insert_after": "taxes_and_charges",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_etims_exempt",
			"label": "Exempt from eTIMS",
			"fieldtype": "Check",
			"insert_after": "custom_etims_section",
			"description": "Skip fiscalisation for this invoice. Use for internal or proforma "
			"documents only - an ordinary sale must be declared.",
		},
		{
			"fieldname": "custom_etims_adjustment_type",
			"label": "eTIMS Credit Note Type",
			"fieldtype": "Select",
			"options": ADJUSTMENT_TYPES,
			"insert_after": "custom_etims_exempt",
			"depends_on": "eval:doc.is_return",
			"mandatory_depends_on": "eval:doc.is_return",
			"default": "Goods Return",
			"description": "Goods Return: stock comes back, credited at the original price. "
			"Price Adjustment: no goods move, and the payload is encoded at the original "
			"unit price with a fractional quantity so the invoice can be adjusted again later.",
		},
		{
			"fieldname": "custom_etims_original_invoice",
			"label": "eTIMS Original Invoice",
			"fieldtype": "Link",
			"options": "Sales Invoice",
			"insert_after": "custom_etims_adjustment_type",
			"depends_on": "eval:doc.is_return",
			"mandatory_depends_on": "eval:doc.is_return && doc.custom_etims_adjustment_type=='Price Adjustment'",
			"description": "The invoice being adjusted. Kept separate from 'Return Against' on "
			"purpose: ERPNext counts a return against the original's quantity, which would "
			"block every later price adjustment.",
		},
		{
			"fieldname": "custom_etims_refund_reason",
			"label": "eTIMS Refund Reason",
			"fieldtype": "Select",
			"options": REFUND_REASONS,
			"insert_after": "custom_etims_original_invoice",
			"depends_on": "eval:doc.is_return",
			"mandatory_depends_on": "eval:doc.is_return",
			"description": "Required by KRA on every credit note (rfdRsnCd).",
		},
		{
			"fieldname": "custom_etims_exemption_number",
			"label": "Tax Exemption Certificate",
			"fieldtype": "Data",
			"insert_after": "custom_etims_refund_reason",
		},
		{
			"fieldname": "custom_etims_branch",
			"label": "eTIMS Branch",
			"fieldtype": "Data",
			"insert_after": "custom_etims_exemption_number",
			"description": "Optional. Routes this invoice to the eTIMS Device configured for the branch.",
		},
		{
			"fieldname": "custom_etims_column_break",
			"fieldtype": "Column Break",
			"insert_after": "custom_etims_branch",
		},
		{
			"fieldname": "custom_etims_status",
			"label": "eTIMS Status",
			"fieldtype": "Select",
			"options": ETIMS_STATUSES,
			"insert_after": "custom_etims_column_break",
			"read_only": 1,
			"allow_on_submit": 1,
			"in_standard_filter": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_transmission",
			"label": "eTIMS Transmission",
			"fieldtype": "Link",
			"options": "ETIMS Transmission",
			"insert_after": "custom_etims_status",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_is_test",
			"label": "Test Mode (not sent to KRA)",
			"fieldtype": "Check",
			"insert_after": "custom_etims_transmission",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
			"depends_on": "eval:doc.custom_etims_is_test",
		},
		# ---- signature: written by the Transmission, printed on the receipt ----
		{
			"fieldname": "custom_etims_signature_section",
			"label": "eTIMS Fiscal Receipt",
			"fieldtype": "Section Break",
			"insert_after": "custom_etims_is_test",
			"collapsible": 1,
			"depends_on": "eval:doc.custom_etims_cu_invoice_no",
		},
		{
			"fieldname": "custom_etims_cu_invoice_no",
			"label": "CU Invoice Number",
			"fieldtype": "Data",
			"insert_after": "custom_etims_signature_section",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_scu_id",
			"label": "SCU ID",
			"fieldtype": "Data",
			"insert_after": "custom_etims_cu_invoice_no",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_receipt_signature",
			"label": "Receipt Signature",
			"fieldtype": "Data",
			"insert_after": "custom_etims_scu_id",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_signature_column",
			"fieldtype": "Column Break",
			"insert_after": "custom_etims_receipt_signature",
		},
		{
			"fieldname": "custom_etims_internal_data",
			"label": "Internal Data",
			"fieldtype": "Data",
			"insert_after": "custom_etims_signature_column",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_signed_at",
			"label": "Signed At",
			"fieldtype": "Datetime",
			"insert_after": "custom_etims_internal_data",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
		{
			"fieldname": "custom_etims_signature_link",
			"label": "Verification Link",
			"fieldtype": "Small Text",
			"insert_after": "custom_etims_signed_at",
			"read_only": 1,
			"allow_on_submit": 1,
			"no_copy": 1,
		},
	],
	"Item": [
		{
			"fieldname": "custom_etims_section",
			"label": "eTIMS",
			"fieldtype": "Section Break",
			"insert_after": "item_group",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_etims_item_class_code",
			"label": "eTIMS Item Classification",
			"fieldtype": "Link",
			"options": "ETIMS Item Classification",
			"insert_after": "custom_etims_section",
			"description": "KRA classification code. Required before the item can be registered.",
		},
		{
			"fieldname": "custom_etims_tax_type",
			"label": "eTIMS Tax Type",
			"fieldtype": "Select",
			"options": BANDS,
			"insert_after": "custom_etims_item_class_code",
			"description": "The VAT band this item is registered under on the device. This is the "
			"authoritative band for its invoice lines - a mismatch is rejected as E321.",
		},
		{
			"fieldname": "custom_etims_product_type",
			"label": "eTIMS Product Type",
			"fieldtype": "Select",
			"options": PRODUCT_TYPES,
			"insert_after": "custom_etims_tax_type",
		},
		{
			"fieldname": "custom_etims_origin_country",
			"label": "Country of Origin (eTIMS)",
			"fieldtype": "Link",
			"options": "Country",
			"insert_after": "custom_etims_product_type",
			"default": "Kenya",
		},
		{
			"fieldname": "custom_etims_item_column",
			"fieldtype": "Column Break",
			"insert_after": "custom_etims_origin_country",
		},
		{
			"fieldname": "custom_etims_package_unit",
			"label": "eTIMS Packaging Unit",
			"fieldtype": "Link",
			"options": "ETIMS Packaging Unit",
			"insert_after": "custom_etims_item_column",
		},
		{
			"fieldname": "custom_etims_quantity_unit",
			"label": "eTIMS Quantity Unit",
			"fieldtype": "Link",
			"options": "ETIMS Quantity Unit",
			"insert_after": "custom_etims_package_unit",
		},
		{
			"fieldname": "custom_etims_levy_rate",
			"label": "eTIMS Levy Rate (%)",
			"fieldtype": "Percent",
			"insert_after": "custom_etims_quantity_unit",
			"description": "Band G. Leave at 0 unless this item carries a statutory levy inside "
			"its selling price.",
		},
		{
			"fieldname": "custom_etims_surcharge_rate",
			"label": "eTIMS Special Charge Rate (%)",
			"fieldtype": "Percent",
			"insert_after": "custom_etims_levy_rate",
			"description": "Band F. Leave at 0 unless this item carries a special charge.",
		},
		{
			"fieldname": "custom_etims_plu_no",
			"label": "eTIMS PLU Number",
			"fieldtype": "Data",
			"insert_after": "custom_etims_surcharge_rate",
			"read_only": 1,
			"no_copy": 1,
			"description": "Assigned when the item is registered on a device.",
		},
	],
	"Item Group": [
		{
			"fieldname": "custom_etims_tax_type",
			"label": "Default eTIMS Tax Type",
			"fieldtype": "Select",
			"options": BANDS,
			"insert_after": "item_group_name",
			"description": "Fallback band for items in this group that have none of their own.",
		},
	],
}


# ---------------------------------------------------------------- code masters

# Doc table 10. Sent to the device as "CODE-Code Name".
PACKAGING_UNITS = [
	("AM", "Ampoule"), ("BA", "Barrel"), ("BC", "Bottlecrate"), ("BE", "Bundle"),
	("BF", "Balloon, non-protected"), ("BG", "Bag"), ("BJ", "Bucket"), ("BK", "Basket"),
	("BL", "Bale"), ("BQ", "Bottle, protected cylindrical"), ("BR", "Bar"),
	("BV", "Bottle, bulbous"), ("BZ", "Bag"), ("CA", "Can"), ("CH", "Chest"),
	("CJ", "Coffin"), ("CL", "Coil"), ("CR", "Wooden Box, Wooden Case"), ("CS", "Cassette"),
	("CT", "Carton"), ("CTN", "Container"), ("CY", "Cylinder"), ("DR", "Drum"),
	("GT", "Extra Countable Item"), ("HH", "Hand Baggage"), ("IZ", "Ingots"), ("JR", "Jar"),
	("JU", "Jug"), ("JY", "Jerry CAN Cylindrical"), ("KZ", "Canester"),
	("LZ", "Logs, in bundle/bunch/truss"), ("NT", "Net"), ("OU", "Non-Exterior Packaging Unit"),
	("PD", "Poddon"), ("PG", "Plate"), ("PI", "Pipe"), ("PO", "Pilot"), ("PU", "Traypack"),
	("RL", "Reel"), ("RO", "Roll"), ("RZ", "Rods, in bundle/bunch/truss"), ("SK", "Skeleton case"),
	("TY", "Tank, cylindrical"), ("VG", "Bulk, gas (at 1031mbar 15C)"),
	("VL", "Bulk, liquid (at normal temperature/pressure)"),
	("VO", 'Bulk, solid, large particles ("nodules")'),
	("VQ", "Bulk, gas (liquefied at abnormal temperature/pressure)"),
	("VR", 'Bulk, solid, granular particles ("grains")'), ("VT", "Extra Bulk Item"),
	("VY", 'Bulk, fine particles ("powder")'), ("ML", "Mills cigarette Mills"), ("TN", "TAN"),
]

# Doc table 11, with the ERPNext UOM each maps to where one exists.
QUANTITY_UNITS = [
	("4B", "Pair", "Pair"), ("AV", "Cap", None), ("BA", "Barrel", None), ("BE", "bundle", None),
	("BG", "bag", "Bag"), ("BL", "block", None), ("BLL", "BLL Barrel", None), ("BX", "box", "Box"),
	("CA", "Can", None), ("CEL", "Cell", None), ("CMT", "centimetre", "Centimeter"),
	("CR", "CARAT", None), ("DR", "Drum", None), ("DZ", "Dozen", None), ("GLL", "Gallon", "Gallon"),
	("GRM", "Gram", "Gram"), ("GRO", "Gross", None), ("KG", "Kilogram", "Kg"),
	("KTM", "kilometre", "Kilometer"), ("KWT", "kilowatt", None), ("L", "Litre", "Litre"),
	("LBR", "pound", "Pound"), ("LK", "link", None), ("LTR", "Litre", None), ("M", "Metre", "Meter"),
	("M2", "Square Metre", "Square Meter"), ("M3", "Cubic Metre", "Cubic Meter"),
	("MGM", "milligram", None), ("MTR", "metre", None), ("MWT", "megawatt hour (1000 kW.h)", None),
	("NO", "Number", None), ("NX", "part per thousand", None), ("PA", "packet", None),
	("PG", "plate", None), ("PR", "pair", None), ("RL", "reel", None), ("RO", "roll", None),
	("SET", "set", "Set"), ("ST", "sheet", None), ("TNE", "tonne (metric ton)", "Tonne"),
	("TU", "tube", None), ("U", "Pieces/item [Number]", "Nos"), ("YRD", "yard", "Yard"),
]

# The KRA master list runs to thousands of codes and is published as a
# spreadsheet, so only the generic fallbacks are seeded. Import the full list
# with `bench --site <site> data-import` against ETIMS Item Classification:
# https://docs.google.com/spreadsheets/d/1g3Xm0g6rgLNVp8h5paTednaBRkfzppnTulqg2OY5xNI
# Only the two generic codes that actually appear in the vendor documentation.
# The official list runs to thousands of codes published as a spreadsheet, and a
# plausible-looking invented code is worse than a missing one: the device accepts
# it and the goods are misdeclared to KRA. Load the real list with
# seed.import_item_classifications.
ITEM_CLASSIFICATIONS = [
	("99000000", "General (unclassified)"),
	("99010000", "General goods (unclassified)"),
]


def after_install():
	setup()


def after_migrate():
	setup()


def setup():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_masters()


def seed_masters():
	for index, (code, name) in enumerate(PACKAGING_UNITS):
		_ensure("ETIMS Packaging Unit", code, {"code_name": name, "sort_order": index + 1})

	uoms = set(frappe.get_all("UOM", pluck="name"))
	for index, (code, name, uom) in enumerate(QUANTITY_UNITS):
		_ensure(
			"ETIMS Quantity Unit",
			code,
			{"code_name": name, "sort_order": index + 1, "uom": uom if uom in uoms else None},
		)

	for code, description in ITEM_CLASSIFICATIONS:
		_ensure("ETIMS Item Classification", code, {"description": description})


def _ensure(doctype, code, values):
	"""
	Create the master row if it is absent; leave an existing one alone.

	Seeds must never overwrite: a site may well have corrected a code name or
	pointed a unit at a different UOM, and a migrate should not undo that.
	"""
	if frappe.db.exists(doctype, code):
		return

	frappe.get_doc(dict(doctype=doctype, code=code, **values)).insert(ignore_permissions=True)
