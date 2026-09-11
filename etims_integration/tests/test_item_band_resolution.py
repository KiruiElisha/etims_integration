"""
An item configured in bulk must be registrable.

The device rejects an invoice line for an item it does not hold (E337), so
registration is a hard prerequisite. Registration used to read the band straight
off the Item while invoicing went through ``tax.resolve_band``, which also
consults the eTIMS Tax Mapping and the Item Group. Anything configured either of
those two ways was therefore invoiceable but never registrable: every sync
counted it as unconfigured and skipped it.

Skipped rather than errored without a Frappe environment, like the other tests
here that need a site.
"""

import unittest
from unittest import mock

try:
	from etims_integration.mapping import item as item_mapping
	from etims_integration.mapping import tax

	HAS_FRAPPE = True
except Exception:  # pragma: no cover - depends on the runner
	HAS_FRAPPE = False


def item(**overrides):
	"""A fully-configured item, minus whatever the case is testing."""
	base = {
		"name": "TEST-ITEM",
		"item_name": "Test Item",
		"item_group": "Ungrouped",
		"custom_etims_item_class_code": "99010000",
		"custom_etims_package_unit": "BG",
		"custom_etims_quantity_unit": "U",
		"custom_etims_tax_type": None,
		"taxes": [],
	}
	base.update(overrides)
	return base


@unittest.skipUnless(HAS_FRAPPE, "needs a Frappe environment")
class TestBandForItem(unittest.TestCase):
	def setUp(self):
		self.groups = {"Beverages": "C-0%", "Ungrouped": None}
		patcher = mock.patch.object(
			tax.frappe,
			"get_cached_value",
			side_effect=lambda doctype, name, field: self.groups.get(name),
		)
		patcher.start()
		self.addCleanup(patcher.stop)

		mapping = mock.patch.object(
			tax, "_template_band_map", return_value={"KE VAT 16%": "B", "KE Zero": "C"}
		)
		mapping.start()
		self.addCleanup(mapping.stop)

	def test_the_items_own_band_wins(self):
		self.assertEqual(tax.band_for_item(item(custom_etims_tax_type="E-8%")), "E")

	def test_item_group_default_is_honoured(self):
		"""The regression: this is what the sync skipped on every run."""
		self.assertEqual(tax.band_for_item(item(item_group="Beverages")), "C")

	def test_tax_template_mapping_is_honoured(self):
		self.assertEqual(
			tax.band_for_item(item(taxes=[{"item_tax_template": "KE VAT 16%"}])), "B"
		)

	def test_item_beats_group(self):
		resolved = tax.band_for_item(item(custom_etims_tax_type="A-Exempt", item_group="Beverages"))
		self.assertEqual(resolved, "A")

	def test_disagreeing_templates_fall_through_rather_than_guess(self):
		"""
		Two templates naming different bands is ambiguity. Picking one silently is
		the class of error this app exists to remove, so it defers to the group.
		"""
		resolved = tax.band_for_item(
			item(
				item_group="Beverages",
				taxes=[{"item_tax_template": "KE VAT 16%"}, {"item_tax_template": "KE Zero"}],
			)
		)
		self.assertEqual(resolved, "C")

	def test_nothing_configured_stays_unresolved(self):
		self.assertIsNone(tax.band_for_item(item()))


@unittest.skipUnless(HAS_FRAPPE, "needs a Frappe environment")
class TestMissingConfiguration(unittest.TestCase):
	def setUp(self):
		self.groups = {"Beverages": "C-0%", "Ungrouped": None}
		patcher = mock.patch.object(
			tax.frappe,
			"get_cached_value",
			side_effect=lambda doctype, name, field: self.groups.get(name),
		)
		patcher.start()
		self.addCleanup(patcher.stop)

		mapping = mock.patch.object(tax, "_template_band_map", return_value={})
		mapping.start()
		self.addCleanup(mapping.stop)

	def test_group_configured_item_is_registrable(self):
		self.assertEqual(item_mapping.missing_configuration(item(item_group="Beverages")), [])

	def test_unbanded_item_still_names_the_band(self):
		self.assertIn("eTIMS Tax Type", item_mapping.missing_configuration(item()))

	def test_other_required_fields_are_still_required(self):
		"""
		The band gained a fallback chain; the code masters did not. A missing
		packaging unit is still a genuinely unregistrable item.
		"""
		missing = item_mapping.missing_configuration(
			item(item_group="Beverages", custom_etims_package_unit=None)
		)
		self.assertEqual(missing, ["eTIMS Packaging Unit"])


if __name__ == "__main__":
	unittest.main()
