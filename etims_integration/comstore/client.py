"""
HTTP client for the Comstore Smart VSCU service.

Deliberately free of Frappe imports. Everything here is about speaking to one
device: URLs, the ``X-API-KEY`` header, timeouts, and turning a rejection into a
classified :class:`ComstoreError`. It performs no database work, logs nothing and
raises rather than messaging the user -- which is what makes it testable without a
site and replaceable if KRA's direct OSCU API ever supersedes the local bridge.
"""

import json

import requests

from etims_integration.comstore.errors import (
	AUTH_REJECTED,
	TRANSPORT,
	ComstoreError,
	classify,
	classify_http,
	looks_like_cdn,
	summarise_body,
)
from etims_integration.comstore.schema import Buyer, PLUItem, PLULine, SignStructure, WorkflowResult

DEFAULT_PORT = 4000
DEFAULT_TIMEOUT = 45

# Identify ourselves. Some proxies reject or challenge a request carrying a bare
# HTTP-library user agent, and when something does go wrong upstream this is what
# tells the host's logs who was calling.
USER_AGENT = "ETIMSIntegration/1.0 (+https://github.com/KiruiElisha/etims_integration)"


# Suffixes that mean "this name is on the LAN", where a device port is meaningful.
_LAN_SUFFIXES = (".local", ".internal", ".lan", ".home", ".arpa")


def is_public_domain(hostname):
	"""
	True for a name that resolves on the public internet (``hedgeinc.co.ke``) and
	False for anything naming a machine on the LAN. A single-label name
	(``desktop-7f2``) is a LAN name, and so is anything under a LAN suffix.
	"""
	hostname = (hostname or "").lower()
	if "." not in hostname:
		return False
	return not hostname.endswith(_LAN_SUFFIXES)


def normalise_base_url(host, port=None, use_https=False):
	"""
	Accepts what people actually type into a settings field -- ``192.168.1.5``,
	``localhost:4000``, ``hedgeinc.co.ke``, ``http://host:4000/`` -- and returns a
	base URL with no trailing slash.

	The ``port`` argument is only ever applied to a LAN address, which is where a
	Comstore device actually lives. A public domain is assumed to answer on 80/443
	and has any port stripped, including one typed into the field: 4000 is
	firewalled on the hosted service, so carrying it over turns a working endpoint
	into a 20-second timeout. Pass a full URL with a scheme to override that.
	"""
	host = (host or "").strip().rstrip("/")
	if not host:
		raise ValueError("No host configured for the eTIMS device.")

	if "://" in host:
		return host

	scheme = "https" if use_https else "http"
	authority = host.split("/", 1)[0]
	hostname = authority.split(":")[0]
	has_port = ":" in authority
	looks_like_ip = authority.replace(".", "").replace(":", "").isdigit()
	is_local = hostname in ("localhost", "127.0.0.1")

	if not has_port and port and (looks_like_ip or is_local):
		host = f"{host}:{int(port)}"
	elif has_port and not looks_like_ip and not is_local and is_public_domain(hostname):
		# Keep whatever path was typed; drop only the port.
		host = hostname + host[len(authority) :]

	return f"{scheme}://{host}"


