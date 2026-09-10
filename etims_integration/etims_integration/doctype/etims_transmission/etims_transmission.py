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
		self.update_invoice()

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
		self.update_invoice()

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
		self.update_invoice()

	def cancel_transmission(self, reason=""):
		if self.status in TERMINAL:
			return
		self.db_set(
			{"status": "Cancelled", "error_message": reason or _("Sales Invoice was cancelled."),
			 "next_attempt_at": None},
			update_modified=True,
		)
		self.update_invoice()

	# ------------------------------------------------------------------ invoice

	def update_invoice(self):
		"""
		Mirror this Transmission's state onto its Sales Invoice.

		Called from *every* state change, not only from :meth:`mark_signed`. Before
		that, only a signature and a device rejection reached the invoice, so a
		transmission that failed on transport, was cancelled, or was held back
		before it ever reached the device left its invoice reading ``Queued``
		indefinitely -- and ``Queued`` is precisely the state nobody investigates.
		The invoice list is where people look; it has to be able to be wrong out
		loud.

		``db_set`` on the invoice rather than ``save``: this can run inside the
		invoice's own submit, where saving collides with the in-flight write and
		loses the fiscal fields entirely.
		"""
		if not self.sales_invoice:
			return

		values = {
			"custom_etims_status": self.status,
			"custom_etims_transmission": self.name,
			"custom_etims_is_test": self.is_test,
		}

		# The signature fields are only ever written by a signature. Clearing them
		# on a later failure would erase the fiscal record of a receipt the
		# customer is already holding.
		if self.status == "Signed":
			values.update(
				{
					"custom_etims_cu_invoice_no": self.cu_invoice_no,
					"custom_etims_scu_id": self.scu_id,
					"custom_etims_receipt_signature": self.receipt_signature,
					"custom_etims_internal_data": self.internal_data,
					"custom_etims_signature_link": self.signature_link,
					"custom_etims_signed_at": self.signed_at,
				}
			)

		try:
			invoice = frappe.get_doc("Sales Invoice", self.sales_invoice)
		except frappe.DoesNotExistError:
			# The invoice was deleted under us. That is worth knowing about, but it
			# is not a reason to lose the device's reply.
			frappe.log_error(
				title="eTIMS: transmission points at a missing invoice",
				message=f"{self.name} -> Sales Invoice {self.sales_invoice}",
			)
			return

		# One db_set with the whole dict, not one per field. `db_set` reloads
		# doc_before_save and fires before_change/on_change every time it is
		# called, so the old field-by-field loop paid for that eight times over --
		# and this now runs on every attempt, not just on a signature.
		invoice.db_set(values, update_modified=False)

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

		self.requeue()
		return _("Queued for resend.")

	def requeue(self):
		"""
		Put this transmission back in the queue with a fresh retry budget.

		Resetting ``attempt`` is the point. It is the budget counter that
		:meth:`mark_failed` compares against ``max_retries``, so a transmission
		that exhausted its budget and was parked in ``Blocked`` came back with
		``attempt`` still at the limit: the operator fixed the cause, pressed
		Retry, hit one transport hiccup, and watched it go straight back to
		``Blocked`` without ever getting a second attempt. The full attempt history
		survives in ETIMS KRA Response, which is where it belongs -- this field is
		a budget, not an archive.

		The stale error is cleared at the same time, so the form does not show the
		fault the operator has just fixed as though it were still current.
		"""
		self.db_set(
			{
				"status": "Queued",
				"attempt": 0,
				"next_attempt_at": None,
				"error_code": None,
				"error_summary": None,
				"remedy": None,
				"error_message": None,
			},
			update_modified=True,
		)
		self.update_invoice()

		from etims_integration.services.transmit import send_transmission

		frappe.enqueue(
			send_transmission,
			queue="short",
			transmission=self.name,
			enqueue_after_commit=True,
		)


@frappe.whitelist()
def retry_transmissions(names):
	"""
	Retry several transmissions at once, from the list view.

	The single-invoice fix is rarely the real case: registering one item on the
	device, or bringing the device back up, unblocks every invoice that touched
	it. Without this the operator reopens them one at a time.

	Returns a per-name outcome rather than throwing on the first refusal -- one
	already-signed row in a selection of forty must not cost the other
	thirty-nine their resend.
	"""
	if isinstance(names, str):
		names = json.loads(names)

	results = {"queued": [], "skipped": {}}
	for name in names or []:
		try:
			doc = frappe.get_doc("ETIMS Transmission", name)
			doc.check_permission("write")
			if doc.status in TERMINAL:
				results["skipped"][name] = _("Already {0}.").format(doc.status)
				continue
			doc.requeue()
			results["queued"].append(name)
		except Exception as e:
			results["skipped"][name] = str(e)

	return results


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
