"""
Two faults that only appear once the device is reached over the public internet
rather than on the LAN, both found against the hosted test service.
"""

import unittest

from etims_integration.comstore.client import normalise_base_url
from etims_integration.comstore.errors import (
	AUTH_REJECTED,
	EDGE_CHALLENGE,
	classify_http,
	looks_like_auth_rejection,
)

CLOUDFLARE = {"Server": "cloudflare", "CF-RAY": "a38dfcec8cb991f2-AMS"}

# Verbatim from https://hedgeinc.co.ke/api/buyers/TEST with a wrong key.
BAD_KEY_BODY = '{"error":"Invalid API key.","timestamp":"2026-09-10 14:34:00"}'
NO_KEY_BODY = '{"error":"Missing X-API-KEY header.","timestamp":"2026-09-10 14:34:08"}'


class TestAuthRejection(unittest.TestCase):
	"""
	The device is behind Cloudflare, so a bare "401 via a CDN" rule reports a
	wrong key as a bot challenge -- non-retryable, blocking, and with a remedy
	that sends an operator to ask their host about WAF rules they do not need to
	touch.
	"""

	def test_invalid_key_is_not_a_challenge(self):
		data = {"error": "Invalid API key.", "timestamp": "2026-09-10 14:34:00"}
		spec = classify_http(401, BAD_KEY_BODY, CLOUDFLARE, data)
		self.assertIs(spec, AUTH_REJECTED)

	def test_missing_header_is_an_auth_fault(self):
		data = {"error": "Missing X-API-KEY header.", "timestamp": "2026-09-10 14:34:08"}
		spec = classify_http(401, NO_KEY_BODY, CLOUDFLARE, data)
		self.assertIs(spec, AUTH_REJECTED)

	def test_auth_fault_is_blocking_and_not_retryable(self):
		# Re-sending the same wrong key a hundred times produces a hundred 401s.
		self.assertFalse(AUTH_REJECTED.retryable)
		self.assertTrue(AUTH_REJECTED.blocking)

	def test_real_challenge_still_wins(self):
		"""An HTML challenge does not parse to a dict, so it stays a challenge."""
		body = '<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>'
		self.assertIs(classify_http(403, body, CLOUDFLARE, None), EDGE_CHALLENGE)

	def test_cdn_json_is_not_mistaken_for_an_auth_fault(self):
		data = {"error_name": "challenge", "ray_id": "abc", "zone": "hedgeinc.co.ke"}
		self.assertFalse(looks_like_auth_rejection(403, data, "CHALLENGE"))

	def test_only_401_and_403(self):
		self.assertFalse(looks_like_auth_rejection(500, {"error": "API KEY"}, "API KEY"))


class TestBaseUrl(unittest.TestCase):
	def test_public_host_drops_the_device_port(self):
		"""The bug: :4000 is firewalled on the hosted service."""
		self.assertEqual(
			normalise_base_url("hedgeinc.co.ke:4000", 4000, True), "https://hedgeinc.co.ke"
		)

	def test_public_host_without_a_port_is_unchanged(self):
		self.assertEqual(normalise_base_url("hedgeinc.co.ke", 4000, True), "https://hedgeinc.co.ke")

	def test_lan_ip_keeps_its_port(self):
		self.assertEqual(normalise_base_url("192.168.1.5", 4000), "http://192.168.1.5:4000")
		self.assertEqual(normalise_base_url("192.168.1.5:9000", 4000), "http://192.168.1.5:9000")

	def test_localhost_keeps_its_port(self):
		self.assertEqual(normalise_base_url("localhost", 4000), "http://localhost:4000")
		self.assertEqual(normalise_base_url("localhost:8080", 4000), "http://localhost:8080")

	def test_lan_names_keep_their_port(self):
		"""A named device on the LAN is the case that must not regress."""
		self.assertEqual(normalise_base_url("vscu.local:4000", 4000), "http://vscu.local:4000")
		self.assertEqual(normalise_base_url("desktop-7f2:4000", 4000), "http://desktop-7f2:4000")

	def test_full_url_is_the_escape_hatch(self):
		"""Someone who really does expose :4000 publicly types the scheme."""
		self.assertEqual(
			normalise_base_url("https://hedgeinc.co.ke:4000", 4000), "https://hedgeinc.co.ke:4000"
		)

	def test_path_survives_the_port_being_dropped(self):
		self.assertEqual(normalise_base_url("hedgeinc.co.ke:4000/vscu", 4000, True), "https://hedgeinc.co.ke/vscu")


if __name__ == "__main__":
	unittest.main()
