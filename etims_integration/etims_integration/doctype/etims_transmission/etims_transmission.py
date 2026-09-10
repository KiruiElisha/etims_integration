# Copyright (c) 2026, Rono and contributors
# For license information, please see license.txt

"""
The unit of work, and the audit record, for one fiscalisation.

Everything the app does to an invoice happens *through* a Transmission: the
queue, the retry budget, the signature, the failure and its remedy all live on
one row. That is the main structural difference from the old TIMS app, where a
failure was a line in the Error Log and there was no object that meant "this
invoice still owes KRA a signature".

State machine
-------------
``Queued`` -> ``Sending`` -> ``Signed``
                          -> ``Failed``  (transport fault; retried with backoff)
                          -> ``Blocked`` (payload rejected, or a pre-send concern;
                                          waits for a human, never auto-retried)
``Cancelled`` is terminal and set when the invoice itself is cancelled.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

TERMINAL = ("Signed", "Cancelled")
RETRYABLE_STATES = ("Queued", "Failed")


class ETIMSTransmission(Document):
	def before_insert(self):
		if not self.attempt:
			self.attempt = 0

	# ------------------------------------------------------------ state changes

	def mark_sending(self):
		self.db_set(
			{"status": "Sending", "attempt": (self.attempt or 0) + 1, "next_attempt_at": None},
			update_modified=True,
		)

	def mark_signed(self, result, request_payload=None):
		"""Records the signature, then mirrors it onto the invoice for printing."""
		values = {
			"status": "Signed",
			"cu_invoice_no": result.cu_invoice_number,
			"scu_id": result.scu_id,
			"internal_data": result.internal_data,
			"receipt_signature": result.receipt_signature,
			"signature_link": result.signature_link,
			"signature": result.signature,
			# The device's own stamp where it gave a readable one. On a queue
			# that retries, that can be minutes before we processed the reply.
			"signed_at": result.signed_at() or now_datetime(),
			"error_code": None,
			"error_summary": None,
			"remedy": None,
			"error_message": None,
			"next_attempt_at": None,
			"response_json": _pretty(result.raw),
		}
		if request_payload is not None:
			values["request_json"] = _pretty(request_payload)
		self.db_set(values, update_modified=True)
		self.update_invoice()

	def mark_blocked(self, summary, remedy="", detail="", code="", request_payload=None):
		"""
		A fault no retry can clear. Parked with the remedy attached so the person
		who opens it knows what to change without reading the vendor PDF.
		"""
		self.db_set(
			{
				"status": "Blocked",
				"error_code": code or None,
				"error_summary": _truncate(summary),
				"remedy": remedy or None,
				"error_message": detail or summary,
				"next_attempt_at": None,
				"request_json": _pretty(request_payload) if request_payload is not None else self.request_json,
			},
			update_modified=True,
		)

	def mark_failed(self, error, request_payload=None, max_retries=5, backoff_minutes=2):
		"""
		A transport fault. Schedules the next attempt with exponential backoff, and
		gives up into ``Blocked`` once the budget is spent so a dead device does not
		generate work forever.
		"""
		spec = error.spec
		attempt = self.attempt or 0
		exhausted = attempt >= max_retries

		values = {
			"error_code": spec.code,
			"error_summary": _truncate(spec.summary),
			"remedy": spec.remedy,
			"error_message": str(error)[:5000],
			"response_json": _pretty(error.response) if error.response else self.response_json,
		}
		if request_payload is not None:
			values["request_json"] = _pretty(request_payload)

		if exhausted:
			values["status"] = "Blocked"
			values["next_attempt_at"] = None
			values["remedy"] = _("Gave up after {0} attempts. {1}").format(attempt, spec.remedy)
		else:
			values["status"] = "Failed"
			# 2, 4, 8, 16 ... minutes. A device that is down tends to stay down for
			# longer than the last outage, and hammering it helps nobody.
			values["next_attempt_at"] = add_to_date(
				now_datetime(), minutes=backoff_minutes * (2**attempt), as_datetime=True
			)

		self.db_set(values, update_modified=True)

	def cancel_transmission(self, reason=""):
		if self.status in TERMINAL:
			return
		self.db_set(
			{"status": "Cancelled", "error_message": reason or _("Sales Invoice was cancelled."),
			 "next_attempt_at": None},
			update_modified=True,
		)

	# ------------------------------------------------------------------ invoice

	def update_invoice(self):
		"""
		Mirror the signature onto the Sales Invoice.

		``db_set`` on the invoice rather than ``save``: this can run inside the
		invoice's own submit, where saving collides with the in-flight write and
		loses the fiscal fields entirely.
		"""
		invoice = frappe.get_doc("Sales Invoice", self.sales_invoice)
		for field, value in {
			"custom_etims_status": "Signed",
			"custom_etims_cu_invoice_no": self.cu_invoice_no,
			"custom_etims_scu_id": self.scu_id,
			"custom_etims_receipt_signature": self.receipt_signature,
			"custom_etims_internal_data": self.internal_data,
			"custom_etims_signature_link": self.signature_link,
			"custom_etims_signed_at": self.signed_at,
			"custom_etims_transmission": self.name,
			"custom_etims_is_test": self.is_test,
		}.items():
			invoice.db_set(field, value, update_modified=False)

	# -------------------------------------------------------------------- desk

	@frappe.whitelist()
	def retry(self):
		"""
		Manual retry. Explicitly allowed from ``Blocked`` -- that is the whole point
		of the state: a human has fixed the cause and is vouching for the resend.
		"""
		self.check_permission("write")
		if self.status in TERMINAL:
			frappe.throw(_("{0} is {1} and cannot be resent.").format(self.name, self.status))

		from etims_integration.services.transmit import send_transmission

		self.db_set({"status": "Queued", "next_attempt_at": None}, update_modified=True)
		frappe.enqueue(
			send_transmission,
			queue="short",
			transmission=self.name,
			enqueue_after_commit=True,
		)
		return _("Queued for resend.")


def _pretty(value):
	if value is None:
		return None
	try:
		return json.dumps(value, indent=2, default=str)
	except (TypeError, ValueError):
		return str(value)


def _truncate(text, length=140):
	text = str(text or "")
	return text if len(text) <= length else text[: length - 1] + "…"
