"""
Credit allowance: what an invoice has left to be credited at eTIMS.

The device accumulates credited quantity and value against the invoice being
credited and rejects anything over (E220, E335, E221). Neither ERPNext nor the
old TIMS app tracked that, so the ceiling was discovered by having a real
customer's credit note rejected -- after the credit note had already been
submitted, leaving the customer's ledger adjusted and KRA's records not.

The figures are read from what was *actually sent*: the ``request_json`` of the
signed Transmissions. That is the only account that matches the device's own,
because it reflects the encoding we chose rather than what ERPNext happens to
show. A credit note ERPNext knows nothing about (because ``return_against`` was
left blank for a financial-only adjustment) still counts here, and one that
failed to sign correctly does not.
"""

import json
from decimal import Decimal

import frappe
from frappe import _

ZERO = Decimal("0")


def _dec(value):
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value or 0))


def _plu_key(line):
	"""
	Match device lines across transmissions. Barcode is the device's own unique
	key for an item, so it wins; the name is the fallback for items registered
	before a PLU number was assigned.
	"""
	return (line.get("Barcode") or "").strip() or (line.get("item_Name") or "").strip()


def _transmission_lines(transmission_name, payload_field="request_json"):
	raw = frappe.db.get_value("ETIMS Transmission", transmission_name, payload_field)
	if not raw:
		return []
	try:
		return (json.loads(raw) or {}).get("plu_data") or []
	except (TypeError, ValueError):
		frappe.log_error(
			title=f"eTIMS: unreadable payload on {transmission_name}",
			message="Could not parse the stored request payload; allowance figures will be understated.",
		)
		return []


def original_lines(invoice):
	"""
	What we declared to KRA for this invoice, keyed by device line.

	Returns ``{key: {"item_name", "unit_price", "qty", "amount"}}``. The unit price
	here is the ceiling E219 imposes on any credit against the line, and the
	quantity is its budget.
	"""
	transmission = frappe.db.get_value(
		"ETIMS Transmission",
		{"sales_invoice": invoice, "status": "Signed", "invoice_type": "Original"},
		"name",
		order_by="creation desc",
	)
	if not transmission:
		return {}

	lines = {}
	for line in _transmission_lines(transmission):
		key = _plu_key(line)
		if not key:
			continue
		entry = lines.setdefault(
			key,
			{"item_name": line.get("item_Name") or key, "unit_price": ZERO, "qty": ZERO, "amount": ZERO},
		)
		# A repeated item on one invoice is one budget, so quantities and amounts
		# add while the unit price stays the highest declared -- that is the cap
		# the device will apply.
		entry["unit_price"] = max(entry["unit_price"], _dec(line.get("SalePrice")))
		entry["qty"] += _dec(line.get("SaleQty"))
		entry["amount"] += _dec(line.get("SaleAmount"))

	return lines


def credit_notes_against(invoice):
	"""
	Every credit note pointing at this invoice, by either route.

	A credit note references its original through ``return_against`` (a goods
	return) or ``custom_etims_original_invoice`` (a financial-only price
	adjustment, where ``return_against`` is left blank so ERPNext does not couple
	it to the return-quantity budget). Both are queried, so neither route can hide
	budget that has already been consumed.
	"""
	invoice_table = frappe.qb.DocType("Sales Invoice")
	rows = (
		frappe.qb.from_(invoice_table)
		.select(invoice_table.name)
		.where(
			(invoice_table.docstatus == 1)
			& (invoice_table.is_return == 1)
			& (
				(invoice_table.custom_etims_original_invoice == invoice)
				| (
					(invoice_table.return_against == invoice)
					& (
						invoice_table.custom_etims_original_invoice.isnull()
						| (invoice_table.custom_etims_original_invoice == "")
					)
				)
			)
		)
	).run(pluck=True)
	return rows


def credited_lines(invoice, exclude_transmission=None):
	"""Everything already credited against this invoice, by the same key."""
	notes = credit_notes_against(invoice)
	if not notes:
		return {}

	transmissions = frappe.get_all(
		"ETIMS Transmission",
		filters={"sales_invoice": ("in", notes), "status": "Signed", "invoice_type": "Credit"},
		pluck="name",
	)

	credited = {}
	for name in transmissions:
		if name == exclude_transmission:
			continue
		for line in _transmission_lines(name):
			key = _plu_key(line)
			if not key:
				continue
			entry = credited.setdefault(key, {"qty": ZERO, "amount": ZERO})
			entry["qty"] += _dec(line.get("SaleQty"))
			entry["amount"] += _dec(line.get("SaleAmount"))

	return credited


def original_invoice_of(credit_note):
	"""
	The invoice a credit note adjusts, whichever field carries it.

	``custom_etims_original_invoice`` wins: it is the explicit eTIMS reference and
	is the only one set on a financial-only adjustment, where ``return_against``
	is deliberately left blank so ERPNext does not couple the adjustment to the
	return-quantity budget.
	"""
	row = frappe.db.get_value(
		"Sales Invoice",
		credit_note,
		["custom_etims_original_invoice", "return_against"],
		as_dict=True,
	)
	if not row:
		return None
	return row.custom_etims_original_invoice or row.return_against


def remaining(invoice, exclude_transmission=None):
	"""
	Headroom left on each line of an invoice: quantity and value still creditable.

	This is the figure to check an adjustment against *before* submitting it.
	"""
	originals = original_lines(invoice)
	credited = credited_lines(invoice, exclude_transmission=exclude_transmission)

	result = {}
	for key, entry in originals.items():
		used = credited.get(key, {"qty": ZERO, "amount": ZERO})
		result[key] = {
			"item_name": entry["item_name"],
			"unit_price": entry["unit_price"],
			"original_qty": entry["qty"],
			"original_amount": entry["amount"],
			"credited_qty": used["qty"],
			"credited_amount": used["amount"],
			"remaining_qty": entry["qty"] - used["qty"],
			"remaining_amount": entry["amount"] - used["amount"],
		}
	return result


def summary(invoice):
	"""Totals across the invoice, for showing on a form."""
	lines = remaining(invoice)
	if not lines:
		return None

	return {
		"invoice": invoice,
		"original_amount": sum(v["original_amount"] for v in lines.values()),
		"credited_amount": sum(v["credited_amount"] for v in lines.values()),
		"remaining_amount": sum(v["remaining_amount"] for v in lines.values()),
		"lines": lines,
	}


@frappe.whitelist()
def get_allowance(invoice):
	"""Desk-facing view of the headroom, for the credit note form."""
	frappe.has_permission("Sales Invoice", "read", doc=invoice, throw=True)

	data = summary(invoice)
	if not data:
		return {
			"available": False,
			"message": _("{0} has no signed eTIMS transmission, so nothing can be credited against it yet.").format(invoice),
		}

	return {
		"available": True,
		"invoice": invoice,
		"original_amount": str(data["original_amount"]),
		"credited_amount": str(data["credited_amount"]),
		"remaining_amount": str(data["remaining_amount"]),
		"lines": [
			{
				"item_name": v["item_name"],
				"unit_price": str(v["unit_price"]),
				"original_qty": str(v["original_qty"]),
				"credited_qty": str(v["credited_qty"]),
				"remaining_qty": str(v["remaining_qty"]),
				"remaining_amount": str(v["remaining_amount"]),
			}
			for v in data["lines"].values()
		],
	}
