// Sales Invoice: eTIMS status, and the two actions a user actually needs.
frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			return;
		}

		show_status(frm);

		// Safe on any submitted invoice, and the fastest way to answer
		// "what exactly would KRA be told about this?" before it is told.
		frm.add_custom_button(__("Preview Payload"), () => preview(frm), __("eTIMS"));

		if (frm.doc.custom_etims_status !== "Signed" && !frm.doc.custom_etims_exempt) {
			frm.add_custom_button(__("Send to eTIMS"), () => send(frm), __("eTIMS"));
		}

		frm.add_custom_button(__("Device History"), () => history(frm), __("eTIMS"));

		if (frm.doc.custom_etims_transmission) {
			frm.add_custom_button(
				__("Open Transmission"),
				() => frappe.set_route("Form", "ETIMS Transmission", frm.doc.custom_etims_transmission),
				__("eTIMS")
			);
		}
	},
});

function show_status(frm) {
	const status = frm.doc.custom_etims_status;
	if (!status || status === "Not Sent") {
		return;
	}

	const colours = { Signed: "green", Queued: "blue", Failed: "orange", Blocked: "red" };
	frm.dashboard.add_indicator(__("eTIMS: {0}", [status]), colours[status] || "gray");

	// A test-mode signature is indistinguishable from a real one on the form.
	// Say plainly that KRA never saw it, so nobody files it as a declared sale.
	if (frm.doc.custom_etims_is_test) {
		frm.dashboard.add_comment(
			__("Signed in test mode. This invoice was NOT transmitted to KRA."),
			"orange",
			true
		);
	}
}

function send(frm) {
	frappe.call({
		method: "etims_integration.services.transmit.send_now",
		args: { invoice: frm.doc.name },
		freeze: true,
		freeze_message: __("Queueing for eTIMS..."),
		callback: (r) => {
			if (r.message && r.message.queued) {
				frappe.show_alert({ message: __("Queued for eTIMS."), indicator: "blue" });
				setTimeout(() => frm.reload_doc(), 2500);
			}
		},
	});
}

function preview(frm) {
	frappe.call({
		method: "etims_integration.services.transmit.preview",
		args: { invoice: frm.doc.name },
		freeze: true,
		callback: (r) => {
			if (!r.message) {
				return;
			}

			const dialog = new frappe.ui.Dialog({
				title: __("eTIMS Payload for {0}", [frm.doc.name]),
				size: "large",
				fields: [
					{ fieldtype: "HTML", fieldname: "summary" },
					{
						fieldtype: "Code",
						fieldname: "payload",
						label: __("Raw Payload"),
						options: "JSON",
						read_only: 1,
					},
				],
				primary_action_label: __("Close"),
				primary_action() {
					this.hide();
				},
			});

			dialog.fields_dict.summary.$wrapper.html(summary_html(r.message));
			dialog.set_value("payload", JSON.stringify(r.message.payload, null, 2));
			dialog.show();
		},
	});
}

function summary_html(d) {
	const esc = frappe.utils.escape_html;

	const concerns = (d.concerns || []).length
		? `<div class="alert alert-warning"><b>${__("Held for review before sending")}</b><ul>${d.concerns
				.map((c) => `<li>${esc(c)}</li>`)
				.join("")}</ul></div>`
		: `<div class="alert alert-success">${__("Agrees with the invoice. Ready to send.")}</div>`;

	const test_pill = d.is_test
		? ` <span class="indicator-pill orange">${__("test mode - not sent to KRA")}</span>`
		: "";

	const totals = `<table class="table table-bordered">
			<tr><th style="width:40%">${__("Device")}</th><td>${esc(d.device)}${test_pill}</td></tr>
			<tr><th>${__("Invoice total")}</th><td>${esc(d.invoice_total)}</td></tr>
			<tr><th>${__("Would be declared")}</th><td>${esc(d.declared_total)}</td></tr>
		</table>`;

	const rows = Object.entries(d.bands || {});
	const bands = rows.length
		? `<table class="table table-bordered">
				<thead><tr><th>${__("Band")}</th><th>${__("Net")}</th><th>${__("Tax")}</th></tr></thead>
				<tbody>${rows
					.map(([band, v]) => `<tr><td>${esc(band)}</td><td>${esc(v.net)}</td><td>${esc(v.tax)}</td></tr>`)
					.join("")}</tbody>
			</table>`
		: "";

	return concerns + totals + bands;
}

// Every exchange with the device for this invoice, oldest first. The Transmission
// only carries the latest attempt; this is where a retry's history lives.
function history(frm) {
	frappe.call({
		method: "etims_integration.services.response_log.get_history",
		args: { invoice: frm.doc.name },
		freeze: true,
		callback: (r) => {
			const rows = r.message || [];
			const esc = frappe.utils.escape_html;

			const body = rows.length
				? `<table class="table table-bordered" style="font-size:12px">
						<thead><tr>
							<th>#</th><th>${__("When")}</th><th>${__("Outcome")}</th>
							<th>${__("Code")}</th><th>${__("Detail")}</th>
						</tr></thead>
						<tbody>${rows
							.map((x) => {
								const colour = { Signed: "green", Rejected: "red", Unreachable: "orange" }[x.outcome] || "gray";
								const detail = x.cu_invoice_no
									? `${__("Receipt")} ${esc(x.cu_invoice_no)}`
									: esc(x.remedy || x.message || "");
								return `<tr>
									<td>${x.attempt || ""}</td>
									<td>${frappe.datetime.str_to_user(x.creation)}</td>
									<td><span class="indicator-pill ${colour}">${esc(x.outcome || "")}</span>
										${x.is_test ? ` <span class="text-muted">${__("test")}</span>` : ""}</td>
									<td>${esc(x.response_code || x.error_code || "")}</td>
									<td>${detail}</td>
								</tr>`;
							})
							.join("")}</tbody>
					</table>`
				: `<p class="text-muted">${__("Nothing has been sent to the device for this invoice yet.")}</p>`;

			const dialog = new frappe.ui.Dialog({
				title: __("eTIMS Device History"),
				size: "large",
				fields: [{ fieldtype: "HTML", fieldname: "log" }],
				primary_action_label: __("Close"),
				primary_action() {
					this.hide();
				},
			});
			dialog.fields_dict.log.$wrapper.html(body);
			dialog.show();
		},
	});
}
