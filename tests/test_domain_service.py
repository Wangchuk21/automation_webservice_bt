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
from config import settings
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
        return notifier.forwarding_text("wank.bt", kind, "198.51.100.9", observed)

    def test_a_record_wording_shows_the_address(self):
        t = self._text("a", ["198.51.100.9"])
        self.assertIn("wank.bt", t)
        self.assertIn("198.51.100.9", t)

    def test_nameserver_wording_shows_the_delegation(self):
        t = self._text("nameserver", ["ns1.theirhost.com"])
        self.assertIn("name server ns1.theirhost.com", t)
        self.assertIn("host -t ns wank.bt", t)

    def test_it_does_not_claim_an_address_it_did_not_see(self):
        """With nothing verified there is no evidence to show, and an invented
        one would be the whole problem this feature exists to avoid."""
        t = self._text("a", [])
        self.assertNotIn("has address", t)


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


class TestTheEmailIsEditableBeforeItIsSent(unittest.TestCase):
    """
    The wording goes to a customer, so it has to be readable and changeable
    before it does. The default matches the format BT already uses, including
    the lookup output, so what the customer reads is the same evidence the
    operator was shown.

    What the operator may change is the prose. The gate is not theirs to lift:
    the server still refuses unless a check has passed, whatever is typed.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()
        cls.app = (root / "app.py").read_text()

    def test_the_default_matches_the_house_format(self):
        from notifier import forwarding_text
        body = forwarding_text("dewachen.bt", "nameserver", "ns1.vercel-dns.com",
                               ["ns1.vercel-dns.com", "ns2.vercel-dns.com"])
        self.assertIn("host -t ns dewachen.bt", body)
        self.assertIn("dewachen.bt name server ns1.vercel-dns.com.", body)
        self.assertIn("dewachen.bt name server ns2.vercel-dns.com.", body)
        self.assertIn("Regards", body)

    def test_an_a_record_forwarding_shows_its_address(self):
        from notifier import forwarding_text
        body = forwarding_text("wank.bt", "a", "198.51.100.9", ["198.51.100.9"])
        self.assertIn("198.51.100.9", body)
        self.assertNotIn("name server", body)

    def test_the_evidence_shown_is_the_verified_values(self):
        """Not the ones that were requested. Those are what BT said they set;
        these are what DNS actually says."""
        from notifier import forwarding_text
        body = forwarding_text("wank.bt", "a", "198.51.100.9", ["203.0.113.5"])
        self.assertIn("203.0.113.5", body)
        self.assertNotIn("198.51.100.9", body)

    def test_an_edited_body_is_what_gets_sent(self):
        from unittest.mock import patch
        from notifier import send_forwarding_confirmation
        with patch("notifier._deliver") as deliver, \
             patch("notifier.settings.SMTP_ENABLED", True), \
             patch("notifier.settings.SMTP_HOST", "mail.bt"), \
             patch("notifier.settings.SMTP_SSL", True), \
             patch("notifier.settings.SMTP_FROM_EMAIL", "hosting@bt.bt"), \
             patch("notifier.settings.SMTP_FROM_NAME", "Web Hosting Support"), \
             patch("notifier.settings.SMTP_CC_EMAIL", ""):
            ok, _ = send_forwarding_confirmation(
                "wank.bt", "k@x.bt", "a", "198.51.100.9", ["198.51.100.9"],
                body="Operator's own wording.", subject="Custom subject")
        self.assertTrue(ok)
        msg = deliver.call_args[0][0]
        sent = msg.as_string()
        self.assertIn("Custom subject", sent)
        # The body is base64 in a multipart message; get_payload(decode=True)
        # already decodes it, so decoding again would throw.
        parts = msg.get_payload()
        bodies = "".join(
            p.get_payload(decode=True).decode("utf-8")
            for p in parts if p.get_content_type() == "text/plain")
        self.assertIn("Operator's own wording", bodies)

    def test_the_preview_endpoint_sends_nothing(self):
        self.assertIn("email-preview", self.app)
        body = self.app.split("def preview_domain_email(")[1].split("\n@app.")[0]
        self.assertNotIn("send_forwarding_confirmation", body)

    def test_the_preview_uses_the_check_when_there_is_none_yet(self):
        """Otherwise the body would have an empty evidence section, which reads
        as a forwarding that went nowhere."""
        body = self.app.split("def preview_domain_email(")[1].split("\n@app.")[0]
        self.assertIn("check_forwarding(", body)

    def test_the_editor_exists_and_sends_what_is_typed(self):
        self.assertIn('id="ds-email-editor"', self.html)
        self.assertIn('id="ds-email-body"', self.html)
        self.assertTrue('id="ds-email-subject"' in self.html)
        self.assertTrue("body," in self.js or "body }" in self.js,
                        "the editor must post the body the operator typed")
        self.assertTrue("Reset to the standard wording" in self.html,
                        "the operator needs a way back to the default wording")
        self.assertTrue("emailDefault" in self.js, "reset does nothing without it")

    def test_editing_cannot_bypass_the_server_gate(self):
        """The client sends the body; the server still checks the state first,
        so an edited email cannot get out before verification."""
        body = self.app.split("def notify_domain_service(")[1].split("\n@app.")[0]
        self.assertLess(body.index('!= VERIFIED'), body.index("send_forwarding_confirmation"),
                        "the gate must be checked before the send, not after")

    def test_an_edited_body_is_never_paired_with_generated_html(self):
        """
        Found by the test above, not by reading the code.

        The plain-text part took the operator's wording while the HTML
        alternative still said "it is now live and resolves to ..." -- so the
        same email said two different things depending on which part the
        customer's mail client rendered.
        """
        from unittest.mock import patch
        from notifier import send_forwarding_confirmation
        with patch("notifier._deliver") as deliver, \
             patch("notifier.settings.SMTP_ENABLED", True), \
             patch("notifier.settings.SMTP_HOST", "mail.bt"), \
             patch("notifier.settings.SMTP_SSL", True), \
             patch("notifier.settings.SMTP_FROM_EMAIL", "hosting@bt.bt"), \
             patch("notifier.settings.SMTP_FROM_NAME", "Web Hosting Support"), \
             patch("notifier.settings.SMTP_CC_EMAIL", ""):
            send_forwarding_confirmation("wank.bt", "k@x.bt", "a", "198.51.100.9",
                                         ["198.51.100.9"],
                                         body="Our own wording.")
        types = {p.get_content_type() for p in deliver.call_args[0][0].get_payload()}
        self.assertEqual(types, {"text/plain"},
                         "a hand-written message must not be accompanied by a "
                         "generated HTML version that may contradict it")

    def test_the_default_still_gets_both_parts(self):
        from unittest.mock import patch
        from notifier import send_forwarding_confirmation
        with patch("notifier._deliver") as deliver, \
             patch("notifier.settings.SMTP_ENABLED", True), \
             patch("notifier.settings.SMTP_HOST", "mail.bt"), \
             patch("notifier.settings.SMTP_SSL", True), \
             patch("notifier.settings.SMTP_FROM_EMAIL", "hosting@bt.bt"), \
             patch("notifier.settings.SMTP_FROM_NAME", "Web Hosting Support"), \
             patch("notifier.settings.SMTP_CC_EMAIL", ""):
            send_forwarding_confirmation("wank.bt", "k@x.bt", "a", "198.51.100.9",
                                         ["198.51.100.9"])
        types = {p.get_content_type() for p in deliver.call_args[0][0].get_payload()}
        self.assertEqual(types, {"text/plain", "text/html"})


from tests.test_dns_check import js_function  # noqa: E402


class TestTheSentEmailIsKept(unittest.TestCase):
    """
    "Told" is not a record.

    Once the email leaves there is no copy anywhere else, and an operator can
    now change the wording before it goes. A customer who later disputes what
    they were told has to be answered from what is stored here, and "notified at
    14:02" does not say what was said, or to which address.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        p = patch.object(domain_service, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        domain_service.record_registration(
            domain="dewachen.bt", customer_name="Dewachen", email="c@x.bt",
            service="forwarding", forwarding_kind="nameserver",
            forwarding_target="ns1.vercel-dns.com", registry_action="created")
        domain_service.get_state("dewachen.bt")["observed"] = ["ns1.vercel-dns.com"]

    def test_the_email_body_is_stored(self):
        from notifier import forwarding_subject, forwarding_text
        body = forwarding_text("dewachen.bt", "nameserver", "ns1.vercel-dns.com",
                               ["ns1.vercel-dns.com"])
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject=forwarding_subject("dewachen.bt"),
            body=body, recipient="c@x.bt", kind="nameserver")
        got = domain_service.get_state("dewachen.bt")["notification"]
        self.assertEqual(got["body"], body)
        self.assertEqual(got["to"], "c@x.bt")
        self.assertIn("subject", got)
        self.assertTrue(got["at"])

    def test_an_edited_email_is_marked_as_one(self):
        """The one worth a second look: wording a colleague chose by hand."""
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="s", body="Our own wording.",
            recipient="c@x.bt", kind="nameserver", edited=True)
        self.assertTrue(domain_service.get_state("dewachen.bt")["notification"]["edited"])

    def test_the_standard_wording_is_not_marked_as_edited(self):
        from notifier import forwarding_subject, forwarding_text
        body = forwarding_text("dewachen.bt", "nameserver", "ns1.vercel-dns.com",
                               ["ns1.vercel-dns.com"])
        # edited=False is what the endpoint passes when no body was supplied.
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject=forwarding_subject("dewachen.bt"),
            body=body, recipient="c@x.bt", kind="nameserver", edited=False)
        self.assertFalse(domain_service.get_state("dewachen.bt")["notification"]["edited"])
        # And it must not depend on regenerating the default from stored state.
        self.assertNotIn("forwarding_text", domain_service.record_notification.__doc__ or "")

    def test_a_failed_send_is_not_recorded_as_a_sent_email(self):
        """Otherwise a later review would show a customer an email that never
        reached them."""
        domain_service.record_notification(
            "dewachen.bt", False, "SMTP refused", body="would have gone here",
            recipient="c@x.bt", kind="nameserver")
        self.assertNotIn("notification", domain_service.get_state("dewachen.bt"))

    def test_the_record_survives_a_later_read(self):
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="s", body="The text.",
            recipient="c@x.bt", kind="nameserver")
        again = domain_service.get_state("dewachen.bt")
        self.assertEqual(again["notification"]["body"], "The text.")


