"""
Tests for the surrender path.

These cover the parts that must not be wrong: evidence validation, the
hosting-before-domain ordering, the audit trail, and the fail-closed auth
gate. None of them touch a live server -- the panel and registry steps are
injected as fakes, which is why perform_surrender() takes them as arguments.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from config import settings
import surrender
from surrender import (
    SurrenderError,
    evidence_path,
    get_audit,
    list_audits,
    perform_surrender,
    preview_surrender,
    store_evidence,
)

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n" + b"\x00" * 32
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
PHP_BYTES = b"<?php system($_GET['c']); ?>"


class SurrenderTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        # Redirect all surrender output into a throwaway directory.
        self._orig_dir = settings.SURRENDER_UPLOAD_DIR
        self._orig_audit = settings.SURRENDER_AUDIT_LOG
        self._orig_max = settings.SURRENDER_MAX_UPLOAD_MB
        self._orig_req = settings.SURRENDER_REQUIRE_EVIDENCE
        settings.SURRENDER_UPLOAD_DIR = str(self.tmp_path / "surrenders")
        settings.SURRENDER_AUDIT_LOG = str(self.tmp_path / "surrenders" / "audit.jsonl")
        settings.SURRENDER_MAX_UPLOAD_MB = 1
        settings.SURRENDER_REQUIRE_EVIDENCE = True

    def tearDown(self):
        settings.SURRENDER_UPLOAD_DIR = self._orig_dir
        settings.SURRENDER_AUDIT_LOG = self._orig_audit
        settings.SURRENDER_MAX_UPLOAD_MB = self._orig_max
        settings.SURRENDER_REQUIRE_EVIDENCE = self._orig_req
        self._tmp.cleanup()


class TestEvidenceValidation(SurrenderTestBase):
    def test_accepts_real_pdf(self):
        ev = store_evidence(io.BytesIO(PDF_BYTES), "surrender-letter.pdf")
        self.assertTrue(ev.stored_name.endswith(".pdf"))
        self.assertTrue((self.tmp_path / "surrenders" / ev.stored_name).is_file())
        self.assertEqual(ev.content_type, "application/pdf")

    def test_accepts_real_jpeg(self):
        ev = store_evidence(io.BytesIO(JPEG_BYTES), "letter.jpg")
        self.assertEqual(ev.content_type, "image/jpeg")

    def test_rejects_php_disguised_as_pdf(self):
        """The whole point of magic-byte sniffing: a renamed payload is refused."""
        with self.assertRaises(SurrenderError) as ctx:
            store_evidence(io.BytesIO(PHP_BYTES), "innocent.pdf")
        self.assertIn("does not look like", str(ctx.exception))

    def test_rejected_upload_leaves_nothing_on_disk(self):
        with self.assertRaises(SurrenderError):
            store_evidence(io.BytesIO(PHP_BYTES), "innocent.pdf")
        stored = list((self.tmp_path / "surrenders").glob("*")) if (self.tmp_path / "surrenders").exists() else []
        self.assertEqual(stored, [], "a rejected upload must not be left behind")

    def test_rejects_disallowed_extension(self):
        with self.assertRaises(SurrenderError):
            store_evidence(io.BytesIO(b"whatever"), "letter.png")
        with self.assertRaises(SurrenderError):
            store_evidence(io.BytesIO(b"whatever"), "letter.exe")

    def test_rejects_empty_file(self):
        with self.assertRaises(SurrenderError) as ctx:
            store_evidence(io.BytesIO(b""), "letter.pdf")
        self.assertIn("empty", str(ctx.exception))

    def test_rejects_oversized_upload(self):
        big = b"%PDF-" + b"A" * (2 * 1024 * 1024)  # cap is 1 MB in these tests
        with self.assertRaises(SurrenderError) as ctx:
            store_evidence(io.BytesIO(big), "huge.pdf")
        self.assertIn("exceeds", str(ctx.exception))

    def test_traversal_filename_cannot_escape_upload_dir(self):
        ev = store_evidence(io.BytesIO(PDF_BYTES), "../../../../etc/cron.d/evil.pdf")
        resolved = (self.tmp_path / "surrenders" / ev.stored_name).resolve()
        self.assertEqual(resolved.parent, (self.tmp_path / "surrenders").resolve())

    def test_original_name_is_preserved_for_audit_only(self):
        ev = store_evidence(io.BytesIO(PDF_BYTES), "My Letter (final).pdf")
        self.assertEqual(ev.original_name, "My Letter (final).pdf")
        self.assertNotIn(" ", ev.stored_name)

    def test_evidence_path_blocks_traversal(self):
        with self.assertRaises(SurrenderError):
            evidence_path("../../../etc/passwd")
        with self.assertRaises(SurrenderError):
            evidence_path("no-such-file.pdf")


class TestOrchestration(SurrenderTestBase):
    def _evidence(self):
        return store_evidence(io.BytesIO(PDF_BYTES), "letter.pdf")

    def test_hosting_runs_before_domain(self):
        """The domain is the harder asset to restore, so it goes last."""
        order = []
        record = perform_surrender(
            domain="client.bt", scope="both", username="client",
            evidence=self._evidence(),
            hosting_delete=lambda u: (order.append("hosting"), {"success": True, "message": "ok"})[1],
            domain_delete=lambda d: (order.append("domain"), {"success": True, "message": "ok"})[1],
        )
        self.assertEqual(order, ["hosting", "domain"])
        self.assertEqual(record["status"], "completed")

    def test_scope_hosting_only_skips_registry(self):
        called = []
        record = perform_surrender(
            domain="client.bt", scope="hosting", username="client",
            evidence=self._evidence(),
            hosting_delete=lambda u: {"success": True, "message": "ok"},
            domain_delete=lambda d: called.append(d) or {"success": True},
        )
        self.assertEqual(called, [])
        self.assertEqual([a["target"] for a in record["actions"]], ["hosting"])

    def test_scope_domain_only_skips_hosting(self):
        called = []
        record = perform_surrender(
            domain="client.bt", scope="domain", username="client",
            evidence=self._evidence(),
            hosting_delete=lambda u: called.append(u) or {"success": True},
            domain_delete=lambda d: {"success": True, "message": "ok"},
        )
        self.assertEqual(called, [])
        self.assertEqual([a["target"] for a in record["actions"]], ["domain"])

    def test_registry_failure_does_not_hide_hosting_result(self):
        record = perform_surrender(
            domain="client.bt", scope="both", username="client",
            evidence=self._evidence(),
            hosting_delete=lambda u: {"success": True, "message": "hosting gone"},
            domain_delete=lambda d: {"success": False, "message": "registry said no"},
        )
        self.assertEqual(record["status"], "partial")
        by_target = {a["target"]: a for a in record["actions"]}
        self.assertTrue(by_target["hosting"]["success"])
        self.assertFalse(by_target["domain"]["success"])
        self.assertIn("registry said no", by_target["domain"]["message"])

    def test_raising_handler_is_contained(self):
        def boom(_):
            raise RuntimeError("kaboom")
        record = perform_surrender(
            domain="client.bt", scope="hosting", username="client",
            evidence=self._evidence(), hosting_delete=boom,
        )
        self.assertEqual(record["status"], "failed")
        self.assertIn("kaboom", record["actions"][0]["message"])

    def test_evidence_required_by_default(self):
        with self.assertRaises(SurrenderError) as ctx:
            perform_surrender(domain="client.bt", scope="hosting", username="client")
        self.assertIn("evidence", str(ctx.exception).lower())

    def test_hosting_scope_requires_username(self):
        with self.assertRaises(SurrenderError):
            perform_surrender(
                domain="client.bt", scope="hosting", evidence=self._evidence(),
                hosting_delete=lambda u: {"success": True},
            )

    def test_invalid_scope_rejected(self):
        with self.assertRaises(SurrenderError):
            perform_surrender(domain="client.bt", scope="everything", username="c")


class TestAuditTrail(SurrenderTestBase):
    def test_record_is_written_and_retrievable(self):
        ev = store_evidence(io.BytesIO(PDF_BYTES), "letter.pdf")
        rec = perform_surrender(
            domain="client.bt", scope="hosting", username="client", reason="Customer resigned",
            evidence=ev, operator="wangchuk",
            hosting_delete=lambda u: {"success": True, "message": "deleted"},
        )
        self.assertIsNotNone(get_audit(rec["id"]))
        fetched = get_audit(rec["id"])
        self.assertEqual(fetched["reason"], "Customer resigned")
        self.assertEqual(fetched["evidence"]["sha256"], ev.sha256)

    def test_started_record_written_before_deletion(self):
        """A crash mid-surrender must still leave evidence it was attempted."""
        ev = store_evidence(io.BytesIO(PDF_BYTES), "letter.pdf")

        def explode(_):
            raise SystemExit("process dies")

        with self.assertRaises(SystemExit):
            perform_surrender(
                domain="client.bt", scope="hosting", username="client",
                evidence=ev, hosting_delete=explode,
            )
        statuses = [r.get("status") for r in list_audits()]
        self.assertIn("started", statuses)

    def test_audit_lines_are_valid_json(self):
        import json
        ev = store_evidence(io.BytesIO(PDF_BYTES), "letter.pdf")
        perform_surrender(
            domain="client.bt", scope="hosting", username="client", evidence=ev,
            hosting_delete=lambda u: {"success": True},
        )
        raw = Path(settings.SURRENDER_AUDIT_LOG).read_text().strip().splitlines()
        for line in raw:
            json.loads(line)


class TestPreview(SurrenderTestBase):
    def test_preview_reports_presence(self):
        out = preview_surrender(
            "client.bt", "both", "client",
            hosting_exists=lambda u: True,
            domain_exists=lambda d: False,
        )
        by_target = {s["target"]: s for s in out["steps"]}
        self.assertTrue(by_target["hosting"]["present"])
        self.assertFalse(by_target["domain"]["present"])

    def test_preview_changes_nothing(self):
        calls = []
        preview_surrender(
            "client.bt", "both", "client",
            hosting_exists=lambda u: calls.append(u) or True,
            domain_exists=lambda d: True,
        )
        self.assertEqual(len(calls), 1, "preview must only read")

    def test_preview_rejects_bad_scope(self):
        with self.assertRaises(SurrenderError):
            preview_surrender("client.bt", "nonsense", "client")


class TestDestructiveAuthGate(SurrenderTestBase):
    def test_fails_closed_when_no_token_configured(self):
        """An open-API instance must not be able to destroy customer services."""
        import app as app_module
        orig = settings.API_AUTH_TOKEN
        settings.API_AUTH_TOKEN = None
        try:
            with self.assertRaises(HTTPExceptionError) as ctx:
                app_module.require_token_for_destructive()
            self.assertEqual(ctx.exception.status_code, 503)
        finally:
            settings.API_AUTH_TOKEN = orig


try:
    from fastapi import HTTPException as HTTPExceptionError
except ImportError:  # pragma: no cover
    HTTPExceptionError = Exception


if __name__ == "__main__":
    unittest.main(verbosity=2)
