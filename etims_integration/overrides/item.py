"""
Item hooks.

Registration is queued rather than performed: creating an item must not wait on a
fiscal device, and an item is usually created before anybody fills in its KRA
classification codes.
"""

from etims_integration.mapping import item as item_mapping
from etims_integration.services import item_sync


def on_update(doc, method=None):
	# Only the fields the device actually stores matter. Re-registering on every
	# save would push the whole catalogue at the device for a changed description.
	previous = doc.get_doc_before_save()
	if previous and item_mapping.fingerprint(previous) == item_mapping.fingerprint(doc):
		return

	item_sync.mark_stale(doc.name)
	item_sync.queue_item(doc.name)
