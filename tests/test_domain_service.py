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

import activity
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


class TestTwoRegistryFormsCanCoexist(unittest.TestCase):
    """
    The nic.bt.bt field form now appears twice on the page: once inside the
    hosting form, once in the domain service card. It used to be a set of
    page-wide functions that found its inputs with document.querySelector, so
    two copies would have found each other's fields and quietly submitted the
    wrong customer's details to the national registry.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_lookups_are_scoped_to_the_instance(self):
        self.assertTrue("this.root.querySelector" in self.js)
        self.assertIn("class RegistryForm", self.js)

    def test_no_page_wide_lookup_of_a_registry_field(self):
        """A document-wide query would be the exact bug this refactor fixed."""
        for line in self.js.splitlines():
            stripped = line.strip()
            if stripped.startswith("document.querySelector") and "data-reg" in stripped:
                self.fail(f"registry field found page-wide: {stripped}")

    def test_both_forms_are_wired_to_their_own_containers(self):
        self.assertTrue('root: document.getElementById("nic-reg-form")' in self.js)
        self.assertTrue('root: document.getElementById("ds-reg-form")' in self.js)
        self.assertTrue('domainInput: document.getElementById("domain")' in self.js)
        self.assertTrue('domainInput: document.getElementById("ds_domain")' in self.js)

    def test_the_spec_is_shared_so_they_cannot_drift(self):
        self.assertTrue("let REG_SPEC = null" in self.js)
        self.assertTrue("let REG_EXTENSIONS = null" in self.js)
        self.assertTrue("if (!REG_EXTENSIONS)" in self.js,
                        "the extension list should be fetched once, not per form")

    def test_the_submit_path_uses_the_hosting_instance(self):
        self.assertTrue("hostingRegistry.values()" in self.js)
        self.assertTrue("hostingRegistry.missing()" in self.js)

    def test_the_domain_service_sends_its_own_values(self):
        self.assertTrue("nic_fields: domainRegistry.values()" in self.js)


class TestTheQueueOffersTheRightActions(unittest.TestCase):
    """
    The notify button is a convenience, not the control. The server refuses
    unless a check has passed, and the page should not offer a button that can
    only ever fail.
    """

    @classmethod
    def setUpClass(cls):
        cls.js = (Path(__file__).resolve().parent.parent
                  / "static" / "js" / "app.js").read_text()

    def test_notify_is_only_offered_once_verified(self):
        self.assertTrue('r.status === "verified"' in self.js)

    def test_a_check_is_offered_while_waiting(self):
        self.assertTrue("verifyDomain(" in self.js)

    def test_notified_rows_are_shown_without_an_action(self):
        self.assertTrue('r.status === "notified"' in self.js)

    def test_the_button_defers_to_the_server(self):
        """It asks for confirmation and says the server will refuse; it does not
        pretend the button is the thing enforcing it."""
        self.assertTrue("window.confirm" in self.js)
        self.assertTrue("the server will refuse" in self.js)

    def test_switching_forwarding_kind_clears_a_mismatched_target(self):
        """An address left in the box after switching to name servers would be
        compared against a delegation and quietly never match."""
        self.assertTrue("syncKind" in self.js)


class TestTheLiveLookup(unittest.TestCase):
    """
    Reading the domain's real records off the screen, rather than typing them.

    A mistyped nameserver fails every later check and looks exactly like the
    forwarding was never done, so the operator needs to be able to see what is
    actually there and copy it.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_the_lookup_asks_for_the_right_question(self):
        from dns_check import lookup_records
        self.assertIn("dns/records?domain=", self.js)
        got = lookup_records("bt.bt", "nameserver")
        self.assertEqual(sorted(got["observed"]), ["ns1.druknet.bt", "ns2.druknet.bt"])
        self.assertIn("delegated", got["message"])

    def test_an_unknown_kind_is_refused_rather_than_guessed(self):
        from dns_check import lookup_records
        got = lookup_records("bt.bt", "carrier-pigeon")
        self.assertEqual(got["observed"], [])
        self.assertIn("Unknown kind", got["message"])

    def test_a_domain_with_no_record_says_so_without_pretending(self):
        from dns_check import lookup_records
        got = lookup_records("wank.bt", "a")
        self.assertEqual(got["observed"], [])
        self.assertIn("no address record", got["message"])

    def test_the_answer_can_be_copied_into_the_target(self):
        self.assertTrue('id="ds-live-use"' in self.html)
        self.assertTrue("lastLive.observed.join" in self.js)

    def test_it_says_when_the_entry_disagrees_with_reality(self):
        """The case that matters: the operator has typed the wrong nameserver and
        would otherwise not find out until the check failed."""
        self.assertTrue("ds-live-diff" in self.js)
        self.assertIn("different from what you entered", self.js)

    def test_switching_kind_reruns_the_lookup(self):
        """A record for the wrong question is worse than none: an address would
        be compared against a delegation and quietly never match."""
        self.assertIn('kind.addEventListener("change"', self.js)

    def test_the_card_does_not_duplicate_the_hosting_form(self):
        self.assertIn("use the provisioning form above next", self.html)
        self.assertIn("This card is for the domain itself", self.html)


