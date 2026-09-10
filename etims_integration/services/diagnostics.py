"""
Answering "why did nothing happen?"

Two tools, both copied in spirit from the TIMS app, because both earn their keep
the first time an invoice silently fails to fiscalise on a customer site.

:func:`check_setup` answers *is this app actually wired up here* -- run it first
when submits appear to do nothing at all.

:func:`diagnose` runs the whole submission path for one invoice and returns every
intermediate result instead of routing failures to msgprint and the Error Log.
Use it when an invoice will not sign and the Transmission does not say enough.

Both are whitelisted so they can be driven from ``bench execute``::

    bench --site <site> execute etims_integration.services.diagnostics.check_setup
    bench --site <site> execute etims_integration.services.diagnostics.diagnose \\
        --args "['ACC-SINV-2026-00001']"
"""

import frappe
from frappe.utils import cint

from etims_integration.comstore.errors import ComstoreError

# Kept in step with install.CUSTOM_FIELDS. Listed explicitly rather than derived,
# so that a field quietly dropped from install.py shows up here as missing rather
# than as agreement between two copies of the same mistake.
EXPECTED_INVOICE_FIELDS = [
	"custom_etims_exempt",
	"custom_etims_adjustment_type",
	"custom_etims_original_invoice",
	"custom_etims_refund_reason",
	"custom_etims_exemption_number",
	"custom_etims_branch",
	"custom_etims_status",
	"custom_etims_transmission",
	"custom_etims_is_test",
	"custom_etims_cu_invoice_no",
	"custom_etims_scu_id",
	"custom_etims_receipt_signature",
	"custom_etims_internal_data",
	"custom_etims_signed_at",
	"custom_etims_signature_link",
]

EXPECTED_ITEM_FIELDS = [
	"custom_etims_item_class_code",
	"custom_etims_tax_type",
	"custom_etims_product_type",
	"custom_etims_origin_country",
	"custom_etims_package_unit",
	"custom_etims_quantity_unit",
	"custom_etims_levy_rate",
	"custom_etims_surcharge_rate",
	"custom_etims_plu_no",
]

EXPECTED_DOCTYPES = (
	"ETIMS Settings",
	"ETIMS Device",
	"ETIMS Transmission",
	"ETIMS KRA Response",
	"ETIMS Item Registration",
	"ETIMS Buyer Registration",
	"ETIMS Item Classification",
	"ETIMS Packaging Unit",
	"ETIMS Quantity Unit",
)


@frappe.whitelist()
def check_setup():
	"""Whether the app is actually installed and configured on this site."""
	frappe.only_for("System Manager")

	report = {}

	report["doctypes_present"] = {
		dt: bool(frappe.db.exists("DocType", dt)) for dt in EXPECTED_DOCTYPES
	}

	report["custom_fields"] = {
		"Sales Invoice": _field_report("Sales Invoice", EXPECTED_INVOICE_FIELDS),
		"Item": _field_report("Item", EXPECTED_ITEM_FIELDS),
	}

	hooks = frappe.get_hooks("doc_events") or {}
	report["hooks"] = {
		"Sales Invoice": (hooks.get("Sales Invoice") or {}),
		"Item": (hooks.get("Item") or {}),
	}

	report["masters_seeded"] = {
		"ETIMS Packaging Unit": frappe.db.count("ETIMS Packaging Unit"),
		"ETIMS Quantity Unit": frappe.db.count("ETIMS Quantity Unit"),
		"ETIMS Item Classification": frappe.db.count("ETIMS Item Classification"),
	}

	settings = frappe.get_cached_doc("ETIMS Settings")
	report["settings"] = {
		"enabled": cint(settings.enabled),
		"send_on_submit": cint(settings.send_on_submit),
		"send_credit_notes": cint(settings.send_credit_notes),
		"submit_mode": settings.submit_mode,
		"tax_mappings": len(settings.tax_mappings or []),
	}

	report["devices"] = frappe.get_all(
		"ETIMS Device",
		fields=["name", "company", "status", "is_test_mode", "host", "port",
		        "connection_status", "pending_invoices"],
	)

	report["counts"] = {
		"transmissions": frappe.db.count("ETIMS Transmission"),
		"signed": frappe.db.count("ETIMS Transmission", {"status": "Signed"}),
		"blocked": frappe.db.count("ETIMS Transmission", {"status": "Blocked"}),
		"kra_responses": frappe.db.count("ETIMS KRA Response"),
		"items_registered": frappe.db.count("ETIMS Item Registration", {"status": "Registered"}),
	}

	report["problems"] = _problems(report)
	return report


def _field_report(doctype, expected):
	"""
	Both halves are checked. A Custom Field row can exist while its database
	column does not, if a migrate was interrupted -- and then reads succeed while
	writes fail, which is a confusing way to find out.
	"""
	existing = set(
		frappe.get_all("Custom Field", filters={"dt": doctype, "fieldname": ("in", expected)}, pluck="fieldname")
	)
	columns = set(frappe.db.get_table_columns(doctype))
	return {
		"missing_fields": sorted(set(expected) - existing),
		"missing_columns": sorted(f for f in expected if f not in columns),
	}


