"""
Fiscalisation orchestration.

The rule this module exists to enforce: **no HTTP call ever happens inside a
Sales Invoice's submit transaction by default.** ``on_submit`` creates a
Transmission and returns; a worker does the talking. A device that is slow, or
dead, or mid-reboot then costs the user nothing, and the retry has somewhere to
live.

Synchronous mode is offered for POS counters that must print a QR code before the
customer walks away, but it is opt-in and time-boxed.
"""

import frappe
from frappe import _
from frappe.model.naming import getseries
from frappe.utils import cint, now_datetime
from frappe.utils.synchronization import filelock

from etims_integration.comstore.errors import ComstoreError
from etims_integration.etims_integration.doctype.etims_device.etims_device import resolve_device
from etims_integration.mapping import invoice as invoice_mapping
from etims_integration.services import response_log

# Trader invoice numbers are numeric and monotonic. The offset keeps them a
# consistent width from the first invoice, which some firmware is fussy about,
# and keeps them clearly distinct from ERPNext's own naming series.
TRADER_NUMBER_BASE = 1_000_000_000
TRADER_SERIES_KEY = "etims_trader_invoice"

SYNCHRONOUS_TIMEOUT = 25


def next_trader_invoice_number():
	"""
	The idempotency key for one fiscalisation.

	Allocated once, when the Transmission is created, and never regenerated --
	every retry re-sends the same number. The device's duplicate check is
	mandatory and cannot be disabled, so a fresh number on retry would either be
	rejected or, worse, register a second invoice with KRA for one sale.
	"""
	return str(TRADER_NUMBER_BASE + int(getseries(TRADER_SERIES_KEY, 10)))


# ------------------------------------------------------------------ entry point


def queue_invoice(doc, settings=None):
	"""
	Called from Sales Invoice ``on_submit``. Creates the Transmission and hands it
	to a worker (or sends it inline in Synchronous mode).

	Returns the Transmission name, or ``None`` when the invoice is deliberately
	out of scope -- which is always said out loud rather than returned silently.
	"""
	settings = settings or frappe.get_cached_doc("ETIMS Settings")

	reason = out_of_scope(doc, settings)
	if reason:
		_announce_skip(doc, reason)
		return None

	existing = frappe.db.get_value(
		"ETIMS Transmission",
		{"sales_invoice": doc.name, "status": ("not in", ("Cancelled",))},
		["name", "status"],
		as_dict=True,
	)
	if existing:
		# Never fiscalise the same invoice twice. Re-submitting after an amend gets
		# the existing record, in whatever state it reached.
		return existing.name

	device_name = resolve_device(doc.company, doc.get("custom_etims_branch"))
	device = frappe.get_cached_doc("ETIMS Device", device_name)

	transmission = frappe.get_doc(
		{
			"doctype": "ETIMS Transmission",
			"sales_invoice": doc.name,
			"company": doc.company,
			"device": device.name,
			"invoice_type": "Credit" if doc.get("is_return") else "Original",
			"status": "Queued",
			"is_test": cint(device.is_test_mode),
			"trader_invoice_no": next_trader_invoice_number(),
			"posting_date": doc.posting_date,
			"currency": doc.currency,
			"grand_total": abs(doc.base_grand_total or 0),
		}
	).insert(ignore_permissions=True)

	doc.db_set("custom_etims_status", "Queued", update_modified=False)
	doc.db_set("custom_etims_transmission", transmission.name, update_modified=False)

	if (settings.submit_mode or "Background") == "Synchronous":
		send_transmission(transmission.name, timeout=SYNCHRONOUS_TIMEOUT)
		_report_synchronous_outcome(transmission.name, settings)
	else:
		# after_commit: the worker must not start before the invoice row it is
		# about to read is actually committed.
		frappe.enqueue(
			send_transmission,
			queue="short",
			transmission=transmission.name,
			enqueue_after_commit=True,
		)

	return transmission.name


def out_of_scope(doc, settings):
	"""Why this invoice is not being fiscalised, or None if it is."""
	if not cint(settings.enabled):
		return _("The eTIMS integration is disabled in eTIMS Settings.")
	if not cint(settings.send_on_submit):
		return _("'Fiscalise Invoices on Submit' is off in eTIMS Settings.")
	if doc.get("is_return") and not cint(settings.send_credit_notes):
		return _("'Fiscalise Credit Notes' is off in eTIMS Settings.")
	if doc.get("custom_etims_exempt"):
		return _("This invoice is marked 'Exempt from eTIMS'.")
	if doc.get("is_opening") == "Yes":
		return _("Opening invoices are not fiscalised.")
	return None


