// Bulk retry from the list view. The single-invoice fix is rarely the real case:
// registering one item on the device, or bringing the device back up, unblocks
// every invoice that touched it, and reopening those one at a time is the
// difference between a minute's work and an afternoon's.
frappe.listview_settings["ETIMS Transmission"] = {
	add_fields: ["status", "error_summary"],

	get_indicator(doc) {
		const colours = {
			Signed: "green",
			Queued: "blue",
			Sending: "blue",
			Failed: "orange",
			Blocked: "red",
			Cancelled: "gray",
		};
		return [__(doc.status), colours[doc.status] || "gray", `status,=,${doc.status}`];
	},

	onload(listview) {
		listview.page.add_actions_menu_item(__("Retry Selected"), () => retry(listview), false);
	},
};

function retry(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		frappe.msgprint(__("Select the transmissions to retry."));
		return;
	}

	frappe.confirm(
		__("Resend {0} transmission(s) to the device?", [names.length]),
		() => {
			frappe.call({
				method: "etims_integration.etims_integration.doctype.etims_transmission.etims_transmission.retry_transmissions",
				args: { names: names },
				freeze: true,
				freeze_message: __("Queueing..."),
				callback: (r) => {
					const result = r.message || {};
					const queued = (result.queued || []).length;
					const skipped = Object.entries(result.skipped || {});

					// Say what did NOT go, and why. A bulk action that only reports
					// its successes is how a blocked invoice gets forgotten.
					let message = __("{0} queued for resend.", [queued]);
					if (skipped.length) {
						message += `<br><br><b>${__("Not queued")}</b><ul>${skipped
							.map(
								([name, why]) =>
									`<li>${frappe.utils.escape_html(name)} — ${frappe.utils.escape_html(why)}</li>`
							)
							.join("")}</ul>`;
					}

					frappe.msgprint({
						title: __("eTIMS Retry"),
						indicator: skipped.length ? "orange" : "blue",
						message: message,
					});
					listview.refresh();
				},
			});
		}
	);
}
