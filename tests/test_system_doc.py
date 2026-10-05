"""
The system diagram.

Two things can go wrong with documentation like this, and neither announces
itself: the boxes stop lining up after an edit, and a number in it quietly stops
being true. Both were caught by hand here -- a hand-counted diagram was off by one
on most of its lines -- so they are checked here instead.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "SYSTEM.md"


def diagram_lines(text):
    """The lines inside the first fenced block."""
    blocks = re.findall(r"```\n(.*?)\n```", text, re.S)
    return blocks[0].splitlines() if blocks else []


class TestTheDiagramExists(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text()
        self.lines = diagram_lines(self.text)

    def test_there_is_a_diagram(self):
        self.assertTrue(self.lines, "no fenced diagram block found in docs/SYSTEM.md")

    def test_the_fences_are_balanced(self):
        self.assertEqual(self.text.count("```") % 2, 0,
                         "an unclosed code fence makes the rest of the file a code block")

    def test_no_trailing_whitespace(self):
        """It renders as trailing space in the source and trips linters."""
        for i, line in enumerate(self.text.splitlines(), 1):
            self.assertEqual(line, line.rstrip(), f"line {i} has trailing whitespace")


class TestTheBoxesLineUp(unittest.TestCase):
    """The container box: every line between its top and bottom border."""

    def _container(self):
        lines = diagram_lines(DOC.read_text())
        top = next(i for i, l in enumerate(lines) if "┌" in l and "▼" in l)
        indent = len(lines[top]) - len(lines[top].lstrip())
        # Match the container's own indent: nested boxes inside it have "└" too,
        # and picking the first of those silently checked the wrong thing.
        bottom = next(i for i, l in enumerate(lines)
                      if i > top and l[:indent].strip() == ""
                      and l[indent:indent + 1] == "└")
        return lines[top:bottom + 1]

    def test_every_container_line_is_the_same_width(self):
        widths = {len(l) for l in self._container()}
        self.assertEqual(len(widths), 1,
                         f"container lines are {sorted(widths)} chars -- a border "
                         f"has drifted out of line")

    def test_the_top_border_holds_the_incoming_arrow(self):
        lines = diagram_lines(DOC.read_text())
        top = next(l for l in lines if "┌" in l)
        self.assertIn("▼", top, "the arrow from the browser should land on the border")

    def test_the_stems_sit_on_the_branch_points(self):
        """A connector that points at nothing is worse than no connector."""
        lines = diagram_lines(DOC.read_text())
        top = next(i for i, l in enumerate(lines) if "┌" in l and "▼" in l)
        indent = len(lines[top]) - len(lines[top].lstrip())
        bottom_i = next(i for i, l in enumerate(lines)
                        if i > top and l[indent:indent + 1] == "└")
        bottom = lines[bottom_i]
        branches = [i for i, ch in enumerate(bottom) if ch == "┬"]
        self.assertEqual(len(branches), 2, "expected one branch per panel")
        stem = lines[bottom_i + 1]
        for col in branches:
            self.assertEqual(stem[col], "│",
                             f"the stem at column {col} is not under its branch point")

    def test_the_named_panels_are_both_present(self):
        joined = "\n".join(diagram_lines(DOC.read_text()))
        for host in ("thimpchu", "yongnay"):
            self.assertIn(host, joined, f"{host} is missing from the diagram")


class TestTheDiagramIsStillTrue(unittest.TestCase):
    """
    A number in a diagram stops being true the moment the code changes and
    nothing complains. These are the claims that would rot fastest.
    """

    def setUp(self):
        self.text = DOC.read_text()

    def test_the_endpoint_count_matches_the_code(self):
        actual = len(re.findall(r'@app\.(?:get|post|patch|delete)\("',
                                (ROOT / "app.py").read_text()))
        claimed = re.search(r"(\d+) endpoints", self.text)
        self.assertIsNotNone(claimed, "the diagram no longer states an endpoint count")
        self.assertEqual(int(claimed.group(1)), actual,
                         f"the diagram says {claimed.group(1)} endpoints, app.py has {actual}")

    def test_the_module_names_it_lists_all_exist(self):
        listed = re.findall(r"^\| `([a-z_]+\.py)`", self.text, re.M)
        self.assertGreater(len(listed), 5, "the layout table looks empty")
        for name in listed:
            self.assertTrue((ROOT / name).exists(), f"{name} is listed but does not exist")

    def test_the_recorded_paths_are_the_configured_ones(self):
        from config import settings
        joined = "\n".join(diagram_lines(self.text))
        for path in (settings.ACTIVITY_LOG, settings.SURRENDER_AUDIT_LOG,
                     settings.SUSPENSION_AUDIT_LOG):
            self.assertIn(path.lstrip("./"), joined,
                          f"{path} is configured but not shown in the diagram")

    def test_it_names_the_deployment_traps(self):
        """The two that have actually cost time, and which look harmless."""
        self.assertIn("COPY", self.text, "the Dockerfile's explicit module list")
        self.assertIn("--build", self.text, "docker compose up without --build")


class TestRiskLevelsLookDifferent(unittest.TestCase):
    """
    The suspension card borrowed `.surrender-warning`, which is the rose styling
    used by the surrender card -- the one that destroys a customer's data for
    good, with no way back.

    That gave the two risk levels the same visual weight, and both appear on the
    same page. A box styled like the worst thing in the app is a box people stop
    seeing, and this one carries the instruction to check a domain before cutting
    off a live website.

    The message is kept, deliberately. Removing a safety warning because it looks
    loud would be the wrong trade; the styling was what was wrong.
    """

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "templates" / "index.html").read_text()
        cls.css = (ROOT / "static" / "css" / "style.css").read_text()

    def _card(self, marker):
        i = self.html.index(marker)
        j = self.html.index("</section>", i)
        return self.html[i:j]

    def test_the_surrender_card_keeps_the_rose_warning(self):
        """It destroys customer data permanently. That genuinely deserves the
        strongest styling in the app."""
        card = self._card('id="surrender"')
        self.assertIn('class="surrender-warning"', card)

    def test_the_suspension_card_does_not_borrow_it(self):
        card = self._card('id="suspension-review"')
        self.assertNotIn('class="surrender-warning"', card,
                         "suspension must not look like data destruction")

    def test_it_still_warns_though(self):
        """Quieter, not absent. The instruction to check the domain is the point
        of the box."""
        card = self._card('id="suspension-review"')
        self.assertIn("live customer website", card)
        self.assertIn("detects", card)

    def test_the_notice_opens_without_an_unqualified_this(self):
        """"This switches off a live customer website" -- the card, the button or
        the nightly job could each be what "this" meant, and a safety notice is
        the worst place to make the reader work it out."""
        card = self._card('id="suspension-review"')
        self.assertNotIn(">This ", card, "the antecedent of 'This' is never stated")
        self.assertIn("Suspending switches off", card,
                      "the warning must name the action it qualifies")

    def test_amber_is_defined_and_distinct_from_the_rose(self):
        self.assertIn(".notice-amber", self.css)
        amber = self.css[self.css.index(".notice-amber"):][:400]
        self.assertNotIn("244, 63, 94", amber,
                         "the amber notice must not reuse the rose danger colour")

    def test_the_detect_only_claim_is_still_true_of_the_code(self):
        """The warning says the job never suspends by itself. If that ever
        changes the box becomes a lie, so it is asserted against the crontab."""
        crontab = (ROOT / "deploy" / "crontab").read_text()
        command = [l for l in crontab.splitlines()
                   if "suspend_expired" in l and not l.strip().startswith("#")]
        self.assertTrue(command, "the nightly command is missing from the crontab")
        self.assertNotIn("--live", command[0],
                         "the warning claims nothing is suspended automatically, "
                         "and --live would make that false")
