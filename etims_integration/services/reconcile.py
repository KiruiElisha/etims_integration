"""
Device reconciliation.

A signature is not compliance. The device signs locally and forwards to KRA on
its own schedule, and the doc is blunt that this "may not be the case 100% of the
time" -- so an invoice can be signed, printed, handed to a customer, and still
not have reached KRA days later. Nothing in ERPNext would show it.

This module closes that gap: it polls each device's backlog, records it, nudges
the device when the backlog stops shrinking, and raises an alert when it does not
recover. It is the difference between believing you are compliant and knowing.
"""

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from etims_integration.comstore.errors import ComstoreError


def reconcile_devices():
	"""Hourly. One pass over every active device."""
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not (cint(settings.enabled) and cint(settings.reconcile_enabled)):
		return

	for name in frappe.get_all("ETIMS Device", filters={"status": "Active"}, pluck="name"):
		try:
			reconcile_device(name, settings)
		except Exception:
			frappe.log_error(title=f"eTIMS: reconciliation failed for {name}", message=frappe.get_traceback())


def reconcile_device(device_name, settings=None):
	settings = settings or frappe.get_cached_doc("ETIMS Settings")
	device = frappe.get_doc("ETIMS Device", device_name)
	client = device.get_client()

	try:
		status = client.invoice_status()
	except ComstoreError as e:
		device.db_set(
			{"connection_status": f"Error: {e.spec.summary}", "last_health_check": now_datetime()},
			update_modified=False,
		)
		return {"device": device_name, "error": str(e)}

	previous_pending = cint(device.pending_invoices)
	pending = status["pending"]

	device.db_set(
		{
			"invoices_on_device": status["on_device"],
			"invoices_uploaded": status["uploaded"],
			"pending_invoices": pending,
			"last_reconciled": now_datetime(),
			"connection_status": "Connected",
			"last_health_check": now_datetime(),
		},
		update_modified=False,
	)

	result = {"device": device_name, "pending": pending, "previous": previous_pending, "nudged": False}

	if pending <= cint(settings.pending_threshold):
		return result

	# Only intervene when the device has stopped making progress by itself.
	# Forcing an upload while it is already draining just duplicates work.
	stalled = previous_pending and pending >= previous_pending
	if stalled and cint(settings.auto_manual_upload):
		try:
			client.manual_upload()
			result["nudged"] = True
		except ComstoreError as e:
			result["nudge_error"] = str(e)

	_raise_backlog_alert(device, pending, stalled)
	return result


def _raise_backlog_alert(device, pending, stalled):
	"""
	Tell somebody. A backlog that only ever appears in a log is a backlog nobody
	acts on until KRA asks about it.
	"""
	if not stalled:
		return

	frappe.log_error(
		title=f"eTIMS: {device.name} has {pending} invoices not yet at KRA",
		message=_(
			"The device is holding {0} invoices that KRA has not acknowledged, and the "
			"backlog did not shrink since the last check. A forced upload was attempted. "
			"Check the device's network path to KRA."
		).format(pending),
	)

	for user in _recipients():
		frappe.publish_realtime(
			"msgprint",
			{
				"message": _("eTIMS device {0}: {1} invoices are still waiting to reach KRA.").format(
					device.name, pending
				),
				"title": _("eTIMS Backlog"),
				"indicator": "red",
			},
			user=user,
		)


def _recipients():
	return frappe.get_all(
		"Has Role",
		filters={"role": "Accounts Manager", "parenttype": "User"},
		pluck="parent",
		limit=20,
	)


# ------------------------------------------------------------------------ desk


@frappe.whitelist()
def reconcile_now(device):
	frappe.has_permission("ETIMS Device", "write", doc=device, throw=True)
	return reconcile_device(device)


@frappe.whitelist()
def unsigned_invoices(company=None, days=30):
	"""
	Submitted invoices with no accepted signature. The question an accountant
	actually asks at month end, answered from one query instead of by eye.
	"""
	conditions = ["si.docstatus = 1", "si.posting_date >= DATE_SUB(CURDATE(), INTERVAL %(days)s DAY)"]
	values = {"days": cint(days) or 30}

	if company:
		conditions.append("si.company = %(company)s")
		values["company"] = company

	return frappe.db.sql(
		f"""
		select si.name, si.posting_date, si.customer, si.base_grand_total,
			coalesce(si.custom_etims_status, 'Not Sent') as etims_status,
			tx.name as transmission, tx.status as transmission_status,
			tx.error_summary, tx.remedy
		from `tabSales Invoice` si
		left join `tabETIMS Transmission` tx
			on tx.sales_invoice = si.name and tx.status != 'Cancelled'
		where {" and ".join(conditions)}
			and coalesce(si.custom_etims_status, '') != 'Signed'
			and coalesce(si.custom_etims_exempt, 0) = 0
		order by si.posting_date asc
		""",
		values,
		as_dict=True,
	)
