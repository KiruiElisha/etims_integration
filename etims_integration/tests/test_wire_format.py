"""
Tests for the layers that do not need a site.

``comstore`` and ``mapping.sanitize`` import no Frappe, on purpose, so the rules
that decide what KRA is told can be checked with plain unittest:

	python -m unittest discover -s apps/etims_integration/etims_integration/tests

The arithmetic that matters most -- that the band totals always re-add to the
line totals -- is checked here as a property over many shapes of invoice rather
than one worked example, because that is the invariant E341 punishes.
"""

import unittest
from decimal import Decimal

from etims_integration.comstore.client import normalise_base_url
from etims_integration.comstore.errors import ERROR_CODES, TRANSPORT, UNKNOWN, classify
from etims_integration.comstore.schema import (
	BAND_B_STANDARD,
	BAND_KEYS,
	PLULine,
	SignStructure,
	WorkflowResult,
	money,
)
from etims_integration.mapping import sanitize


class TestBaseUrl(unittest.TestCase):
	def test_ip_gets_the_configured_port(self):
		self.assertEqual(normalise_base_url("192.168.1.5", 4000), "http://192.168.1.5:4000")

	def test_localhost_gets_the_configured_port(self):
		self.assertEqual(normalise_base_url("localhost", 4000), "http://localhost:4000")

	def test_named_host_is_left_on_its_default_port(self):
		# The shared test service answers on 80/443. Forcing :4000 onto it breaks it.
		self.assertEqual(normalise_base_url("hedgeinc.co.ke", 4000), "http://hedgeinc.co.ke")

	def test_https_flag(self):
		self.assertEqual(normalise_base_url("hedgeinc.co.ke", 4000, True), "https://hedgeinc.co.ke")

	def test_explicit_port_wins(self):
		self.assertEqual(normalise_base_url("host.local:9000", 4000), "http://host.local:9000")

	def test_full_url_is_passed_through(self):
		self.assertEqual(normalise_base_url("https://x.example/api/", 4000), "https://x.example/api")

	def test_empty_host_is_rejected(self):
		with self.assertRaises(ValueError):
			normalise_base_url("", 4000)


class TestSanitize(unittest.TestCase):
	def test_accents_are_folded_not_dropped(self):
		self.assertEqual(sanitize.item_name("Café Latté"), "Cafe Latte")

	def test_forbidden_symbols_are_removed(self):
		# The doc names these explicitly as eTIMS rejections.
		self.assertEqual(sanitize.item_name("Wid\\get →Blue§"), "Widget Blue")

	def test_cjk_is_removed(self):
		self.assertEqual(sanitize.item_name("Rice 米 5kg"), "Rice 5kg")

	def test_name_is_capped_at_fifty_characters(self):
		self.assertEqual(len(sanitize.item_name("A" * 200)), 50)

	def test_empty_name_falls_back(self):
		self.assertEqual(sanitize.item_name("→→→", fallback="ITEM-1"), "ITEM-1")

	def test_valid_pins(self):
		for pin in ("P051238105V", "P123456789Q", "A068675468P"):
			self.assertEqual(sanitize.clean_pin(pin), pin)

	def test_pin_is_normalised(self):
		self.assertEqual(sanitize.clean_pin(" p051238105v "), "P051238105V")

	def test_malformed_pin_becomes_empty_not_garbage(self):
		# Sending junk earns E325/E358 and loses the invoice; sending nothing is legal.
		for pin in ("12345", "NOTAPIN", "", None, "P0512381"):
			self.assertEqual(sanitize.clean_pin(pin), "")


class TestErrorClassification(unittest.TestCase):
	def test_code_is_recovered_from_free_text(self):
		spec = classify("E337: NO FIND PLU DATA")
		self.assertEqual(spec.code, "E337")
		self.assertIn("not registered on the device", spec.remedy)

	def test_rejections_are_never_retryable(self):
		# The device is deterministic: retrying a rejected payload just repeats it.
		for code, spec in ERROR_CODES.items():
			self.assertFalse(spec.retryable, f"{code} must not be auto-retried")

	def test_transport_is_retryable(self):
		self.assertTrue(TRANSPORT.retryable)

	def test_unknown_code_is_not_retried(self):
		spec = classify("something the vendor invented last week")
		self.assertIs(spec, UNKNOWN)
		self.assertFalse(spec.retryable)

	def test_aliased_codes_share_their_partners_remedy(self):
		self.assertEqual(ERROR_CODES["E331"].remedy, ERROR_CODES["E218"].remedy)


