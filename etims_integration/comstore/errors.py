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

# Faults that happen *in front of* the device. Naming these separately matters:
# the generic "unrecognised device error" sends an operator to
# ComstoreFC4Api.log, and in every case below the device never saw the request
# and its log will be silent.

ORIGIN_UNREACHABLE = _spec(
	"ORIGIN_DOWN",
	"Device host unreachable behind its proxy",
	"A CDN or reverse proxy answered, but it could not reach the machine running "
	"ComstoreApiService (Cloudflare 52x/530 means the tunnel or origin is down). "
	"Check that the service and any tunnel client are running on the device host.",
	retryable=True,
	blocking=False,
)

EDGE_CHALLENGE = _spec(
	"EDGE_CHALLENGE",
	"Blocked by a bot challenge in front of the device",
	"A CDN (Cloudflare) is serving a browser challenge instead of passing the "
	"request to the device. An API client cannot answer a challenge that requires "
	"a browser. Ask whoever hosts the endpoint to exempt the API path from bot "
	"protection, or to allow this server's IP - or connect to the device directly "
	"on the LAN rather than through the proxy.",
	retryable=False,
	blocking=True,
)

NOT_THE_API = _spec(
	"NOT_API",
	"Endpoint did not return JSON",
	"Something other than the Comstore service answered - an HTML page came back "
	"where JSON was expected. Check the host and port point at ComstoreApiService "
	"and not at a web server, login page or proxy.",
	retryable=False,
	blocking=True,
)

AUTH_REJECTED = _spec(
	"AUTH_REJECTED",
	"API key rejected",
	"ComstoreApiService answered, but refused the X-API-KEY header. Check the API "
	"Key on the eTIMS Device against the one issued for this host. Note that a "
	"green health check proves nothing here: /api/health is unauthenticated and "
	"answers 200 to a wrong key, or to no key at all.",
	retryable=False,
	blocking=True,
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


# Markers that identify a CDN challenge page. Matched against the body and the
# response headers, because the status code alone does not distinguish "the
# device said no" from "the device was never asked".
_CHALLENGE_MARKERS = (
	"JUST A MOMENT",
	"CHALLENGES.CLOUDFLARE.COM",
	"CF-CHALLENGE",
	"ATTENTION REQUIRED",
	"CHECKING YOUR BROWSER",
	"ENABLE JAVASCRIPT AND COOKIES",
	"DDOS-GUARD",
)


# Keys that only a CDN error document carries. Cloudflare's problem+json shares
# `error_code` with the Comstore schema, so the presence of an error code proves
# nothing -- these do.
CDN_JSON_KEYS = frozenset({"cloudflare_error", "ray_id", "error_name", "error_category", "zone"})


def looks_like_cdn(data):
	"""True when a parsed JSON body came from a CDN rather than the device."""
	return isinstance(data, dict) and bool(CDN_JSON_KEYS & set(data))


# The service's own auth refusal is a compact JSON document naming the header it
# wanted. It has to be told apart from a CDN challenge: both arrive as a 401 from
# behind Cloudflare, and their remedies are opposite -- one is a wrong key in our
# own settings, the other a WAF rule only the endpoint's host can change.
_AUTH_MARKERS = ("X-API-KEY", "API KEY", "APIKEY", "INVALID KEY", "UNAUTHORIZED")


def looks_like_auth_rejection(status, data, text):
	"""True when a 401/403 carries the API's own "bad key" JSON rather than a challenge."""
	if status not in (401, 403):
		return False

	# A challenge page is HTML and never parses to a dict, so requiring one is what
	# keeps this from swallowing the CDN case.
	if not isinstance(data, dict) or looks_like_cdn(data):
		return False

	return any(marker in text for marker in _AUTH_MARKERS)


def classify_http(status, body, headers=None, data=None):
	"""
	Classify a reply that was not usable JSON, using the status, the body and the
	response headers together.

	Ordered most specific first: a Cloudflare 530 and a Cloudflare challenge are
	both "not the device", but they need opposite handling -- one is worth
	retrying, the other will repeat forever until a human changes a WAF rule.
	"""
	text = (body or "").upper()
	headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
	via_cdn = (
		"cloudflare" in headers.get("server", "").lower()
		or "cf-ray" in headers
		or looks_like_cdn(data)
	)

	# A CDN error document names its own fault far better than any guess from the
	# status code, so trust it when it is there.
	if looks_like_cdn(data):
		name = str(data.get("error_name") or "").lower()
		if "tunnel" in name or "origin" in name or "unreachable" in name:
			return ORIGIN_UNREACHABLE
		if "challenge" in name or "captcha" in name or "block" in name:
			return EDGE_CHALLENGE

	# Must beat the 401-behind-a-CDN rule below. This endpoint is always behind
	# Cloudflare, so that rule on its own reports every wrong key as a bot
	# challenge and sends an operator to argue with their host about WAF rules.
	if looks_like_auth_rejection(status, data, text):
		return AUTH_REJECTED

	# 52x/530 are Cloudflare's "I could not reach the origin" family.
	if status in (521, 522, 523, 524, 525, 526, 530) or (via_cdn and status >= 520):
		return ORIGIN_UNREACHABLE

	if "cf-mitigated" in headers or any(marker in text for marker in _CHALLENGE_MARKERS):
		return EDGE_CHALLENGE

	if status in (401, 403) and via_cdn:
		return EDGE_CHALLENGE

	if status >= 500:
		return TRANSPORT

	# Any other HTML body: something answered, but not the API.
	if "<HTML" in text or "<!DOCTYPE" in text:
		return NOT_THE_API

	return UNKNOWN


def summarise_body(body, limit=300, data=None):
	"""
	A short, readable account of a non-JSON body.

	An error carrying 800 characters of Cloudflare markup buries the one useful
	fact in noise, so an HTML page is reduced to its <title>.
	"""
	# A CDN problem document explains itself in prose; use it rather than dumping
	# the raw JSON.
	if looks_like_cdn(data):
		parts = [str(data.get("title") or "").strip(), str(data.get("detail") or "").strip()]
		summary = " - ".join(p for p in parts if p)
		if summary:
			return summary[:limit]

	text = (body or "").strip()
	if not text:
		return "empty response"

	upper = text.upper()
	if "<HTML" in upper or "<!DOCTYPE" in upper:
		start = upper.find("<TITLE>")
		if start != -1:
			end = upper.find("</TITLE>", start)
			if end != -1:
				return "HTML page: " + text[start + 7 : end].strip()
		return "HTML page (no title)"

	return text[:limit]
