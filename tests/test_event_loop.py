"""
Tests that blocking I/O is not run on the event loop.

The dashboard was slow, and the cause was not the panels: every request was
serialised behind every other one. Ten handlers were declared `async def` while
doing blocking work -- requests calls to the panels, socket lookups, SSH. In
asyncio one blocking call stalls the whole worker, and uvicorn runs a single
worker, so nothing could proceed until the previous call returned.

Measured before the fix: four endpoints the browser requests in parallel took
3.35s together, and 3.33s run one after another. They were not overlapping at
all.

The fix is to declare them as plain `def`, which is what FastAPI's threadpool is
for: they then run off the event loop and do overlap.

This is easy to regress, because every handler was `async def` at one point and
adding a new one is most naturally written that way too.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP = Path(__file__).resolve().parent.parent / "app.py"

# The one handler that must stay async: it is middleware and genuinely awaits
# the next handler in the chain.
MUST_STAY_ASYNC = {"no_stale_assets"}

def handlers():
    """(name, is_async, body) for every route handler."""
    src = APP.read_text()
    out = []
    for m in re.finditer(r"\n(async )?def (\w+)\(", src):
        name = m.group(2)
        start = m.end()
        nxt = src.find("\n@app.", start)
        out.append((name, bool(m.group(1)), src[start:nxt if nxt > 0 else len(src)]))
    return out


class TestBlockingHandlersAreOffTheEventLoop(unittest.TestCase):
    def test_no_route_handler_is_async(self):
        offenders = sorted(n for n, is_async, _ in handlers()
                           if is_async and n not in MUST_STAY_ASYNC)
        self.assertEqual(offenders, [],
                         "these block on I/O but are async, so they serialise "
                         "every other request. Declare them as plain def.")

    def test_the_middleware_is_still_async(self):
        names = {n for n, is_async, _ in handlers()}
        self.assertIn("no_stale_assets", names)
        for n, is_async, _ in handlers():
            if n == "no_stale_assets":
                self.assertTrue(is_async, "the middleware awaits call_next")

    def test_the_page_load_endpoints_are_named_and_covered(self):
        """The four the browser requests together when the dashboard opens.
        They are the ones that made the page feel slow, so they are named
        explicitly rather than found by a fuzzy scan."""
        wanted = {"check_servers", "nic_extensions", "suspension_report",
                  "nic_field_spec", "dns_check", "serve_dashboard"}
        found = {n: a for n, a, _ in handlers()}
        missing = wanted - set(found)
        self.assertEqual(missing, set(), f"handlers renamed or removed: {missing}")
        for name in sorted(wanted):
            self.assertFalse(found[name], f"{name} is back on the event loop")

    def test_there_is_only_one_worker(self):
        """A second reason the symptom looked like slow panels. With one worker
        and blocking handlers, concurrency was impossible from both sides."""
        dockerfile = (APP.parent / "Dockerfile").read_text()
        cmd = [ln for ln in dockerfile.splitlines() if ln.strip().startswith("CMD [")]
        self.assertTrue(cmd, "no CMD line found in the Dockerfile")
        self.assertNotIn("--workers", cmd[0],
                         "this test records the single-worker setup; if workers "
                         "are added, blocking handlers still need fixing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
