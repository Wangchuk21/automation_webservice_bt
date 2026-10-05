"""
How the scheduled job is actually launched.

Everything here was found on the server, and none of it was visible from the
laptop: the job exited on its first check with "BSCS_ENABLED is false", wrote no
audit record, and the dashboard showed a stale list with no explanation -- three
nights running.

The cause is that cron runs jobs in its own environment, not the one its parent
was started with. The web service works because compose injects .env into PID 1
and uvicorn inherits it. PID 1 here is cron, and the job cron spawns did not get
any of it. And config.py's load_dotenv could not rescue it, because .env is
excluded from the image by .dockerignore and nothing mounted it.

The same gap lost TZ=Asia/Thimphu, so 02:17 in the crontab meant 02:17 UTC --
08:17 in Thimphu -- and every timestamp the job wrote was UTC as well.

Both are asserted here rather than discovered again. A test cannot run cron, but
it can assert the two things whose absence causes this.
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def compose_service(name):
    """One service block, by indentation.

    Slicing to the next occurrence of the *same* name was wrong: the suspender
    has `depends_on: provisioner`, so the slice ran on into it and the check
    passed on text belonging to a different service.
    """
    text = (ROOT / "docker-compose.yml").read_text()
    lines = text.splitlines(keepends=True)
    start = next(i for i, l in enumerate(lines) if l.rstrip() == f"  {name}:")
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        out.append(line)
    return "".join(out)


class TestTheJobCanSeeItsConfiguration(unittest.TestCase):
    def test_the_env_file_is_mounted_where_config_looks_for_it(self):
        """config.py loads /app/.env, and .env is excluded from the image. With
        no mount there is nothing to load and every setting reads as absent."""
        service = compose_service("suspender")
        self.assertIn("/app/.env", service,
                      "the suspender cannot read its settings without this")
        self.assertIn(":ro", service, "and it must be mounted read-only")

    def test_it_is_the_same_file_compose_already_uses(self):
        """Two sources of truth would let a setting be right in one service and
        wrong in the other, which is what this bug was."""
        text = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("env_file:", text, "compose should still inject .env for PID 1")

    def test_the_web_service_does_not_need_the_mount(self):
        """It inherits from PID 1. Adding a mount there as well would be
        harmless but misleading about where the problem actually was."""
        self.assertNotIn("/app/.env", compose_service("provisioner"))


class TestTheScheduleIsInTheRightTimezone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crontab = (ROOT / "deploy" / "crontab").read_text()
        cls.commands = [l for l in cls.crontab.splitlines()
                        if l.strip() and not l.strip().startswith("#")]

    def test_cron_tz_is_set(self):
        """Read by cron itself rather than inherited by the job, which is how
        TZ=Asia/Thimphu was lost in the first place."""
        self.assertTrue(any(l.startswith("CRON_TZ=") for l in self.commands),
                        "without CRON_TZ the schedule is read as UTC, so 02:17 "
                        "runs at 08:17 in Thimphu")
        self.assertIn("Asia/Thimphu", self.crontab)

    def test_cron_tz_precedes_the_schedule(self):
        """It applies to the entries below it, so putting it last would leave the
        job on UTC and read as though it were configured."""
        lines = [i for i, l in enumerate(self.crontab.splitlines())
                 if l.startswith("CRON_TZ=") or l.startswith("17 2 ")]
        self.assertEqual(len(lines), 2)
        self.assertLess(lines[0], lines[1])

    def test_the_job_is_given_the_timezone_too(self):
        """
        CRON_TZ and TZ are not the same setting, and one cannot do both.

        Verified on this cron: with CRON_TZ=Asia/Thimphu alone, a `date` inside
        the job still printed 10:22 UTC. CRON_TZ decides when the job fires; TZ
        decides what the job writes. Without the second, the schedule would have
        been right while every timestamp in the audit trail was UTC -- mixing
        +00:00 from the nightly run with +06:00 from manual ones, which is the
        exact six-hour disagreement the provisioner was fixed for earlier.
        """
        command = next(l for l in self.commands if "suspend_expired" in l)
        self.assertIn("TZ=Asia/Thimphu", command,
                      "the job's own timestamps would be UTC without this")

    def test_the_schedule_and_the_job_agree(self):
        """Two different clocks in one command is a bug waiting to be read."""
        cron_tz = next(l for l in self.commands if l.startswith("CRON_TZ="))
        command = next(l for l in self.commands if "suspend_expired" in l)
        self.assertEqual(cron_tz.split("=", 1)[1],
                         command.split("TZ=", 1)[1].split()[0])

    def test_it_must_agree_with_the_container_timezone(self):
        """Two different Thimphus would be worse than one wrong one."""
        service = compose_service("suspender")
        self.assertIn("TZ: Asia/Thimphu", service)
        self.assertIn("Asia/Thimphu", self.crontab)

    def test_the_job_still_runs_unprivileged(self):
        command = next(l for l in self.commands if "suspend_expired" in l)
        self.assertIn("provisioner cd /app", command)

    def test_it_still_reports_rather_than_suspending(self):
        """The card tells operators nothing is suspended automatically. --live
        would make that false, so it is asserted against the command rather than
        the file -- the file explains in a comment how to enable it."""
        command = next(l for l in self.commands if "suspend_expired" in l)
        self.assertNotIn("--live", command,
                         "the warning claims nothing is suspended automatically")


class TestThisCannotHappenUnnoticedAgain(unittest.TestCase):
    """
    The gap is not checkable by running the suite against the app -- the failure
    only appears through cron, on a host where the job is launched differently
    from every other process in the container. So the two causes are pinned as
    facts about the deployment rather than hoped for.
    """

    def test_the_job_fails_loudly_when_settings_are_missing(self):
        """It did exit non-zero, but only into cron, where nothing reads it. The
        heartbeat exists for exactly this: the run has to leave a trace whatever
        happens."""
        script = (ROOT / "scripts" / "suspend_expired.py").read_text()
        self.assertIn("write_heartbeat(False, detail", script)
        self.assertIn("except Exception", script)

    def test_the_crontab_and_the_image_agree(self):
        """The Dockerfile copies deploy/crontab into the image, so the file
        tested here is the one cron will read -- as long as it is copied."""
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("deploy/crontab /etc/cron.d/bt-suspension", dockerfile)
