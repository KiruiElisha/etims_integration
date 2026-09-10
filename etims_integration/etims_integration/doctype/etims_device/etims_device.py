# Copyright (c) 2026, Rono and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from etims_integration.comstore.client import ComstoreClient, normalise_base_url
from etims_integration.comstore.errors import DEVICE_DISCONNECTED, ComstoreError, describe
from etims_integration.mapping.sanitize import clean_pin


class ETIMSDevice(Document):
	def validate(self):
		self.validate_pin()
		self.enforce_single_default()

	def validate_pin(self):
		pin = clean_pin(self.pin_of_shop)
		if not pin:
			frappe.throw(
				_("{0} is not a valid KRA PIN. It must be 2 letters followed by 9 digits, e.g. P051238105V.").format(
					self.pin_of_shop
				)
			)
		self.pin_of_shop = pin

	def enforce_single_default(self):
		"""One default per company, so device resolution never has to pick between two."""
		if not self.is_default:
			return
		frappe.db.set_value(
			"ETIMS Device",
			{"company": self.company, "is_default": 1, "name": ("!=", self.name)},
			"is_default",
			0,
		)

	# -------------------------------------------------------------------- client

	@property
	def base_url(self):
		return normalise_base_url(self.host, self.port, self.use_https)

	def get_client(self):
		return ComstoreClient(
			base_url=self.base_url,
			api_key=self.get_password("api_key", raise_exception=False) or "",
			serial_number=self.serial_number,
			timeout=self.timeout or 45,
		)

	# -------------------------------------------------------------------- health

	@frappe.whitelist()
	def test_connection(self):
		"""
		Health plus backlog in one call, written back to the device record so the
		list view answers 'is fiscalisation working right now' without opening
		anything.
		"""
		self.check_permission("write")
		client = self.get_client()
		result = {"base_url": self.base_url, "ok": True, "checks": {}}

		try:
			health = client.health()
			result["health"] = health
			result["checks"]["health"] = "ok"
			connection = (health.get("deviceConnection") or "").strip()
			self.db_set(
				{
					"connection_status": connection or health.get("status") or "Unknown",
					"service_version": health.get("version") or "",
					"last_health_check": now_datetime(),
				},
				update_modified=False,
			)
			# /api/health answers 200 with apiService "Running" while the service's
			# link to the fiscal device is down -- that is a documented reply, not an
			# error, and reporting it as a clean pass is how a device that cannot
			# sign anything shows up green.
			if connection and connection.lower() != "connected":
				result["ok"] = False
				result["checks"]["health"] = "device disconnected"
				result["error"] = describe(DEVICE_DISCONNECTED)
		except ComstoreError as e:
			self.db_set(
				{"connection_status": f"Error: {e.spec.summary}", "last_health_check": now_datetime()},
				update_modified=False,
			)
			result["ok"] = False
			result["checks"]["health"] = "failed"
			result["error"] = describe(e)
			return result

		# /api/health is unauthenticated and answers 200 to a wrong key or none at
		# all, so a green health check proves nothing about the API key. This is the
		# only check that does.
		try:
			status = client.invoice_status()
			result["invoice_status"] = status
			result["checks"]["invoice_status"] = "ok"
			self.db_set(
				{
					"invoices_on_device": status["on_device"],
					"invoices_uploaded": status["uploaded"],
					"pending_invoices": status["pending"],
					"last_reconciled": now_datetime(),
				},
				update_modified=False,
			)
		except ComstoreError as e:
			# A device that answers /health but not /invoices/status can often still
			# sign, so this does not fail the whole test -- but it is a real fault
			# with a real remedy, and returning str(e) gave the desk a sentence it
			# could neither colour nor act on. It goes back classified.
			result["ok"] = False
			result["checks"]["invoice_status"] = "failed"
			result["invoice_status_error"] = describe(e)

		return result

	@frappe.whitelist()
	def initialise(self):
		"""Re-establish the service's link to the fiscal device."""
		self.check_permission("write")
		if not self.device_ip:
			frappe.throw(_("Set the Fiscal Device IP before initialising."))
		return self.get_client().init(self.device_ip)

	@frappe.whitelist()
	def force_upload(self):
		"""Push whatever the device is still holding to KRA."""
		self.check_permission("write")
		return self.get_client().manual_upload()

	@frappe.whitelist()
	def restart(self):
		self.check_permission("write")
		if not self.device_ip:
			frappe.throw(_("Set the Fiscal Device IP before restarting the device."))
		return self.get_client().restart_device(self.device_ip)


def resolve_device(company, branch=None):
	"""
	The device an invoice belongs to. Explicit default first, then the only active
	device for the company. Ambiguity is an error rather than a coin toss -- sending
	an invoice from the wrong device misattributes it to the wrong KRA PIN.
	"""
	filters = {"company": company, "status": "Active"}
	if branch:
		branch_match = frappe.get_all("ETIMS Device", filters=dict(filters, branch=branch), pluck="name")
		if len(branch_match) == 1:
			return branch_match[0]

	default = frappe.get_all("ETIMS Device", filters=dict(filters, is_default=1), pluck="name")
	if default:
		return default[0]

	devices = frappe.get_all("ETIMS Device", filters=filters, pluck="name")
	if len(devices) == 1:
		return devices[0]
	if not devices:
		frappe.throw(_("No active eTIMS Device is configured for {0}.").format(company))

	frappe.throw(
		_("{0} has {1} active eTIMS Devices and none is marked default. Mark one as 'Default for Company'.").format(
			company, len(devices)
		)
	)
