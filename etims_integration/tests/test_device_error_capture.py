"""
Reading the device's own account of a failure.

Every case here is one the live device actually produced against
hedgeinc.co.ke, and every one of them used to arrive as ``UNKNOWN:
Unrecognised device error`` -- a classification that is wrong twice over. It
sends the operator to read ComstoreFC4Api.log for a fault the device has
already named in plain English, and it parks a transient device-state fault in
``Blocked``, where nothing retries it.

The three spellings matter because the layer that answers decides which one you
get: the doc's headings use ``E337``, complete-workflow puts a bare ``(Code
337)`` inside a sentence, and the status endpoints put a negative code in a
field and nothing useful in the prose.
"""

import unittest

from etims_integration.comstore.client import ComstoreClient, _count
from etims_integration.comstore.errors import (
	DEVICE_DISCONNECTED,
	DEVICE_FAULT,
	DUPLICATE_INVOICE,
	ERROR_CODES,
	UNKNOWN,
	classify,
	describe,
)

# Verbatim from ETIMS-RESP-2609-00005, which the app filed as UNKNOWN.
LIVE_E337 = "Signature generation failed: NO FIND PLU DATA (Code 337) after 1 attempts"

# Verbatim from a Test Connection against the same host.
LIVE_MINUS_99 = "Failed to read invoice status from device | Error Code -99 | -99"


class TestCodeRecovery(unittest.TestCase):
	def test_bare_code_in_prose_is_the_documented_error(self):
		spec = classify(LIVE_E337)
		self.assertEqual(spec.code, "E337")
		self.assertIn("not registered on the device", spec.remedy)

	def test_prefixed_and_bare_spellings_agree(self):
		self.assertIs(classify("E337: NO FIND PLU DATA"), classify(LIVE_E337))

	def test_the_phrase_alone_is_enough(self):
		# Some firmware sends the phrase and no code at all.
		self.assertEqual(classify("NO FIND PLU DATA").code, "E337")
		self.assertEqual(classify("PLU SALES SUM ERROR").code, "E341")
		self.assertEqual(classify("taxrate is error").code, "E321")

	def test_the_devices_log_folder_spelling_is_a_code(self):
		# The unsigned log is filed under status_325 (doc, figure 2).
		self.assertEqual(classify("saved to unsigned/20250730/status_325").code, "E325")

	def test_a_number_that_is_not_a_code_is_not_read_as_one(self):
		# This is why a bare number only counts inside a code context: the trader
		# invoice number is right there in the message, and 1000000341 must not
		# become E341 with a remedy for a tax defect the invoice does not have.
		self.assertIs(classify("Signature generation failed for invoice 1000000341"), UNKNOWN)

	def test_error_code_field_is_consulted_alongside_the_message(self):
		# The status endpoints put the code in a field and prose in the message.
		spec = classify("Failed to initiate manual invoice upload", error_code=-1)
		self.assertIs(spec, DEVICE_FAULT)


class TestServiceFaultsAreRetried(unittest.TestCase):
	def test_negative_code_is_a_device_fault_not_a_rejection(self):
		self.assertIs(classify(LIVE_MINUS_99), DEVICE_FAULT)

	def test_device_faults_are_retryable_and_do_not_block(self):
		# The whole point of the reclassification: -99 is "I could not read from
		# the device", which is exactly the fault a retry exists for.
		self.assertTrue(DEVICE_FAULT.retryable)
		self.assertFalse(DEVICE_FAULT.blocking)

	def test_disconnected_still_wins_over_a_negative_code(self):
		spec = classify("Device not initialized | Error Code -1")
		self.assertIs(spec, DEVICE_DISCONNECTED)

	def test_payload_rejections_are_still_never_retried(self):
		for code, spec in ERROR_CODES.items():
			self.assertFalse(spec.retryable, f"{code} must not be auto-retried")


class TestDuplicateDetection(unittest.TestCase):
	def test_duplicate_is_named_and_blocked(self):
		spec = classify("Duplicate invoice number detected")
		self.assertIs(spec, DUPLICATE_INVOICE)
		self.assertFalse(spec.retryable)
		self.assertTrue(spec.blocking)

	def test_the_remedy_forbids_renumbering(self):
		# Duplicate prevention is mandatory and cannot be disabled (doc, p. 44).
		# Getting past it with a fresh number declares the same sale to KRA twice.
		self.assertIn("twice", DUPLICATE_INVOICE.remedy)


class TestMessageAssembly(unittest.TestCase):
	def test_the_code_is_not_repeated_three_times(self):
		# The endpoint says the same thing in message, error_message and
		# error_code; joining them blindly produced "... | Error Code -99 | -99".
		message = ComstoreClient._message(
			{
				"message": "Failed to read invoice status from device",
				"error_message": "Error Code -99",
				"error_code": -99,
			}
		)
		self.assertEqual(message.count("-99"), 1)
		self.assertIn("Failed to read invoice status from device", message)

	def test_a_silent_reply_still_says_something(self):
		self.assertIn("No message", ComstoreClient._message({}))

	def test_error_code_is_found_whatever_its_spelling(self):
		self.assertEqual(ComstoreClient._error_code({"ErrorCode": -1}), -1)
		self.assertIsNone(ComstoreClient._error_code({"error_code": None}))


class TestCounters(unittest.TestCase):
	def test_counters_survive_every_shape_the_firmware_sends(self):
		# Documented as an integer; arrives as a string, a float or null. A bare
		# int() on any of those raises outside the ComstoreError contract, and
		# the caller gets a crash instead of a fault with a remedy.
		for value, expected in (("82989", 82989), (82989, 82989), (80.0, 80), (None, 0), ("", 0), ("n/a", 0)):
			self.assertEqual(_count(value), expected, f"{value!r}")


class TestDescribe(unittest.TestCase):
	def test_a_described_error_can_be_acted_on(self):
		described = describe(DEVICE_FAULT)
		self.assertEqual(described["code"], "DEVICE_FAULT")
		self.assertTrue(described["retryable"])
		self.assertIn("remedy", described)
