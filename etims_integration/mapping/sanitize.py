"""
Text and PIN normalisation.

The doc devotes a whole section to this: eTIMS rejects non-ASCII input, and the
resulting faults (E034, "External component has thrown an exception",
"SetPluDataInfoEX failed") name the transport rather than the character, so they
are painful to diagnose from the far side. Cheaper to never send one.
"""

import re
import unicodedata

MAX_ITEM_NAME = 50
MAX_BUYER_NAME = 60

# 2 letters + 9 digits, per the doc. KRA PINs are conventionally a letter, nine
# digits and a trailing letter (P051238105V) -- both shapes are accepted here
# because the device validates the real rule and we only screen out obvious junk.
PIN_PATTERN = re.compile(r"^[A-Z]{1,2}[0-9]{9}[A-Z]?$")

# Characters the doc calls out by name, plus the escape that breaks the JSON the
# device's own parser builds internally.
_FORBIDDEN = dict.fromkeys(map(ord, "\\↓→↑↔←∟↨§"), None)

_TRANSLITERATIONS = {
	"–": "-",
	"—": "-",
	"‘": "'",
	"’": "'",
	"“": '"',
	"”": '"',
	"…": "...",
	"×": "x",
	"°": "deg",
	"€": "EUR",
	"£": "GBP",
	"½": "1/2",
	"¼": "1/4",
	"&": "and",
}


def ascii_text(value, max_length=None, fallback=""):
	"""
	Reduce any string to plain ASCII the device will accept.

	Accents are folded rather than dropped (``Café`` -> ``Cafe``), the handful of
	symbols people actually paste are transliterated, and anything still outside
	ASCII is removed. Truncation is last so a name never ends mid-escape.
	"""
	text = str(value or "").strip()
	if not text:
		return fallback

	for source, target in _TRANSLITERATIONS.items():
		text = text.replace(source, target)

	text = text.translate(_FORBIDDEN)
	# NFKD splits accented letters into letter + combining mark; the ASCII encode
	# then drops the marks and keeps the letters.
	text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
	text = re.sub(r"\s+", " ", text).strip()

	if max_length:
		text = text[:max_length].strip()

	return text or fallback


def item_name(value, fallback="ITEM"):
	return ascii_text(value, MAX_ITEM_NAME, fallback)


def clean_pin(value):
	"""
	Returns a PIN the device will accept, or "" -- never a malformed one. Sending
	junk earns E325/E358 and a rejected invoice; sending nothing is legal for a
	buyer and merely means the sale is not attributed to them.
	"""
	pin = ascii_text(value).upper().replace(" ", "").replace("-", "")
	return pin if PIN_PATTERN.match(pin) else ""


def is_valid_pin(value):
	return bool(clean_pin(value))
