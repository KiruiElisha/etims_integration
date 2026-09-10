// eTIMS Device: the operational buttons. Everything here is a live call to the
// device, so each reports what actually came back rather than "done".
frappe.ui.form.on("ETIMS Device", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(__("Test Connection"), () => call(frm, "test_connection"));
		frm.add_custom_button(__("Initialise Device"), () => call(frm, "initialise"), __("Device"));
		frm.add_custom_button(__("Force Upload to KRA"), () => call(frm, "force_upload"), __("Device"));
		// The device is where the shared causes get fixed -- an item registered, a
		// link brought back up -- so it is where the invoices that fix unblocks
		// should be resendable from.
		frm.add_custom_button(__("Retry Blocked Transmissions"), () => retry_blocked(frm), __("Device"));

		frm.add_custom_button(
			__("Restart Device"),
			() =>
				frappe.confirm(__("Reboot the fiscal device at {0}?", [frm.doc.device_ip]), () =>
					call(frm, "restart")
				),
			__("Device")
		);

		if (frm.doc.is_test_mode) {
			frm.dashboard.add_comment(
				__("Test mode: signatures are generated locally and nothing reaches KRA."),
				"orange",
				true
			);
		}

		if (frm.doc.pending_invoices > 0) {
			frm.dashboard.add_indicator(
				__("{0} invoices pending at KRA", [frm.doc.pending_invoices]),
				"orange"
			);
		}
	},
});

function call(frm, method) {
	frm.call({
		doc: frm.doc,
		method: method,
		freeze: true,
		freeze_message: __("Talking to the device..."),
		callback: (r) => {
			frm.reload_doc();
			show_result(r.message || {});
		},
	});
}

// A partial failure used to render as a green "Device Response" with the fault
// buried in a JSON dump: a test connection whose invoice-status check failed
// looked exactly like one that passed. Every classified error the reply carries
// is surfaced with its remedy, and any one of them makes the dialog red.
function show_result(result) {
	const errors = [];
	if (result.error) {
		errors.push([__("Device health"), result.error]);
	}
	if (result.invoice_status_error) {
		errors.push([__("Invoice status"), result.invoice_status_error]);
	}

	const failed = errors.length > 0 || result.ok === false || result.success === false;
	const esc = frappe.utils.escape_html;

	const problems = errors
		.map(([where, e]) => {
			// Older endpoints still answer with a bare string.
			if (typeof e === "string") {
				return `<div class="alert alert-danger"><b>${where}</b><br>${esc(e)}</div>`;
			}
			const retry = e.retryable
				? `<div class="text-muted small">${__("This clears on its own; it will be retried.")}</div>`
				: "";
			return `<div class="alert alert-danger">
					<b>${where} — ${esc(e.summary || e.code || "")}</b>
					<div class="small text-muted">${esc(e.code || "")}</div>
					${e.detail ? `<div style="margin-top:6px">${esc(e.detail)}</div>` : ""}
					${e.remedy ? `<div style="margin-top:6px"><b>${__("What to do")}:</b> ${esc(e.remedy)}</div>` : ""}
					${retry}
				</div>`;
		})
		.join("");

	const raw = `<details style="margin-top:8px">
			<summary class="text-muted">${__("Raw device response")}</summary>
			<pre style="white-space:pre-wrap;margin-top:8px">${esc(JSON.stringify(result, null, 2))}</pre>
		</details>`;

	frappe.msgprint({
		title: failed ? __("Device Error") : __("Device Response"),
		indicator: failed ? "red" : "green",
		message: problems + raw,
	});
}

function retry_blocked(frm) {
	frappe.confirm(
		__("Resend every Blocked transmission for {0}? Do this only once the cause is fixed.", [frm.doc.name]),
		() => {
			frappe.call({
				method: "etims_integration.services.transmit.retry_blocked",
				args: { device: frm.doc.name },
				freeze: true,
				freeze_message: __("Queueing..."),
				callback: (r) => {
					const result = r.message || {};
					const queued = (result.queued || []).length;
					frappe.msgprint({
						title: __("eTIMS Retry"),
						indicator: queued ? "blue" : "orange",
						message: queued
							? __("{0} transmission(s) queued for resend.", [queued])
							: __("Nothing was blocked for this device."),
					});
				},
			});
		}
	);
}
