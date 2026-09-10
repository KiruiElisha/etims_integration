"""
Telling "the device said no" apart from "the device was never asked".

Every case here reached something other than ComstoreApiService. Classifying
them as device errors is actively harmful: the remedy points an operator at
ComstoreFC4Api.log on a machine that never received the request, and a retryable
fault gets parked as Blocked (or worse, an unanswerable one gets retried forever).
"""

import unittest

from etims_integration.comstore.errors import (
	EDGE_CHALLENGE,
	looks_like_cdn,
	NOT_THE_API,
	ORIGIN_UNREACHABLE,
	TRANSPORT,
	UNKNOWN,
	classify_http,
	summarise_body,
)

CLOUDFLARE = {"Server": "cloudflare", "CF-RAY": "a38665924d7afbf4-LHR"}

CHALLENGE_BODY = (
	'<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
	'<meta http-equiv="content-security-policy" content="script-src '
	"'nonce-x' https://challenges.cloudflare.com\">"
	"</head><body></body></html>"
)

ORIGIN_DOWN_BODY = "<html><head><title>hedgeinc.co.ke | 530: Origin unreachable</title></head></html>"


class TestEdgeClassification(unittest.TestCase):
	def test_cloudflare_challenge_is_named_as_such(self):
		spec = classify_http(403, CHALLENGE_BODY, CLOUDFLARE)
		self.assertIs(spec, EDGE_CHALLENGE)
		self.assertIn("bot protection", spec.remedy)

	def test_challenge_is_not_retried(self):
		# A browser challenge cannot be answered by an API client, so retrying
		# only burns the budget and delays the operator seeing the real cause.
		self.assertFalse(EDGE_CHALLENGE.retryable)
		self.assertTrue(EDGE_CHALLENGE.blocking)

	def test_cf_mitigated_header_alone_is_enough(self):
		self.assertIs(classify_http(403, "", {"cf-mitigated": "challenge"}), EDGE_CHALLENGE)

	def test_origin_down_is_retryable(self):
		# The tunnel or the host is down; the same request may well work later.
		spec = classify_http(530, ORIGIN_DOWN_BODY, CLOUDFLARE)
		self.assertIs(spec, ORIGIN_UNREACHABLE)
		self.assertTrue(spec.retryable)
		self.assertFalse(spec.blocking)

	def test_all_cloudflare_5xx_origin_codes(self):
		for status in (521, 522, 523, 524, 525, 526, 530):
			self.assertIs(classify_http(status, "", CLOUDFLARE), ORIGIN_UNREACHABLE, f"HTTP {status}")

	def test_html_from_something_else_is_not_the_api(self):
		# Classic wrong-port symptom: a web server answers where the device should.
		spec = classify_http(200, "<html><title>Welcome to nginx!</title></html>", {"Server": "nginx"})
		self.assertIs(spec, NOT_THE_API)
		self.assertIn("host and port", spec.remedy)

	def test_plain_5xx_without_a_cdn_stays_transport(self):
		self.assertIs(classify_http(500, "internal error", {}), TRANSPORT)

	def test_unrecognised_stays_unknown_and_is_not_retried(self):
		spec = classify_http(418, "teapot", {})
		self.assertIs(spec, UNKNOWN)
		self.assertFalse(spec.retryable)


class TestBodySummary(unittest.TestCase):
	def test_html_is_reduced_to_its_title(self):
		# The old behaviour dumped 800 characters of markup into the error.
		self.assertEqual(summarise_body(CHALLENGE_BODY), "HTML page: Just a moment...")
		self.assertLess(len(summarise_body(CHALLENGE_BODY)), 60)

	def test_untitled_html_is_still_identified(self):
		self.assertEqual(summarise_body("<html><body>hi</body></html>"), "HTML page (no title)")

	def test_empty_body(self):
		for empty in ("", None, "   "):
			self.assertEqual(summarise_body(empty), "empty response")

	def test_plain_text_is_passed_through_truncated(self):
		self.assertEqual(summarise_body("device busy"), "device busy")
		self.assertEqual(len(summarise_body("x" * 5000, limit=300)), 300)


# Captured verbatim from hedgeinc.co.ke on 2026-09-09. Cloudflare content-
# negotiates: it returns this problem+json only because our client sends
# `Accept: application/json`, and HTML otherwise. Both have to classify the same.
CLOUDFLARE_530_JSON = {
	"type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1033/",
	"title": "Error 1033: Cloudflare Tunnel error",
	"status": 530,
	"detail": "The host is configured as a Cloudflare Tunnel, but Cloudflare is currently unable to reach it.",
	"error_code": 1033,
	"error_name": "tunnel_error",
	"error_category": "config",
	"ray_id": "a3866b3d0a21fba8",
	"cloudflare_error": True,
	"zone": "hedgeinc.co.ke",
}


class TestCdnJsonDocument(unittest.TestCase):
	"""
	The case that actually bit: a CDN error page that is *valid JSON* and shares
	`error_code` with the Comstore schema. Parsing successfully proves nothing
	about who answered.
	"""

	def test_cloudflare_json_is_recognised_as_cdn(self):
		self.assertTrue(looks_like_cdn(CLOUDFLARE_530_JSON))

	def test_a_real_device_reply_is_not_mistaken_for_cdn(self):
		health = {"status": "OK", "version": "1.6.1", "apiService": "Running",
		          "deviceConnection": "Connected"}
		workflow_error = {"success": False, "message": "E337: NO FIND PLU DATA",
		                  "error_code": -1, "serial_number": "DJV012"}
		for reply in (health, workflow_error):
			self.assertFalse(looks_like_cdn(reply))

	def test_tunnel_error_classifies_as_origin_down_not_device_error(self):
		spec = classify_http(530, "", CLOUDFLARE, data=CLOUDFLARE_530_JSON)
		self.assertIs(spec, ORIGIN_UNREACHABLE)
		self.assertNotIn("ComstoreFC4Api.log", spec.remedy)

	def test_cdn_prose_is_used_for_the_message(self):
		# Far better than the bare "1033" the old path produced.
		summary = summarise_body("", data=CLOUDFLARE_530_JSON)
		self.assertIn("Cloudflare Tunnel error", summary)
		self.assertIn("unable to reach it", summary)

	def test_error_code_alone_does_not_make_it_a_device_reply(self):
		from etims_integration.comstore.client import ComstoreClient

		self.assertFalse(ComstoreClient._looks_like_device_reply(CLOUDFLARE_530_JSON))
		self.assertTrue(ComstoreClient._looks_like_device_reply({"success": True}))
		self.assertTrue(ComstoreClient._looks_like_device_reply({"apiService": "Running"}))


if __name__ == "__main__":
	unittest.main()
