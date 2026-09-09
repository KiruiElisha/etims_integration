"""
Customer -> device buyer record.

Registering buyers is optional for fiscalisation -- an invoice can carry
``pinOfBuyer`` without the buyer being on the device -- but the device keeps a
buyer table for its own receipts, and keeping it in step with ERPNext is what
lets a shop reprint a compliant receipt without ERPNext being reachable.

Only customers with a valid KRA PIN are ever sent: the device rejects the whole
batch over one malformed PIN (E358).
"""

import frappe

from etims_integration.comstore.schema import Buyer
from etims_integration.mapping import sanitize


def build(customer, buyer_no):
	pin = sanitize.clean_pin(customer.get("tax_id"))
	if not pin:
		return None

	return Buyer(
		buyer_no=str(buyer_no),
		pin=pin,
		name=sanitize.ascii_text(customer.get("customer_name") or customer.name, sanitize.MAX_BUYER_NAME),
		mobile=sanitize.ascii_text(customer.get("mobile_no") or "", 20),
		address=sanitize.ascii_text(_primary_address(customer.name), 60),
		email=sanitize.ascii_text(customer.get("email_id") or "", 60),
		fax="",
	)


def _primary_address(customer):
	address = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"},
		"parent",
	)
	if not address:
		return ""
	row = frappe.db.get_value("Address", address, ["address_line1", "city"], as_dict=True)
	if not row:
		return ""
	return ", ".join(part for part in (row.address_line1, row.city) if part)
