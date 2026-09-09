"""
Sales Invoice hooks.

Kept deliberately thin. Everything here either validates something cheap or hands
work to :mod:`etims_integration.services.transmit`; no HTTP, no mapping and no
payload construction happens in the submit path.
"""

import frappe
from frappe import _
from frappe.utils import add_months, cint, flt, getdate

from etims_integration.services import transmit


def validate(doc, method=None):
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not cint(settings.enabled):
		return

	if not cint(settings.allow_backdated) and getdate(doc.posting_date) != getdate():
		frappe.throw(
			_("eTIMS is configured to fiscalise same-day invoices only. Posting date {0} is not today.").format(
				doc.posting_date
			)
		)

	if doc.get("is_return") and cint(settings.send_credit_notes) and not doc.get("custom_etims_exempt"):
		_validate_credit_note(doc)


def _validate_credit_note(doc):
	"""
	Catch at validate time what would otherwise be discovered by the device.

	A credit note needs the invoice it adjusts and a reason code; finding that out
	at submit means the user has already committed the document.
	"""
	from etims_integration.mapping.invoice import ADJUSTMENT_PRICE, original_invoice_of

	original = original_invoice_of(doc)
	if not original:
		frappe.throw(
			_("Set 'eTIMS Original Invoice' (or 'Return Against') so eTIMS can be told which invoice this adjusts.")
		)

	if not doc.get("custom_etims_refund_reason"):
		frappe.throw(_("Select an eTIMS Refund Reason on this credit note."))

	if doc.get("custom_etims_adjustment_type") == ADJUSTMENT_PRICE:
		_validate_price_adjustment(doc, original)

	_warn_on_vat_window(doc, original)


def _validate_price_adjustment(doc, original):
	"""
	A price adjustment moves money, not goods.

	``update_stock`` must be off or ERPNext brings the goods back into stock and
	credits COGS for a return that never happened. ``return_against`` must be
	blank or ERPNext counts this against the original's return quantity and
	refuses the next adjustment -- which is the whole problem this type exists to
	solve.
	"""
	if doc.get("update_stock"):
		frappe.throw(
			_("A Price Adjustment must have 'Update Stock' off: no goods are returned, so stock and COGS must not move.")
		)

	if doc.return_against:
		frappe.throw(
			_("A Price Adjustment must leave 'Return Against' blank and use 'eTIMS Original Invoice' instead. "
			  "ERPNext counts a return against the original invoice's quantity, which would block every later adjustment.")
		)

	_check_allowance(doc, original)


def _check_allowance(doc, original):
	"""
	Refuse an adjustment eTIMS would reject.

	Blocking here is deliberate. The device's rejection arrives after the credit
	note is already submitted, leaving the customer's ledger adjusted and KRA's
	records not -- and nothing on the invoice to say so.
	"""
	from etims_integration.services import allowance as allowance_service

	headroom = allowance_service.summary(original)
	if not headroom:
		frappe.throw(
			_("{0} has no signed eTIMS transmission, so nothing can be adjusted against it yet.").format(original)
		)

	requested = abs(flt(doc.base_grand_total))
	available = float(headroom["remaining_amount"])
	if requested > available + 0.01:
		frappe.throw(
			_("This adjustment is {0} but {1} has only {2} left that eTIMS will accept as a credit ({3} of {4} already credited).").format(
				requested, original, round(available, 2),
				round(float(headroom["credited_amount"]), 2), round(float(headroom["original_amount"]), 2),
			),
			title=_("Exceeds eTIMS Credit Allowance"),
		)


# Section 16 of the VAT Act 2013 allows six months to issue a credit note that
# adjusts output VAT. Slow-moving stock is exactly where this is missed.
VAT_CREDIT_NOTE_MONTHS = 6


def _warn_on_vat_window(doc, original):
	original_date = frappe.db.get_value("Sales Invoice", original, "posting_date")
	if not original_date:
		return

	deadline = add_months(getdate(original_date), VAT_CREDIT_NOTE_MONTHS)
	if getdate(doc.posting_date) <= deadline:
		return

	frappe.msgprint(
		_("{0} was invoiced on {1}, more than {2} months ago. Confirm with your tax advisor that "
		  "the output VAT on this credit note can still be adjusted.").format(
			original, original_date, VAT_CREDIT_NOTE_MONTHS
		),
		title=_("Outside the VAT Credit Note Window"),
		indicator="orange",
	)


def on_submit(doc, method=None):
	"""
	Queue the invoice. Never raises on a device fault: the Transmission carries
	the failure, and an unreachable device is not a reason to refuse a sale.
	"""
	try:
		transmit.queue_invoice(doc)
	except Exception:
		frappe.log_error(title=f"eTIMS: could not queue {doc.name}", message=frappe.get_traceback())
		settings = frappe.get_cached_doc("ETIMS Settings")
		if cint(settings.block_submission_on_failure):
			raise
		frappe.msgprint(
			_("{0} could not be queued for eTIMS. See the Error Log; the invoice itself submitted normally.").format(
				doc.name
			),
			title=_("eTIMS"),
			indicator="orange",
		)


def before_cancel(doc, method=None):
	"""
	A fiscal signature cannot be withdrawn from KRA. Cancelling the ERPNext
	invoice would leave a signed sale declared with nothing behind it, so the
	compliant reversal is a credit note and this says so rather than letting the
	books and KRA silently diverge.
	"""
	if doc.get("custom_etims_status") != "Signed":
		return

	if cint(frappe.get_cached_doc("ETIMS Settings").allow_cancel_after_signing):
		return

	frappe.throw(
		_("{0} has been fiscalised with eTIMS (receipt {1}) and cannot be cancelled. Issue a credit note against it instead.").format(
			doc.name, doc.get("custom_etims_cu_invoice_no")
		),
		title=_("Already Declared to KRA"),
	)


def on_cancel(doc, method=None):
	"""Stand down any transmission that has not yet been signed."""
	for name in frappe.get_all(
		"ETIMS Transmission",
		filters={"sales_invoice": doc.name, "status": ("not in", ("Signed", "Cancelled"))},
		pluck="name",
	):
		frappe.get_doc("ETIMS Transmission", name).cancel_transmission()
