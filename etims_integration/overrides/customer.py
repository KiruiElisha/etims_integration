"""Customer hooks: keep the device's buyer table in step with the KRA PINs we hold."""

from etims_integration.mapping.sanitize import clean_pin
from etims_integration.services import buyer_sync


def on_update(doc, method=None):
	previous = doc.get_doc_before_save()
	if previous and clean_pin(previous.get("tax_id")) == clean_pin(doc.get("tax_id")):
		return

	buyer_sync.queue_customer(doc.name)
