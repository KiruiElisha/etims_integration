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

from etims_integration.comstore.errors import TRANSPORT, UNKNOWN, ComstoreError, classify
from etims_integration.comstore.schema import Buyer, PLUItem, PLULine, SignStructure, WorkflowResult

DEFAULT_PORT = 4000
DEFAULT_TIMEOUT = 45


def normalise_base_url(host, port=None, use_https=False):
	"""
	Accepts what people actually type into a settings field -- ``192.168.1.5``,
	``localhost:4000``, ``hedgeinc.co.ke``, ``http://host:4000/`` -- and returns a
	base URL with no trailing slash.

	A port already present in the host wins over the ``port`` argument, so pasting
	a full URL does the obvious thing. A named host with no port is left alone: the
	shared test service answers on 80/443, and forcing :4000 onto it would break it.
	"""
	host = (host or "").strip().rstrip("/")
	if not host:
		raise ValueError("No host configured for the eTIMS device.")

	if "://" in host:
		return host

	scheme = "https" if use_https else "http"
	has_port = ":" in host.rsplit("/", 1)[0]
	looks_like_ip = host.split("/")[0].replace(".", "").isdigit()
	is_local = host.split(":")[0] in ("localhost", "127.0.0.1")

	if not has_port and port and (looks_like_ip or is_local):
		host = f"{host}:{int(port)}"

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
		headers = {"Content-Type": "application/json", "Accept": "application/json"}
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
		The service answers JSON when it is healthy and can answer HTML or an empty
		body when it is not. Always end up with a dict, so the exchange is still
		recordable and the operator sees the real body rather than a parse error.
		"""
		body = (response.text or "").strip()
		try:
			data = json.loads(body)
		except ValueError:
			raise ComstoreError(
				TRANSPORT if response.status_code >= 500 else UNKNOWN,
				f"HTTP {response.status_code} from {url}: {body[:800] or 'empty response'}",
				payload=payload,
			) from None

		if not isinstance(data, dict):
			raise ComstoreError(UNKNOWN, f"Unexpected response from {url}: {str(data)[:800]}", payload=payload)

		if response.status_code >= 500:
			raise ComstoreError(TRANSPORT, f"HTTP {response.status_code}: {self._message(data)}", payload=payload, response=data)

		# Casing is inconsistent across endpoints: complete-workflow answers
		# "success", the buyer endpoints answer "Success".
		success = data.get("success", data.get("Success"))
		if success is False or response.status_code >= 400:
			message = self._message(data)
			raise ComstoreError(classify(message), message, payload=payload, response=data)

		return data

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
