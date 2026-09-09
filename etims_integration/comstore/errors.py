"""
Comstore / eTIMS error taxonomy.

The device answers a rejected payload with an ``E***`` code and a terse phrase.
Every code carries three facts we need, and none of them follow from each other:

``retryable``
        Re-sending the identical payload could succeed. True only for transport
        and device-state faults. A rejected payload is never retryable -- the
        device is deterministic, so a hundred retries produce a hundred
        identical rejections and a hundred rows of noise.
``blocking``
        A human has to change something before this invoice can go again. Parks
        the Transmission in ``Blocked`` rather than burning the retry budget.
``remedy``
        What that human has to change. Stored on the Transmission so whoever
        opens it does not need the vendor PDF to make sense of the failure.

Codes and their meanings are from Comstore API Documentation 3.4.2, "API Errors"
(pp. 33-42).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorSpec:
	code: str
	summary: str
	remedy: str
	retryable: bool = False
	blocking: bool = True


def _spec(code, summary, remedy, retryable=False, blocking=True):
	return ErrorSpec(code, summary, remedy, retryable, blocking)


ERROR_CODES = {
	"E034": _spec(
		"E034",
		"Input data error",
		"The payload carries characters eTIMS will not accept. Item names must be plain "
		"ASCII, 50 characters or fewer, with no backslashes or box-drawing marks.",
	),
	"E090": _spec(
		"E090",
		"Insufficient stock on device",
		"The device's stock for this item is depleted. Sync the item with a positive "
		"change_qty before resending.",
	),
	"E218": _spec(
		"E218",
		"Credit note tax exceeds the original",
		"The tax on this credit note is larger than the tax on the invoice it reverses. "
		"Check that the return lines match the original invoice.",
	),
	"E219": _spec(
		"E219",
		"Credit note price exceeds the original",
		"A unit price on this credit note is higher than on the original invoice.",
	),
	"E220": _spec(
		"E220",
		"Credit note quantity exceeds the original",
		"A quantity on this credit note is larger than the quantity invoiced. A partial "
		"return must not exceed what was sold.",
	),
	"E221": _spec(
		"E221",
		"Invoice already credited",
		"eTIMS has already accepted a credit note against this invoice. Confirm the "
		"relevant invoice number points at the invoice you mean to reverse.",
	),
	"E312": _spec("E312", "Invoice category is error", "InvoiceType must be exactly 'original' or 'credit'."),
	"E313": _spec(
		"E313",
		"Relevant invoice number is error",
		"The credit note quotes an invoice number eTIMS does not recognise. It must be "
		"the device's cu-inv-no from the original fiscal receipt, not the ERPNext name.",
	),
	"E314": _spec(
		"E314",
		"Receipt number is error",
		"The quoted receipt number is malformed or belongs to a different invoice.",
	),
	"E315": _spec("E315", "Invoice category is invalid", "InvoiceType must be exactly 'original' or 'credit'."),
	"E321": _spec(
		"E321",
		"Tax rate does not match the device",
		"The band sent for an item differs from the band registered against it on the "
		"device. Fix the eTIMS Tax Mapping or the item's tax type, resync the item, then "
		"resend.",
	),
	"E322": _spec(
		"E322",
		"Sale amount is error",
		"A line's SaleAmount does not agree with SalePrice x SaleQty less its discount.",
	),
	"E325": _spec(
		"E325",
		"pinOfshop is error",
		"The KRA PIN on the eTIMS Device is not the PIN assigned to that device, or is "
		"not in the 2-letter + 9-digit format.",
	),
	"E335": _spec(
		"E335",
		"Credit exceeds the original amount",
		"The credit note total is larger than the invoice it reverses.",
	),
	"E337": _spec(
		"E337",
		"No find PLU data",
		"The item is not registered on the device. Sync it, then resend the invoice.",
	),
	"E341": _spec(
		"E341",
		"PLU sales sum error",
		"The per-band net/tax totals do not add up to the line totals, or a band was "
		"filled for an item that does not belong to it. This is a payload defect - open "
		"the Transmission and compare plu_data against sign_structure.",
	),
	"E351": _spec(
		"E351",
		"Insufficient stock on device",
		"The device's stock for this item is depleted. Sync the item with a positive "
		"change_qty before resending.",
	),
	"E353": _spec(
		"E353",
		"Duplicate item name",
		"An item with this name already exists on the device. eTIMS treats plu_name and "
		"barcode as unique keys.",
	),
	"E358": _spec(
		"E358",
		"pinOfBuyer is error",
		"The customer's KRA PIN is malformed. It must be 2 letters followed by 9 digits, "
		"or be left empty.",
	),
}

# Later firmware reports four of the credit-note faults under a second code. They
# are the same fault, so they inherit their partner's wording rather than drifting
# from it.
for _alias, _original in (("E331", "E218"), ("E332", "E219"), ("E333", "E220"), ("E334", "E221")):
	_partner = ERROR_CODES[_original]
	ERROR_CODES[_alias] = _spec(_alias, _partner.summary, _partner.remedy)


# Faults that are worth another attempt: the payload was never judged, so the same
# bytes may well be accepted a moment later.
TRANSPORT = _spec(
	"TRANSPORT",
	"Device unreachable",
	"The Comstore service did not answer. Check that ComstoreApiService is running on "
	"the device host, that the host is reachable, and that no firewall blocks the port.",
	retryable=True,
	blocking=False,
)

DEVICE_DISCONNECTED = _spec(
	"DISCONNECTED",
	"Fiscal device disconnected",
	"The Comstore service is running but has lost its link to the fiscal device. Check "
	"the device is powered on and reachable at its configured IP.",
	retryable=True,
	blocking=False,
)

UNKNOWN = _spec(
	"UNKNOWN",
	"Unrecognised device error",
	"The device rejected the payload with a code this app does not know. Check "
	"ComstoreFC4Api.log on the device host.",
)


class ComstoreError(Exception):
	"""
	A call that did not produce a signature. Carries the classification so the
	caller decides retry-vs-park from the spec rather than by matching on strings.
	"""

	def __init__(self, spec, detail="", payload=None, response=None):
		self.spec = spec
		self.detail = detail or ""
		self.payload = payload
		self.response = response
		super().__init__(f"{spec.code}: {spec.summary}" + (f" - {self.detail}" if self.detail else ""))

	@property
	def retryable(self):
		return self.spec.retryable

	@property
	def blocking(self):
		return self.spec.blocking


# eTIMS codes appear in free text ("E337: NO FIND PLU DATA", "Error Code -1"), so
# the code has to be recovered from the message rather than read from a field.
def classify(message):
	"""
	Maps a device message onto its ErrorSpec. Falls back to UNKNOWN, which is
	non-retryable and blocking: an error we cannot read is not one we should
	silently hammer the device with.
	"""
	text = (message or "").upper()

	# A documented eTIMS code is a judgement on the payload and always wins: the
	# device rejected these bytes and will reject them again.
	for code, spec in ERROR_CODES.items():
		if code in text:
			return spec

	# No code, but the service is telling us it has no device to talk to. That is a
	# state fault, not a payload fault, so the identical payload may well succeed
	# once the link is back.
	if any(phrase in text for phrase in ("DEVICE NOT INITIALIZED", "NOT CONNECTED", "DEVICE DISCONNECTED")):
		return DEVICE_DISCONNECTED

	return UNKNOWN