def _announce_skip(doc, reason):
	"""
	A skipped invoice must be distinguishable from a broken one. ``msgprint`` alone
	is invisible from a background job or the API, so the reason also goes on the
	invoice where anyone can see it later.
	"""
	doc.db_set("custom_etims_status", "Not Sent", update_modified=False)
	frappe.msgprint(
		_("{0} was not sent to eTIMS. {1}").format(doc.name, reason),
		title=_("eTIMS"),
		indicator="orange",
	)


def _report_synchronous_outcome(transmission_name, settings):
	transmission = frappe.get_doc("ETIMS Transmission", transmission_name)
	if transmission.status == "Signed":
		return

	message = _("{0} was not fiscalised: {1}").format(
		transmission.sales_invoice, transmission.error_summary or transmission.status
	)
	if transmission.remedy:
		message += "<br><br>" + transmission.remedy

	if cint(settings.block_submission_on_failure):
		frappe.throw(message, title=_("eTIMS"))

	frappe.msgprint(
		message + "<br><br>" + _("It stays queued and will be retried."),
		title=_("eTIMS"),
		indicator="orange",
	)


# ---------------------------------------------------------------------- worker


def send_transmission(transmission, timeout=None):
	"""
	Send one Transmission. Safe to call twice: the file lock and the state check
	together mean only one worker can be in flight for a given record.
	"""
	with filelock(f"etims-transmission-{transmission}", timeout=5):
		_send(transmission, timeout=timeout)


def _send(name, timeout=None):
	transmission = frappe.get_doc("ETIMS Transmission", name)
	if transmission.status in ("Signed", "Cancelled", "Sending"):
		return transmission.status

	settings = frappe.get_cached_doc("ETIMS Settings")
	if not cint(settings.enabled):
		return transmission.mark_blocked(
			_("eTIMS integration is disabled."),
			remedy=_("Enable it in eTIMS Settings, then retry this transmission."),
		)

	invoice = frappe.get_doc("Sales Invoice", transmission.sales_invoice)
	if invoice.docstatus == 2:
		return transmission.cancel_transmission()
	if invoice.docstatus == 0:
		return transmission.mark_blocked(
			_("The Sales Invoice is a draft."),
			remedy=_("Submit the invoice, then retry."),
		)

	device = frappe.get_doc("ETIMS Device", transmission.device)
	if device.status != "Active":
		return transmission.mark_blocked(
			_("eTIMS Device {0} is {1}.").format(device.name, device.status),
			remedy=_("Set the device back to Active, then retry."),
		)

	# ---- build ---------------------------------------------------------------
	try:
		mapped = invoice_mapping.build(invoice, device, settings, transmission.trader_invoice_no)
	except Exception as e:
		frappe.log_error(title="eTIMS: could not build payload", message=frappe.get_traceback())
		return transmission.mark_blocked(
			_("The payload could not be built."), remedy=str(e), detail=frappe.get_traceback()
		)

	if not mapped.sendable:
		# Recorded even though nothing was sent: "we deliberately did not send, and
		# here is why" is exactly the question the log exists to answer.
		response_log.record(
			transmission,
			response_log.OUTCOME_ERROR,
			request_payload=mapped.as_payload() if mapped.sign_structure else None,
			message=" ".join(mapped.concerns),
		)
		# Fiscalisation is irreversible, so anything that would declare figures
		# differing from the invoice waits for a person. This is the single most
		# important guard in the app.
		return transmission.mark_blocked(
			_("Held for review before declaring to KRA."),
			remedy=" ".join(mapped.concerns),
			detail="\n".join(mapped.concerns),
			request_payload=mapped.as_payload() if mapped.sign_structure else None,
		)

	transmission.db_set(
		{
			"declared_total": mapped.declared_total,
			"band_summary": _format_bands(mapped.band_summary),
			"request_json": None,
		},
		update_modified=False,
	)

	# ---- send ----------------------------------------------------------------
	client = device.get_client()
	if timeout:
		client.timeout = timeout

	transmission.mark_sending()
	try:
		result, payload = client.complete_workflow(
			mapped.lines, mapped.sign_structure, is_test=cint(device.is_test_mode)
		)
	except ComstoreError as e:
		response_log.record(
			transmission,
			response_log.OUTCOME_UNREACHABLE if e.retryable else response_log.OUTCOME_REJECTED,
			request_payload=e.payload or mapped.as_payload(),
			error=e,
			endpoint=client.base_url,
		)
		if e.retryable:
			transmission.mark_failed(
				e,
				request_payload=e.payload or mapped.as_payload(),
				max_retries=cint(settings.max_retries) or 5,
				backoff_minutes=cint(settings.retry_backoff_minutes) or 2,
			)
		else:
			transmission.mark_blocked(
				e.spec.summary,
				remedy=e.spec.remedy,
				detail=str(e),
				code=e.spec.code,
				request_payload=e.payload or mapped.as_payload(),
			)
			invoice.db_set("custom_etims_status", "Blocked", update_modified=False)
		return transmission.status
	except Exception:
		frappe.log_error(title="eTIMS: unexpected send failure", message=frappe.get_traceback())
		response_log.record(
			transmission,
			response_log.OUTCOME_ERROR,
			request_payload=mapped.as_payload(),
			message=frappe.get_traceback(limit=3),
			endpoint=client.base_url,
		)
		transmission.mark_blocked(
			_("Unexpected failure while sending."),
			remedy=_("See the Error Log."),
			detail=frappe.get_traceback(),
		)
		return transmission.status

	response_log.record(
		transmission,
		response_log.OUTCOME_SIGNED,
		request_payload=payload,
		result=result,
		endpoint=client.base_url,
	)
	transmission.mark_signed(result, request_payload=payload)
	return "Signed"


