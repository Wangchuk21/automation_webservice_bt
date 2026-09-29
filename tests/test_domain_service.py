"""
Tests for domain services: register, verify, notify.

The whole point of the design is that the customer is only told their domain is
live when DNS says it is. Forwarding is done by BT staff by hand, so there is
always a window where the domain is registered but the forwarding is not done
yet, and emailing during that window tells a customer something untrue.

The states are therefore kept apart on purpose. `verified` means a check passed;
`notified` means a human decided to send the email. They are not the same event
and must not collapse into one.

Nothing here sends an email. An earlier manual check of the real endpoint did,
which is why every test stubs the notifier rather than relying on the
configuration being off.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
import domain_service
import notifier
from dns_check import FORWARDED, MISMATCH, NOT_FORWARDED


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        patcher = patch.object(domain_service, "log_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def register(self, **kw):
        args = dict(domain="wank.bt", customer_name="Karma", email="karma@example.bt",
                    service="forwarding", forwarding_kind="a",
                    forwarding_target="198.51.100.9", registry_action="created")
        args.update(kw)
        return domain_service.record_registration(**args)


class TestStatesAreKeptApart(StorageCase):
    def test_registration_starts_awaiting_dns(self):
        self.assertEqual(self.register()["status"], domain_service.AWAITING_DNS)

    def test_hosting_registration_is_not_awaiting_dns(self):
        """Hosting has no forwarding to wait for, so it would sit in a queue
        that does not apply to it."""
        self.assertEqual(self.register(service="hosting")["status"],
                         domain_service.REGISTERED)

    def test_verified_and_notified_are_different_events(self):
        self.register()
        checked = domain_service.record_verification(
            "wank.bt", {"status": FORWARDED, "kind": "a", "target": "198.51.100.9",
                        "observed": ["198.51.100.9"], "message": "ok"})
        self.assertEqual(checked["status"], domain_service.VERIFIED)
        sent = domain_service.record_notification("wank.bt", True, "sent")
        self.assertEqual(sent["status"], domain_service.NOTIFIED)
        self.assertTrue(sent["notified_at"])

    def test_a_failed_check_does_not_advance_the_state(self):
        """A domain verified last week and changed since must not silently keep
        its verified status."""
        self.register()
        domain_service.record_verification(
            "wank.bt", {"status": FORWARDED, "kind": "a", "target": "198.51.100.9",
                        "observed": ["198.51.100.9"], "message": "ok"})
        after = domain_service.record_verification(
            "wank.bt", {"status": MISMATCH, "kind": "a", "target": "198.51.100.9",
                        "observed": ["203.0.113.1"], "message": "different now"})
        self.assertNotEqual(after["status"], domain_service.NOTIFIED)
        self.assertEqual(after["last_check_status"], MISMATCH)

    def test_the_latest_line_wins(self):
        self.register(forwarding_target="198.51.100.9")
        self.register(forwarding_target="203.0.113.5")
        states = domain_service.current_states()
        self.assertEqual(len(states), 1, "two lines for one domain, not two domains")
        self.assertEqual(states[0]["forwarding_target"], "203.0.113.5")


class TestDomainValidation(StorageCase):
    def test_a_real_domain_is_accepted(self):
        self.assertEqual(domain_service.normalise_domain(" Wank.BT. "), "wank.bt")

    def test_junk_is_refused(self):
        for bad in ("", "   ", "wank", "wank bt", "wank..bt", "-bad.bt", None):
            with self.assertRaises(ValueError, msg=f"{bad!r} was accepted"):
                domain_service.normalise_domain(bad)

    def test_an_invalid_service_is_refused(self):
        with self.assertRaises(ValueError):
            self.register(service="something else")


class TestUnwritableLogDoesNotBreakTheFlow(StorageCase):
    def test_a_registry_success_is_not_undone_by_a_log_failure(self):
        """A domain is registered on the registry whether or not the local log
        line lands, and raising here would make the operator think the
        registration failed."""
        with patch.object(domain_service, "log_path",
                          return_value=Path("/proc/nope/services.jsonl")):
            with self.assertLogs(domain_service.logger, level="ERROR"):
                entry = self.register()
        self.assertEqual(entry["domain"], "wank.bt")

    def test_a_torn_line_does_not_lose_the_rest(self):
        self.register()
        with open(self.path, "a") as fh:
            fh.write('{"domain": "half-writ')     # a line cut off mid-write
        with self.assertLogs(domain_service.logger, level="WARNING"):
            states = domain_service.current_states()
        self.assertEqual(len(states), 1)


class TestEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        p = patch.object(domain_service, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)

        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        app_module.app.dependency_overrides[app_module.require_token_for_destructive] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def _register(self, kind="a", target="198.51.100.9", email="karma@example.bt"):
        return domain_service.record_registration(
            domain="wank.bt", customer_name="Karma", email=email,
            service="forwarding", forwarding_kind=kind,
            forwarding_target=target, registry_action="created")

    def test_notify_is_refused_before_verification(self):
        """The gate. A customer must not be told their domain is live when the
        forwarding has not been done."""
        self._register()
        r = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("not verified", r.json()["detail"])

    def test_notify_is_refused_when_verification_failed(self):
        self._register()
        with patch.object(app_module, "check_forwarding",
                          return_value={"status": NOT_FORWARDED, "message": "no record"}):
            self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        r = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 409)

    def test_notify_sends_once_verified(self):
        self._register()
        with patch.object(app_module, "check_forwarding",
                          return_value={"status": FORWARDED, "observed": ["198.51.100.9"],
                                        "kind": "a", "target": "198.51.100.9",
                                        "message": "ok"}):
            self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        with patch.object(app_module, "send_forwarding_confirmation",
                          return_value=(True, "sent")) as send:
            r = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 200)
        send.assert_called_once()
        # The verified values go in the email, not the ones that were asked for.
        self.assertEqual(send.call_args.kwargs["observed"], ["198.51.100.9"])

    def test_a_second_notification_is_refused(self):
        self._register()
        with patch.object(app_module, "check_forwarding",
                          return_value={"status": FORWARDED, "observed": ["198.51.100.9"],
                                        "kind": "a", "target": "198.51.100.9", "message": "ok"}):
            self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        with patch.object(app_module, "send_forwarding_confirmation",
                          return_value=(True, "sent")):
            self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
            again = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(again.status_code, 409, "the customer would be emailed twice")

    def test_a_failed_send_is_not_recorded_as_notified(self):
        self._register()
        with patch.object(app_module, "check_forwarding",
                          return_value={"status": FORWARDED, "observed": ["198.51.100.9"],
                                        "kind": "a", "target": "198.51.100.9", "message": "ok"}):
            self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        with patch.object(app_module, "send_forwarding_confirmation",
                          return_value=(False, "SMTP refused")):
            r = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 502)
        self.assertNotEqual(domain_service.get_state("wank.bt")["status"],
                            domain_service.NOTIFIED)

    def test_no_email_recorded_means_nobody_to_notify(self):
        self._register(email="")
        with patch.object(app_module, "check_forwarding",
                          return_value={"status": FORWARDED, "observed": ["1.1.1.1"],
                                        "kind": "a", "target": "198.51.100.9", "message": "ok"}):
            self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        r = self.client.post("/api/v1/domain-services/notify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 422)

    def test_verify_needs_a_record(self):
        r = self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 404)

    def test_hosting_has_no_forwarding_to_verify(self):
        domain_service.record_registration(
            domain="wank.bt", customer_name="K", email="k@x.bt", service="hosting")
        r = self.client.post("/api/v1/domain-services/verify", json={"domain": "wank.bt"})
        self.assertEqual(r.status_code, 400)

    def test_registering_requires_a_target_it_can_later_verify(self):
        """Without a target there is nothing to check the DNS against, and the
        domain would sit in the queue forever with no way to move it."""
        with patch.object(app_module, "NICClient") as nic:
            nic.return_value.register_or_update_domain.return_value = {
                "success": True, "action": "created", "message": "ok"}
            r = self.client.post("/api/v1/domain-services/register", json={
                "domain": "wank.bt", "customer_name": "K", "email": "k@x.bt",
                "service": "forwarding", "forwarding_kind": "a",
            })
        self.assertEqual(r.status_code, 422)
        nic.return_value.register_or_update_domain.assert_not_called()

    def test_a_registry_failure_is_reported_and_nothing_is_recorded(self):
        with patch.object(app_module, "NICClient") as nic:
            nic.return_value.register_or_update_domain.return_value = {
                "success": False, "message": "registry said no"}
            r = self.client.post("/api/v1/domain-services/register", json={
                "domain": "wank.bt", "customer_name": "K", "email": "k@x.bt",
                "service": "forwarding", "forwarding_kind": "a",
                "forwarding_target": "198.51.100.9"})
        self.assertEqual(r.status_code, 502)
        self.assertIsNone(domain_service.get_state("wank.bt"),
                          "a failed registration must not leave a record")

    def test_an_unknown_forwarding_kind_is_refused(self):
        with patch.object(app_module, "NICClient"):
            r = self.client.post("/api/v1/domain-services/register", json={
                "domain": "wank.bt", "customer_name": "K", "email": "k@x.bt",
                "service": "forwarding", "forwarding_kind": "carrier-pigeon",
                "forwarding_target": "x"})
        self.assertEqual(r.status_code, 422)

    def test_listing_can_be_filtered_by_status(self):
        self._register()
        r = self.client.get("/api/v1/domain-services",
                            params={"status": domain_service.AWAITING_DNS})
        self.assertEqual(r.json()["count"], 1)
        r = self.client.get("/api/v1/domain-services",
                            params={"status": domain_service.NOTIFIED})
        self.assertEqual(r.json()["count"], 0)


class TestTheEmailSaysWhatWasVerified(unittest.TestCase):
    """The wording is a factual claim about the public internet, so it must
    state the values that were actually observed, not the ones requested."""

    def _text(self, kind, observed):
        return notifier._forwarding_text("wank.bt", kind, "198.51.100.9", observed)

    def test_a_record_wording_says_it_is_live(self):
        t = self._text("a", ["198.51.100.9"])
        self.assertIn("wank.bt", t)
        self.assertIn("198.51.100.9", t)

    def test_nameserver_wording_says_delegated(self):
        t = self._text("nameserver", ["ns1.theirhost.com"])
        self.assertIn("delegated", t)
        self.assertNotIn("visitors who type", t.lower())

    def test_it_does_not_claim_an_address_it_did_not_see(self):
        t = self._text("a", [])
        self.assertNotIn("resolves to .", t, "must not invent a resolved address")


if __name__ == "__main__":
    unittest.main(verbosity=2)
