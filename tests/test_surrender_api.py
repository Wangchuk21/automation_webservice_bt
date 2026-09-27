"""
HTTP-level tests for the surrender endpoints.

The panel and registry handlers are replaced with fakes, so these exercise the
request handling, auth gate and evidence plumbing without contacting a live
server or deleting anything real.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from config import settings
import surrender as surrender_mod
from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n" + b"\x00" * 16
PHP_BYTES = b"<?php system($_GET['c']); ?>"


class SurrenderHTTPTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)

        self._orig = {
            "dir": settings.SURRENDER_UPLOAD_DIR,
            "audit": settings.SURRENDER_AUDIT_LOG,
            "token": settings.API_AUTH_TOKEN,
            "req": settings.SURRENDER_REQUIRE_EVIDENCE,
        }
        settings.SURRENDER_UPLOAD_DIR = str(tmp / "surrenders")
        settings.SURRENDER_AUDIT_LOG = str(tmp / "surrenders" / "audit.jsonl")
        settings.SURRENDER_REQUIRE_EVIDENCE = True
        settings.API_AUTH_TOKEN = "test-token"

        # Record what was called instead of performing real deletions.
        # `calls` and `results` are captured from the enclosing scope: inside
        # these functions the first parameter is the provisioner instance, not
        # the test case, so `self` would refer to the wrong object. `results`
        # is a mutable dict so a test can change an outcome in place.
        calls = self.calls = []
        self.results = {
            "hosting": {"success": True, "message": "deleted (fake)"},
            "domain": {"success": True, "message": "deleted (fake)"},
        }
        results = self.results

        def fake_cpanel_delete(_prov, username, reason="", confirm=False):
            calls.append(("cpanel.delete", username))
            return dict(results["hosting"])

        def fake_cpanel_exists(_prov, username):
            calls.append(("cpanel.exists", username))
            return True

        def fake_da_delete(_prov, username, reason="", confirm=False):
            calls.append(("da.delete", username))
            return dict(results["hosting"])

        def fake_da_exists(_prov, username):
            calls.append(("da.exists", username))
            return True

        def fake_nic_delete(_nic, domain, confirm=False):
            calls.append(("nic.delete", domain))
            return dict(results["domain"])

        def fake_nic_find(_nic, base, ext):
            calls.append(("nic.find", base))
            return "9999"

        self._patches = [
            (CPanelProvisioner, "delete_account", fake_cpanel_delete),
            (CPanelProvisioner, "account_exists", fake_cpanel_exists),
            (DirectAdminProvisioner, "delete_account", fake_da_delete),
            (DirectAdminProvisioner, "account_exists", fake_da_exists),
            (app_module.NICClient, "delete_domain", fake_nic_delete),
            (app_module.NICClient, "find_domain_id", fake_nic_find),
        ]
        for cls, name, fn in self._patches:
            self._saved = getattr(cls, name, None)
            setattr(cls, name, fn)

        self.client = TestClient(app_module.app)

    def tearDown(self):
        for cls, name, fn in self._patches:
            setattr(cls, name, self._saved)
        settings.SURRENDER_UPLOAD_DIR = self._orig["dir"]
        settings.SURRENDER_AUDIT_LOG = self._orig["audit"]
        settings.API_AUTH_TOKEN = self._orig["token"]
        settings.SURRENDER_REQUIRE_EVIDENCE = self._orig["req"]
        self._tmp.cleanup()

    def _form(self, **over):
        data = {
            "domain": "client.bt",
            "scope": "both",
            "panel": "cpanel",
            "username": "client",
            "reason": "Surrender letter BT/2026/114",
            "confirm": "true",
        }
        data.update(over)
        return data

    def _headers(self, token="test-token"):
        return {"X-API-Token": token} if token else {}

    # --- auth gate -------------------------------------------------------
    def test_refuses_when_no_token_configured(self):
        settings.API_AUTH_TOKEN = None
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form())
        self.assertEqual(r.status_code, 503)
        self.assertIn("API_AUTH_TOKEN", r.json()["detail"])

    def test_refuses_with_wrong_token(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(), headers=self._headers("nope"))
        self.assertEqual(r.status_code, 401)

    def test_refuses_with_no_token_supplied(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form())
        self.assertEqual(r.status_code, 401)

    def test_nothing_called_when_auth_fails(self):
        self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                         data=self._form(), headers=self._headers("nope"))
        self.assertEqual(self.calls, [], "a rejected request must not reach the servers")

    # --- confirmation & evidence ----------------------------------------
    def test_requires_explicit_confirm(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(confirm="false"), headers=self._headers())
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_requires_evidence(self):
        r = self.client.post("/api/v1/surrenders", data=self._form(), headers=self._headers())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [])

    def test_rejects_disguised_payload(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PHP_BYTES, "application/pdf")},
                             data=self._form(), headers=self._headers())
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.calls, [], "bad evidence must be rejected before any deletion")

    def test_rejects_bad_domain(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(domain="not a domain"), headers=self._headers())
        self.assertIn(r.status_code, (400, 422))
        self.assertEqual(self.calls, [])

    def test_rejects_bad_username(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(username="bad;rm -rf /"), headers=self._headers())
        self.assertIn(r.status_code, (400, 422))
        self.assertEqual(self.calls, [])

    # --- happy paths -----------------------------------------------------
    def test_surrenders_both_in_order(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(), headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["status"], "completed")
        order = [c for c in self.calls if c[0].endswith("delete")]
        self.assertEqual(order, [("cpanel.delete", "client"), ("nic.delete", "client.bt")])

    def test_hosting_only_does_not_touch_registry(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(scope="hosting"), headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(any(c[0] == "nic.delete" for c in self.calls))

    def test_domain_only_does_not_touch_hosting(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(scope="domain", username=""), headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(any(c[0].endswith(".delete") and c[0] != "nic.delete" for c in self.calls))

    def test_partial_failure_returns_409(self):
        self.results["domain"] = {"success": False, "message": "registry refused"}
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(), headers=self._headers())
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "partial")

    def test_jpeg_evidence_accepted(self):
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.jpg", jpeg, "image/jpeg")},
                             data=self._form(), headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)

    def test_evidence_is_stored_and_listed(self):
        self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                         data=self._form(), headers=self._headers())
        r = self.client.get("/api/v1/surrenders", headers=self._headers())
        self.assertEqual(r.status_code, 200)
        entries = r.json()["surrenders"]
        self.assertTrue(entries)
        self.assertTrue(entries[0]["evidence"]["sha256"])

    def test_evidence_download_roundtrip(self):
        self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                         data=self._form(), headers=self._headers())
        sid = self.client.get("/api/v1/surrenders", headers=self._headers()).json()["surrenders"][0]["id"]
        r = self.client.get(f"/api/v1/surrenders/{sid}/evidence", headers=self._headers())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, PDF_BYTES)

    def test_evidence_download_404_for_unknown_id(self):
        r = self.client.get("/api/v1/surrenders/SUR-does-not-exist/evidence", headers=self._headers())
        self.assertEqual(r.status_code, 404)

    def test_preview_is_read_only(self):
        before = len(self.calls)
        r = self.client.post("/api/v1/surrenders/preview", json={"domain": "client.bt", "username": "client",
                                                                "panel": "cpanel", "scope": "both"},
                             headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(any(c[0] == "cpanel.exists" for c in self.calls))
        self.assertFalse(any("delete" in c[0] for c in self.calls), "preview must never delete")
        self.assertGreaterEqual(len(self.calls), before)

    def test_directadmin_manual_action_is_reported(self):
        """DirectAdmin cannot be scripted, so the record must say so."""
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(panel="directadmin"), headers=self._headers())
        self.assertIn(r.status_code, (200, 409))
        self.assertIn("da.delete", [c[0] for c in self.calls])

    def test_operator_header_recorded(self):
        r = self.client.post("/api/v1/surrenders", files={"evidence": ("l.pdf", PDF_BYTES, "application/pdf")},
                             data=self._form(), headers={**self._headers(), "X-Operator": "wangchuk"})
        self.assertEqual(r.json()["operator"], "wangchuk")


if __name__ == "__main__":
    unittest.main(verbosity=2)
