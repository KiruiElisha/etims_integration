app_name = "etims_integration"
app_title = "ETIMS Integration"
app_publisher = "Rono"
app_description = "KRA eTIMS integration for ERPNext, via the Comstore Smart VSCU service"
app_email = "ronoelisha625@gmail.com"
app_license = "mit"

required_apps = ["erpnext"]

# ---------------------------------------------------------------------- assets

doctype_js = {
	"Sales Invoice": "public/js/sales_invoice.js",
	"ETIMS Device": "public/js/etims_device.js",
}

doctype_list_js = {
	"Item": "public/js/item_list.js",
	"ETIMS Transmission": "public/js/etims_transmission_list.js",
}

# Both eTIMS records point at the invoice, so the invoice should point back.
override_doctype_dashboards = {
	"Sales Invoice": "etims_integration.overrides.sales_invoice_dashboard.get_data",
}

add_to_apps_screen = [
	{
		"name": "etims_integration",
		"logo": "/assets/etims_integration/images/etims.svg",
		"title": "eTIMS",
		"route": "/app/etims",
	}
]

# ----------------------------------------------------------------- lifecycle

after_install = "etims_integration.install.after_install"
# Custom fields and code masters are reconciled on every migrate, so a site that
# skipped a release still ends up with the fields the code expects.
after_migrate = "etims_integration.install.after_migrate"

# ------------------------------------------------------------------- document

doc_events = {
	"Sales Invoice": {
		"validate": "etims_integration.overrides.sales_invoice.validate",
		"on_submit": "etims_integration.overrides.sales_invoice.on_submit",
		"before_cancel": "etims_integration.overrides.sales_invoice.before_cancel",
		"on_cancel": "etims_integration.overrides.sales_invoice.on_cancel",
	},
	"Item": {
		"on_update": "etims_integration.overrides.item.on_update",
	},
	"Customer": {
		"on_update": "etims_integration.overrides.customer.on_update",
	},
}

# ------------------------------------------------------------------ scheduler

scheduler_events = {
	"cron": {
		# Transport faults clear on their own timescale; this only picks up
		# transmissions whose own backoff has already elapsed.
		"*/15 * * * *": [
			"etims_integration.services.transmit.retry_failed",
		],
	},
	"hourly_long": [
		# A signature is not compliance: this is what notices the device is sitting
		# on invoices KRA has never seen.
		"etims_integration.services.reconcile.reconcile_devices",
		"etims_integration.services.transmit.queue_stale",
	],
	"daily_long": [
		"etims_integration.services.item_sync.sync_pending",
		"etims_integration.services.buyer_sync.sync_pending",
	],
}

# --------------------------------------------------------------------- jinja

jinja = {
	"methods": [
		"etims_integration.utils.print_format.etims_qr_code",
		"etims_integration.utils.print_format.etims_receipt_details",
	]
}

# ------------------------------------------------------------------ fixtures

# Nothing is shipped as a fixture, deliberately.
#
#  * Custom Fields are created by install.setup(). Fixtures on *standard* doctypes
#    collide with other apps that touch the same doctype on migrate, and cannot
#    express "create only if absent".
#  * The app's own DocTypes and its Print Format live under etims_integration/ and
#    are installed by bench migrate. Exporting those as fixtures too would leave
#    two competing definitions racing on every migrate.
fixtures = []
