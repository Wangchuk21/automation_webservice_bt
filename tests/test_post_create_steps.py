"""
Tests for the two post-create steps: IPv6 on cPanel, SFTP access on DirectAdmin.

Both are post-create steps, and the property that matters most is that a failure
in either never fails an account that was created correctly. An account that
exists but cannot SFTP is a problem the operator can see and fix; an account
that exists while the API says "failed" invites a retry, which creates a second
one.

The DirectAdmin step is also the only code in this project that edits a file
governing SSH access for every customer, so its guard rails are tested hard: it
must never load a config it has not validated.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner


def da(monkey_results):
    """A DirectAdminProvisioner whose SSH returns canned output."""
    p = DirectAdminProvisioner.__new__(DirectAdminProvisioner)
    p.ssh = MagicMock()
    p.ssh.execute.return_value = (0, monkey_results, "")
    return p


def cp(monkey_results):
    p = CPanelProvisioner.__new__(CPanelProvisioner)
    p.ssh = MagicMock()
    p.ssh.execute.return_value = (0, monkey_results, "")
    return p


class TestDirectAdminSftpAccess(unittest.TestCase):
    def test_adds_the_account_and_reports_success(self):
        got = da("OK\n").allow_sftp_user("wankbt")
        self.assertTrue(got["success"])
        self.assertIn("AllowUsers", got["message"])

    def test_an_account_already_listed_is_a_no_op(self):
        """Re-adding would duplicate the name, and this runs on every update."""
        got = da("ALREADY\n").allow_sftp_user("wankbt")
        self.assertTrue(got["success"])
        self.assertIn("already", got["message"].lower())

    def test_a_refused_edit_reports_the_reason(self):
        got = da("NOBACKUP\n").allow_sftp_user("wankbt")
        self.assertFalse(got["success"])
        self.assertIn("nothing was changed", got["message"])

    def test_an_invalid_config_is_never_reloaded(self):
        """This file is the only thing between the internet and every
        customer's file transfer. An invalid sshd_config stops sshd coming
        back, so the edit must be reverted rather than loaded."""
        got = da("INVALID\n").allow_sftp_user("wankbt")
        self.assertFalse(got["success"])
        self.assertIn("NOT reloaded", got["message"])

    def test_a_failed_reload_is_reported_not_claimed_as_success(self):
        got = da("RELOADFAIL\n").allow_sftp_user("wankbt")
        self.assertFalse(got["success"])
        self.assertIn("restored", got["message"])

    def test_an_unexpected_answer_fails_closed(self):
        got = da("something nobody expected\n").allow_sftp_user("wankbt")
        self.assertFalse(got["success"])


class TestDirectAdminRefusesBadNames(unittest.TestCase):
    """
    Written after a test string reached the live server.

    shlex.quote stopped the string being executed, but an AllowUsers entry is a
    whitespace-separated list, so "bad name; rm -rf /" was written as five real
    entries and the method reported success. A name that is not a plain account
    name is now refused before anything is written.
    """

    def test_names_that_are_not_account_names_are_refused(self):
        for bad in ("bad name; rm -rf /", "wank.hmt", "-rf", "UPPER/../etc",
                    "wank bt", "wank;reboot", "$(id)", "`id`", "a|b"):
            got = da("OK\n").allow_sftp_user(bad)
            self.assertFalse(got["success"], f"{bad!r} was accepted")
            self.assertIn("nothing was written", got["message"])

    def test_nothing_is_sent_to_the_server_for_a_refused_name(self):
        p = da("OK\n")
        p.allow_sftp_user("bad name; rm -rf /")
        p.ssh.execute.assert_not_called()

    def test_empty_is_refused(self):
        for blank in ("", "   ", None):
            self.assertFalse(da("OK\n").allow_sftp_user(blank)["success"])

    def test_an_ordinary_name_is_still_accepted(self):
        self.assertTrue(da("OK\n").allow_sftp_user("wank-bt_2")["success"])


class TestDirectAdminCommandShape(unittest.TestCase):
    def test_the_password_is_piped_not_passed_as_an_argument(self):
        """An argument is visible to every user on the box through `ps`. This
        is the same leak that was fixed once already, so the password must
        appear only in stdin_data and never in the command string."""
        with patch("provisioners.directadmin.settings") as st:
            st.DIRECTADMIN.sudo_password = "supersecret"
            p = da("OK\n")
            p.allow_sftp_user("wankbt")
        args, kwargs = p.ssh.execute.call_args
        self.assertNotIn("supersecret", args[0])
        self.assertIn("supersecret", kwargs["stdin_data"])
        self.assertIn("sh -s", args[0])

    def test_it_validates_before_reloading(self):
        p = da("OK\n")
        p.allow_sftp_user("wankbt")
        script = p.ssh.execute.call_args[1]["stdin_data"]
        self.assertIn("sshd -t", script)
        # The reload must come after the check, or the check means nothing.
        self.assertLess(script.index("sshd -t"), script.index("systemctl reload"))

    def test_it_takes_a_backup_before_editing(self):
        p = da("OK\n")
        p.allow_sftp_user("wankbt")
        script = p.ssh.execute.call_args[1]["stdin_data"]
        self.assertIn("cp -p", script)
        self.assertLess(script.index("cp -p"), script.index("python3"))

    def test_it_reloads_rather_than_restarts(self):
        """A restart would drop every customer's live SFTP session."""
        p = da("OK\n")
        p.allow_sftp_user("wankbt")
        script = p.ssh.execute.call_args[1]["stdin_data"]
        self.assertIn("reload", script)
        # The word appears in a comment; what must not appear is the command.
        for banned in ("systemctl restart", "service sshd restart",
                       "service ssh restart", "/etc/init.d/ssh restart"):
            self.assertNotIn(banned, script, f"{banned} would drop live sessions")


