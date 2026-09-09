"""
Jinja helpers for the fiscal receipt.

KRA requires the receipt to carry the QR code that resolves to the eTIMS
verification page, plus the SCU id and receipt signature. The QR payload is the
``signature_link`` the device returns verbatim -- it must not be rebuilt from
parts, because the trailing hash is what eTIMS actually verifies.
"""

import frappe


def etims_qr_code(doc, scale=4):
	"""
	Data URI for the verification QR, or "" when the invoice is unsigned.

	SVG rather than PNG: a fiscal receipt is often printed at 203dpi on thermal
	paper, where a rasterised QR at print scale is the usual reason a scanner
	cannot read it.
	"""
	link = _get(doc, "custom_etims_signature_link")
	if not link:
		return ""

	try:
		import base64
		import io

		import pyqrcode

		buffer = io.BytesIO()
		pyqrcode.create(link, error="M").svg(buffer, scale=scale, xmldecl=False, svgns=True, quiet_zone=2)
		encoded = base64.b64encode(buffer.getvalue()).decode()
		return f"data:image/svg+xml;base64,{encoded}"
	except Exception:
		# A receipt must still print if the QR cannot be drawn.
		frappe.log_error(title="eTIMS: QR generation failed", message=frappe.get_traceback())
		return ""


def etims_receipt_details(doc):
	"""Everything the fiscal footer needs, in one call."""
	return {
		"cu_invoice_no": _get(doc, "custom_etims_cu_invoice_no"),
		"scu_id": _get(doc, "custom_etims_scu_id"),
		"receipt_signature": _get(doc, "custom_etims_receipt_signature"),
		"internal_data": _get(doc, "custom_etims_internal_data"),
		"signed_at": _get(doc, "custom_etims_signed_at"),
		"link": _get(doc, "custom_etims_signature_link"),
		"is_test": _get(doc, "custom_etims_is_test"),
		"signed": _get(doc, "custom_etims_status") == "Signed",
	}


def _get(doc, fieldname):
	if isinstance(doc, str):
		doc = frappe.get_cached_doc("Sales Invoice", doc)
	return doc.get(fieldname) or ""
