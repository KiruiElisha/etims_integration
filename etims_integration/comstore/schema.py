"""
Wire shapes for the Comstore API.

The device's JSON keys are awkward -- ``"Vat B(16.00%) net"``, ``"ItemDisCount(%)"``,
``"cu-inv-no"`` -- and every amount crosses the wire as a *string*. Both facts are
confined to this module: the rest of the app works with the dataclasses below and
with ``Decimal``, and only ``as_payload()`` knows what the device actually wants.

Amounts are ``Decimal`` everywhere. eTIMS rejects an invoice whose bands do not sum
to its lines exactly (E341), and binary floats cannot promise that.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

TWO_PLACES = Decimal("0.01")

# eTIMS VAT bands. The letter is the device's own label; the rate is what the
# band charges. Bands F and G are the special charge and the levy -- they sit in
# the same structure but are not VAT.
BAND_A_EXEMPT = "A"
BAND_B_STANDARD = "B"
BAND_C_ZERO = "C"
BAND_D_NON_VAT = "D"
BAND_E_TOURISM = "E"

VAT_BANDS = (BAND_A_EXEMPT, BAND_B_STANDARD, BAND_C_ZERO, BAND_D_NON_VAT, BAND_E_TOURISM)

BAND_RATES = {
	BAND_A_EXEMPT: Decimal("0"),
	BAND_B_STANDARD: Decimal("16"),
	BAND_C_ZERO: Decimal("0"),
	BAND_D_NON_VAT: Decimal("0"),
	BAND_E_TOURISM: Decimal("8"),
}

BAND_LABELS = {
	BAND_A_EXEMPT: "A-Exempt",
	BAND_B_STANDARD: "B-16.00%",
	BAND_C_ZERO: "C-0%",
	BAND_D_NON_VAT: "D-Non-VAT",
	BAND_E_TOURISM: "E-8%",
}

# The sign_structure key each band's net/value pair travels under. These strings
# are load-bearing: the device matches on them literally.
BAND_KEYS = {
	BAND_A_EXEMPT: ("Vat A(Exempt) net", "Vat A(Exempt) value"),
	BAND_B_STANDARD: ("Vat B(16.00%) net", "Vat B(16.00%) value"),
	BAND_C_ZERO: ("Vat C(0%) net", "Vat C(0%) value"),
	BAND_D_NON_VAT: ("Vat D(Non-VAT) net", "Vat D(Non-VAT) value"),
	BAND_E_TOURISM: ("Vat E(8%) net", "Vat E(8%) value"),
}

SURCHARGE_KEYS = ("Schg F(10.00%) net", "Schg F(10.00%) value")
LEVY_KEYS = ("Levy G(2.00%) net", "Levy G(2.00%) value")

INVOICE_ORIGINAL = "original"
INVOICE_CREDIT = "credit"

# Refund reason codes (doc table 6). Required on every credit note.
REFUND_REASONS = {
	"01": "Missing Quantity",
	"02": "Missing Data",
	"03": "Damaged/Wasted",
	"04": "Raw Material",
	"05": "Shortage",
	"06": "Refund",
}


def money(value):
	"""Quantise to the 2 decimal places the device works in, half-up like KRA."""
	if value is None:
		value = 0
	if not isinstance(value, Decimal):
		value = Decimal(str(value))
	return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def qty(value, places="0.000"):
	if value is None:
		value = 0
	if not isinstance(value, Decimal):
		value = Decimal(str(value))
	return value.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def _s(value):
	"""Every numeric field on this API is a string. Normalise once, here."""
	return format(value, "f") if isinstance(value, Decimal) else str(value)


@dataclass
class PLULine:
	"""One sale line as the device wants it.

	``sale_price`` is the *undiscounted* unit price and ``sale_amount`` is the
	line total *after* discount -- the doc is explicit that these two disagree
	whenever a discount applies, and sending the discounted figure in both is
	what produces E322.
	"""

	item_name: str
	sale_price: Decimal
	sale_qty: Decimal
	sale_amount: Decimal
	barcode: str = ""
	discount_percent: Decimal = Decimal("0")
	discount_amount: Decimal = Decimal("0")
	surcharge: Decimal = Decimal("0")
	levy: Decimal = Decimal("0")
	# Not sent. Carried so the band totals can be derived from the same list of
	# lines that becomes plu_data, which is what keeps the two sides of E341 equal.
	band: str = BAND_B_STANDARD
	tax_amount: Decimal = Decimal("0")
	net_amount: Decimal = Decimal("0")

	def as_payload(self):
		return {
			"item_Name": self.item_name,
			"Barcode": self.barcode or "",
			"SalePrice": _s(money(self.sale_price)),
			"SaleQty": _s(qty(self.sale_qty)),
			"SaleAmount": _s(money(self.sale_amount)),
			"ItemDisCount(%)": _s(money(self.discount_percent)),
			"ItemDisCount": _s(money(self.discount_amount)),
			"Schg": _s(money(self.surcharge)),
			"Levy": _s(money(self.levy)),
		}


@dataclass
class SignStructure:
	"""The tax and payment declaration that accompanies the lines."""

	pin_of_shop: str
	trader_invoice_number: str
	is_live: bool = True
	invoice_type: str = INVOICE_ORIGINAL
	cash_amount: Decimal = Decimal("0")
	card_amount: Decimal = Decimal("0")
	check_amount: Decimal = Decimal("0")
	# Total discount across the lines. Absent from the PDF's parameter table but
	# present in both of the vendor's own Postman examples, so it is sent: an
	# undocumented field the vendor always sends is likelier to be expected than
	# ignored.
	discount_amount: Decimal = Decimal("0")
	pin_of_buyer: str = ""
	exemption_number: str = ""
	relevant_invoice_number: str = ""
	refund_reason_code: str = ""
	exchange_rate: Decimal | None = None
	net_total: Decimal | None = None
	# band -> (net, tax)
	bands: dict = field(default_factory=dict)
	surcharge: tuple | None = None
	levy: tuple | None = None

	def as_payload(self):
		payload = {
			"SignType": "1" if self.is_live else "0",
			"CashAmt": _s(money(self.cash_amount)),
			"DiscAmt": _s(money(self.discount_amount)),
			"CheckAmt": _s(money(self.check_amount)),
			"CardAmt": _s(money(self.card_amount)),
			"InvoiceType": self.invoice_type,
			"relevantInvoiceNumber": self.relevant_invoice_number or "",
			"pinOfBuyer": self.pin_of_buyer or "",
			"exemptionNumber": self.exemption_number or "",
			"pinOfshop": self.pin_of_shop,
			"TraderSystemInvoiceNumber": self.trader_invoice_number,
			"rfdRsnCd": self.refund_reason_code or "",
			"NetTotal": _s(money(self.net_total)) if self.net_total is not None else "",
			"EXCHANGERate": _s(money(self.exchange_rate)) if self.exchange_rate is not None else "",
		}

		# Every VAT band is mandatory and defaults to "0" -- omitting one is not the
		# same as zeroing it. Bands F and G are optional and stay empty unless used,
		# because a "0" there is itself enough to trip E341 on some firmware.
		for band, (net_key, value_key) in BAND_KEYS.items():
			net, tax = self.bands.get(band, (Decimal("0"), Decimal("0")))
			payload[net_key] = _s(money(net))
			payload[value_key] = _s(money(tax))

		for keys, pair in ((SURCHARGE_KEYS, self.surcharge), (LEVY_KEYS, self.levy)):
			net_key, value_key = keys
			if pair:
				payload[net_key], payload[value_key] = _s(money(pair[0])), _s(money(pair[1]))
			else:
				payload[net_key] = payload[value_key] = ""

		return payload


# The device stamps its own time and the exact shape varies by firmware. Parsing
# is best-effort by design: an unreadable stamp must never cost us the rest of the
# reply, so every caller falls back to the server clock rather than failing.
DEVICE_TIME_FORMATS = (
	"%Y-%m-%d %H:%M:%S",
	"%Y-%m-%d %H:%M",
	"%Y-%m-%dT%H:%M:%S",
	"%Y/%m/%d %H:%M:%S",
	"%d/%m/%Y %H:%M:%S",
	"%d/%m/%Y %H:%M",
	"%d-%m-%Y %H:%M:%S",
	"%Y%m%d%H%M%S",
	"%Y-%m-%d",
	"%d/%m/%Y",
)


def parse_device_timestamp(value):
	"""Device time string -> datetime, or None if no known format matches."""
	if not value:
		return None
	if isinstance(value, datetime):
		return value

	text = str(value).strip()
	for fmt in DEVICE_TIME_FORMATS:
		try:
			return datetime.strptime(text, fmt)
		except ValueError:
			continue
	return None


@dataclass
class WorkflowResult:
	"""The signature the device returns once KRA has accepted an invoice."""

	signature: str = ""
	serial_number: str = ""
	invoice_number: str = ""
	cu_invoice_number: str = ""
	scu_id: str = ""
	internal_data: str = ""
	receipt_signature: str = ""
	signature_link: str = ""
	version: str = ""
	timestamp: str = ""
	message: str = ""
	raw: dict = field(default_factory=dict)

	def signed_at(self):
		"""
		When the *device* says it signed, not when we happened to process the
		reply. On a queue that retries, those can be minutes apart, and the fiscal
		record should carry the device's account of it.
		"""
		return parse_device_timestamp(self.timestamp)

	@classmethod
	def from_response(cls, data):
		return cls(
			signature=data.get("signature") or "",
			serial_number=data.get("serial_number") or "",
			invoice_number=data.get("invoice_number") or "",
			# Hyphenated keys, exactly as the device spells them.
			cu_invoice_number=data.get("cu-inv-no") or "",
			scu_id=data.get("scu_id") or "",
			internal_data=data.get("internal-data") or "",
			receipt_signature=data.get("Receipt Signature") or "",
			signature_link=data.get("signature_link") or "",
			version=data.get("version") or "",
			timestamp=data.get("timestamp") or "",
			message=data.get("message") or "",
			raw=data,
		)


@dataclass
class PLUItem:
	"""An item as it is registered on the device."""

	plu_no: str
	plu_name: str
	unit_price: Decimal
	item_class_code: str
	package_unit: str
	quantity_unit: str
	origin_country: str
	tax_type: str
	product_type: str
	barcode: str = ""
	batch_no: str = ""
	additional_info: str = ""
	safety_qty: Decimal = Decimal("0")
	insurance_applicable: bool = False
	change_qty: Decimal = Decimal("0")
	stocks: Decimal = Decimal("0")
	active: bool = True

	def as_payload(self):
		return {
			"plu_no": str(self.plu_no),
			# The doc directs using plu_no as the barcode when an item has none;
			# barcode is a unique key on the device, so it cannot be left blank.
			"barcode": str(self.barcode or self.plu_no),
			"plu_name": self.plu_name,
			"unit_price": _s(money(self.unit_price)),
			"item_cls_code": self.item_class_code,
			"pkg_unit_cd": self.package_unit,
			"qty_unit_cd": self.quantity_unit,
			"orgn_nat_cd": self.origin_country,
			"btch_no": self.batch_no or "",
			"add_info": self.additional_info or "",
			"tax_type": self.tax_type,
			"sfty_qty": _s(qty(self.safety_qty, "0.00")),
			"type_code": self.product_type,
			"isrc_aplcb_yn": "1" if self.insurance_applicable else "0",
			"change_qty": _s(qty(self.change_qty, "0.00")),
			"stocks": _s(qty(self.stocks, "0.00")),
			"use_yor_n": "1" if self.active else "0",
		}


@dataclass
class Buyer:
	buyer_no: str
	pin: str
	name: str
	mobile: str = ""
	address: str = ""
	email: str = ""
	fax: str = ""

	def as_payload(self):
		return {
			"BuyerNo": str(self.buyer_no),
			"BuyerPin": self.pin,
			"BuyerName": self.name,
			"BuyerMobileNo": self.mobile or "",
			"Address": self.address or "",
			"Email": self.email or "",
			"FaxN": self.fax or "",
		}
