"""
Transport-level tests for the Lusha call.

Two environment failures are easy to reintroduce and expensive to debug:
Cloudflare rejects the default urllib user-agent with error 1010, and a
python.org macOS install ships no CA bundle at all.
"""

import ssl
import unittest
from unittest.mock import patch

from pipeline import enrich


class FakeResponse:
    def __init__(self, body=b'{"results": []}'):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RequestHeaders(unittest.TestCase):
    def send(self):
        captured = {}

        def fake_urlopen(request, **kwargs):
            captured["request"] = request
            captured["kwargs"] = kwargs
            return FakeResponse()

        with patch.dict("os.environ", {"LUSHA_API_KEY": "test-key"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                result = enrich.lusha_post(enrich.LUSHA_CONTACTS_URL, {"contacts": []})
        return result, captured

    def test_an_explicit_user_agent_is_sent(self):
        _, captured = self.send()
        # urllib title-cases header keys on the Request object.
        self.assertEqual(
            captured["request"].get_header("User-agent"), enrich.LUSHA_USER_AGENT
        )
        self.assertNotIn("python-urllib", enrich.LUSHA_USER_AGENT.lower())

    def test_the_api_key_and_json_headers_are_sent(self):
        result, captured = self.send()
        request = captured["request"]
        self.assertEqual(request.get_header("Api_key"), "test-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(result["status"], "ok")

    def test_a_verifying_tls_context_is_passed(self):
        _, captured = self.send()
        context = captured["kwargs"].get("context")
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class TlsContext(unittest.TestCase):
    def test_the_context_has_certificates_loaded(self):
        self.assertTrue(enrich.tls_context().get_ca_certs())

    def test_certifi_is_used_when_the_system_store_is_empty(self):
        empty = ssl.create_default_context(cafile=None, capath=None)
        self.assertFalse(empty.get_ca_certs())

        with patch("ssl.create_default_context", side_effect=[empty, empty]) as mocked:
            enrich.tls_context()

        # Second call supplies the certifi bundle rather than reusing the default.
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("cafile", mocked.call_args.kwargs)
        self.assertTrue(mocked.call_args.kwargs["cafile"])


if __name__ == "__main__":
    unittest.main()