def _format_bands(summary):
	if not summary:
		return ""
	return "\n".join(f"{band}: net {values['net']}, tax {values['tax']}" for band, values in summary.items())


# ------------------------------------------------------------------- scheduler


def retry_failed():
	"""
	Hourly. Picks up transmissions whose backoff has elapsed.

	Only ``Failed`` is retried automatically -- those are transport faults, where
	the identical payload may now succeed. ``Blocked`` is left alone by design: the
	device already judged that payload and will judge it the same way again.
	"""
	if not cint(frappe.get_cached_doc("ETIMS Settings").enabled):
		return

	due = frappe.get_all(
		"ETIMS Transmission",
		filters={"status": "Failed", "next_attempt_at": ("<=", now_datetime())},
		pluck="name",
		limit=200,
		order_by="next_attempt_at asc",
	)
	for name in due:
		frappe.enqueue(send_transmission, queue="long", transmission=name)


def queue_stale():
	"""
	Safety net for transmissions that never left ``Queued`` -- a worker that died
	mid-job, or a Redis restart that lost the enqueue. Without this an invoice can
	sit unfiscalised forever with nothing indicating why.
	"""
	from frappe.utils import add_to_date

	stale = frappe.get_all(
		"ETIMS Transmission",
		filters={
			"status": ("in", ("Queued", "Sending")),
			"modified": ("<", add_to_date(now_datetime(), minutes=-30, as_datetime=True)),
		},
		pluck="name",
		limit=100,
	)
	for name in stale:
		frappe.db.set_value("ETIMS Transmission", name, "status", "Queued", update_modified=False)
		frappe.enqueue(send_transmission, queue="long", transmission=name)


# ------------------------------------------------------------------------ desk


@frappe.whitelist()
def preview(invoice):
	"""
	Build the payload without sending it, so a user can see exactly what would be
	declared to KRA before it is. Backs the 'Preview eTIMS Payload' action.
	"""
	frappe.has_permission("Sales Invoice", "read", doc=invoice, throw=True)

	doc = frappe.get_doc("Sales Invoice", invoice)
	settings = frappe.get_cached_doc("ETIMS Settings")
	device = frappe.get_cached_doc("ETIMS Device", resolve_device(doc.company, doc.get("custom_etims_branch")))

	mapped = invoice_mapping.build(doc, device, settings, "<allocated on send>")

	return {
		"invoice": doc.name,
		"device": device.name,
		"is_test": cint(device.is_test_mode),
		"concerns": mapped.concerns,
		"invoice_total": str(mapped.invoice_total),
		"declared_total": str(mapped.declared_total),
		"bands": {band: {k: str(v) for k, v in values.items()} for band, values in mapped.band_summary.items()},
		"payload": mapped.as_payload(),
	}


@frappe.whitelist()
def send_now(invoice):
	"""Fiscalise an invoice on demand, for one that was skipped or never queued."""
	frappe.has_permission("Sales Invoice", "submit", doc=invoice, throw=True)

	doc = frappe.get_doc("Sales Invoice", invoice)
	if doc.docstatus != 1:
		frappe.throw(_("Only a submitted invoice can be sent to eTIMS."))

	settings = frappe.get_cached_doc("ETIMS Settings")
	name = frappe.db.get_value(
		"ETIMS Transmission", {"sales_invoice": invoice, "status": ("not in", ("Cancelled",))}, "name"
	)
	if not name:
		name = queue_invoice(doc, settings)
		if not name:
			return {"queued": False}
	else:
		frappe.db.set_value("ETIMS Transmission", name, "status", "Queued", update_modified=True)
		frappe.enqueue(send_transmission, queue="short", transmission=name, enqueue_after_commit=True)

	return {"queued": True, "transmission": name}
