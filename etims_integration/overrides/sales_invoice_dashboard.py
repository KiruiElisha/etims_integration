"""
eTIMS connections on a Sales Invoice.

Both records point *at* the invoice by ``sales_invoice``, so without this the
link only ran one way: you could get from a Transmission to its invoice, but
from the invoice you had to know the doctype existed and go and filter a list.
The custom button gets you to the current Transmission; this gets you to all of
them, including the ones an amend or a cancellation left behind.
"""

from frappe import _


def get_data(data=None):
	data = data or {}
	data.setdefault("fieldname", "sales_invoice")
	data.setdefault("non_standard_fieldnames", {})
	data.setdefault("transactions", [])
	data["non_standard_fieldnames"].update(
		{"ETIMS Transmission": "sales_invoice", "ETIMS KRA Response": "sales_invoice"}
	)
	data["transactions"].append(
		{"label": _("eTIMS"), "items": ["ETIMS Transmission", "ETIMS KRA Response"]}
	)
	return data