class TestTheSentEmailIsVisible(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.app = (root / "app.py").read_text()

    def test_a_notified_domain_offers_the_email(self):
        self.assertIn("View sent email", self.js)

    def test_the_saved_email_can_be_fetched(self):
        """The queue row is all the operator has to go on, so there has to be a
        way to ask for the mail behind it."""
        self.assertIn("/api/v1/domain-services/{domain}", self.app)

    def test_the_record_is_read_only(self):
        """It is what a customer already received, not a draft that can still be
        changed. An editable box would imply it can."""
        body = js_function(self.js, "showSentEmail")
        self.assertIn("readOnly = true", body)
        self.assertIn("ds-email-send", body)

    def test_a_new_draft_clears_the_read_only_state(self):
        """Otherwise the next domain to notify gets a locked box with the send
        button still hidden, and the queue looks permanently finished."""
        body = js_function(self.js, "openEmailEditor")
        self.assertIn("readOnly = false", body)
        self.assertIn("ds-email-send", body)

    def test_sending_leaves_the_email_on_screen(self):
        """It is the only copy. Closing the box left nothing to read."""
        self.assertIn("showSentEmail(domain, data.sent)", self.js)


class TestTheSendButtonIsThere(unittest.TestCase):
    """
    Reported as "there is no sent button on Domain Service again".

    The button rendered only when the row's status was already "verified", and a
    freshly registered forwarding domain starts at "awaiting_dns" -- so the only
    way to see it was to press Check, watch the row change, and notice a button
    that had not been there a moment earlier. The gate was correct; the flow
    looked broken.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()

    def _queue(self):
        return js_function(self.js, "loadDomainServiceQueue")

    def test_a_forwarding_domain_always_offers_the_send_button(self):
        """Not gated on the status: an unverified domain shows the button and is
        told why it cannot send yet."""
        body = self._queue()
        self.assertIn("Send confirmation", body)
        self.assertNotIn('r.status === "verified"\n                 ?', body,
                         "the button must not depend on the row already being verified")

    def test_a_hosting_domain_is_not_offered_a_forwarding_email(self):
        """There is no forwarding to confirm for a hosted domain, and the server
        refuses it."""
        body = self._queue()
        self.assertIn('r.service === "forwarding"', body)

    def test_sending_checks_first(self):
        """Otherwise the operator gets a bare 409 and no idea what to do next."""
        body = js_function(self.js, "notifyDomain")
        self.assertLess(body.index("verify"), body.index("openEmailEditor"))

    def test_it_explains_a_failed_check_instead_of_going_quiet(self):
        body = js_function(self.js, "notifyDomain")
        self.assertIn("Not sending:", body)
        self.assertIn("data.check.message", body)

    def test_the_editor_still_only_opens_on_a_real_forwarding(self):
        body = js_function(self.js, "notifyDomain")
        self.assertIn('data.check.status !== "forwarded"', body)

    def test_only_one_notify_function_exists(self):
        """A second definition silently wins, and the button would call the wrong
        one."""
        self.assertEqual(self.js.count("async function notifyDomain("), 1,
                         "there are two notifyDomain definitions")

    def test_the_button_is_passed_in_rather_than_taken_from_a_global(self):
        body = js_function(self.js, "notifyDomain", signature=True)
        self.assertIn("async function notifyDomain(btn, domain)", body)
        self.assertNotIn("event.target", body,
                         "the implicit `event` global is not dependable here")


class TestTheNextStepIsWhereTheOperatorIsStanding(unittest.TestCase):
    """
    Reported with a screenshot of the registration form and the question "where is
    to send an email saying their domain have been successfully forwarded?"

    The Send button existed the whole time, in the queue card below the form. The
    form filled the viewport, so the button was entirely off-screen, and the
    success message pointed at a differently-named card rather than offering the
    action. Nothing was missing; it was simply nowhere near where anyone would
    look for it.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_registering_offers_the_send_right_there(self):
        body = js_function(self.js, "registerDomainService")
        self.assertTrue("Send confirmation email" in body,
                        "the next step must be in the panel the operator is reading")
        self.assertIn("notifyDomain(this,", body)

    def test_it_offers_the_check_too(self):
        body = js_function(self.js, "registerDomainService")
        self.assertTrue("Check forwarding" in body, "no check offered after registering")

    def test_it_no_longer_points_at_a_card_instead_of_offering_the_action(self):
        body = js_function(self.js, "registerDomainService")
        self.assertFalse("It will appear under" in body,
                         "naming another card is not the same as offering the button")

    def test_a_hosted_domain_is_told_to_provision_instead(self):
        """There is no forwarding to confirm, so offering a send button would be
        a dead end. The wording is in the script, not the template."""
        body = js_function(self.js, "registerDomainService")
        self.assertIn("Now create the hosting account", body)

    def test_the_queue_is_reachable_from_the_form(self):
        self.assertTrue('href="#domain-service-queue"' in self.html,
                        "the queue is not reachable from the form")

    def test_the_queue_says_which_button_sends(self):
        body = self.html.split('id="domain-service-queue"')[1][:900]
        self.assertIn("Send confirmation", body)


class TestCorrectingAnEmailThatAlreadyWentOut(unittest.TestCase):
    """
    "I made a mistake and I cannot change, I need an edit button."

    There is no such button and there cannot be. Nothing recalls a message a
    customer already holds; editing the stored copy would only make the record
    disagree with their inbox, which is the one thing the record exists to
    prevent. So the route offered is a second email that says it replaces the
    first.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        p = patch.object(domain_service, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        domain_service.record_registration(
            domain="dewachen.bt", customer_name="Dewachen", email="c@x.bt",
            service="forwarding", forwarding_kind="nameserver",
            forwarding_target="ns1.vercel-dns.com", registry_action="created")
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="Original subject",
            body="The original text.", recipient="c@x.bt", kind="nameserver")

    def test_the_first_email_is_never_overwritten(self):
        """The customer still has it. A record that agreed with them about the
        wrong thing would be worse than no record."""
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="Corrected",
            body="The corrected text.", recipient="c@x.bt", kind="nameserver",
            correction=True, reason="typo")
        sends = domain_service.get_state("dewachen.bt")["sends"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0]["body"], "The original text.")
        self.assertEqual(sends[1]["body"], "The corrected text.")

    def test_a_correction_points_at_what_it_replaces(self):
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="C", body="b", recipient="c@x.bt",
            kind="nameserver", correction=True, reason="typo")
        sends = domain_service.get_state("dewachen.bt")["sends"]
        self.assertTrue(sends[1]["correction"])
        self.assertEqual(sends[1]["correction_of"], sends[0]["at"])

    def test_the_reason_is_kept_for_the_record(self):
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="C", body="b", recipient="c@x.bt",
            kind="nameserver", correction=True, reason="Name server mistyped.")
        self.assertEqual(domain_service.get_state("dewachen.bt")["sends"][-1]["reason"],
                         "Name server mistyped.")

    def test_the_latest_send_is_what_the_record_points_at(self):
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="C", body="b", recipient="c@x.bt",
            kind="nameserver", correction=True, reason="typo")
        self.assertEqual(domain_service.get_state("dewachen.bt")["notification"]["body"], "b")

    def test_a_correction_can_go_to_a_different_address(self):
        """The first email may have reached the wrong person entirely."""
        domain_service.record_notification(
            "dewachen.bt", True, "Sent", subject="C", body="b",
            recipient="right@x.bt", kind="nameserver", correction=True, reason="wrong address")
        self.assertEqual(domain_service.get_state("dewachen.bt")["sends"][-1]["to"],
                         "right@x.bt")
        self.assertEqual(domain_service.get_state("dewachen.bt")["sends"][0]["to"], "c@x.bt")

    def test_a_failed_correction_is_not_recorded(self):
        domain_service.record_notification(
            "dewachen.bt", False, "SMTP refused", body="b", recipient="c@x.bt",
            kind="nameserver", correction=True, reason="typo")
        self.assertEqual(len(domain_service.get_state("dewachen.bt")["sends"]), 1)


class TestTheCorrectionEmailItself(unittest.TestCase):
    def test_it_says_it_replaces_the_first(self):
        """A customer holding two emails about one domain cannot tell which to
        believe unless the second says so."""
        from notifier import correction_intro
        text = correction_intro("dewachen.bt", "The address was mistyped.")
        self.assertIn("correct an earlier email", text)
        self.assertIn("replaces what we sent before", text)
        self.assertIn("The address was mistyped.", text)

    def test_the_subject_says_it_is_a_correction(self):
        from notifier import correction_subject
        self.assertIn("Correction", correction_subject("dewachen.bt"))

    def test_a_correction_without_a_reason_is_refused(self):
        """Otherwise the customer gets a second email that explains nothing."""
        from notifier import send_forwarding_correction
        with patch("notifier.settings.SMTP_ENABLED", True):
            ok, msg = send_forwarding_correction("dewachen.bt", "c@x.bt", "a", "1.2.3.4",
                                                 reason="  ")
        self.assertFalse(ok)
        self.assertIn("reason", msg.lower())

    def test_the_reason_is_required_by_the_api_too(self):
        """Client-side validation is a convenience; this is the control."""
        root = Path(__file__).resolve().parent.parent
        app_src = (root / "app.py").read_text()
        body = app_src.split("def correct_domain_notification(")[1].split("\n@app.")[0]
        self.assertTrue("len(reason) < 5" in body, "the reason is not length-checked")
        self.assertLess(body.index("len(reason) < 5"), body.index("send_forwarding_correction"),
                        "the reason must be checked before anything is sent")


TOKEN = {"X-API-Token": "t"}   # the correction endpoint emails a customer


class TestACorrectionAlwaysSaysItIsOne(unittest.TestCase):
    """
    Found by testing my own change, not by reading it.

    The correction body was the operator's text whenever they supplied any, which
    dropped the "this replaces the earlier email" framing. A corrected email could
    therefore go out looking exactly like a first one: the customer would hold two
    contradictory messages with nothing to say which was current. That is worse
    than sending nothing, because it looks like the problem is handled.

    The framing is not the operator's to drop. They edit the correction; the
    statement that it replaces the earlier email stays.
    """

    def test_it_says_so_even_when_the_operator_writes_the_whole_body(self):
        from notifier import correction_body
        text = correction_body("bt.bt", "The address was mistyped.",
                              "nameserver", "ns1.druknet.bt", ["ns1.druknet.bt"],
                              "The correct address is ns1.druknet.bt.")
        self.assertIn("correct an earlier email", text)
        self.assertIn("replaces what we sent before", text)
        self.assertIn("The correct address is ns1.druknet.bt.", text)

    def test_it_says_so_with_the_generated_text(self):
        from notifier import correction_body
        text = correction_body("bt.bt", "mistyped", "nameserver",
                              "ns1.druknet.bt", ["ns1.druknet.bt"])
        self.assertIn("correct an earlier email", text)
        self.assertIn("name server ns1.druknet.bt", text)

    def test_the_salutation_appears_once(self):
        """The intro says Dear Customer and so does the generated text, which
        read as two letters pasted together."""
        from notifier import correction_body
        for body in (None, "Some corrected text."):
            text = correction_body("bt.bt", "mistyped", "nameserver",
                                  "ns1.druknet.bt", ["ns1.druknet.bt"], body)
            self.assertEqual(text.count("Dear Customer"), 1,
                             "the salutation must not be doubled")

    def test_a_pasted_whole_email_is_not_doubled(self):
        """An operator who pastes the entire thing back should not get the
        framing printed twice in one message."""
        from notifier import correction_body, correction_intro
        whole = correction_body("bt.bt", "mistyped", "nameserver",
                                "ns1.druknet.bt", ["ns1.druknet.bt"], "Fixed.")
        again = correction_body("bt.bt", "mistyped", "nameserver",
                                "ns1.druknet.bt", ["ns1.druknet.bt"], whole)
        self.assertEqual(again.count("correct an earlier email"), 1)

    def test_the_api_uses_the_same_rule(self):
        """The endpoint and the notifier must not disagree about what a
        correction is, or the record and the customer's inbox diverge again."""
        root = Path(__file__).resolve().parent.parent
        src = (root / "app.py").read_text()
        body = src.split("def correct_domain_notification(")[1].split("\n@app.")[0]
        self.assertTrue("correction_body(" in body,
                        "the endpoint must build corrections the same way")
        self.assertFalse("correction_intro(" in body,
                         "the endpoint must not assemble the intro itself")


class TestTheCorrectionIsRechecked(unittest.TestCase):
    """
    The original was justified by a check that passed at the time. A second claim
    that the domain is forwarded is a new claim about the present, and forwarding
    can lapse. Correcting a typo about an address that has since stopped
    resolving would be telling the customer something false twice.
    """

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.app = (root / "app.py").read_text()

    def test_the_check_runs_before_the_send(self):
        body = self.app.split("def correct_domain_notification(")[1].split("\n@app.")[0]
        self.assertTrue("check_forwarding(" in body, "no re-check before correcting")
        self.assertLess(body.index("check_forwarding("), body.index("send_forwarding_correction"),
                        "the re-check must come before the send, not after")

    def test_it_refuses_when_no_longer_forwarded(self):
        body = self.app.split("def correct_domain_notification(")[1].split("\n@app.")[0]
        self.assertTrue('result.get("status") != "forwarded"' in body,
                        "a correction must be refused when the domain is no longer forwarded")

    def test_a_second_correction_is_refused(self):
        """One correction is the record. A third email needs a reason beyond
        'the correction was also wrong'."""
        body = self.app.split("def correct_domain_notification(")[1].split("\n@app.")[0]
        self.assertTrue("already a correction" in body,
                        "a third email must not be a free-for-all")

    def test_it_refuses_when_nothing_was_sent_yet(self):
        """Exercised rather than read: matching the text of a wrapped f-string
        tests the line wrapping, not the behaviour."""
        import tempfile as tf
        from fastapi.testclient import TestClient
        with tf.TemporaryDirectory() as tmp:
            with patch.object(domain_service, "log_path",
                              return_value=Path(tmp) / "services.jsonl"):
                domain_service.record_registration(
                    domain="never.bt", customer_name="Never", email="c@x.bt",
                    service="forwarding", forwarding_kind="nameserver",
                    forwarding_target="ns1.druknet.bt", registry_action="created")
                client = TestClient(app_module.app)
                with patch.object(settings, "API_AUTH_TOKEN", "t"):
                    r = client.post("/api/v1/domain-services/correct", headers=TOKEN,
                                    json={"domain": "never.bt", "reason": "typo in it"})
        self.assertEqual(r.status_code, 409, r.text[:200])
        self.assertIn("nothing", str(r.json().get("detail", "")).lower())

    def test_it_refuses_a_third_email(self):
        """One correction is the record. A third needs a reason beyond 'the
        correction was also wrong'."""
        import tempfile as tf
        from fastapi.testclient import TestClient
        with tf.TemporaryDirectory() as tmp:
            with patch.object(domain_service, "log_path",
                              return_value=Path(tmp) / "services.jsonl"):
                domain_service.record_registration(
                    domain="twice.bt", customer_name="Twice", email="c@x.bt",
                    service="forwarding", forwarding_kind="nameserver",
                    forwarding_target="ns1.druknet.bt", registry_action="created")
                domain_service.record_notification(
                    "twice.bt", True, "Sent", subject="a", body="b",
                    recipient="c@x.bt", kind="nameserver")
                client = TestClient(app_module.app)
                with patch.object(settings, "API_AUTH_TOKEN", "t"), \
                     patch("app.check_forwarding") as chk:
                    chk.return_value = {"status": "forwarded", "observed": ["ns1.druknet.bt"]}
                    with patch("notifier._deliver"), \
                         patch("notifier.settings.SMTP_ENABLED", True), \
                         patch("notifier.settings.SMTP_HOST", "mail.bt"), \
                         patch("notifier.settings.SMTP_SSL", True), \
                         patch("notifier.settings.SMTP_FROM_EMAIL", "h@bt.bt"), \
                         patch("notifier.settings.SMTP_FROM_NAME", "Support"), \
                         patch("notifier.settings.SMTP_CC_EMAIL", ""):
                        first = client.post("/api/v1/domain-services/correct", headers=TOKEN,
                                            json={"domain": "twice.bt",
                                                  "reason": "address was mistyped"})
                        second = client.post("/api/v1/domain-services/correct", headers=TOKEN,
                                             json={"domain": "twice.bt",
                                                   "reason": "and again"})
        self.assertEqual(first.status_code, 200, first.text[:200])
        self.assertEqual(second.status_code, 409, second.text[:200])
        self.assertIn("already a correction", str(second.json().get("detail", "")))

    def test_the_first_email_survives_the_correction(self):
        """The customer still holds it. Asserted through the API, because this is
        the guarantee the record exists to give."""
        import tempfile as tf
        from fastapi.testclient import TestClient
        with tf.TemporaryDirectory() as tmp:
            with patch.object(domain_service, "log_path",
                              return_value=Path(tmp) / "services.jsonl"):
                domain_service.record_registration(
                    domain="keep.bt", customer_name="Keep", email="c@x.bt",
                    service="forwarding", forwarding_kind="nameserver",
                    forwarding_target="ns1.druknet.bt", registry_action="created")
                domain_service.record_notification(
                    "keep.bt", True, "Sent", subject="first subject",
                    body="The FIRST text.", recipient="c@x.bt", kind="nameserver")
                client = TestClient(app_module.app)
                with patch.object(settings, "API_AUTH_TOKEN", "t"), \
                     patch("app.check_forwarding") as chk:
                    chk.return_value = {"status": "forwarded", "observed": ["ns1.druknet.bt"]}
                    with patch("notifier._deliver"), \
                         patch("notifier.settings.SMTP_ENABLED", True), \
                         patch("notifier.settings.SMTP_HOST", "mail.bt"), \
                         patch("notifier.settings.SMTP_SSL", True), \
                         patch("notifier.settings.SMTP_FROM_EMAIL", "h@bt.bt"), \
                         patch("notifier.settings.SMTP_FROM_NAME", "Support"), \
                         patch("notifier.settings.SMTP_CC_EMAIL", ""):
                        r = client.post("/api/v1/domain-services/correct", headers=TOKEN,
                                        json={"domain": "keep.bt",
                                              "reason": "address was mistyped",
                                              "body": "The CORRECTED text."})
                self.assertEqual(r.status_code, 200, r.text[:200])
                state = domain_service.get_state("keep.bt")
        self.assertEqual(len(state["sends"]), 2)
        # The first is untouched.
        self.assertEqual(state["sends"][0]["body"], "The FIRST text.")
        # The second is recorded as it was actually sent, framing included, so the
        # record and the customer's inbox match.
        self.assertIn("The CORRECTED text.", state["sends"][1]["body"])
        self.assertIn("correct an earlier email", state["sends"][1]["body"])
        self.assertTrue(state["sends"][1]["correction"])



class TestTheCorrectionIsOfferedInTheRightPlace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_a_notified_domain_offers_a_correction(self):
        self.assertIn("Send correction", self.js)

    def test_not_offered_when_the_last_email_was_already_a_correction(self):
        body = js_function(self.js, "loadDomainServiceQueue")
        self.assertIn("r.notification.correction", body)

    def test_the_reason_field_exists(self):
        self.assertIn('id="ds-correction-reason"', self.html)

    def test_the_recipient_can_be_changed(self):
        """The first email may have gone to the wrong address, and that is not
        fixable by changing the wording."""
        self.assertIn('id="ds-correction-to"', self.html)

    def test_it_starts_from_what_was_sent(self):
        """Fixing a typo must not mean retyping the message and risking a second
        mistake."""
        body = js_function(self.js, "openCorrectionEditor")
        self.assertIn("last.body", body)
        self.assertIn("last.subject", body)

    def test_the_send_button_routes_to_the_right_flow(self):
        """One button, two flows. Left ambiguous, the first domain corrected
        would send as a fresh notification."""
        self.assertIn("correctingDomain ? sendCorrection()", self.js)

    def test_opening_a_fresh_draft_clears_the_correction_state(self):
        body = js_function(self.js, "openEmailEditor")
        self.assertIn("hideCorrectionFields()", body)

    def test_closing_clears_it_too(self):
        self.assertIn("hideCorrectionFields()", js_function(self.js, "closeEmailEditor"))

    def test_the_editor_is_still_read_only_when_viewing_a_sent_email(self):
        """Viewing a sent email is reading the record. It must not become a way
        to rewrite what a customer was told."""
        self.assertIn("readOnly = true", js_function(self.js, "showSentEmail"))

    def test_the_editor_strips_the_framing_before_offering_it(self):
        """The server adds it back on every correction. Showing the operator
        their own text with the framing already on it invites a second copy."""
        body = js_function(self.js, "openCorrectionEditor")
        self.assertTrue("stripCorrectionIntro" in body)
        self.assertTrue("stripCorrectionIntro" in self.js)

    def test_the_strip_only_removes_the_framing(self):
        """An ordinary forwarded confirmation must come back untouched -- the
        words "disregard the earlier message" do not appear in it, and if they
        ever did, cutting at the first one would silently drop real content."""
        self.assertIn("function stripCorrectionIntro", self.js)


class TestCorrectingTheForwardingTarget(unittest.TestCase):
    """
    Registered goldentakinholidays.bt pointed at
    "lina.ns.cloudflare.com.sleo.ns.cloudflare.com." -- two Cloudflare hosts
    pasted together with a dot instead of a comma. The check compared against one
    hostname that cannot exist, so the row read "mismatch" and the only way out
    was to surrender the registration and start again.

    This is the distinction that makes the feature safe: the target is a record
    of what was *asked for*, so correcting a typo in it is honest. What DNS
    actually returned is evidence, and rewriting that to match what somebody hoped
    for is the one thing this page exists to prevent.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        p = patch.object(domain_service, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        domain_service.record_registration(
            domain="goldentakinholidays.bt", customer_name="Ashika Rai",
            email="a@x.bt", service="forwarding", forwarding_kind="nameserver",
            forwarding_target="lina.ns.cloudflare.com.sleo.ns.cloudflare.com.",
            registry_action="created")

    def test_the_target_can_be_corrected(self):
        e = domain_service.update_forwarding(
            "goldentakinholidays.bt", "nameserver",
            "lina.ns.cloudflare.com, sleo.ns.cloudflare.com")
        self.assertEqual(e["forwarding_target"],
                         "lina.ns.cloudflare.com, sleo.ns.cloudflare.com")

    def test_the_kind_can_be_corrected(self):
        e = domain_service.update_forwarding("goldentakinholidays.bt", "a", "198.51.100.9")
        self.assertEqual(e["forwarding_kind"], "a")

    def test_the_old_value_is_kept(self):
        """So it is visible that a correction happened, and what it was."""
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt, ns2.correct.bt")
        self.assertEqual(e["previous_target"],
                         "lina.ns.cloudflare.com.sleo.ns.cloudflare.com.")
        self.assertTrue(e["target_edited_at"])

    def test_the_stale_check_result_is_cleared(self):
        """It described the old target. Left in place it would be read as though
        it had been run against the new one."""
        domain_service.get_state("goldentakinholidays.bt")
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt")
        self.assertEqual(e["last_check_status"], "")
        self.assertEqual(e["observed"], [])

    def test_observed_is_never_editable(self):
        """There is no parameter through which to set it. What DNS returned is
        evidence about the public internet."""
        import inspect
        params = list(inspect.signature(domain_service.update_forwarding).parameters)
        self.assertEqual(params, ["domain", "kind", "target"])

    def test_it_stays_editable_after_the_customer_has_been_emailed(self):
        """
        I first locked this, on the reasoning that the record then describes a
        claim made to the customer. That was wrong about which field this is. The
        target is BT's own note of what the forwarding was requested to be, and
        the customer is never told it -- the email is built from the observed
        values. So correcting it cannot change or contradict anything the customer
        was sent, and an operator who emailed by hand from their own machine would
        otherwise be locked out of their own note.
        """
        domain_service.record_notification(
            "goldentakinholidays.bt", True, "Sent", subject="s", body="b",
            recipient="a@x.bt", kind="nameserver")
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt")
        self.assertEqual(e["forwarding_target"], "ns1.correct.bt")

    def test_but_the_observed_evidence_is_untouched(self):
        """The part that is what the customer was actually told."""
        domain_service.record_notification(
            "goldentakinholidays.bt", True, "Sent", subject="s",
            body="ns1.real.bt", recipient="a@x.bt", kind="nameserver")
        domain_service.get_state("goldentakinholidays.bt")
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt")
        self.assertEqual(e["sends"][0]["body"], "ns1.real.bt",
                         "the email the customer holds must not change")
        self.assertEqual(e["previous_target"],
                         "lina.ns.cloudflare.com.sleo.ns.cloudflare.com.")

    def test_it_refuses_for_a_hosted_domain(self):
        domain_service.record_registration(
            domain="hosted.bt", customer_name="H", email="a@x.bt",
            service="hosting", registry_action="created")
        with self.assertRaises(ValueError) as cm:
            domain_service.update_forwarding("hosted.bt", "a", "198.51.100.9")
        self.assertIn("no", str(cm.exception).lower())

    def test_it_refuses_an_empty_target(self):
        with self.assertRaises(ValueError):
            domain_service.update_forwarding("goldentakinholidays.bt", "nameserver", "  ")

    def test_it_refuses_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            domain_service.update_forwarding("goldentakinholidays.bt", "mx", "x.bt")

    def test_it_refuses_an_unknown_domain(self):
        with self.assertRaises(ValueError):
            domain_service.update_forwarding("never-seen.bt", "a", "198.51.100.9")

    def test_the_registration_is_not_lost(self):
        """The reason this exists: the alternative was surrendering the domain on
        nic.bt.bt and registering it again."""
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt")
        self.assertEqual(e["customer_name"], "Ashika Rai")
        self.assertEqual(e["service"], "forwarding")
        self.assertTrue(e["created_at"])


class TestTheForwardingEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.js = (root / "static" / "js" / "app.js").read_text()
        cls.html = (root / "templates" / "index.html").read_text()

    def test_a_row_offers_the_correction(self):
        self.assertIn("Correct nameserver", self.js)

    def test_the_parse_is_shown_as_it_is_typed(self):
        """The failure was invisible because the field took the mangled value
        without complaint and only said "mismatch" much later."""
        self.assertIn("describeParsedTarget", self.js)
        self.assertIn("Will be checked as", self.js)

    def test_a_dot_joined_pair_is_called_out(self):
        self.assertIn("ds-fe-parsed-warn", self.js)
        self.assertIn("pasted together", self.js)

    def test_saving_checks_straight_away(self):
        """The point of correcting the target is to find out whether the
        forwarding was right all along."""
        body = js_function(self.js, "saveForwardingCorrection")
        self.assertIn("verifyDomain(", body)

    def test_a_hosted_domain_gets_no_correction_button(self):
        self.assertIn('r.service === "forwarding"', js_function(self.js, "loadDomainServiceQueue"))

    def test_the_editor_exists(self):
        for el in ('id="ds-forwarding-editor"', 'id="ds-fe-kind"',
                   'id="ds-fe-target"', 'id="ds-fe-parsed"'):
            self.assertTrue(el in self.html, f"{el} is missing")


class TestRecordingAnEmailSentByHand(unittest.TestCase):
    """
    The operator wrote to the customer from their own mail client. Reasonable
    thing to do, and the system had no idea -- so the row went on offering a Send
    button for someone who had already been told. One press and they received the
    same news twice.

    Recording it stops that. What it must not do is invent a copy of the wording:
    the message left from a mail client, and this system never saw it. A
    fabricated email in the one record meant to be evidence is worse than an
    honest gap.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "services.jsonl"
        p = patch.object(domain_service, "log_path", return_value=self.path)
        p.start(); self.addCleanup(p.stop)
        domain_service.record_registration(
            domain="goldentakinholidays.bt", customer_name="Ashika Rai",
            email="a@x.bt", service="forwarding", forwarding_kind="nameserver",
            forwarding_target="lina.ns.cloudflare.com, alec.ns.cloudflare.com",
            registry_action="created")

    def test_it_marks_the_domain_as_emailed(self):
        e = domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt")
        self.assertEqual(e["status"], domain_service.NOTIFIED)

    def test_no_copy_of_the_wording_is_stored(self):
        e = domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt")
        self.assertEqual(e["sends"][-1]["body"], "")
        self.assertNotIn("Dear Customer", e["sends"][-1]["body"])

    def test_it_says_plainly_that_no_copy_is_stored(self):
        e = domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt")
        self.assertTrue(e["sends"][-1]["manual"])
        self.assertIn("no copy is stored", e["sends"][-1]["note"].lower())

    def test_an_operators_note_is_kept(self):
        e = domain_service.record_manual_send(
            "goldentakinholidays.bt", "a@x.bt", "Sent from Outlook, quoted her ticket")
        self.assertIn("Outlook", e["sends"][-1]["note"])

    def test_it_can_be_backdated(self):
        """A ticket is often filled in after the email went out."""
        e = domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt",
                                              at="2026-09-28T10:00:00+06:00")
        self.assertEqual(e["notified_at"], "2026-09-28T10:00:00+06:00")

    def test_the_target_is_still_correctable_afterwards(self):
        """A manual send is a send, and the target is BT's own note of intent
        rather than anything the customer was told."""
        domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt")
        e = domain_service.update_forwarding("goldentakinholidays.bt", "nameserver",
                                             "ns1.correct.bt, ns2.correct.bt")
        self.assertEqual(e["forwarding_target"], "ns1.correct.bt, ns2.correct.bt")

    def test_it_needs_a_recipient(self):
        with self.assertRaises(ValueError):
            domain_service.record_manual_send("goldentakinholidays.bt", "  ")

    def test_it_needs_a_known_domain(self):
        with self.assertRaises(ValueError):
            domain_service.record_manual_send("never-seen.bt", "a@x.bt")

    def test_it_refuses_to_fake_a_second_send_at_the_same_moment(self):
        """A double-click must not write two notes."""
        domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt",
                                          at="2026-09-28T10:00:00+06:00")
        e = domain_service.record_manual_send("goldentakinholidays.bt", "a@x.bt",
                                              at="2026-09-28T10:00:00+06:00")
        self.assertEqual(len(e["sends"]), 1)


