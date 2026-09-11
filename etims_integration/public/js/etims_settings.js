// eTIMS Settings: the demo-data buttons.
//
// The catalogue jobs run in the background -- 479 Items outlives an HTTP request,
// and on Frappe Cloud the proxy closes the connection long before the work is
// done -- so their result arrives over realtime rather than as a call response.
// The per-band demo items are six documents and answer in place.
frappe.ui.form.on("ETIMS Settings", {
	refresh(frm) {
		const group = __("Demo Data");

		frm.add_custom_button(__("Load Demo Catalogue"), () => load_catalogue(), group);
		frm.add_custom_button(__("Remove Demo Catalogue"), () => remove_catalogue(), group);
		frm.add_custom_button(__("Create Demo Items (one per VAT band)"), () => demo_items("create"), group);
		frm.add_custom_button(__("Delete Demo Items"), () => demo_items("delete"), group);

		listen(frm);
	},
});

// Bound once per form. refresh() fires on every save and reload, and a listener
// added each time would report one finished job as many identical dialogs.
function listen(frm) {
	if (frm.__etims_demo_bound) {
		return;
	}
	frm.__etims_demo_bound = true;
	frappe.realtime.on("etims_demo_catalogue", (data) => show_catalogue_result(data || {}));
}

function load_catalogue() {
	const dialog = new frappe.ui.Dialog({
		title: __("Load Demo Catalogue"),
		fields: [
			{
				fieldname: "limit",
				label: __("How many items"),
				fieldtype: "Int",
				description: __(
					"Leave blank to load all 479. Items that already exist are skipped, so a partial load is topped up by running this again."
				),
			},
			{
				fieldtype: "HTML",
				options: `<div class="text-muted small">${__(
					"Every item on this list is standard rated (16%). For the VAT bands, use Create Demo Items instead."
				)}</div>`,
			},
		],
		primary_action_label: __("Load"),
		primary_action(values) {
			dialog.hide();
			queue("load", values.limit);
		},
	});
	dialog.show();
}

function remove_catalogue() {
	frappe.confirm(
		__(
			"Delete every demo catalogue item? Any that a transaction already points at are kept, not deleted."
		),
		() => queue("delete")
	);
}

function queue(action, limit) {
	frappe.call({
		method: "etims_integration.seed.enqueue_demo_catalogue",
		args: { action: action, limit: limit || null },
		freeze: true,
		freeze_message: __("Queueing..."),
		callback: () => {
			frappe.show_alert({
				message: __("Running in the background. You will be told here when it finishes."),
				indicator: "blue",
			});
		},
	});
}

function show_catalogue_result(data) {
	const esc = frappe.utils.escape_html;

	if (data.error) {
		frappe.msgprint({
			title: __("Demo Catalogue"),
			indicator: "red",
			message: esc(data.error),
		});
		return;
	}

	const result = data.result || {};
	const lines = [];

	if (data.action === "delete") {
		lines.push(__("Deleted: {0}", [result.deleted || 0]));
		if (result.kept_count) {
			lines.push(
				__("Kept because a transaction uses them: {0}", [result.kept_count]) +
					list(result.kept_because_in_use, esc)
			);
		}
		if (result.remaining) {
			lines.push(__("Still on the site: {0}", [result.remaining]));
		}
	} else {
		lines.push(__("Created: {0}", [result.created || 0]));
		lines.push(__("Skipped, already present: {0}", [result.skipped_existing || 0]));
		lines.push(__("Total demo items on this site: {0}", [result.total_on_site || 0]));
	}

	if (result.problem_count || (result.problems || []).length) {
		lines.push(
			`<b>${__("Problems: {0}", [result.problem_count || result.problems.length])}</b>` +
				list(result.problems, esc)
		);
	}

	const failed = Boolean(result.problem_count || (result.problems || []).length);

	frappe.msgprint({
		title: data.action === "delete" ? __("Demo Catalogue Removed") : __("Demo Catalogue Loaded"),
		indicator: failed ? "orange" : "green",
		message: lines.map((l) => `<div style="margin-bottom:4px">${l}</div>`).join(""),
	});
}

function list(items, esc) {
	if (!items || !items.length) {
		return "";
	}
	return `<ul style="margin-top:4px">${items.map((i) => `<li>${esc(String(i))}</li>`).join("")}</ul>`;
}

function demo_items(action) {
	const method =
		action === "create"
			? "etims_integration.seed.create_demo_items"
			: "etims_integration.seed.delete_demo_items";

	const run = () =>
		frappe.call({
			method: method,
			freeze: true,
			freeze_message: __("Working..."),
			callback: (r) => {
				const result = r.message || {};
				const esc = frappe.utils.escape_html;
				const parts = [];

				if (action === "create") {
					parts.push(__("Created: {0}", [(result.created || []).length]));
					parts.push(__("Already present: {0}", [(result.skipped || []).length]));
					if (result.next) {
						parts.push(`<div class="text-muted" style="margin-top:6px">${esc(result.next)}</div>`);
					}
				} else {
					parts.push(__("Deleted: {0}", [(result.deleted || []).length]));
					if ((result.kept_because_in_use || []).length) {
						parts.push(
							__("Kept because a transaction uses them: {0}", [
								result.kept_because_in_use.length,
							]) + list(result.kept_because_in_use, esc)
						);
					}
				}

				frappe.msgprint({
					title: __("Demo Items"),
					indicator: "green",
					message: parts.map((p) => `<div style="margin-bottom:4px">${p}</div>`).join(""),
				});
			},
		});

	if (action === "delete") {
		frappe.confirm(__("Delete the per-band demo items?"), run);
	} else {
		run();
	}
}