class TestTheCardSitsWhereTheOperatorIsLooking(unittest.TestCase):
    """
    It was stranded near the bottom of the page, past the surrender and
    suspension sections. Registering a domain and then finding out its
    forwarding is still pending are the same piece of work, and an operator
    working from the provisioning form would not have seen it there.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parent.parent
                    / "templates" / "index.html").read_text()

    def _order(self):
        h = self.html
        return {
            name: h.index(marker)
            for name, marker in [
                ("provisioning", 'id="provision-form"'),
                ("domain_service", 'id="domain-service"'),
                ("queue", 'id="domain-service-queue"'),
                ("dns_tool", 'id="dns-check"'),
                ("surrender", 'id="surrender"'),
            ]
        }

    def test_it_comes_directly_after_the_provisioning_form(self):
        o = self._order()
        self.assertLess(o["provisioning"], o["domain_service"])
        self.assertLess(o["domain_service"], o["queue"])
        self.assertLess(o["queue"], o["dns_tool"])

    def test_nothing_buried_is_between_the_form_and_the_card(self):
        """The two id="surrender" / "id="suspension-review" sections must not
        come first, which is where this started."""
        o = self._order()
        self.assertLess(o["domain_service"], o["surrender"])


class TestActivityLog(unittest.TestCase):
    """
    The record of what has been done.

    It exists because the handover kit only ever showed the result of the action
    you had just taken, and a reload lost it, and a provisioning left no durable
    trace at all -- it existed only in container stdout, which rotates. The
    first provisionings in this system are not recoverable from the system.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "activity.jsonl"
        p = patch.object(activity, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)

    def test_an_event_round_trips(self):
        activity.record_event(activity.PROVISIONED, "Provisioned wank.bt",
                              panel="cpanel", username="wank", domain="wank.bt")
        got = activity.recent()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], activity.PROVISIONED)
        self.assertEqual(got[0]["domain"], "wank.bt")
        self.assertTrue(got[0]["at"])

    def test_newest_comes_first(self):
        for d in ("one.bt", "two.bt", "three.bt"):
            activity.record_event(activity.PROVISIONED, d, domain=d)
        self.assertEqual([e["domain"] for e in activity.recent()],
                         ["three.bt", "two.bt", "one.bt"])

    def test_refusals_are_kept(self):
        """A refused activation is the answer to 'who tried to bring back an
        account suspended for abuse'. A successes-only feed hides it."""
        activity.record_event(activity.ACTIVATED, "REFUSED", outcome=activity.REFUSED)
        got = activity.recent()
        self.assertEqual(got[0]["outcome"], activity.REFUSED)

    def test_an_unwritable_log_does_not_fail_the_action(self):
        """The action already happened; failing the request because the diary
        is full would be a bad trade."""
        with patch.object(activity, "log_path",
                          return_value=Path("/proc/nope/activity.jsonl")):
            with self.assertLogs(activity.logger, level="ERROR"):
                self.assertIsNone(activity.record_event(activity.PROVISIONED, "x"))

    def test_a_torn_line_does_not_lose_the_rest(self):
        activity.record_event(activity.PROVISIONED, "first", domain="one.bt")
        with open(self.path, "a") as fh:
            fh.write('{"kind": "cut off')
        with self.assertLogs(activity.logger, level="WARNING"):
            self.assertEqual(len(activity.recent()), 1)

    def test_counts_summarise_the_feed(self):
        activity.record_event(activity.PROVISIONED, "a")
        activity.record_event(activity.SUSPENDED, "b")
        activity.record_event(activity.SUSPENDED, "c")
        self.assertEqual(activity.counts()[activity.SUSPENDED], 2)

    def test_filtering_by_kind(self):
        activity.record_event(activity.PROVISIONED, "a")
        activity.record_event(activity.SUSPENDED, "b")
        got = activity.recent(kinds=[activity.SUSPENDED])
        self.assertEqual(len(got), 1)


class TestEveryActionIsRecorded(unittest.TestCase):
    """Each of these left no trace, or an incomplete one, before."""

    @classmethod
    def setUpClass(cls):
        cls.app = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        cls.js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text()
        cls.html = (Path(__file__).resolve().parent.parent
                    / "templates" / "index.html").read_text()

    def test_a_provisioning_is_recorded(self):
        self.assertIn("activity.PROVISIONED", self.app)

    def test_a_manual_suspension_is_recorded(self):
        self.assertIn("activity.SUSPENDED", self.app)

    def test_an_activation_is_recorded(self):
        self.assertIn("activity.ACTIVATED", self.app)

    def test_a_refused_activation_is_recorded(self):
        body = self.app.split("if (state.get(\"reason\") or \"\").strip().lower()")[1]
        self.assertIn("activity.REFUSED", body[:600],
                      "the refusal must be logged before the raise")

    def test_a_domain_registration_is_recorded(self):
        self.assertIn("activity.DOMAIN_REGISTERED", self.app)

    def test_the_feed_refreshes_after_each_action(self):
        self.assertTrue("loadActivity" in self.js)
        self.assertIn('id="activity-list"', self.html)

    def test_the_formal_trails_are_not_replaced(self):
        """The activity feed is a convenience. The surrender, suspension and
        domain-service logs are the records, and they stay."""
        from config import settings
        for keep in ("SURRENDER_AUDIT_LOG", "SUSPENSION_AUDIT_LOG",
                     "DOMAIN_SERVICE_LOG", "ACTIVITY_LOG"):
            self.assertTrue(getattr(settings, keep, None),
                            f"{keep} was removed -- it is the formal record, "
                            f"and the activity feed does not replace it")
        self.assertIn("are not replaced by this list", self.js)