class TestTheManualSendStopsASecondEmail(unittest.TestCase):
    def test_the_existing_guard_now_fires(self):
        """The whole point. Without it the row would offer to send a customer the
        same news twice."""
        import tempfile as tf
        from fastapi.testclient import TestClient
        TOKEN = {"X-API-Token": "t"}
        with tf.TemporaryDirectory() as tmp:
            with patch.object(domain_service, "log_path",
                              return_value=Path(tmp) / "services.jsonl"):
                domain_service.record_registration(
                    domain="manual.bt", customer_name="M", email="a@x.bt",
                    service="forwarding", forwarding_kind="nameserver",
                    forwarding_target="ns1.druknet.bt", registry_action="created")
                client = TestClient(app_module.app)
                with patch.object(settings, "API_AUTH_TOKEN", "t"):
                    r = client.post("/api/v1/domain-services/manual.bt/manual-send",
                                    headers=TOKEN,
                                    json={"recipient": "a@x.bt",
                                          "note": "sent from Outlook"})
                self.assertEqual(r.status_code, 200, r.text[:200])
                # Now a real send is attempted.
                domain_service.get_state("manual.bt")
                with patch.object(settings, "API_AUTH_TOKEN", "t"), \
                     patch("notifier._deliver") as deliver, \
                     patch("notifier.settings.SMTP_ENABLED", True):
                    second = client.post("/api/v1/domain-services/notify",
                                         headers=TOKEN, json={"domain": "manual.bt"})
        self.assertEqual(second.status_code, 409, second.text[:200])
        self.assertEqual(deliver.call_count, 0, "no second email may go out")

    def test_the_row_offers_the_manual_note(self):
        root = Path(__file__).resolve().parent.parent
        js = (root / "static" / "js" / "app.js").read_text()
        html = (root / "templates" / "index.html").read_text()
        self.assertIn("Already emailed by hand?", js)
        self.assertIn('id="ds-manual-send"', html)
        self.assertIn('id="ds-ms-to"', html)

    def test_a_manual_note_is_not_offered_as_an_email_to_read(self):
        """There is no body. Offering "View sent email" would open an empty box
        and imply a copy exists."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text()
        body = js_function(js, "showSentEmail")
        self.assertIn("n.manual", body)
        self.assertIn("No copy was stored", body)

    def test_the_two_panels_do_not_stack(self):
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text()
        self.assertIn("closeForwardingEditor()", js_function(js, "openManualSend"))
        self.assertIn("closeManualSend()", js_function(js, "openForwardingEditor"))
