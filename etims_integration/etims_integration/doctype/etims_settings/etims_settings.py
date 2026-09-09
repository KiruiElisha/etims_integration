# Copyright (c) 2026, Rono and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ETIMSSettings(Document):
	def validate(self):
		self.validate_unique_mappings()
		self.validate_retry_policy()

	def validate_unique_mappings(self):
		"""
		Two rows for one template would make band resolution order-dependent, which
		is precisely the kind of quiet ambiguity this table exists to remove.
		"""
		seen = set()
		for row in self.tax_mappings or []:
			if row.item_tax_template in seen:
				frappe.throw(
					_("Row {0}: {1} is mapped more than once.").format(row.idx, row.item_tax_template)
				)
			seen.add(row.item_tax_template)

	def validate_retry_policy(self):
		if (self.max_retries or 0) < 0:
			frappe.throw(_("Max Retries cannot be negative."))
		if (self.retry_backoff_minutes or 0) < 1:
			self.retry_backoff_minutes = 1


def get_settings():
	"""Cached accessor. Read on every invoice submit, so it should not hit the DB."""
	return frappe.get_cached_doc("ETIMS Settings")
