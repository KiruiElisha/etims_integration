"""
Sales Invoice -> Comstore payload.

Pure mapping: reads documents, writes nothing, performs no HTTP. That makes the
whole tax and rounding argument testable without a device, which matters because
this is the file where compliance is either correct or quietly wrong.

The one structural rule here is that **the line total is authoritative**. Every
band figure is derived by decomposing the same rounded ``SaleAmount`` that goes
into ``plu_data``, so ``sum(bands) == sum(lines)`` holds by construction rather
than by tolerance. That is what makes E341 ("PLU SALES SUM ERROR") and E322
("SaleAmount is error") structurally unreachable instead of merely unlikely.
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _

from etims_integration.comstore.schema import (
	INVOICE_CREDIT,
	INVOICE_ORIGINAL,
	VAT_BANDS,
	PLULine,
	SignStructure,
	money,
)
from etims_integration.mapping import credit as credit_encoding
from etims_integration.mapping import sanitize
from etims_integration.mapping.tax import UnresolvedBand, band_rate, describe, resolve_band

ADJUSTMENT_PRICE = "Price Adjustment"
ADJUSTMENT_RETURN = "Goods Return"

# Bands may disagree with the invoice by at most this much before a human is asked
# to look. One cent of rounding across many lines is expected; more is a mapping
# fault we must not declare to KRA unattended.
TOTAL_TOLERANCE = Decimal("0.05")

ZERO = Decimal("0")


class MappingError(frappe.ValidationError):
	pass


@dataclass
class MappedInvoice:
	lines: list = field(default_factory=list)
	sign_structure: SignStructure = None
	# Reasons a human should look before this is declared. Non-empty means the
	# payload is *not* sent unattended, however healthy the device is.
	concerns: list = field(default_factory=list)
	declared_total: Decimal = ZERO
	invoice_total: Decimal = ZERO
	band_summary: dict = field(default_factory=dict)

	@property
	def sendable(self):
		return not self.concerns

	def as_payload(self):
		return {
			"plu_data": [line.as_payload() for line in self.lines],
			"sign_structure": self.sign_structure.as_payload() if self.sign_structure else {},
		}


def _dec(value):
	return Decimal(str(value or 0))


def _round(value):
	return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decompose(gross, vat_rate, levy_rate=ZERO, surcharge_rate=ZERO):
	"""
	Split a VAT-inclusive line total into its net, VAT, levy and surcharge parts.

	Generalises the documentation's own worked examples: with no levy this is the
	familiar ``net = gross / 1.16``; with the 2% levy inside the gross it becomes
	``gross / 1.18``, with VAT and levy each struck on the *net*.

	Two properties have to hold at once, and the order below is what buys both:

	* ``net + vat + levy + surcharge == gross`` exactly, or eTIMS rejects the
	  invoice for a sum error (E341);
	* ``vat == round(net x rate)`` exactly, because VAT is the figure KRA checks
	  against the band the item is registered under (E321).

	They can conflict by a cent, so VAT is computed from the rate and the leftover
	cent is pushed into the levy or surcharge instead -- the smaller, ancillary
	figures. With neither of those present there is nothing to reconcile and VAT
	absorbs it. This reproduces every worked example in the vendor documentation,
	including ``100.00 -> 84.75 / 13.56 / 1.69``, which a naive residual-into-VAT
	split gets wrong by a cent.
	"""
	divisor = Decimal("1") + (vat_rate + levy_rate + surcharge_rate) / Decimal("100")
	net = _round(gross / divisor)
	vat = _round(net * vat_rate / Decimal("100"))
	levy = _round(net * levy_rate / Decimal("100"))
	surcharge = _round(net * surcharge_rate / Decimal("100"))

	residual = gross - (net + vat + levy + surcharge)
	if residual:
		if levy_rate:
			levy += residual
		elif surcharge_rate:
			surcharge += residual
		else:
			vat += residual

	return net, vat, levy, surcharge


def _item_rates(item_code):
	"""Optional per-item levy (band G) and special charge (band F)."""
	if not item_code:
		return ZERO, ZERO
	rates = frappe.get_cached_value(
		"Item", item_code, ["custom_etims_levy_rate", "custom_etims_surcharge_rate"], as_dict=True
	)
	if not rates:
		return ZERO, ZERO
	return _dec(rates.custom_etims_levy_rate), _dec(rates.custom_etims_surcharge_rate)


def build_lines(doc, allowance=None):
	"""
	Sales Invoice Items -> PLULine, one per row, amounts already decomposed.

	Quantities and amounts are taken absolute: a credit note is identified to the
	device by ``InvoiceType: credit``, not by sign, and negative figures are
	rejected outright.

	``allowance`` is the per-line headroom from :mod:`services.allowance`, supplied
	only for a price adjustment. When present, each line is re-encoded at the
	*original* unit price with a fractional quantity, which is what lets an invoice
	be adjusted repeatedly -- see :mod:`etims_integration.mapping.credit`.
	"""
	lines = []
	unresolved = []
	overdrawn = []

	is_adjustment = allowance is not None

	for row in doc.items:
		try:
			band = resolve_band(row.item_code, row.item_tax_template, row.item_name)
		except UnresolvedBand:
			unresolved.append(row)
			continue

		vat_rate = band_rate(band)
		levy_rate, surcharge_rate = _item_rates(row.item_code)

		quantity = abs(_dec(row.qty)) or Decimal("1")
		net_amount = abs(_dec(row.base_net_amount))
		gross = _round(net_amount * (Decimal("1") + (vat_rate + levy_rate + surcharge_rate) / Decimal("100")))

		item_name = sanitize.item_name(row.item_name or row.item_code, fallback=row.item_code)
		barcode = _barcode(row.item_code)
		discount = ZERO

		if is_adjustment:
			encoded = _encode_adjustment(gross, item_name, barcode, allowance)
			if encoded is None:
				overdrawn.append((row, gross))
				continue
			unit_price, quantity, discount = encoded
			sale_amount = gross
		else:
			# The unit price is derived from the line total and then the line total
			# is re-derived from the unit price, so SalePrice x SaleQty ==
			# SaleAmount is an identity rather than a hope. Any sub-cent remainder
			# is absorbed here, once, instead of accumulating across the bands.
			unit_price = _round(gross / quantity)
			sale_amount = _round(unit_price * quantity)

		net, vat, levy, surcharge = _decompose(sale_amount, vat_rate, levy_rate, surcharge_rate)

		lines.append(
			PLULine(
				item_name=item_name,
				barcode=barcode,
				sale_price=unit_price,
				sale_qty=quantity,
				sale_amount=sale_amount,
				discount_amount=discount,
				surcharge=surcharge,
				levy=levy,
				band=band,
				net_amount=net,
				tax_amount=vat,
			)
		)

	return lines, unresolved, overdrawn


def _encode_adjustment(gross, item_name, barcode, allowance):
	"""
	Re-encode one adjustment line at the original unit price.

	Returns ``(unit_price, quantity, discount)``, or ``None`` when the line has no
	matching original or not enough headroom left -- the caller turns that into a
	concern rather than letting the device reject it after the fact.
	"""
	entry = allowance.get(barcode) or allowance.get(item_name)
	if not entry or entry["unit_price"] <= ZERO:
		return None

	try:
		credit_encoding.check_fits(
			gross,
			entry["unit_price"],
			entry["remaining_qty"],
			entry["remaining_amount"],
		)
	except credit_encoding.AdjustmentTooLarge:
		return None

	return credit_encoding.encode(gross, entry["unit_price"])


def _barcode(item_code):
	"""
	The device keys items on barcode. Prefer the item's own registered PLU number
	so an invoice line points at the same device record the item sync created.
	"""
	if not item_code:
		return ""
	return frappe.get_cached_value("Item", item_code, "custom_etims_plu_no") or ""


def summarise_bands(lines):
	"""Per-band (net, tax) totals, summed from the very lines being sent."""
	bands = {band: [ZERO, ZERO] for band in VAT_BANDS}
	levy_net = levy_value = ZERO
	surcharge_net = surcharge_value = ZERO

	for line in lines:
		bucket = bands.setdefault(line.band, [ZERO, ZERO])
		bucket[0] += line.net_amount
		bucket[1] += line.tax_amount
		if line.levy:
			levy_net += line.net_amount
			levy_value += line.levy
		if line.surcharge:
			surcharge_net += line.net_amount
			surcharge_value += line.surcharge

	return (
		{band: (values[0], values[1]) for band, values in bands.items()},
		(levy_net, levy_value) if levy_value else None,
		(surcharge_net, surcharge_value) if surcharge_value else None,
	)


# Mode of Payment "type" -> the sign_structure bucket it belongs in. The device
# only knows three; anything not cash or a cheque is settled as card.
def split_payments(doc, total, default_bucket="cash"):
	"""
	Allocate the invoice total across CashAmt / CheckAmt / CardAmt.

	The three must add up to the declared total, so POS payment rows are used when
	present and the whole amount otherwise falls into the configured default. Any
	rounding remainder is pushed into the largest bucket rather than left to make
	the totals disagree.
	"""
	buckets = {"cash": ZERO, "check": ZERO, "card": ZERO}

	rows = [row for row in (doc.get("payments") or []) if _dec(row.amount)]
	if rows:
		for row in rows:
			mode_type = (frappe.get_cached_value("Mode of Payment", row.mode_of_payment, "type") or "").lower()
			name = (row.mode_of_payment or "").lower()
			if mode_type == "cash" or "cash" in name:
				key = "cash"
			elif "cheque" in name or "check" in name:
				key = "check"
			else:
				key = "card"
			buckets[key] += abs(_dec(row.amount))
	else:
		buckets[default_bucket if default_bucket in buckets else "cash"] = total

	allocated = sum(buckets.values())
	if allocated != total:
		# Reconcile to the declared total. The device checks the sum, and an
		# invoice partially paid at the till is still fiscalised in full.
		key = max(buckets, key=lambda k: buckets[k]) if allocated else default_bucket
		buckets[key] += total - allocated

	return buckets


def original_invoice_of(doc):
	"""
	The invoice this credit note adjusts.

	``custom_etims_original_invoice`` is authoritative and is the *only* field set
	on a financial-only price adjustment. Such a credit note deliberately leaves
	``return_against`` blank: ERPNext counts a return against the original's
	quantity budget, which would stop a second price adjustment ever being raised,
	and no goods are moving anyway.
	"""
	return doc.get("custom_etims_original_invoice") or doc.get("return_against")


def relevant_invoice_number(doc):
	"""
	A credit note must quote the *device's* receipt number for the invoice it
	adjusts -- ``cu-inv-no`` -- which lives on that invoice's signed Transmission,
	not on the credit note. Quoting the ERPNext name earns E313.
	"""
	original = original_invoice_of(doc)
	if not original:
		frappe.throw(
			_("Credit note {0} does not say which invoice it adjusts. Set 'eTIMS Original Invoice' (or 'Return Against' for a goods return).").format(doc.name),
			exc=MappingError,
		)

	number = frappe.db.get_value("Sales Invoice", original, "custom_etims_cu_invoice_no")
	if not number:
		number = frappe.db.get_value(
			"ETIMS Transmission",
			{"sales_invoice": original, "status": "Signed"},
			"cu_invoice_no",
			order_by="creation desc",
		)

	if not number:
		frappe.throw(
			_("Invoice {0} has no eTIMS receipt number recorded, so a credit note against it cannot be sent. Fiscalise the original invoice first.").format(original),
			exc=MappingError,
		)

	return number


def _is_price_adjustment(doc):
	"""
	A credit note that moves money without moving goods.

	Encoded differently from a goods return -- at the original unit price with a
	fractional quantity -- so that an invoice can be adjusted as many times as its
	value allows rather than once. See :mod:`etims_integration.mapping.credit`.
	"""
	return bool(doc.get("is_return")) and doc.get("custom_etims_adjustment_type") == ADJUSTMENT_PRICE


def build(doc, device, settings, trader_invoice_number):
	"""
	Assemble the full payload for one Sales Invoice.

	Returns a :class:`MappedInvoice`. Anything that would have KRA told a
	different story from the invoice lands in ``concerns`` rather than raising:
	the caller records it against the Transmission so the operator sees precisely
	what stopped the send.
	"""
	concerns = []

	# A price adjustment is re-encoded against what the original invoice has left,
	# so the headroom has to be loaded before the lines are built.
	allowance = None
	is_adjustment = _is_price_adjustment(doc)
	if is_adjustment:
		from etims_integration.services import allowance as allowance_service

		allowance = allowance_service.remaining(original_invoice_of(doc))
		if not allowance:
			concerns.append(
				_("{0} has no signed eTIMS transmission, so there is nothing to adjust against it.").format(
					original_invoice_of(doc)
				)
			)

	lines, unresolved, overdrawn = build_lines(doc, allowance=allowance)

	if overdrawn:
		listed = ", ".join(
			_("{0} (needs {1})").format(row.item_name or row.item_code, amount) for row, amount in overdrawn[:10]
		)
		concerns.append(
			_("These lines exceed what {0} has left to be credited at eTIMS, or have no matching line on it: {1}. Check the remaining allowance on the original invoice.").format(
				original_invoice_of(doc), listed
			)
		)

	if unresolved:
		listed = ", ".join(f"{row.item_code} ({row.item_name})" for row in unresolved[:10])
		concerns.append(
			_("No eTIMS tax band is configured for: {0}. Set the eTIMS tax type on the item, or map its Item Tax Template in eTIMS Settings.").format(listed)
		)

	if not lines:
		concerns.append(_("The invoice has no sendable lines."))
		return MappedInvoice(concerns=concerns)

	bands, levy, surcharge = summarise_bands(lines)
	declared_total = sum(line.sale_amount for line in lines)
	invoice_total = _round(abs(_dec(doc.base_grand_total)))

	if abs(declared_total - invoice_total) > TOTAL_TOLERANCE:
		concerns.append(
			_("The invoice total is {0} but {1} would be declared to KRA. Check the tax bands against the invoice's tax rows before sending.").format(
				invoice_total, declared_total
			)
		)

	is_return = bool(doc.get("is_return"))
	buyer_pin = sanitize.clean_pin(doc.get("tax_id") or frappe.db.get_value("Customer", doc.customer, "tax_id"))
	if doc.get("tax_id") and not buyer_pin:
		concerns.append(
			_("The customer's KRA PIN {0} is not a valid PIN. Correct it or clear it -- eTIMS rejects a malformed buyer PIN (E358).").format(doc.get("tax_id"))
		)

	refund_reason = ""
	if is_return:
		refund_reason = (doc.get("custom_etims_refund_reason") or "").split("-")[0].strip()
		if not refund_reason:
			concerns.append(_("Select an eTIMS refund reason on this credit note before sending."))

	payments = split_payments(doc, declared_total, (settings.default_payment_bucket or "cash").lower())

	sign_structure = SignStructure(
		pin_of_shop=sanitize.clean_pin(device.pin_of_shop),
		trader_invoice_number=str(trader_invoice_number),
		is_live=not device.is_test_mode,
		invoice_type=INVOICE_CREDIT if is_return else INVOICE_ORIGINAL,
		cash_amount=payments["cash"],
		check_amount=payments["check"],
		card_amount=payments["card"],
		pin_of_buyer=buyer_pin,
		exemption_number=doc.get("custom_etims_exemption_number") or "",
		relevant_invoice_number=relevant_invoice_number(doc) if is_return else "",
		refund_reason_code=refund_reason,
		exchange_rate=_dec(doc.conversion_rate) if doc.currency != doc.company_currency else None,
		bands=bands,
		levy=levy,
		surcharge=surcharge,
	)

	if not sign_structure.pin_of_shop:
		concerns.append(
			_("eTIMS Device {0} has no valid KRA PIN. Set the business PIN in the 2-letter + 9-digit format (E325).").format(device.name)
		)

	return MappedInvoice(
		lines=lines,
		sign_structure=sign_structure,
		concerns=concerns,
		declared_total=money(declared_total),
		invoice_total=money(invoice_total),
		band_summary={
			describe(band): {"net": money(net), "tax": money(tax)}
			for band, (net, tax) in bands.items()
			if net or tax
		},
	)
