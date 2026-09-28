"""
Tests for reading the registry's extension dropdown.

The dropdown on nic.bt.bt is the authority on which extensions it accepts.
These were written after a hardcoded list was found to have drifted silently,
and after the first version of the parser leaked the "Choose.." placeholder
into the list of valid extensions.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nic_client import (
    _reject_unsupported_ext, available_extensions, split_domain_ext,
)

# Shaped like the real create form: the empty option carries the value
# NULL_VALUE behind a "Choose.." label, which is the trap.
FORM = """<form>
<select name="ext">
  <option value="NULL_VALUE" selected>Choose..</option>
  <option value=".bt">.bt</option>
  <option value=".com.bt">.com.bt</option>
  <option value=".org.bt">.org.bt</option>
  <option value=".gov.bt">.gov.bt</option>
  <option value=".edu.bt">.edu.bt</option>
  <option value=".net.bt">.net.bt</option>
</select>
<select name="country"><option value="">- All -</option><option value="BT">BT</option></select>
</form>"""

# The six extensions the live portal offers, read from its create screen.
LIVE = [".bt", ".com.bt", ".org.bt", ".gov.bt", ".edu.bt", ".net.bt"]


class TestAvailableExtensions(unittest.TestCase):
    def test_reads_all_six_from_the_form(self):
        self.assertEqual(available_extensions(FORM), LIVE)

    def test_placeholder_is_excluded(self):
        """"Choose.." carries the value NULL_VALUE; it is not an extension."""
        got = available_extensions(FORM)
        self.assertNotIn("NULL_VALUE", got)
        self.assertNotIn("Choose..", got)
        self.assertEqual(len(got), 6)

    def test_other_selects_are_ignored(self):
        self.assertNotIn("BT", available_extensions(FORM))
        self.assertNotIn("- All -", available_extensions(FORM))

    def test_handles_missing_or_broken_markup(self):
        self.assertEqual(available_extensions(""), [])
        self.assertEqual(available_extensions("<html>no form here</html>"), [])
        self.assertEqual(available_extensions("<<>><<"), [])

    def test_options_without_value_fall_back_to_label(self):
        html = '<select name="ext"><option>.bt</option><option>.com.bt</option></select>'
        self.assertEqual(available_extensions(html), [".bt", ".com.bt"])


class TestRejectUnsupportedExt(unittest.TestCase):
    def test_accepts_an_offered_extension(self):
        _reject_unsupported_ext(".com.bt", LIVE)  # must not raise

    def test_rejects_one_the_portal_does_not_offer(self):
        with self.assertRaises(ValueError) as ctx:
            _reject_unsupported_ext(".biz.bt", LIVE)
        self.assertIn(".biz.bt", str(ctx.exception))
        self.assertIn(".bt", str(ctx.exception), "the message should list what is available")

    def test_skips_when_the_dropdown_could_not_be_read(self):
        """A markup change must degrade to the portal's own error, not a false
        rejection of a valid extension."""
        _reject_unsupported_ext(".anything", [])

    def test_splitter_agrees_with_the_dropdown(self):
        """Every extension the splitter can produce must be one the portal offers.

        This is the test that would have caught the hardcoded list drifting.
        """
        for domain, expected in [("wank.bt", ".bt"), ("foo.com.bt", ".com.bt"),
                                 ("foo.net.bt", ".net.bt"), ("foo.edu.bt", ".edu.bt"),
                                 ("foo.gov.bt", ".gov.bt"), ("foo.org.bt", ".org.bt")]:
            _, ext = split_domain_ext(domain)
            self.assertEqual(ext, expected, domain)
            self.assertIn(ext, available_extensions(FORM),
                          f"{domain} yields {ext}, which the portal does not offer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
