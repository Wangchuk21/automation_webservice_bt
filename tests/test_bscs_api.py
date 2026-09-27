"""
HTTP-level tests for the read-only BSCS endpoints.

The portal client is stubbed, so these check the request handling, the
disabled/unconfigured gates, and the read-only guarantee without contacting
the billing system.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from config import settings

CUSTOMERS = {
    "success": True,
    "count": 1,
    "customers": [{"Customer Number": "100001", "Name": "Test", "Status": "Active"}],
    "message": "Found 1 customer(s).",
}


class BscsHTTPTest(unittest.TestCase):
    def setUp(self):
        self._orig = {
            "enabled": settings.BSCS_ENABLED,
            "url": settings.BSCS_BASE_URL,
            "user": settings.BSCS_USERNAME,
            "token": settings.API_AUTH_TOKEN,
        }
        settings.BSCS_ENABLED = True
        settings.BSCS_BASE_URL = "http://portal/custcare_cu"
        settings.BSCS_USERNAME = "BTSS-00000"
        # Cleared so these tests exercise routing and validation rather than the
        # token gate, which is covered separately below. A real .env usually has
        # API_AUTH_TOKEN set, which would otherwise 401 every request here.
        settings.API_AUTH_TOKEN = None

        # Captured from the enclosing scope: inside these methods the first
        # parameter is the stub instance, not the test case, so `self.calls`
        # would resolve to the wrong object.
        calls = self.calls = []

        class StubClient:
            def __init__(self, *a, **kw):
                pass

            def test_connection(self):
                calls.append(("test",))
                return {"success": True, "message": "ok", "read_only": True}

            def search_customers(self, **criteria):
                calls.append(("customers", criteria))
                return CUSTOMERS

            def search_contracts(self, customer_id, **criteria):
                calls.append(("contracts", customer_id, criteria))
                return {"success": True, "count": 0, "contracts": [], "message": "none"}

        self._saved = app_module.BSCSClient
        app_module.BSCSClient = StubClient
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.BSCSClient = self._saved
        settings.BSCS_ENABLED = self._orig["enabled"]
        settings.BSCS_BASE_URL = self._orig["url"]
        settings.BSCS_USERNAME = self._orig["user"]
        settings.API_AUTH_TOKEN = self._orig["token"]

    # -- gating -----------------------------------------------------------
    def test_disabled_returns_503(self):
        settings.BSCS_ENABLED = False
        r = self.client.post("/api/v1/bscs/test")
        self.assertEqual(r.status_code, 503)
        self.assertIn("BSCS_ENABLED", r.json()["detail"])

    def test_unconfigured_returns_503(self):
        settings.BSCS_BASE_URL = ""
        r = self.client.post("/api/v1/bscs/test")
        self.assertEqual(r.status_code, 503)
        self.assertIn("not configured", r.json()["detail"])

    def test_requires_api_token(self):
        """
        The BSCS endpoints sit behind the same token gate as the rest of the
        API, so a deployed instance is not an open reader of the billing system.
        """
        settings.API_AUTH_TOKEN = "test-token-for-bscs"
        try:
            r = self.client.post("/api/v1/bscs/test")
            self.assertEqual(r.status_code, 401, "no token presented")
            r = self.client.post("/api/v1/bscs/test", headers={"X-API-Token": "wrong"})
            self.assertEqual(r.status_code, 401, "wrong token presented")
            r = self.client.post("/api/v1/bscs/test", headers={"X-API-Token": "test-token-for-bscs"})
            self.assertEqual(r.status_code, 200, "correct token accepted")
        finally:
            settings.API_AUTH_TOKEN = None

    def test_search_also_requires_token(self):
        settings.API_AUTH_TOKEN = "test-token-for-bscs"
        try:
            r = self.client.post("/api/v1/bscs/customers/search", json={"customer_id": "1"})
            self.assertEqual(r.status_code, 401)
        finally:
            settings.API_AUTH_TOKEN = None

    # -- search validation ------------------------------------------------
    def test_empty_search_refused(self):
        r = self.client.post("/api/v1/bscs/customers/search", json={})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [], "an empty search must not reach the portal")

    def test_blank_only_search_refused(self):
        r = self.client.post("/api/v1/bscs/customers/search",
                             json={"customer_id": "", "full_name": "   "})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_search_passes_criteria_through(self):
        r = self.client.post("/api/v1/bscs/customers/search",
                             json={"customer_id": "100001", "status": "Active"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["count"], 1)
        self.assertEqual(self.calls[0][1], {"customer_id": "100001", "status": "Active"})

    def test_contracts_require_customer_id(self):
        r = self.client.post("/api/v1/bscs/contracts/search", json={"full_name": "Test"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("customer_id", r.json()["detail"])
        self.assertEqual(self.calls, [])

    def test_contracts_pass_customer_id(self):
        r = self.client.post("/api/v1/bscs/contracts/search", json={"customer_id": "100001"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.calls[0][1], "100001")

    # -- read-only guarantee ---------------------------------------------
    def test_no_bscs_write_endpoint_exists(self):
        """We hold no rights to write, so no mutating route may be exposed."""
        import re
        paths = [getattr(r, "path", "") for r in app_module.app.routes]
        bscs = [p for p in paths if "bscs" in p.lower()]
        self.assertTrue(bscs, "expected BSCS routes")
        for p in bscs:
            for verb in ("activate", "deactivate", "suspend", "modify",
                         "create", "delete", "update", "charge", "assign"):
                self.assertNotIn(verb, p.lower(), f"mutating BSCS route exposed: {p}")

    def test_search_uses_post_not_get(self):
        """
        Customer identifiers in a query string land in proxy and access logs,
        so search must be a POST with the criteria in the body.
        """
        for r in app_module.app.routes:
            path = getattr(r, "path", "")
            if "bscs" in path.lower() and "search" in path.lower():
                methods = getattr(r, "methods", set()) or set()
                self.assertNotIn("GET", methods, f"{path} must not accept GET")
                self.assertIn("POST", methods)


if __name__ == "__main__":
    unittest.main(verbosity=2)