class ComstoreClient:
	def __init__(self, base_url, api_key="", serial_number="", timeout=DEFAULT_TIMEOUT, session=None):
		self.base_url = base_url.rstrip("/")
		self.api_key = api_key or ""
		self.serial_number = serial_number or ""
		self.timeout = timeout or DEFAULT_TIMEOUT
		self.session = session or requests.Session()

	# ------------------------------------------------------------------ plumbing

	def _headers(self):
		headers = {
			"Content-Type": "application/json",
			"Accept": "application/json",
			"User-Agent": USER_AGENT,
		}
		if self.api_key:
			headers["X-API-KEY"] = self.api_key
		return headers

	def _call(self, method, path, payload=None, timeout=None):
		url = f"{self.base_url}{path}"
		try:
			response = self.session.request(
				method,
				url,
				# json= would serialise Decimal poorly; everything is already a
				# string by the time it gets here, but be explicit about it.
				data=json.dumps(payload, default=str) if payload is not None else None,
				headers=self._headers(),
				timeout=timeout or self.timeout,
			)
		except requests.RequestException as e:
			raise ComstoreError(TRANSPORT, f"{method} {url}: {e}", payload=payload) from e

		return self._parse(response, url, payload)

	def _parse(self, response, url, payload):
		"""
		Turn a reply into the device's JSON, or into a classified error.

		The awkward case is that a CDN in front of the device answers with
		perfectly valid JSON of its own -- Cloudflare returns an RFC-7807
		``problem+json`` document for its 5xx and challenge pages. Parsing
		successfully therefore proves nothing about *who* answered, so an error
		status is classified from the whole reply and the device's own semantics
		are used only when the body actually looks like the device talking.
		"""
		body = (response.text or "").strip()

		try:
			data = json.loads(body)
		except ValueError:
			data = None

		if data is not None and not isinstance(data, dict):
			data = None

		# A CDN document can be valid JSON and can even share key names with the
		# device, so it is ruled out explicitly rather than by absence of evidence.
		from_device = (
			data is not None and not looks_like_cdn(data) and self._looks_like_device_reply(data)
		)

		if response.status_code >= 400 or data is None:
			if from_device:
				# The device itself rejected this. Its message carries the E-code.
				message = self._message(data)
				raise ComstoreError(classify(message), message, payload=payload, response=data)

			# Something in front of the device answered, or nothing usable did.
			raise ComstoreError(
				classify_http(response.status_code, body, response.headers, data),
				f"HTTP {response.status_code} from {url}: {summarise_body(body, data=data)}",
				payload=payload,
				response={"http_status": response.status_code, "body": body[:4000]},
			)

		# Casing is inconsistent across endpoints: complete-workflow answers
		# "success", the buyer endpoints answer "Success".
		if data.get("success", data.get("Success")) is False:
			message = self._message(data)
			raise ComstoreError(classify(message), message, payload=payload, response=data)

		return data

	# Keys that only ever appear in a Comstore reply. Used to tell the device's own
	# answer apart from a proxy's, since both can be valid JSON.
	# Distinctive to Comstore. Generic names like `message`, `error_code` and
	# `version` are deliberately excluded: CDNs and proxies use them too, and it
	# was exactly `error_code` that made a Cloudflare 530 look like a device reply.
	DEVICE_KEYS = frozenset(
		{
			"success", "Success", "apiService", "deviceConnection",
			"serial_number", "SerialNumber", "signature", "invoice_number",
			"items_processed", "invoices_on_device", "Buyers", "RecordsProcessed",
			"scu_id", "cu-inv-no",
		}
	)

	@classmethod
	def _looks_like_device_reply(cls, data):
		return bool(cls.DEVICE_KEYS & set(data))

	@staticmethod
	def _message(data):
		parts = [
			data.get("message") or data.get("Message") or "",
			data.get("error_message") or data.get("ErrorMessage") or "",
			str(data.get("error_code") or data.get("ErrorCode") or ""),
		]
		return " | ".join(p for p in parts if p and p != "None") or "No message returned by the device."

	# ------------------------------------------------------------------ endpoints

	def health(self):
		"""Liveness of the API service *and* of its link to the fiscal device."""
		return self._call("GET", "/api/health", timeout=10)

	def init(self, ip, serial_number=None):
		"""
		Re-establish the service's connection to the device. Note the doc's own
		example has ``sn`` and ``ip`` transposed; the field names are what count.
		"""
		return self._call(
			"POST",
			"/api/init",
			{"sn": serial_number or self.serial_number, "ip": ip},
			timeout=20,
		)

	def check_credentials(self, serial_number=None):
		"""
		Prove the API key, which :meth:`health` cannot: ``/api/health`` is
		unauthenticated and answers 200 to a wrong key, or to no key at all.

		Returns ``(ok, detail)``. Any answer other than an auth refusal counts as
		accepted -- a device-level complaint about the serial number still proves
		the request got past the header check, and this method is about the key.
		"""
		try:
			self.invoice_status(serial_number)
		except ComstoreError as e:
			if e.spec is AUTH_REJECTED:
				return False, e.detail or "The device refused the API key."
			return True, f"Key accepted; the device then reported: {e.spec.summary}"

		return True, "Key accepted."

	def is_connected(self):
		try:
			return (self.health().get("deviceConnection") or "").lower() == "connected"
		except ComstoreError:
			return False

	def complete_workflow(self, lines, sign_structure, is_test=False, serial_number=None):
		"""
		Fiscalise an invoice or a credit note and return its signature.

		``lines`` are :class:`PLULine` and ``sign_structure`` a
		:class:`SignStructure`; both know their own wire shape.
		"""
		payload = {
			"sn": serial_number or self.serial_number,
			"is_test": bool(is_test),
			"plu_data": [line.as_payload() if isinstance(line, PLULine) else line for line in lines],
			"sign_structure": (
				sign_structure.as_payload() if isinstance(sign_structure, SignStructure) else sign_structure
			),
		}
		data = self._call("POST", "/api/complete-workflow", payload)

		result = WorkflowResult.from_response(data)
		if not result.cu_invoice_number and not result.signature:
			# A 200 with no signature is a rejection the service failed to flag.
			raise ComstoreError(
				classify(result.message),
				f"Device returned no signature: {result.message}",
				payload=payload,
				response=data,
			)
		return result, payload

	def upload_plu(self, items, serial_number=None, from_no=1, end_no=10_000_000, update_flag=0):
		payload = {
			"sn": serial_number or self.serial_number,
			"plu_items": [i.as_payload() if isinstance(i, PLUItem) else i for i in items],
			"from_no": int(from_no),
			"end_no": int(end_no),
			"update_flag": int(update_flag),
			"file_signal": "",
		}
		return self._call("POST", "/api/upload-plu-data", payload), payload

	def set_buyers(self, buyers, serial_number=None, from_number=1, end_number=30, update_flag=0):
		payload = {
			"SerialNumber": serial_number or self.serial_number,
			"Buyers": [b.as_payload() if isinstance(b, Buyer) else b for b in buyers],
			"FromNumber": int(from_number),
			"EndNumber": int(end_number),
			"UpdateFlag": int(update_flag),
		}
		return self._call("POST", "/api/buyers/set", payload), payload

	def get_buyers(self, serial_number=None):
		return self._call("GET", f"/api/buyers/{serial_number or self.serial_number}")

	def invoice_status(self, serial_number=None):
		"""
		How many invoices the device holds versus how many KRA has taken. The gap
		is the compliance exposure -- the device retries on its own but the doc is
		explicit that it does not always succeed.
		"""
		data = self._call("GET", f"/api/invoices/status/{serial_number or self.serial_number}")
		return {
			"on_device": int(data.get("invoices_on_device") or 0),
			"uploaded": int(data.get("invoices_uploaded_to_etims") or 0),
			"pending": int(data.get("pending_invoices") or 0),
			"raw": data,
		}

	def manual_upload(self, serial_number=None):
		"""Force transmission of whatever the device is still holding."""
		return self._call(
			"POST",
			f"/api/invoices/manually-upload/{serial_number or self.serial_number}",
			{},
			timeout=120,
		)

	def restart_device(self, ip):
		return self._call("POST", "/api/restart-device", {"ip_address": ip}, timeout=20)
