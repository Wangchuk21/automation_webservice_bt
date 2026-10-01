"""
JavaScript in static/js/app.js, checked without a browser.

Written after a change that compiled and then threw at runtime. A missing
concatenation operator turned a template-literal expression into a call on the
string before it, so the whole suspension card rendered as:

    (intermediate value)(intermediate value)(intermediate value) is not a function

It compiled. `node --check` passed it. A test that greps for the strings it
introduces passed it. It only failed when the function actually ran, in a
browser, in front of the user.

So there are two checks here, and they are not equally strong. The structural one
always runs and catches this specific shape. The harness below actually executes
the render paths under node, and is skipped when there is no node -- which is the
case on the laptop, so the structural check is what usually protects you.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "static" / "js" / "app.js"

# A line may begin with these only if the line above it left an expression open.
OPENERS = "(["
CONTINUATIONS = "+&|?:,=(["


def _ends_continuing(text: str) -> bool:
    t = text.rstrip()
    if not t:
        return True
    return t[-1] in CONTINUATIONS or t.endswith("=>")


class TestTheScriptIsStructurallySane(unittest.TestCase):
    """
    A line that begins with `(` or `[` continues the expression above it, so the
    line above must have been left open. When it was not, JavaScript reads the
    next line as a *call* on whatever came before -- which is how a string ends up
    being invoked, and how this bug looked on the page.
    """

    def test_no_line_calls_the_expression_above_it(self):
        lines = JS.read_text().splitlines()
        offenders = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("//", "*", "/*")):
                continue
            if stripped[0] not in OPENERS:
                continue
            prev = None
            for j in range(i - 1, -1, -1):
                if lines[j].strip():
                    prev = lines[j].strip()
                    break
            if prev is not None and not _ends_continuing(prev):
                offenders.append((i + 1, prev[-60:], stripped[:60]))
        self.assertEqual(
            offenders, [],
            "these lines start with ( or [ but the line above is a complete "
            "expression, so JS reads them as a call:\n" +
            "\n".join(f"  line {n}\n    ...{p}\n    {c}" for n, p, c in offenders))

    def test_every_backtick_quote_pair_is_balanced(self):
        """An unbalanced template literal swallows the rest of the file, and the
        error it produces points somewhere unrelated."""
        text = JS.read_text()
        # Count only backticks outside comments and strings, approximately: a
        # gross imbalance is the signal, exact parsing is not needed here.
        self.assertEqual(text.count("`") % 2, 0,
                         "odd number of backticks -- a template literal is open")


class TestTheRenderPathsActuallyRun(unittest.TestCase):
    """
    Executes the functions that build the suspension card under a stubbed DOM,
    against a failed attempt, a job that has not run, and a clean run.

    Skipped without node. It runs where node is available -- DirectAdmin has it at
    /usr/bin/node, so this has been executed there rather than only reasoned
    about.
    """

    NODE = shutil.which("node")
    HARNESS = ROOT / "scripts" / "check_render.js"

    def setUp(self):
        if not self.NODE:
            self.skipTest("no node available; the structural check still applies")
        if not self.HARNESS.exists():
            self.skipTest(f"harness missing: {self.HARNESS}")

    def test_the_suspension_card_renders(self):
        proc = subprocess.run([self.NODE, str(self.HARNESS), str(JS)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")


class TestTheDocumentedTrapStillHolds(unittest.TestCase):
    """The Dockerfile copies modules by an explicit list, which is how a missing
    file becomes a missing feature with no error anywhere."""

    def test_the_harness_would_ship_if_it_were_part_of_the_app(self):
        # It is a test aid, not application code, so it is deliberately not in
        # the image. Asserting that keeps the two decisions from being confused.
        root = Path(__file__).resolve().parent.parent
        dockerfile = (root / "Dockerfile").read_text()
        self.assertNotIn("check_render.js", dockerfile,
                         "a test harness does not belong in the runtime image")
