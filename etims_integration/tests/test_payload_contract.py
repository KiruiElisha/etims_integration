"""
The payload contract.

Pins the exact key set the device expects, so a refactor cannot quietly drop or
rename a field. The keys come from the vendor's own Postman collection rather
than from the PDF, because the two disagree: `DiscAmt` appears in every example
the vendor ships and in none of the PDF's parameter tables.
"""

import unittest
from decimal import Decimal

from etims_integration.comstore.schema import (
	INVOICE_CREDIT,
	PLULine,
	SignStructure,
	parse_device_timestamp,
)

# Verbatim from https://documenter.getpostman.com/view/33982185/2sBXcKDe3k
VENDOR_PLU_KEYS = {
	"item_Name", "Barcode", "SalePrice", "SaleQty", "SaleAmount",
	"ItemDisCount(%)", "ItemDisCount", "Schg", "Levy",
}

VENDOR_SIGN_KEYS = {
	"SignType", "CashAmt", "CheckAmt", "CardAmt", "DiscAmt", "InvoiceType",
	"relevantInvoiceNumber", "pinOfBuyer", "exemptionNumber", "pinOfshop",
	"TraderSystemInvoiceNumber", "rfdRsnCd", "NetTotal", "EXCHANGERate",
	"Vat A(Exempt) net", "Vat A(Exempt) value",
	"Vat B(16.00%) net", "Vat B(16.00%) value",
	"Vat C(0%) net", "Vat C(0%) value",
	"Vat D(Non-VAT) net", "Vat D(Non-VAT) value",
	"Vat E(8%) net", "Vat E(8%) value",
	"Schg F(10.00%) net", "Schg F(10.00%) value",
	"Levy G(2.00%) net", "Levy G(2.00%) value",
}


def _line():
	return PLULine(
		item_name="Test Item", sale_price=Decimal("580.00"),
		sale_qty=Decimal("10"), sale_amount=Decimal("5800.00"),
	)


def _sign(**kw):
	return SignStructure(pin_of_shop="P051238105V", trader_invoice_number="1000000001", **kw)


class TestPayloadKeys(unittest.TestCase):
	def test_plu_keys_match_the_vendor_exactly(self):
		self.assertEqual(set(_line().as_payload()), VENDOR_PLU_KEYS)

	def test_sign_structure_keys_match_the_vendor_exactly(self):
		self.assertEqual(set(_sign().as_payload()), VENDOR_SIGN_KEYS)

	def test_disc_amt_is_sent(self):
		# Undocumented in the PDF table, present in every vendor example.
		self.assertEqual(_sign().as_payload()["DiscAmt"], "0.00")
		self.assertEqual(_sign(discount_amount=Decimal("12.3")).as_payload()["DiscAmt"], "12.30")

	def test_every_value_is_a_string(self):
		for payload in (_line().as_payload(), _sign().as_payload()):
			for key, value in payload.items():
				self.assertIsInstance(value, str, f"{key} must cross the wire as a string")

	def test_mandatory_vat_bands_default_to_zero_not_blank(self):
		payload = _sign().as_payload()
		for key in ("Vat A(Exempt) net", "Vat B(16.00%) net", "Vat C(0%) net", "Vat D(Non-VAT) net"):
			self.assertEqual(payload[key], "0.00", f"{key} is mandatory and must be 0, not blank")

	def test_optional_bands_stay_blank(self):
		payload = _sign().as_payload()
		for key in ("Schg F(10.00%) net", "Levy G(2.00%) net"):
			self.assertEqual(payload[key], "", f"{key} must stay blank when unused")

	def test_credit_note_shape(self):
		payload = _sign(
			invoice_type=INVOICE_CREDIT, relevant_invoice_number="0082729", refund_reason_code="06"
		).as_payload()
		self.assertEqual(payload["InvoiceType"], "credit")
		self.assertEqual(payload["relevantInvoiceNumber"], "0082729")
		self.assertEqual(payload["rfdRsnCd"], "06")


class TestDeviceTimestamp(unittest.TestCase):
	def test_parses_the_formats_the_device_returns(self):
		self.assertIsNotNone(parse_device_timestamp("2026-02-22 17:10:38"))
		self.assertIsNotNone(parse_device_timestamp("2026-02-22 17:09"))

	def test_unreadable_stamp_yields_none_rather_than_raising(self):
		# The caller falls back to the server clock; a bad stamp must not cost us
		# the rest of the reply.
		for bad in ("", None, "not a time", "99/99/9999"):
			self.assertIsNone(parse_device_timestamp(bad))


if __name__ == "__main__":
	unittest.main()
