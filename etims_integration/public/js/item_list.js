// Item list: register a selection with eTIMS in one go. Bulk registration is the
// normal case at go-live, when a whole catalogue has to reach the device.
frappe.listview_settings["Item"] = frappe.listview_settings["Item"] || {};

const existing_onload = frappe.listview_settings["Item"].onload;

frappe.listview_settings["Item"].onload = function (listview) {
	if (existing_onload) {
		existing_onload(listview);
	}

	listview.page.add_actions_menu_item(__("Register with eTIMS"), () => {
		const items = listview.get_checked_items(true);
		if (!items.length) {
			frappe.msgprint(__("Select the items to register."));
			return;
		}

		frappe.call({
			method: "etims_integration.services.item_sync.register_items",
			args: { items: items },
			freeze: true,
			freeze_message: __("Registering {0} items...", [items.length]),
			callback: (r) => {
				const result = r.message || {};
				frappe.msgprint({
					title: __("eTIMS Item Registration"),
					indicator: result.failed ? "orange" : "green",
					message: __("Sent: {0}. Skipped: {1}. Failed: {2}.", [
						result.sent || 0,
						result.skipped || 0,
						result.failed || 0,
					]),
				});
			},
		});
	});
};
