"""
Band resolution: which eTIMS VAT band does this line belong to?

The old TIMS app guessed, by looking for the substring "exempt" in a tax template
title and otherwise assuming 16%. Under eTIMS a guess is worse than a failure:
the band an invoice claims must match the band the item is *registered under on
the device*, so a wrong guess earns E321 and the invoice is rejected anyway --
after the wrong figures have already been declared on a retry, if the operator
resends blindly.

So this module resolves a band from configuration, in a fixed order of authority,
and returns ``None`` when nothing says. ``None`` blocks the invoice; it never
becomes a default.
"""

from decimal import Decimal

import frappe

from etims_integration.comstore.schema import (
	BAND_A_EXEMPT,
	BAND_B_STANDARD,
	BAND_C_ZERO,
	BAND_D_NON_VAT,
	BAND_E_TOURISM,
	BAND_RATES,
	VAT_BANDS,
)


class UnresolvedBand(Exception):
	"""No configuration answers for this item. Never guessed around."""

	def __init__(self, item_code, item_name, tax_template=None):
		self.item_code = item_code
		self.item_name = item_name
		self.tax_template = tax_template
		super().__init__(f"No eTIMS tax band configured for {item_code}")


def normalise_band(value):
	"""
	Accept anything a human might have typed or the device might echo -- ``B``,
	``b``, ``B-16.00%``, ``16``, ``16.0`` -- and return the bare letter.
	"""
	if not value:
		return None

	text = str(value).strip().upper()
	if text in VAT_BANDS:
		return text
	if "-" in text and text.split("-")[0] in VAT_BANDS:
		return text.split("-")[0]

	# A bare rate is ambiguous between the three zero-rate bands, so only the two
	# rates that identify a band uniquely are accepted.
	try:
		rate = Decimal(text.rstrip("%"))
	except Exception:
		return None
	if rate == 16:
		return BAND_B_STANDARD
	if rate == 8:
		return BAND_E_TOURISM
	return None


@frappe.request_cache
def _template_band_map():
	"""
	Item Tax Template -> band, from the eTIMS Tax Mapping table on eTIMS Settings.
	Cached per request: an invoice with fifty lines should not read the Single
	fifty times.
	"""
	settings = frappe.get_cached_doc("ETIMS Settings")
	return {
		row.item_tax_template: normalise_band(row.etims_band)
		for row in (settings.tax_mappings or [])
		if row.item_tax_template and normalise_band(row.etims_band)
	}


def resolve_band(item_code, item_tax_template=None, item_name=None):
	"""
	Order of authority, most specific first:

	1. the item's own ``custom_etims_tax_type`` -- set when the item was
	   registered on the device, so it is by definition what the device believes;
	2. the eTIMS Tax Mapping for the invoice line's Item Tax Template;
	3. the Item Group's ``custom_etims_tax_type``, for bulk configuration.

	Deliberately no rate-based fallback. A 0% rate cannot distinguish exempt from
	zero-rated from non-VAT, and picking one of the three at random is exactly the
	class of silent error this app exists to remove.
	"""
	if item_code:
		item = frappe.get_cached_value(
			"Item", item_code, ["custom_etims_tax_type", "item_group"], as_dict=True
		)
		if item:
			band = normalise_band(item.custom_etims_tax_type)
			if band:
				return band

	if item_tax_template:
		band = _template_band_map().get(item_tax_template)
		if band:
			return band

	if item_code and item and item.item_group:
		band = normalise_band(
			frappe.get_cached_value("Item Group", item.item_group, "custom_etims_tax_type")
		)
		if band:
			return band

	raise UnresolvedBand(item_code, item_name or item_code, item_tax_template)


def band_rate(band):
	return BAND_RATES.get(band, Decimal("0"))


def describe(band):
	return {
		BAND_A_EXEMPT: "A - Exempt",
		BAND_B_STANDARD: "B - 16%",
		BAND_C_ZERO: "C - Zero rated",
		BAND_D_NON_VAT: "D - Non-VAT",
		BAND_E_TOURISM: "E - 8% tourism",
	}.get(band, band or "unresolved")
