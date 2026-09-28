"""
Tests for registry-field validation during account creation.

Found because ticking "Register / Update on nic.bt.bt" always failed with
"postal_code is required". Two things were wrong and both are covered here:

The check read payload.postal_code, a field the form stopped sending when the
eight hand-written registry inputs were replaced by the 23 generated from the
field spec. Nothing in the form could ever satisfy it.

Worse, it ran after create_account. The account was created and the API returned
422, so the operator saw "Provisioning Failed" for an account that existed, and
retrying collided with the first one. A validation check that runs after the
thing it validates is not a validation check.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import app as app_module
from nic_client import REGISTRY_FIELDS
from provisioners.base import ProvisionerResult

# What the dashboard sends for a customer whose registrant details are filled in.
FILLED = {
    "customername": "Karma Enterprises", "address": "Norzin Lam",
    "postalcode": "11002", "phone": "+97517110022", "email": "karma@example.bt",
    "country": "BT", "reg_renewal": "2026-10-28", "ext": ".bt",
}

def created():
    """A real ProvisionerResult, since the handler reads its attributes."""
    return ProvisionerResult(
        success=True, panel="cpanel", domain="wank.bt", username="wank",
        password="pw", email="karma@example.bt", web_url="https://wank.bt:2083",
        sftp_host="202.144.128.216", sftp_port=22, doc_root="public_html/",
        nameservers=[], handover_text="", message="created", raw_response={},
    )


class TestRegistryFieldsAreCheckedBeforeAnythingIsCreated(unittest.TestCase):
    def setUp(self):
        app_module.app.dependency_overrides[app_module.require_api_token] = lambda: None
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.app.dependency_overrides = {}

    def test_a_complete_form_is_accepted(self):
        with patch.object(app_module, "get_cpanel_provisioner") as prov, \
             patch.object(app_module, "NICClient"):
            prov.return_value.create_account.return_value = created()
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": True,
                "nic_fields": FILLED,
            })
        self.assertEqual(r.status_code, 200, r.text)
        # The body, not just the status. A 200 with a null body is what a
        # handler that falls off the end returns, and a status-only assertion
        # passes straight over it -- which is how the create endpoint once
        # returned null while this test stayed green.
        body = r.json()
        self.assertIsInstance(body, dict, "the endpoint returned no payload")
        self.assertTrue(body.get("success"))
        self.assertEqual(body["data"]["domain"], "wank.bt")
        self.assertIn("data", body)

    def test_a_missing_field_is_refused(self):
        with patch.object(app_module, "get_cpanel_provisioner") as prov:
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": True,
                "nic_fields": {"ext": ".bt"},
            })
        self.assertEqual(r.status_code, 422)
        self.assertIn("customername", r.json()["detail"])
        # The one that actually broke it.
        self.assertNotIn("postal_code is required", r.json()["detail"])

    def test_nothing_is_created_when_a_field_is_missing(self):
        """The whole point. Previously the account was created and then the
        request failed, so the operator saw a failure for an account that
        existed and a retry collided with it."""
        with patch.object(app_module, "get_cpanel_provisioner") as prov:
            prov.return_value.create_account.return_value = created()
            self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": True,
                "nic_fields": {"ext": ".bt"},
            })
        prov.return_value.create_account.assert_not_called()

    def test_it_names_fields_by_the_names_the_registry_uses(self):
        """So the message matches the labels the operator is looking at."""
        with patch.object(app_module, "get_cpanel_provisioner"):
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": True,
                "nic_fields": {"ext": ".bt"},
            })
        detail = r.json()["detail"]
        known = {f["name"] for f in REGISTRY_FIELDS}
        named = [w.strip() for w in detail.split(":")[1].split(",") if w.strip()]
        for word in named[:3]:
            self.assertIn(word, known, f"{word} is not a registry field name")

    def test_the_old_legacy_field_is_no_longer_required(self):
        """The form stopped sending it; the check must not wait for it."""
        with patch.object(app_module, "get_cpanel_provisioner") as prov, \
             patch.object(app_module, "NICClient"):
            prov.return_value.create_account.return_value = created()
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": True,
                "nic_fields": FILLED,   # no top-level postal_code anywhere
            })
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_dry_run_is_not_blocked(self):
        """A dry run creates nothing and contacts nothing, so requiring the
        registry fields would only get in the way of trying the form out."""
        with patch.object(app_module, "get_cpanel_provisioner") as prov:
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt",
                "register_nic": True, "dry_run": True, "nic_fields": {},
            })
        self.assertNotEqual(r.status_code, 422)

    def test_unticking_the_registry_option_skips_the_check(self):
        with patch.object(app_module, "get_cpanel_provisioner") as prov:
            prov.return_value.create_account.return_value = created()
            r = self.client.post("/api/v1/accounts/create", json={
                "panel": "cpanel", "domain": "wank.bt", "register_nic": False,
            })
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_check_precedes_creation_in_the_source(self):
        """A source-level guard, because moving it back is easy and silent."""
        src = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        body = src.split("async def create_account(payload: AccountCreateRequest):")[1]
        check = body.index("missing_registry_fields")
        create = body.index("prov.create_account(")
        self.assertLess(check, create,
                        "the registry check must run before the account is created")


class TestThePostCreateStepsStillRunAfterwards(unittest.TestCase):
    """The move must not have displaced anything that belongs after creation."""

    def test_sudo_steps_and_nic_registration_still_happen(self):
        src = (Path(__file__).resolve().parent.parent / "app.py").read_text()
        body = src.split("async def create_account(payload: AccountCreateRequest):")[1]
        self.assertIn("run_post_create_steps(result)", body)
        self.assertIn("register_or_update_domain(", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
