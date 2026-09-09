"""
The append-only record of every exchange with the device.

``ETIMS Transmission`` is the *work item*: one row per invoice, carrying its
current state, and updated in place as it is retried. That is the right shape for
a queue and the wrong shape for an audit trail -- each retry overwrites the last
one's response, so by the time an invoice finally signs there is no record of
what the device said on the three attempts before it.

This module keeps the other half, copied from the TIMS app's ``KRA Response``:
one immutable row per exchange, successful or not. Between them you can answer
both "what is the state of this invoice" and "what exactly happened, in order".

The savepoint discipline is the important part and is also from TIMS. Recording
can run inside a Sales Invoice's own submit, where ``frappe.db.commit()`` would
commit a half-submitted invoice and a bare ``frappe.db.rollback()`` would discard
the in-flight submission entirely. Rolling back to a savepoint undoes only a
failed insert and leaves the surrounding transaction intact.
"""

import json

import frappe
from frappe.utils import cint

SAVEPOINT = "etims_kra_response"

OUTCOME_SIGNED = "Signed"
OUTCOME_REJECTED = "Rejected"
OUTCOME_UNREACHABLE = "Unreachable"
OUTCOME_ERROR = "Error"


def record(
	transmission,
	outcome,
	request_payload=None,
	response=None,
	result=None,
	error=None,
	endpoint=None,
	message=None,
):
	"""
	Persist one exchange. Returns the new row's name, or None if it could not be
	written.

	Never raises. A failure to write the audit row must not take down the
	fiscalisation it is describing -- losing the log entry is bad, losing the
	invoice is worse.
	"""
	frappe.db.savepoint(SAVEPOINT)
	try:
		doc = frappe.get_doc(_build(transmission, outcome, request_payload, response, result, error, endpoint, message))
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(
			title="eTIMS: could not record device response",
			message="Transmission: {0}\nOutcome: {1}\n\n{2}".format(
				getattr(transmission, "name", transmission), outcome, frappe.get_traceback()
			),
		)
		return None


def _build(transmission, outcome, request_payload, response, result, error, endpoint, message):
	row = {
		"doctype": "ETIMS KRA Response",
		"sales_invoice": transmission.sales_invoice,
		"transmission": transmission.name,
		"device": transmission.device,
		"outcome": outcome,
		"attempt": cint(transmission.attempt),
		"is_test": cint(transmission.is_test),
		"endpoint": (endpoint or "")[:140],
		"request_json": _pretty(request_payload),
		"response_json": _pretty(response),
		"message": (message or "")[:1000],
	}

	if result is not None:
		row.update(
			{
				"response_code": "000" if result.cu_invoice_number else "",
				"message": (message or result.message or "")[:1000],
				"cu_invoice_no": result.cu_invoice_number,
				"scu_id": result.scu_id,
				"internal_data": result.internal_data,
				"receipt_signature": result.receipt_signature,
				"signature_link": result.signature_link,
				"signature": result.signature,
				"device_timestamp": result.signed_at(),
				"response_json": _pretty(response if response is not None else result.raw),
			}
		)

	if error is not None:
		row.update(
			{
				"error_code": error.spec.code,
				"remedy": error.spec.remedy,
				"message": (message or str(error))[:1000],
				"response_code": _response_code(error),
				"response_json": _pretty(response if response is not None else error.response),
				"request_json": _pretty(request_payload if request_payload is not None else error.payload),
			}
		)

	return row


def _response_code(error):
	"""The device's own code where it gave one, else our classification."""
	raw = error.response or {}
	if isinstance(raw, dict):
		code = raw.get("error_code") or raw.get("ErrorCode")
		if code not in (None, ""):
			return str(code)
	return error.spec.code


def _pretty(value):
	if value is None:
		return None
	if isinstance(value, str):
		return value
	try:
		return json.dumps(value, indent=2, default=str)
	except (TypeError, ValueError):
		return str(value)


# --------------------------------------------------------------------- reading


def history(invoice):
	"""Every exchange for an invoice, oldest first. Backs the invoice-side view."""
	return frappe.get_all(
		"ETIMS KRA Response",
		filters={"sales_invoice": invoice},
		fields=[
			"name", "creation", "outcome", "attempt", "response_code", "message",
			"cu_invoice_no", "error_code", "remedy", "is_test",
		],
		order_by="creation asc",
	)


@frappe.whitelist()
def get_history(invoice):
	frappe.has_permission("Sales Invoice", "read", doc=invoice, throw=True)
	return history(invoice)
