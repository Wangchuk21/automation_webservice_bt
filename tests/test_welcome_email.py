"""
The welcome email.

Restructured to the sample BT's management supplied, which was a change of shape
rather than a rewrite: the renewal text was buried in the opening paragraph and
has been pulled out into its own section, the fragments have become headings, and
the three maintenance points have been reworded to be instructions rather than
descriptions.

One thing in the sample was deliberately not copied: it shows the control panel
URL as the customer's own domain, which would not reach cPanel at all. It reads as
the sample having been anonymised by substituting the account name for the server,
so the real server URL is kept and the label changed to say which one it is.
"""
import unittest
from pathlib import Path

from provisioners.base import (
    HANDOVER_HTML_TEMPLATE, HANDOVER_TEXT_TEMPLATE, BaseProvisioner,
)

DETAILS = dict(
    panel="cpanel", domain="lhalamtravel.bt", username="lhalamtravel",
    password="pRtxkdqfw73dpKL&", web_url="https://202.144.128.216:2083",
    sftp_host="202.144.128.216", sftp_port=2020,
    doc_root="/home/lhalamtravel/public_html", nameservers="ns1.bt.bt",
)

# The section headings management asked for, in order.
SECTIONS = ["Account Details", "FTP/SFTP Access (optional)",
            "Renewal and Discontinuation", "Important Maintenance Notice"]

MAINTENANCE = [
    ("Keep your software updated.",
     "This includes your core application (e.g., WordPress, Joomla), themes, and "
     "plugins or extensions."),
    ("Back up regularly.",
     "Save copies of your website files and database so you can quickly restore "
     "your site if anything goes wrong."),
    ("Use strong, unique passwords.",
     "Please change the password provided above after your first login, and "
     "update it periodically."),
]


