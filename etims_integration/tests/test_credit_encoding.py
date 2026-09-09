"""
Price-adjustment encoding.

The property that matters is not any single adjustment, it is that an invoice can
be adjusted *repeatedly*. Prices fluctuate, so a distributor issues price support
against the same invoice again and again, and an encoding that spends the whole
eTIMS quantity budget on the first one is useless in practice.
"""

import unittest
from decimal import Decimal

from etims_integration.mapping.credit import (
	AdjustmentTooLarge,
	check_fits,
	encode,
	quantity_needed,
)

# 100 units at 580 gross = 58,000.
UNIT = Decimal("580.00")
QTY = Decimal("100")
VALUE = Decimal("58000.00")


class TestEncoding(unittest.TestCase):
	def test_identity_e322_checks_always_holds(self):
		"""SalePrice x SaleQty - ItemDisCount == SaleAmount, exactly."""
		for target in ("5800.00", "3000.00", "0.01", "1234.56", "57999.99", "7.77", "19.99"):
			target = Decimal(target)
			price, qty, discount = encode(target, UNIT)
			self.assertEqual(
				(price * qty).quantize(Decimal("0.01")) - discount,
				target,
				f"encoding of {target} does not re-add",
			)

	def test_unit_price_never_exceeds_the_original(self):
		# E219: the credit's unit price must not be above the original's.
		price, _qty, _d = encode(Decimal("3000.00"), UNIT)
		self.assertEqual(price, UNIT)
		self.assertLessEqual(price, UNIT)

	def test_discount_is_never_negative(self):
		# A negative discount would invent value the invoice never carried.
		for target in ("0.01", "3000.00", "5800.00", "12345.67"):
			_p, _q, discount = encode(Decimal(target), UNIT)
			self.assertGreaterEqual(discount, Decimal("0"))

	def test_exact_multiple_needs_no_discount(self):
		price, qty, discount = encode(Decimal("5800.00"), UNIT)
		self.assertEqual(qty, Decimal("10.000"))
		self.assertEqual(discount, Decimal("0.00"))
		self.assertEqual(price, UNIT)

	def test_awkward_value_absorbs_into_the_discount(self):
		# 3000 / 580 = 5.1724..., which the device's 3dp quantity cannot express.
		price, qty, discount = encode(Decimal("3000.00"), UNIT)
		self.assertEqual(qty, Decimal("5.173"))
		self.assertEqual(price * qty, Decimal("3000.340"))
		self.assertEqual(discount, Decimal("0.34"))

	def test_zero_adjustment_consumes_nothing(self):
		_p, qty, discount = encode(Decimal("0"), UNIT)
		self.assertEqual(qty, Decimal("0"))
		self.assertEqual(discount, Decimal("0"))


class TestRepeatedAdjustments(unittest.TestCase):
	"""
	The reason the encoding is what it is: quantity must not be the binding
	constraint, or the second price adjustment is impossible.
	"""

	def test_many_adjustments_fit_where_one_full_qty_credit_would_not(self):
		remaining_qty, remaining_value = QTY, VALUE
		adjustments = [Decimal("2900.00")] * 15  # fifteen separate price drops

		for i, value in enumerate(adjustments, 1):
			needed = check_fits(value, UNIT, remaining_qty, remaining_value)
			remaining_qty -= needed
			remaining_value -= value
			self.assertGreaterEqual(remaining_qty, 0, f"quantity exhausted at adjustment {i}")

		# 15 x 2,900 = 43,500 of 58,000, and quantity tracks value proportionally.
		self.assertEqual(remaining_value, Decimal("14500.00"))
		self.assertEqual(remaining_qty, Decimal("25.000"))

	def test_quantity_budget_outlasts_the_value_budget(self):
		"""
		Encoding at the original unit price means quantity can never run out first.
		Spend the entire invoice value in small adjustments and quantity still lands
		at zero, not below.
		"""
		remaining_qty, remaining_value = QTY, VALUE
		while remaining_value >= Decimal("580.00"):
			needed = check_fits(Decimal("580.00"), UNIT, remaining_qty, remaining_value)
			remaining_qty -= needed
			remaining_value -= Decimal("580.00")

		self.assertEqual(remaining_value, Decimal("0.00"))
		self.assertEqual(remaining_qty, Decimal("0.000"))

	def test_full_quantity_encoding_would_exhaust_in_one(self):
		"""
		The encoding we rejected, stated as a test so the reason stays on record:
		crediting the full quantity at a reduced price leaves nothing for a second
		adjustment.
		"""
		differential_price = Decimal("58.00")  # 5,800 spread over all 100 units
		qty_consumed = Decimal("5800.00") / differential_price
		self.assertEqual(qty_consumed, QTY)
		self.assertEqual(QTY - qty_consumed, Decimal("0"))

	def test_value_overdraft_is_refused(self):
		with self.assertRaises(AdjustmentTooLarge) as ctx:
			check_fits(Decimal("60000.00"), UNIT, QTY, VALUE)
		self.assertEqual(ctx.exception.dimension, "value")

	def test_quantity_overdraft_is_refused(self):
		# Value available but quantity already spent by an earlier goods return.
		with self.assertRaises(AdjustmentTooLarge) as ctx:
			check_fits(Decimal("5800.00"), UNIT, Decimal("2"), VALUE)
		self.assertEqual(ctx.exception.dimension, "quantity")


class TestQuantityMinimality(unittest.TestCase):
	def test_original_price_minimises_quantity_consumed(self):
		"""
		For a fixed credit value, quantity consumed is value/price, so the highest
		price E219 permits -- the original -- is always the cheapest encoding.
		"""
		value = Decimal("5800.00")
		at_original = quantity_needed(value, UNIT)
		for reduced in ("290.00", "116.00", "58.00"):
			self.assertLess(
				at_original,
				quantity_needed(value, Decimal(reduced)),
				f"encoding at {reduced} should consume more quantity than at {UNIT}",
			)

	def test_zero_original_price_is_rejected_not_guessed(self):
		with self.assertRaises(ValueError):
			encode(Decimal("100.00"), Decimal("0"))


if __name__ == "__main__":
	unittest.main()
