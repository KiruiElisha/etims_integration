"""
Buyer registration.

Optional for fiscalisation -- an invoice carries ``pinOfBuyer`` whether or not the
buyer is on the device -- but the device keeps its own buyer table, and keeping it
in step lets a shop reprint a compliant receipt with ERPNext unreachable.

The device rejects a whole batch over one malformed PIN (E358), so customers
without a valid PIN are filtered out here rather than discovered at the far end.
"""

import frappe
from frappe.utils import cint, now_datetime

from etims_integration.comstore.errors import ComstoreError
from etims_integration.mapping import buyer as buyer_mapping
from etims_integration.mapping.sanitize import clean_pin

BATCH_SIZE = 30


def registration_name(device, customer):
	return f"{device}::{customer}"


def allocate_buyer_no(device):
	highest = frappe.db.sql(
		"select max(cast(buyer_no as unsigned)) from `tabETIMS Buyer Registration` where device = %s",
		device,
	)
	return str((cint(highest[0][0]) if highest and highest[0] else 0) + 1)


def queue_customer(customer_name, device=None):
	"""Called when a Customer with a KRA PIN is created or its PIN changes."""
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not cint(settings.enabled):
		return

	customer = frappe.get_cached_doc("Customer", customer_name)
	if not clean_pin(customer.get("tax_id")):
		return

	devices = frappe.get_all("ETIMS Device", filters={"status": "Active"}, pluck="name")
	targets = [device] if device else devices
	for target in targets:
		name = registration_name(target, customer_name)
		if frappe.db.exists("ETIMS Buyer Registration", name):
			frappe.db.set_value("ETIMS Buyer Registration", name, "status", "Pending", update_modified=False)
		else:
			frappe.get_doc(
				{
					"doctype": "ETIMS Buyer Registration",
					"customer": customer_name,
					"device": target,
					"buyer_no": allocate_buyer_no(target),
					"pin": clean_pin(customer.get("tax_id")),
					"status": "Pending",
				}
			).insert(ignore_permissions=True)


def sync_device_buyers(device):
	device_doc = frappe.get_doc("ETIMS Device", device)
	if device_doc.status != "Active":
		return {"skipped": "Device is not active."}

	registrations = frappe.get_all(
		"ETIMS Buyer Registration",
		filters={"device": device, "status": ("in", ("Pending", "Failed"))},
		fields=["name", "customer", "buyer_no"],
		limit=BATCH_SIZE * 4,
	)

	client = device_doc.get_client()
	results = {"sent": 0, "skipped": 0, "failed": 0}
	batch = []

	for registration in registrations:
		customer = frappe.get_cached_doc("Customer", registration.customer)
		record = buyer_mapping.build(customer, registration.buyer_no)
		if not record:
			frappe.db.set_value(
				"ETIMS Buyer Registration",
				registration.name,
				{"status": "Failed", "error_message": "Customer has no valid KRA PIN."},
				update_modified=False,
			)
			results["skipped"] += 1
			continue

		batch.append((registration, record))
		if len(batch) >= BATCH_SIZE:
			results["sent"] += _flush(client, batch, results)
			batch = []

	if batch:
		results["sent"] += _flush(client, batch, results)

	return results


def _flush(client, batch, results):
	try:
		client.set_buyers([record for _reg, record in batch], end_number=max(BATCH_SIZE, len(batch)))
	except ComstoreError as e:
		for registration, _record in batch:
			frappe.db.set_value(
				"ETIMS Buyer Registration",
				registration.name,
				{"status": "Failed", "error_message": f"{e.spec.summary}: {e.spec.remedy}"},
				update_modified=False,
			)
		results["failed"] += len(batch)
		return 0

	for registration, record in batch:
		frappe.db.set_value(
			"ETIMS Buyer Registration",
			registration.name,
			{"status": "Registered", "pin": record.pin, "last_synced": now_datetime(), "error_message": None},
			update_modified=False,
		)
	return len(batch)


def sync_pending():
	"""Daily."""
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not cint(settings.enabled):
		return
	for device in frappe.get_all("ETIMS Device", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue(sync_device_buyers, queue="long", device=device)