class TestTheWelcomeEmailSaysWhatManagementAsked(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        prov = BaseProvisioner()
        cls.text = prov.format_handover(**DETAILS)
        cls.html = prov.format_handover_html(**DETAILS)

    def test_the_sections_are_present_and_in_order(self):
        for name in ("text", "html"):
            body = getattr(self, name)
            positions = [body.index(s) for s in SECTIONS if s in body]
            self.assertEqual(len(positions), len(SECTIONS),
                             f"a section is missing from the {name} part")
            self.assertEqual(positions, sorted(positions),
                             f"the sections are out of order in the {name} part")

    def test_the_maintenance_points_are_instructions(self):
        """The old wording described what to do; the new one tells the customer
        what to do, which is the point of the change."""
        for lead, body in MAINTENANCE:
            for name in ("text", "html"):
                whole = getattr(self, name)
                self.assertIn(lead, whole, f"'{lead}' missing from the {name} part")
                self.assertIn(body.split(".")[0], whole,
                              f"the detail after '{lead}' is missing from {name}")

    def test_changing_the_password_after_first_login_is_said(self):
        """New in this revision and the most actionable line in it."""
        for name in ("text", "html"):
            self.assertIn("after your first login", getattr(self, name))

    def test_the_old_wording_is_gone(self):
        """Left behind it would contradict the new wording in the other half."""
        for name in ("text", "html"):
            body = getattr(self, name)
            for old in ("Your account has been created with the following details",
                        "If you wish to use FTP, please use the following",
                        "Perform regular backups",
                        "Keep your website software updated",
                        "Use strong, unique credentials",
                        "our service to meet your web hosting needs"):
                self.assertNotIn(old, body, f"old wording survives in the {name} part")

    def test_the_renewal_terms_are_still_stated(self):
        """Pulled out of the opening paragraph, not dropped."""
        for name in ("text", "html"):
            body = getattr(self, name)
            self.assertIn("renewed annually from the date of registration", body)
            self.assertIn("before your next billing date", body)

    def test_the_aup_qualifier_survives(self):
        """The clause about suspending service is the only thing standing between
        a compromised site and an unhappy customer. Losing it in a reword would go
        unnoticed."""
        for name in ("text", "html"):
            body = getattr(self, name)
            self.assertIn("Acceptable Use Policy", body)
            self.assertIn("suspend hosting services", body)
            self.assertIn("active threat", body)

    def test_it_still_says_who_to_contact(self):
        for name in ("text", "html"):
            self.assertIn("systems@bt.bt", getattr(self, name))


class TestBothPartsSayTheSameThing(unittest.TestCase):
    """
    The email is multipart/alternative: one customer reads the plain text, another
    reads the HTML. Nothing checks they agree, so a change to one half alone
    produces an email whose two versions contradict each other -- which is exactly
    what happened when an edited body was paired with generated HTML.
    """

    @classmethod
    def setUpClass(cls):
        prov = BaseProvisioner()
        cls.text = prov.format_handover(**DETAILS)
        cls.html = prov.format_handover_html(**DETAILS)

    def test_the_credentials_appear_in_both(self):
        for value in (DETAILS["username"], DETAILS["password"],
                      DETAILS["web_url"] + "/", str(DETAILS["sftp_port"])):
            self.assertIn(value, self.text, "missing from the plain text part")
            self.assertIn(value, self.html, "missing from the HTML part")

    def test_the_domain_appears_in_both(self):
        self.assertIn(DETAILS["domain"], self.text)
        self.assertIn(DETAILS["domain"], self.html)


class TestTheControlPanelUrlIsReachable(unittest.TestCase):
    """
    The sample showed https://lhalamtravel.bt:2083/ as the control panel URL --
    the customer's own domain. cPanel is served on the server, and a domain that
    has not been pointed at the server yet resolves nowhere, so a customer
    following that link would not reach their panel at all.

    The value is untouched and the label now says what it is, so the mistake
    cannot be reintroduced by relabelling.
    """

    @classmethod
    def setUpClass(cls):
        prov = BaseProvisioner()
        cls.text = prov.format_handover(**DETAILS)
        cls.html = prov.format_handover_html(**DETAILS)

    def test_it_is_the_server_not_the_customer_domain(self):
        for name in ("text", "html"):
            body = getattr(self, name)
            self.assertIn(DETAILS["web_url"], body)
            self.assertNotIn(f"https://{DETAILS['domain']}:2083", body,
                             "the control panel URL must not be the customer's domain")

    def test_the_sftp_host_is_still_the_domain(self):
        """Which the sample agrees with, and which is right: once the domain
        points at the server it resolves to it."""
        for name in ("text", "html"):
            self.assertIn(f"sftp://{DETAILS['domain']}/", getattr(self, name))

    def test_it_is_labelled_so_the_reader_knows_which_is_which(self):
        self.assertIn("Control Panel URL", self.text)
        self.assertIn("FTP/SFTP Access (optional)", self.text)


class TestTheSubject(unittest.TestCase):
    def test_it_names_the_domain_so_the_email_can_be_found_again(self):
        import inspect
        import notifier
        src = inspect.getsource(notifier.send_customer_welcome_email)
        self.assertIn("Your Domain Registration and Web Hosting Details", src)
        self.assertIn("{result.domain}", src)

    def test_it_no_longer_calls_them_credentials(self):
        """'Details' rather than 'Credentials': the subject is read in a list of
        many, and this one covers registration and hosting together."""
        import inspect
        import notifier
        src = inspect.getsource(notifier.send_customer_welcome_email)
        self.assertNotIn("Domain Registration & Web Hosting Credentials", src)


class TestTheTemplatesHaveNoUnrenderedPlaceholders(unittest.TestCase):
    def test_every_placeholder_is_one_the_renderer_supplies(self):
        """A typo in a placeholder renders as literal braces in a customer's
        inbox, and nobody notices until someone complains."""
        import re
        for name, template in (("text", HANDOVER_TEXT_TEMPLATE),
                               ("html", HANDOVER_HTML_TEMPLATE)):
            used = set(re.findall(r"\{\{\s*([a-z_]+)", template))
            supplied = set(DETAILS)
            self.assertEqual(used - supplied, set(),
                             f"the {name} template uses {used - supplied}, "
                             f"which the renderer is never given")
