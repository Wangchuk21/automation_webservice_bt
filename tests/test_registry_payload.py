"""
Tests for build_registry_payload, which decides what is actually sent to
nic.bt.bt.

The form used to expose eight of the registry's 23 fields. The other fifteen
were written out as literals in two places -- once for the create path and once
for the update path -- which is how ten Bhutan Telecom values went unnoticed.
The literals are gone; this function resolves all 23 from the spec, and these
tests hold it to producing exactly what the literals produced.

The compatibility test is the important one: filling the form in the way an
operator did before must produce a byte-identical submission, or a routine
provisioning that used to work would now send something different to a live
national registry.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from config import settings
from nic_client import (
    REGISTRY_FIELDS, REGISTRY_GROUPS, build_registry_payload, missing_registry_fields,
    registry_field_spec,
)

CUSTOMER = {
    "customername": "Karma Enterprises Pvt Ltd",
    "address": "Norzin Lam, Thimphu",
    "postalcode": "11002",
    "phone": "+97517110022",
    "email": "karma@example.bt",
    "country": "BT",
    "reg_renewal": "2026-09-28",
}


def payload(**overrides):
    provided = dict(CUSTOMER)
    provided.update(overrides)
    return build_registry_payload(provided, "wank", ".com.bt", "2026-09-28")


class TestEveryFieldIsSent(unittest.TestCase):
    def test_all_twenty_three_are_present(self):
        got = payload()
        self.assertEqual(set(got), {f["name"] for f in REGISTRY_FIELDS})
        self.assertEqual(len(got), 23)

    def test_no_field_is_blank(self):
        for name, value in payload().items():
            self.assertTrue(str(value).strip(), f"{name} would be sent empty")

    def test_missing_reports_nothing_for_a_complete_form(self):
        self.assertEqual(missing_registry_fields(payload()), [])

    def test_missing_reports_named_fields(self):
        got = build_registry_payload({}, "wank", ".bt", "2026-09-28")
        # Only the customer and the computed pair can be absent; the Bhutan
        # Telecom defaults always have a value.
        self.assertIn("customername", missing_registry_fields(got))
        self.assertNotIn("registrar", missing_registry_fields(got))
        self.assertNotIn("tech_fax", missing_registry_fields(got))


class TestDerivedFieldsFollowTheCustomer(unittest.TestCase):
    """The registry's own convention: the technical contact is the domain
    owner, so tech_* carries the customer's details, not Bhutan Telecom's."""

    def test_technical_fields_copy_the_registrant(self):
        got = payload()
        self.assertEqual(got["tech_name"], CUSTOMER["customername"])
        self.assertEqual(got["tech_address"], CUSTOMER["address"])
        self.assertEqual(got["tech_postalcode"], CUSTOMER["postalcode"])
        self.assertEqual(got["tech_phone"], CUSTOMER["phone"])
        self.assertEqual(got["tech_country"], CUSTOMER["country"])
        self.assertEqual(got["tech_email"], CUSTOMER["email"])

    def test_billing_fields_copy_the_registrant(self):
        got = payload()
        self.assertEqual(got["billing_name"], CUSTOMER["customername"])
        self.assertEqual(got["billing_address"], CUSTOMER["address"])
        self.assertEqual(got["billing_contact"], CUSTOMER["phone"])
        self.assertEqual(got["billing_country"], CUSTOMER["country"])
        self.assertEqual(got["billing_email"], CUSTOMER["email"])

    def test_a_derived_field_can_be_overridden(self):
        """A registrar agent may have its own billing contact; the form must be
        able to say so."""
        got = payload(billing_contact="billing@druk.net.bt")
        self.assertEqual(got["billing_contact"], "billing@druk.net.bt")
        self.assertEqual(got["tech_phone"], CUSTOMER["phone"],
                         "overriding one must not disturb its siblings")


class TestExplicitValuesWin(unittest.TestCase):
    def test_any_field_can_be_set_by_hand(self):
        got = payload(tech_fax="+975-2-322233")
        self.assertEqual(got["tech_fax"], "+975-2-322233")

    def test_whitespace_is_trimmed(self):
        self.assertEqual(payload(customername="  Karma  ")["customername"], "Karma")

    def test_a_blank_value_falls_back_rather_than_being_sent_empty(self):
        """Sending "" would override the derivation with nothing, and the
        registry would reject it."""
        got = payload(phone="   ")
        self.assertEqual(got["phone"], "+975")
        self.assertEqual(got["tech_phone"], "+975")

    def test_unknown_keys_are_dropped_not_forwarded(self):
        """The registry is form-encoded; a field it does not recognise could be
        stored somewhere unintended."""
        got = payload(**{"is_admin": "1", "tech_name; DROP": "x"})
        self.assertNotIn("is_admin", got)
        self.assertEqual(len(got), 23)


class TestCompatibilityWithTheOldLiteralPayload(unittest.TestCase):
    """The exact body the two hardcoded dicts used to send."""

    def setUp(self):
        self.expected = {
            "domain": "wank", "ext": ".com.bt",
            "registrar": settings.NIC_REGISTRAR, "reg_renewal": "2026-09-28",
            "customername": "Karma Enterprises", "address": "Thimphu, Bhutan",
            "postalcode": "11002", "phone": "+97517110022", "email": "a@b.bt",
            "country": "BT",
            "tech_name": "Karma Enterprises", "tech_address": "Thimphu, Bhutan",
            "tech_postalcode": "11002", "tech_phone": "+97517110022",
            "tech_fax": settings.NIC_PLACEHOLDER, "tech_country": "BT",
            "tech_email": "a@b.bt",
            "billing_name": "Karma Enterprises", "billing_address": "Thimphu, Bhutan",
            "billing_contact": "+97517110022", "billing_fax": settings.NIC_PLACEHOLDER,
            "billing_country": "BT", "billing_email": "a@b.bt",
        }

    def _build(self):
        return build_registry_payload(
            {"customername": "Karma Enterprises", "address": "Thimphu, Bhutan",
             "postalcode": "11002", "phone": "+97517110022", "email": "a@b.bt",
             "country": "BT", "reg_renewal": "2026-09-28"},
            "wank", ".com.bt", "2026-09-28")

    def test_identical_to_the_previous_submission(self):
        self.assertEqual(self._build(), self.expected)

    def test_no_key_added_or_lost(self):
        self.assertEqual(set(self._build()), set(self.expected))


class TestGroups(unittest.TestCase):
    def test_every_field_belongs_to_a_declared_group(self):
        keys = {k for k, _ in REGISTRY_GROUPS}
        for f in REGISTRY_FIELDS:
            self.assertIn(f["group"], keys, f["name"])

    def test_the_four_sections_match_the_portals_layout(self):
        by_group = {k: [f["name"] for f in REGISTRY_FIELDS if f["group"] == k]
                    for k, _ in REGISTRY_GROUPS}
        self.assertEqual(by_group["domain"],
                         ["domain", "ext", "registrar", "reg_renewal"])
        self.assertEqual(len(by_group["contact"]), 6)
        self.assertEqual(len(by_group["technical"]), 7)
        self.assertEqual(len(by_group["billing"]), 6)

    def test_the_groups_reach_the_api(self):
        spec = registry_field_spec()
        self.assertEqual([g[0] for g in spec["groups"]], [g[0] for g in REGISTRY_GROUPS])


class TestNicFieldsValidation(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def test_a_mistyped_field_name_is_rejected(self):
        """The client drops unknown keys for safety, which would make a typo
        vanish silently. Failing here names it instead."""
        r = self.client.post("/api/v1/nic/register", json={
            "domain": "wank.bt", "customer_name": "Karma", "email": "a@b.bt",
            "nic_fields": {"tech_faxx": "+975-2-322233"},
        })
        self.assertEqual(r.status_code, 422)
        body = r.json()["detail"]
        self.assertIn("tech_faxx", body[0]["msg"])

    def test_a_valid_field_name_is_accepted(self):
        with unittest.mock.patch.object(
                app_module.NICClient, "register_or_update_domain") as call:
            call.return_value = {"success": True, "action": "created", "domain": "wank.bt"}
            r = self.client.post("/api/v1/nic/register", json={
                "domain": "wank.bt", "customer_name": "Karma", "email": "a@b.bt",
                "nic_fields": {"tech_fax": "+975-2-322233", "billing_contact": "975"},
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(call.call_args.kwargs["fields"]["tech_fax"], "+975-2-322233")


if __name__ == "__main__":
    unittest.main(verbosity=2)