class TestCpanelIpv6(unittest.TestCase):
    """
    The function is ipv6_enable_account, taken from the server's own
    /usr/local/cpanel/Whostmgr/API/1/IPv6.pm. The obvious guess, ipv6_create,
    does not exist on WHM API 1 -- verified by reading the shipped module, not
    from memory.
    """

    def test_reports_the_assigned_range(self):
        got = cp('{"metadata":{"result":1,"reason":"OK"}}').enable_ipv6("wankbt")
        self.assertTrue(got["success"])
        self.assertIn("wankbt", got["message"])

    def test_a_failure_says_why(self):
        got = cp('{"metadata":{"result":0,"reason":"No such account"}}').enable_ipv6("wankbt")
        self.assertFalse(got["success"])
        self.assertIn("No such account", got["message"])

    def test_an_account_that_already_has_one_is_not_a_failure(self):
        """cPanel refuses the second attempt rather than issuing another
        address, and this runs on every update, not just creation."""
        got = cp('{"metadata":{"result":0,"reason":"already has an IP"}}').enable_ipv6("wankbt")
        self.assertTrue(got["success"])
        self.assertIn("already", got["message"].lower())

    def test_no_output_at_all_is_not_reported_as_success(self):
        got = cp("").enable_ipv6("wankbt")
        self.assertFalse(got["success"])

    def test_an_empty_username_is_refused(self):
        self.assertFalse(cp("OK").enable_ipv6("  ")["success"])

    def test_it_uses_the_verified_function_and_range(self):
        p = cp('{"metadata":{"result":1}}')
        with patch("provisioners.cpanel.settings") as st:
            st.CPANEL_IPV6_RANGE = "SHARED"
            st.CPANEL.sudo_password = "pw"
            p.enable_ipv6("wankbt")
        command = p.ssh.execute.call_args[0][0]
        self.assertIn("ipv6_enable_account", command)
        self.assertIn("user=wankbt", command)
        self.assertIn("range=SHARED", command)
        self.assertNotIn("ipv6_create", command)

    def test_the_password_is_piped_not_passed_as_an_argument(self):
        p = cp('{"metadata":{"result":1}}')
        with patch("provisioners.cpanel.settings") as st:
            st.CPANEL_IPV6_RANGE = "SHARED"
            st.CPANEL.sudo_password = "supersecret"
            p.enable_ipv6("wankbt")
        command = p.ssh.execute.call_args[0][0]
        self.assertNotIn("supersecret", command)


class TestTheseNeverFailAnAccount(unittest.TestCase):
    """Both return a result rather than raising, for the same reason."""

    def test_neither_raises_on_a_server_error(self):
        p = cp("")
        p.ssh.execute.return_value = (255, "", "ssh: connect failed")
        self.assertFalse(p.enable_ipv6("wankbt")["success"])

    def test_both_carry_a_step_name(self):
        self.assertEqual(da("OK\n").allow_sftp_user("wankbt")["step"], "sftp_access")
        self.assertEqual(cp('{"metadata":{"result":1}}').enable_ipv6("w")["step"], "ipv6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
