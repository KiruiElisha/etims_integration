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
			const result = r.message || {};
			const failed = result.error || result.success === false;
			frappe.msgprint({
				title: failed ? __("Device Error") : __("Device Response"),
				indicator: failed ? "red" : "green",
				message: `<pre style="white-space:pre-wrap">${frappe.utils.escape_html(
					JSON.stringify(result, null, 2)
				)}</pre>`,
			});
		},
	});
}
