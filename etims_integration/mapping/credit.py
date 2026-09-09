"""
Encoding a price adjustment as an eTIMS credit note.

eTIMS has no concept of "the price fell". A credit note carries only a unit price
and a quantity, and the device validates both against the invoice being credited:

* **E219** -- the credit's unit price must not exceed the original's;
* **E220** -- credited quantity, *accumulated across every credit note*, must not
  exceed the quantity originally invoiced;
* **E335** -- credited value must not exceed the original value.

That makes quantity a finite budget per invoice, and how a price adjustment is
encoded decides how fast it is spent. For a credit of value ``V`` against an
original unit price ``P``, the quantity consumed is ``V / p`` for whatever unit
price ``p`` we choose, and E219 caps ``p`` at ``P``. So quantity consumption is
*minimised* by charging the full original unit price and moving the adjustment
into a fractional quantity.

The consequence is worth stating plainly, because it is the whole point:

    Encoding at the original unit price makes quantity stop being the binding
    constraint. Total quantity consumed is ``sum(V) / P``, and since ``sum(V)``
    can never exceed the original invoice value ``Q x P``, the budget cannot run
    out before the value budget does. Price adjustments become unlimited in
    number, bounded only by not crediting more than was invoiced.

The opposite encoding -- full original quantity at a reduced unit price -- spends
the entire quantity budget on the first adjustment and locks out every later one.
It reads more like "all N units are now worth less", but eTIMS cannot express
that anyway, and it also tells the buyer's records that the whole consignment
came back.

A worked comparison, 100 units at 580 gross (58,000), adjusted by 5,800:

===========================  ============  ==============  ==================
Encoding                     Unit price    Qty consumed    Adjustments left
===========================  ============  ==============  ==================
Full qty, reduced price      58            100 of 100      none
Original price, frac. qty    580           10 of 100       value permitting
===========================  ============  ==============  ==================
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

# The device's quantity precision. The vendor examples show three decimals
# ("1000.000"), and the residual below is what keeps a rounded quantity honest.
QTY_PLACES = Decimal("0.001")
MONEY_PLACES = Decimal("0.01")

ZERO = Decimal("0")


class AdjustmentTooLarge(Exception):
	"""The adjustment does not fit in what the original invoice has left."""

	def __init__(self, requested, available, dimension):
		self.requested = requested
		self.available = available
		self.dimension = dimension
		super().__init__(f"{dimension}: need {requested}, {available} remaining")


def encode(target_value, original_unit_price, qty_places=QTY_PLACES):
	"""
	Express a credit of ``target_value`` in the (price, quantity, discount) triple
	the device wants, consuming as little quantity as eTIMS permits.

	Returns ``(unit_price, quantity, discount)`` such that::

	    unit_price * quantity - discount == target_value      (exactly, to the cent)

	which is the identity E322 checks. The unit price is the original's, so E219
	passes with no margin needed.

	The quantity is rounded *up* to the device's precision and the small excess is
	pushed into the discount field. Rounding down instead would credit fractionally
	less than the accounting entry, and a discount cannot be negative -- so up is
	the only direction that keeps the two sides equal without inventing value.
	"""
	target_value = _money(target_value)
	original_unit_price = _money(original_unit_price)

	if target_value <= ZERO:
		return original_unit_price, ZERO, ZERO

	if original_unit_price <= ZERO:
		raise ValueError("Cannot encode a price adjustment against a zero original unit price.")

	quantity = (target_value / original_unit_price).quantize(qty_places, rounding=ROUND_CEILING)
	gross = _money(original_unit_price * quantity)
	discount = gross - target_value

	return original_unit_price, quantity, discount


def quantity_needed(target_value, original_unit_price, qty_places=QTY_PLACES):
	"""How much of the quantity budget an adjustment of this value would spend."""
	return encode(target_value, original_unit_price, qty_places)[1]


def check_fits(target_value, original_unit_price, remaining_qty, remaining_value, qty_places=QTY_PLACES):
	"""
	Raise unless the adjustment fits in what the original invoice has left.

	Checked here rather than left to the device because an eTIMS rejection arrives
	after the credit note is already submitted in ERPNext, leaving the customer's
	account adjusted and KRA's records not.
	"""
	target_value = _money(target_value)
	if target_value > _money(remaining_value):
		raise AdjustmentTooLarge(target_value, _money(remaining_value), "value")

	needed = quantity_needed(target_value, original_unit_price, qty_places)
	if needed > Decimal(str(remaining_qty)):
		raise AdjustmentTooLarge(needed, Decimal(str(remaining_qty)), "quantity")

	return needed


def _money(value):
	if not isinstance(value, Decimal):
		value = Decimal(str(value or 0))
	return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)
