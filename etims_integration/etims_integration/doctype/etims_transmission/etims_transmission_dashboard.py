"""
Connections shown on an ETIMS Transmission.

The Transmission carries only the *latest* attempt -- each retry overwrites the
last one's response. Every attempt before it lives in ETIMS KRA Response, and
without this link an operator has to know to go and build a filtered list to
find them. The audit trail should be one click from the work item.
"""

from frappe import _


def get_data():
	return {
		"fieldname": "transmission",
		"transactions": [
			{"label": _("Device Exchanges"), "items": ["ETIMS KRA Response"]},
		],
	}