def _problems(report):
	"""The report reduced to a list of things that are actually wrong."""
	problems = []

	missing_dt = [dt for dt, present in report["doctypes_present"].items() if not present]
	if missing_dt:
		problems.append(f"DocTypes missing (run bench migrate): {', '.join(missing_dt)}")

	for doctype, fields in report["custom_fields"].items():
		if fields["missing_fields"]:
			problems.append(f"{doctype} custom fields missing: {', '.join(fields['missing_fields'])}")
		if fields["missing_columns"]:
			problems.append(f"{doctype} database columns missing: {', '.join(fields['missing_columns'])}")

	if not (report["hooks"].get("Sales Invoice") or {}).get("on_submit"):
		problems.append("Sales Invoice on_submit hook is not registered.")

	if not report["masters_seeded"]["ETIMS Packaging Unit"]:
		problems.append("KRA code masters are not seeded (run bench migrate).")

	if not report["settings"]["enabled"]:
		problems.append("eTIMS Settings has the integration disabled.")

	if not report["devices"]:
		problems.append("No eTIMS Device is configured.")
	elif not any(d.status == "Active" for d in report["devices"]):
		problems.append("No eTIMS Device is Active.")

	if not report["settings"]["tax_mappings"]:
		problems.append(
			"No tax band mappings configured. Items without a band of their own will "
			"block their invoices rather than be guessed at."
		)

	return problems or ["No problems found."]


@frappe.whitelist()
def diagnose(invoice, send=False):
	"""
	Walk the whole path for one invoice and report each stage.

	``send`` defaults to False, so this is safe to run on a live invoice: it
	builds and inspects the payload without touching the device. Pass ``send=1``
	to actually transmit -- against a device in test mode that is harmless, and
	against a live one it fiscalises for real.
	"""
	frappe.only_for("System Manager")

	from etims_integration.etims_integration.doctype.etims_device.etims_device import resolve_device
	from etims_integration.mapping import invoice as invoice_mapping

	report = {"invoice": invoice, "stage": "start", "sent": False}

	try:
		doc = frappe.get_doc("Sales Invoice", invoice)
		settings = frappe.get_cached_doc("ETIMS Settings")

		report["stage"] = "invoice"
		report["docstatus"] = doc.docstatus
		report["is_return"] = cint(doc.get("is_return"))
		report["etims_status"] = doc.get("custom_etims_status")
		report["adjustment_type"] = doc.get("custom_etims_adjustment_type")

		from etims_integration.services.transmit import out_of_scope

		report["out_of_scope"] = out_of_scope(doc, settings)

		report["stage"] = "device"
		device = frappe.get_cached_doc("ETIMS Device", resolve_device(doc.company, doc.get("custom_etims_branch")))
		report["device"] = {
			"name": device.name,
			"status": device.status,
			"is_test_mode": cint(device.is_test_mode),
			"base_url": device.base_url,
		}

		report["stage"] = "health"
		try:
			report["health"] = device.get_client().health()
		except ComstoreError as e:
			report["health"] = {"error": str(e), "remedy": e.spec.remedy, "retryable": e.retryable}

		report["stage"] = "build"
		mapped = invoice_mapping.build(doc, device, settings, "<diagnostic>")
		report["concerns"] = mapped.concerns
		report["sendable"] = mapped.sendable
		report["invoice_total"] = str(mapped.invoice_total)
		report["declared_total"] = str(mapped.declared_total)
		report["bands"] = {k: {kk: str(vv) for kk, vv in v.items()} for k, v in mapped.band_summary.items()}
		report["payload"] = mapped.as_payload()

		if cint(send) and mapped.sendable:
			report["stage"] = "send"
			try:
				result, payload = device.get_client().complete_workflow(
					mapped.lines, mapped.sign_structure, is_test=cint(device.is_test_mode)
				)
				report["sent"] = True
				report["result"] = result.raw
				report["cu_invoice_no"] = result.cu_invoice_number
			except ComstoreError as e:
				report["send_error"] = {
					"code": e.spec.code, "summary": e.spec.summary,
					"remedy": e.spec.remedy, "detail": str(e), "response": e.response,
				}

		report["stage"] = "transmissions"
		report["transmissions"] = frappe.get_all(
			"ETIMS Transmission",
			filters={"sales_invoice": invoice},
			fields=["name", "status", "attempt", "error_code", "error_summary", "cu_invoice_no"],
		)

		from etims_integration.services.response_log import history

		report["exchanges"] = history(invoice)
		report["stage"] = "done"

	except Exception as e:
		report["error"] = str(e)
		report["traceback"] = frappe.get_traceback()

	return report