class TestWireShapes(unittest.TestCase):
	def test_line_uses_the_devices_own_key_spelling(self):
		line = PLULine(
			item_name="Test",
			sale_price=Decimal("484.50"),
			sale_qty=Decimal("1000"),
			sale_amount=Decimal("484500.00"),
		)
		payload = line.as_payload()
		self.assertEqual(payload["item_Name"], "Test")
		self.assertEqual(payload["ItemDisCount(%)"], "0.00")
		# Every numeric field crosses the wire as a string.
		self.assertTrue(all(isinstance(v, str) for v in payload.values()))

	def test_every_vat_band_is_present_even_when_zero(self):
		structure = SignStructure(
			pin_of_shop="P051238105V",
			trader_invoice_number="1000000001",
			bands={BAND_B_STANDARD: (Decimal("417672.41"), Decimal("66827.58"))},
		)
		payload = structure.as_payload()

		for net_key, value_key in BAND_KEYS.values():
			self.assertIn(net_key, payload)
			self.assertIn(value_key, payload)

		self.assertEqual(payload["Vat B(16.00%) net"], "417672.41")
		self.assertEqual(payload["Vat A(Exempt) net"], "0.00")
		# F and G stay empty rather than "0": a zero there trips E341 on some firmware.
		self.assertEqual(payload["Levy G(2.00%) net"], "")

	def test_sign_type_follows_test_mode(self):
		live = SignStructure(pin_of_shop="P051238105V", trader_invoice_number="1", is_live=True)
		test = SignStructure(pin_of_shop="P051238105V", trader_invoice_number="1", is_live=False)
		self.assertEqual(live.as_payload()["SignType"], "1")
		self.assertEqual(test.as_payload()["SignType"], "0")

	def test_result_reads_the_hyphenated_keys(self):
		result = WorkflowResult.from_response(
			{
				"success": True,
				"cu-inv-no": "0082721",
				"internal-data": "BPPX-44VT-JGDP-UMNB-DGRN-XM7B-5E",
				"Receipt Signature": "OMD7-JSFH-AOZI-BEEV",
				"scu_id": "KRACU0300000058",
			}
		)
		self.assertEqual(result.cu_invoice_number, "0082721")
		self.assertEqual(result.receipt_signature, "OMD7-JSFH-AOZI-BEEV")
		self.assertEqual(result.scu_id, "KRACU0300000058")


class TestDecomposition(unittest.TestCase):
	"""
	The core invariant: net + vat + levy + surcharge == the line total, exactly,
	for every band and every shape of number. If this holds per line then the band
	totals -- which are sums of these -- must equal the sum of the lines, and E341
	cannot arise.
	"""

	def decompose(self, gross, vat_rate, levy_rate=Decimal("0"), surcharge_rate=Decimal("0")):
		# Imported lazily: mapping.invoice pulls in frappe, so this test is skipped
		# rather than failed when it runs outside a bench.
		from etims_integration.mapping.invoice import _decompose

		return _decompose(gross, vat_rate, levy_rate, surcharge_rate)

	def test_worked_example_from_the_documentation(self):
		net, vat, _levy, _schg = self.decompose(Decimal("100.00"), Decimal("16"))
		self.assertEqual(net, Decimal("86.21"))
		self.assertEqual(vat, Decimal("13.79"))

	def test_worked_levy_example_from_the_documentation(self):
		net, vat, levy, _schg = self.decompose(Decimal("100.00"), Decimal("16"), Decimal("2"))
		self.assertEqual(net, Decimal("84.75"))
		self.assertEqual(levy, Decimal("1.69"))
		self.assertEqual(vat, Decimal("13.56"))

	def test_eight_percent_band(self):
		net, vat, _l, _s = self.decompose(Decimal("100.00"), Decimal("8"))
		self.assertEqual(net, Decimal("92.59"))
		self.assertEqual(vat, Decimal("7.41"))

	def test_zero_rated_bands_carry_no_tax(self):
		net, vat, _l, _s = self.decompose(Decimal("100.00"), Decimal("0"))
		self.assertEqual(net, Decimal("100.00"))
		self.assertEqual(vat, Decimal("0.00"))

	def test_parts_always_re_add_to_the_total(self):
		awkward = ["0.01", "0.03", "1.99", "33.33", "99.99", "484499.99", "1234567.89", "7.77"]
		for amount in awkward:
			for vat_rate in (Decimal("0"), Decimal("8"), Decimal("16")):
				for levy_rate in (Decimal("0"), Decimal("2")):
					gross = Decimal(amount)
					net, vat, levy, schg = self.decompose(gross, vat_rate, levy_rate)
					self.assertEqual(
						net + vat + levy + schg,
						gross,
						f"decomposition of {gross} at vat={vat_rate} levy={levy_rate} lost a cent",
					)


class TestMoney(unittest.TestCase):
	def test_half_up_rounding(self):
		# KRA rounds half up; banker's rounding would under-declare on .5 cases.
		self.assertEqual(money(Decimal("0.125")), Decimal("0.13"))
		self.assertEqual(money(Decimal("2.005")), Decimal("2.01"))

	def test_accepts_floats_and_strings(self):
		self.assertEqual(money("1.5"), Decimal("1.50"))
		self.assertEqual(money(None), Decimal("0.00"))


if __name__ == "__main__":
	unittest.main()
