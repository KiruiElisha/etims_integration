"""
Item registration against the fiscal device.

eTIMS will not accept an invoice line for an item it does not hold (E337), so
this is a hard prerequisite for fiscalisation rather than a convenience. It is
also the part the old TIMS app had no answer for at all.

Two properties matter and both come from the ETIMS Item Registration row:

* **Idempotence.** The device treats ``plu_name`` and ``barcode`` as unique keys
  and rejects a second item claiming either (E353). A PLU number is therefore
  allocated once per item per device and reused for every later update.
* **Stock as a delta.** ``change_qty`` is an increment the device applies to its
  own counter, not an absolute. Sending ERPNext's balance would compound it, so
  deltas are computed against ``stock_on_device`` -- what we last told it.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from etims_integration.comstore.errors import ComstoreError
from etims_integration.etims_integration.doctype.etims_device.etims_device import resolve_device
from etims_integration.mapping import item as item_mapping

BATCH_LIMIT = 200


def registration_name(device, item):
	return f"{device}::{item}"


def get_or_create_registration(item_code, device):
	name = registration_name(device, item_code)
	if frappe.db.exists("ETIMS Item Registration", name):
		return frappe.get_doc("ETIMS Item Registration", name)

	return frappe.get_doc(
		{
			"doctype": "ETIMS Item Registration",
			"item": item_code,
			"device": device,
			"plu_no": allocate_plu_no(device),
			"status": "Pending",
		}
	).insert(ignore_permissions=True)


def allocate_plu_no(device):
	"""
	Next free PLU slot on this device. Numbered from 1 upward, per device, because
	the numbers are the device's own address space and mean nothing across devices.
	"""
	highest = frappe.db.sql(
		"""
		select max(cast(plu_no as unsigned))
		from `tabETIMS Item Registration`
		where device = %s
		""",
		device,
	)
	return str((cint(highest[0][0]) if highest and highest[0] else 0) + 1)


def mark_stale(item_code):
	"""
	Clear the fingerprint on every registration for this item so the next sync
	picks it up. Called when an item's eTIMS-relevant fields change.
	"""
	for name in frappe.get_all("ETIMS Item Registration", filters={"item": item_code}, pluck="name"):
		frappe.db.set_value(
			"ETIMS Item Registration", name, {"fingerprint": "", "status": "Pending"}, update_modified=False
		)


def queue_item(item_code, device=None):
	"""Register or refresh one item. Enqueued from the Item hooks."""
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not (cint(settings.enabled) and cint(settings.auto_sync_items)):
		return

	item = frappe.get_cached_doc("Item", item_code)
	if item_mapping.missing_configuration(item):
		# Not an error: most items are created before anyone fills in their KRA
		# codes. It simply is not registrable yet, and the sync will find it once
		# the codes are set.
		return

	device = device or _default_device_for_item(item)
	if not device:
		return

	get_or_create_registration(item_code, device)
	frappe.enqueue(sync_device_items, queue="long", device=device, enqueue_after_commit=True)


def _default_device_for_item(item):
	"""
	Items are company-agnostic, so an item sync targets the default device of each
	company that has one. Returns the single obvious device, or None when the
	choice is ambiguous and a person should drive it.
	"""
	devices = frappe.get_all("ETIMS Device", filters={"status": "Active"}, pluck="name")
	return devices[0] if len(devices) == 1 else None


# ---------------------------------------------------------------------- syncing


def sync_device_items(device, item_codes=None, force=False):
	"""
	Push every stale registration for one device, in batches.

	Batched rather than one-at-a-time because ``upload-plu-data`` takes an array
	and each call carries a full device round trip; batched rather than all-at-once
	because a single rejected item fails the whole call, and a smaller batch makes
	the culprit obvious.
	"""
	device_doc = frappe.get_doc("ETIMS Device", device)
	if device_doc.status != "Active":
		return {"skipped": _("Device is not active.")}

	settings = frappe.get_cached_doc("ETIMS Settings")
	batch_size = cint(settings.item_sync_batch_size) or 50

	filters = {"device": device}
	if item_codes:
		filters["item"] = ("in", item_codes)
	elif not force:
		filters["status"] = ("in", ("Pending", "Failed"))

	registrations = frappe.get_all(
		"ETIMS Item Registration",
		filters=filters,
		fields=["name", "item", "plu_no", "fingerprint", "stock_on_device"],
		limit=BATCH_LIMIT,
	)

	client = device_doc.get_client()
	pending = []
	results = {
		"sent": 0,
		"skipped": 0,
		"failed": 0,
		"skipped_reasons": {},
		# One run only ever looks at BATCH_LIMIT registrations. Saying so beats a
		# catalogue that appears to stop syncing for no reason at 200 items.
		"unprocessed": max(0, frappe.db.count("ETIMS Item Registration", filters) - len(registrations)),
	}

	for registration in registrations:
		item = frappe.get_cached_doc("Item", registration.item)

		missing = item_mapping.missing_configuration(item)
		if missing:
			_skip(results, registration.item, _("not configured: {0}").format(", ".join(missing)))
			continue

		fingerprint = item_mapping.fingerprint(item)
		if fingerprint == registration.fingerprint and not force:
			_skip(results, registration.item, _("unchanged since the last sync"))
			continue

		try:
			plu = item_mapping.build(
				item,
				registration.plu_no,
				change_qty=_stock_delta(registration, item, settings),
			)
		except frappe.ValidationError as e:
			_fail(registration.name, str(e))
			results["failed"] += 1
			continue

		pending.append((registration, fingerprint, plu))

		if len(pending) >= batch_size:
			results["sent"] += _flush(client, pending, results)
			pending = []

	if pending:
		results["sent"] += _flush(client, pending, results)

	return results


def _skip(results, item_code, reason):
	"""
	Record *why* an item was skipped, grouped by reason.

	A bare count is the least actionable thing a sync can report: "skipped 479"
	reads identically whether every item is missing a packaging unit or every
	item is simply already up to date.
	"""
	results["skipped"] += 1
	entry = results["skipped_reasons"].setdefault(reason, {"count": 0, "examples": []})
	entry["count"] += 1
	if len(entry["examples"]) < 10:
		entry["examples"].append(item_code)


def _flush(client, pending, results):
	"""Send one batch and record the outcome on every registration in it."""
	try:
		client.upload_plu([plu for _reg, _fp, plu in pending])
	except ComstoreError as e:
		# The device rejects a batch as a whole, so the fault is attributed to
		# every member. Re-running with a smaller batch narrows it down.
		for registration, _fp, _plu in pending:
			_fail(registration.name, f"{e.spec.summary}: {e.spec.remedy}")
		results["failed"] += len(pending)
		return 0

	for registration, fingerprint, plu in pending:
		frappe.db.set_value(
			"ETIMS Item Registration",
			registration.name,
			{
				"status": "Registered",
				"fingerprint": fingerprint,
				"last_synced": now_datetime(),
				"error_message": None,
				"stock_on_device": flt(registration.get("stock_on_device")) + flt(plu.change_qty),
			},
			update_modified=False,
		)
		# The invoice mapper reads the PLU number off the Item to fill the line
		# barcode, so the two stay pointed at the same device record.
		frappe.db.set_value("Item", registration.item, "custom_etims_plu_no", plu.plu_no, update_modified=False)

	return len(pending)


def _fail(name, message):
	frappe.db.set_value(
		"ETIMS Item Registration",
		name,
		{"status": "Failed", "error_message": message[:5000]},
		update_modified=False,
	)


def _stock_delta(registration, item, settings):
	"""
	How much stock to add on the device: current ERPNext balance minus what we
	have already told it about. Returns 0 unless stock pushing is switched on --
	a device whose stock is managed elsewhere must not be double-counted.
	"""
	if not (cint(settings.push_stock) and cint(item.is_stock_item)):
		return 0

	balance = (
		frappe.db.sql(
			"select sum(actual_qty) from `tabBin` where item_code = %s", item.name
		)[0][0]
		or 0
	)
	return flt(balance) - flt(registration.get("stock_on_device"))


# ------------------------------------------------------------------- scheduler


def sync_pending():
	"""Daily. Sweeps up items that became registrable since the last run."""
	settings = frappe.get_cached_doc("ETIMS Settings")
	if not (cint(settings.enabled) and cint(settings.auto_sync_items)):
		return

	for device in frappe.get_all("ETIMS Device", filters={"status": "Active"}, pluck="name"):
		frappe.enqueue(sync_device_items, queue="long", device=device)


# ------------------------------------------------------------------------ desk


@frappe.whitelist()
def register_items(items, device=None):
	"""Register a selection of items on demand, from the Item list view."""
	frappe.has_permission("Item", "write", throw=True)

	if isinstance(items, str):
		items = frappe.parse_json(items)

	device = device or resolve_device(frappe.defaults.get_user_default("Company"))
	for item_code in items:
		get_or_create_registration(item_code, device)

	return sync_device_items(device, item_codes=items, force=True)
