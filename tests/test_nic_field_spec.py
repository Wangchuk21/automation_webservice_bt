"""
Tests for the nic.bt.bt registry field spec.

The point of the spec is that nothing the registry requires may be invisible.
It was introduced after ten required fields were found hardcoded inside
nic_client.py, so the client and the dashboard shared no list and the operator
saw none of them.

These assert the spec is complete, that the client takes its Bhutan Telecom
values from config rather than literals, and that the API exposes it.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
import nic_client
from config import settings
from nic_client import REGISTRY_FIELDS, registry_field_spec

# The fields nic.bt.bt marks required on its domain form, read from the live
# portal's create screen.
PORTAL_REQUIRED = {
    "domain", "ext", "registrar", "reg_renewal", "customername", "address",
    "postalcode", "phone", "email", "country", "tech_name", "tech_address",
    "tech_postalcode", "tech_phone", "tech_fax", "tech_country", "tech_email",
    "billing_name", "billing_address", "billing_contact", "billing_fax",
    "billing_country", "billing_email",
}


class TestSpecCompleteness(unittest.TestCase):
    def test_covers_every_field_the_portal_requires(self):
        names = {f["name"] for f in REGISTRY_FIELDS}
        self.assertEqual(names, PORTAL_REQUIRED,
                         "spec and portal requirements have drifted apart")

    def test_every_field_is_marked_required(self):
        for f in REGISTRY_FIELDS:
            self.assertTrue(f.get("required"), f"{f['name']} should be required")

    def test_every_field_has_a_source_and_label(self):
        for f in REGISTRY_FIELDS:
            self.assertIn(f["source"], ("customer", "derived", "default", "computed"),
                          f"{f['name']} has an unknown source")
            self.assertTrue(f.get("label"), f"{f['name']} needs a label for the form")

    def test_derived_fields_name_their_origin(self):
        for f in REGISTRY_FIELDS:
            if f["source"] == "derived":
                self.assertIn("derived_from", f, f"{f['name']} must name its origin")
                self.assertIn(f["derived_from"], PORTAL_REQUIRED)

    def test_no_duplicate_field_names(self):
        names = [f["name"] for f in REGISTRY_FIELDS]
        self.assertEqual(len(names), len(set(names)))


class TestDefaultsComeFromConfig(unittest.TestCase):
    def test_spec_defaults_match_config(self):
        by_name = {f["name"]: f for f in registry_field_spec()["fields"]}
        self.assertEqual(by_name["registrar"]["default"], settings.NIC_REGISTRAR)
        self.assertEqual(by_name["tech_fax"]["default"], settings.NIC_PLACEHOLDER)
        self.assertEqual(by_name["billing_fax"]["default"], settings.NIC_PLACEHOLDER)

    def test_technical_contact_is_the_domain_owner(self):
        """
        The registry follows the international convention: tech_* is the
        registrant, billing_* is the registrar or its agent. Confirmed on real
        records, where tech_email equalled the registrant's own email.

        Getting this backwards publishes BT as the technical contact for every
        customer domain.
        """
        by_name = {f["name"]: f for f in registry_field_spec()["fields"]}
        for field, origin in [("tech_name", "customername"), ("tech_address", "address"),
                              ("tech_postalcode", "postalcode"), ("tech_phone", "phone"),
                              ("tech_country", "country"), ("tech_email", "email")]:
            self.assertEqual(by_name[field]["source"], "derived", field)
            self.assertEqual(by_name[field]["derived_from"], origin, field)

    def test_billing_contact_is_also_the_customer(self):
        """
        Per Bhutan Telecom's requirement, nothing about BT is published as a
        contact: the billing block carries the customer's details too, not the
        registrar's agent as the sampled portal records do.
        """
        by_name = {f["name"]: f for f in registry_field_spec()["fields"]}
        for field, origin in [("billing_name", "customername"),
                              ("billing_address", "address"),
                              ("billing_contact", "phone"),
                              ("billing_country", "country"),
                              ("billing_email", "email")]:
            self.assertEqual(by_name[field]["source"], "derived", field)
            self.assertEqual(by_name[field]["derived_from"], origin, field)

    def test_no_bt_contact_detail_is_published(self):
        """The registrar is the only Bhutan Telecom value on a record."""
        defaults = [f["name"] for f in registry_field_spec()["fields"]
                    if f["source"] == "default"]
        self.assertEqual(sorted(defaults), ["billing_fax", "registrar", "tech_fax"])
        src = Path(nic_client.__file__).read_text()
        for literal in ("Bhutan Telecom Ltd", "systems@bt.bt", "DrukNet Systems",
                        "+975-2-343434"):
            self.assertNotIn(f'"{literal}"', src,
                             f"{literal!r} must not be published on a customer record")

    def test_client_has_no_hardcoded_bt_details(self):
        """The regression this exists to prevent: values living in the code."""
        src = Path(nic_client.__file__).read_text()
        for literal in ("DrukNet Systems", "systems@bt.bt", "Bhutan Telecom Ltd"):
            self.assertNotIn(f'"{literal}"', src,
                             f"{literal!r} is hardcoded in nic_client; use config")

    def test_config_overrides_reach_the_spec(self):
        orig = settings.NIC_REGISTRAR
        settings.NIC_REGISTRAR = "Changed Registrar"
        try:
            by_name = {f["name"]: f for f in registry_field_spec()["fields"]}
            self.assertEqual(by_name["registrar"]["default"], "Changed Registrar")
        finally:
            settings.NIC_REGISTRAR = orig

    def test_source_counts_add_up(self):
        spec = registry_field_spec()
        self.assertEqual(sum(spec["source_counts"].values()), len(REGISTRY_FIELDS))


class TestFieldSpecEndpoint(unittest.TestCase):
    def setUp(self):
        self._tok = settings.API_AUTH_TOKEN
        settings.API_AUTH_TOKEN = "tok"
        self.client = TestClient(app_module.app)

    def tearDown(self):
        settings.API_AUTH_TOKEN = self._tok

    def test_returns_the_whole_spec(self):
        r = self.client.get("/api/v1/nic/field-spec", headers={"X-API-Token": "tok"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(len(d["fields"]), len(REGISTRY_FIELDS))
        self.assertEqual(d["required_count"], len(REGISTRY_FIELDS))

    def test_requires_a_token(self):
        r = self.client.get("/api/v1/nic/field-spec")
        self.assertEqual(r.status_code, 401)

    def test_every_default_field_is_editable_in_the_response(self):
        """The operator must be able to override every BT default, not just see it."""
        d = self.client.get("/api/v1/nic/field-spec",
                            headers={"X-API-Token": "tok"}).json()
        defaults = [f for f in d["fields"] if f["source"] == "default"]
        # Derived from the spec rather than hardcoded, so a change in which
        # fields are BT-supplied does not need the number updated here too.
        self.assertEqual(len(defaults),
                         sum(1 for f in REGISTRY_FIELDS if f["source"] == "default"))
        self.assertTrue(defaults)
        for f in defaults:
            self.assertIn("default", f, f"{f['name']} exposes no default to edit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
