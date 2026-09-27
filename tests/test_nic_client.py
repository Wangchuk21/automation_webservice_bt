"""
Regression tests for the nic.bt.bt HTTP layer.

The retry helper added for the slow registry is the kind of change that passes
every test and still breaks production: routing a request through the bare
`requests` module instead of the authenticated session drops the login cookie,
the CSRF token and the TLS verification setting, and the portal then rejects
the login. These tests pin that down.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

import nic_client
from nic_client import REGISTRY_TIMEOUT, NICClient, _request_with_retry


class FakeResponse:
    def __init__(self, status_code=200, text="", url="https://nic.bt.bt/"):
        self.status_code = status_code
        self.text = text
        self.url = url


class FakeSession:
    """Stands in for requests.Session, recording how it was used."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []
        self.verify = "/etc/ssl/custom-ca.pem"

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.behaviour(len(self.calls))
        if isinstance(result, Exception):
            raise result
        return result

    # The bare module-level helper must never be reachable from here.
    def get(self, *a, **kw):  # pragma: no cover - guards against regressions
        raise AssertionError("session.get should not be used by the retry helper")


class TestRetryHelper(unittest.TestCase):
    def test_uses_the_session_not_the_bare_requests_module(self):
        """The bug this file exists for: a module-level call loses the session."""
        session = FakeSession(lambda n: FakeResponse(200))
        with unittest.mock.patch.object(
            requests, "request", side_effect=AssertionError("bare requests.request used")
        ):
            _request_with_retry(session, "POST", "https://nic.bt.bt/domain")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][0], "POST")

    def test_retries_timeouts_then_succeeds(self):
        def behaviour(n):
            if n < 3:
                return requests.Timeout("simulated")
            return FakeResponse(200)
        session = FakeSession(behaviour)
        with unittest.mock.patch.object(nic_client.time, "sleep", lambda s: None):
            resp = _request_with_retry(session, "POST", "https://nic.bt.bt/domain")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(session.calls), 3)

    def test_retries_connection_errors(self):
        def behaviour(n):
            if n == 1:
                return requests.ConnectionError("reset")
            return FakeResponse(200)
        session = FakeSession(behaviour)
        with unittest.mock.patch.object(nic_client.time, "sleep", lambda s: None):
            resp = _request_with_retry(session, "POST", "https://nic.bt.bt/domain")
        self.assertEqual(resp.status_code, 200)

    def test_does_not_retry_http_error_responses(self):
        """A 4xx/5xx is a decision the server made; repeating it changes nothing."""
        session = FakeSession(lambda n: FakeResponse(422))
        resp = _request_with_retry(session, "POST", "https://nic.bt.bt/domain")
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(len(session.calls), 1, "an HTTP error must not be retried")

    def test_raises_after_exhausting_attempts(self):
        session = FakeSession(lambda n: requests.Timeout("simulated"))
        with unittest.mock.patch.object(nic_client.time, "sleep", lambda s: None):
            with self.assertRaises(requests.Timeout):
                _request_with_retry(session, "POST", "https://nic.bt.bt/domain", attempts=2)
        self.assertEqual(len(session.calls), 2)


class TestRegistryTimeouts(unittest.TestCase):
    def test_timeout_is_generous_enough_for_the_slow_registry(self):
        """nic.bt.bt has been observed taking ~40s; the old flat 15s failed."""
        self.assertGreaterEqual(REGISTRY_TIMEOUT, 45)

    def test_no_short_timeouts_remain_in_the_client(self):
        source = Path(nic_client.__file__).read_text()
        for bad in ("timeout=15", "timeout=10", "timeout=20)"):
            self.assertNotIn(bad, source, f"{bad} is too tight for nic.bt.bt")


class TestLoginUsesSession(unittest.TestCase):
    def test_login_posts_through_the_session(self):
        client = NICClient(base_url="https://nic.bt.bt", username="u", password="p")
        seen = {}

        def fake_request(method, url, **kwargs):
            seen.update({"method": method, "url": url, "kwargs": kwargs, "session": client.session})
            return FakeResponse(200, text='name="_token" value="tok123"')

        client.session.request = fake_request
        client.session.get = lambda *a, **kw: FakeResponse(200, text='name="_token" value="tok123"')

        ok, _ = client.login()
        self.assertTrue(ok)
        self.assertIs(seen.get("session"), client.session)
        self.assertEqual(seen["kwargs"].get("timeout"), REGISTRY_TIMEOUT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
