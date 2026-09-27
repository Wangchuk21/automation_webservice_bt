"""
Tests for the "$$" secret convention.

Docker Compose treats "$NAME" in an env value as a variable reference and
silently substitutes an empty string, so a password containing "$" is delivered
truncated. The agreed workaround is to store it as "$$" and unescape on the
host. These tests pin down both halves of that contract, because a regression
here produces a wrong credential with no error at all.

NOTE: these tests only ever use synthetic values such as "$Secret$@abcd". The
real .env is never loaded, and a real secret is never placed in an assertion
message -- a failing assert would otherwise print the live credential.

Run with:  ./venv/bin/python -m unittest discover -s tests -v
"""
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_PY = REPO / "config.py"

# Child process prints the value it loaded, as a repr, so the parent can assert
# on it. Kept tiny and dependency-free.
_PROBE = textwrap.dedent("""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("cfg", CONFIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.stdout.write(repr(mod.settings.NIC_PASSWORD))
""")


def load_config_with(dotenv_body: str, extra_env=None):
    """
    Run config.py against a temporary .env and return the value it resolves.

    config.py locates .env relative to its own __file__, so config.py itself is
    copied into the temp directory. Copying it is what makes the real .env
    invisible to these tests.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        shutil.copy(CONFIG_PY, target / "config.py")
        (target / ".env").write_text(textwrap.dedent(dotenv_body))

        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("NIC_", "CPANEL_", "DIRECTADMIN_", "SMTP_", "API_"))}
        if extra_env:
            env.update(extra_env)

        out = subprocess.run(
            [sys.executable, "-c", _PROBE.replace("CONFIG_PATH", repr(str(target / "config.py")))],
            capture_output=True, text=True, env=env, cwd=tmp,
        )
        if out.returncode != 0:
            raise AssertionError(f"child process failed: {out.stderr.strip()[:300]}")
        return eval(out.stdout)  # noqa: S307 - the child emitted a repr we control


class TestDollarUnescape(unittest.TestCase):
    def test_double_dollar_collapses_to_single(self):
        self.assertEqual(load_config_with("NIC_PASSWORD=$$Secret$$@abcd\n"), "$Secret$@abcd")

    def test_single_dollar_is_left_alone(self):
        """A lone "$" is not an escape and must survive untouched."""
        self.assertEqual(load_config_with("NIC_PASSWORD=user$name@example\n"), "user$name@example")

    def test_plain_value_unchanged(self):
        self.assertEqual(load_config_with("NIC_PASSWORD=ordinarypassword\n"), "ordinarypassword")

    def test_dollar_at_end_unchanged(self):
        self.assertEqual(load_config_with("NIC_PASSWORD=trailing$\n"), "trailing$")

    def test_mixed_single_and_double(self):
        """Only "$$" collapses; a single "$" beside it is literal."""
        self.assertEqual(load_config_with("NIC_PASSWORD=a$$b$c\n"), "a$b$c")

    def test_quotes_are_still_stripped_by_dotenv(self):
        """Unquoting is python-dotenv's job; it must keep working."""
        self.assertEqual(load_config_with('NIC_PASSWORD="quoted$$value"\n'), "quoted$value")

    def test_ambient_env_is_not_rewritten(self):
        """
        Only keys present in the .env FILE are unescaped. In the container the
        values arrive already unescaped by Compose, so rewriting ambient
        environment variables there would corrupt them a second time.
        """
        got = load_config_with(
            "NIC_PASSWORD=fromfile\n",
            extra_env={"UNRELATED_FROM_ENV": "keep$$this"},
        )
        self.assertEqual(got, "fromfile")
        # The child is a separate process, so re-check the rule directly.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copy(CONFIG_PY, target / "config.py")
            (target / ".env").write_text("NIC_PASSWORD=fromfile\n")
            probe = _PROBE.replace("CONFIG_PATH", repr(str(target / "config.py"))).replace(
                "sys.stdout.write(repr(mod.settings.NIC_PASSWORD))",
                "sys.stdout.write(mod._unescape_dollar_values.__name__ or '')",
            )
            out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                                 text=True, env={**os.environ, "UNRELATED_FROM_ENV": "keep$$this"},
                                 cwd=tmp)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_missing_env_file_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copy(CONFIG_PY, target / "config.py")
            probe = _PROBE.replace("CONFIG_PATH", repr(str(target / "config.py"))).replace(
                "sys.stdout.write(repr(mod.settings.NIC_PASSWORD))",
                "mod._unescape_dollar_values(mod.env_path); sys.stdout.write('ok')",
            )
            out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                                 text=True, cwd=tmp)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("ok", out.stdout)

    def test_commented_dollar_lines_are_ignored(self):
        self.assertEqual(
            load_config_with("# NIC_PASSWORD=$$ignored$$\nNIC_PASSWORD=real\n"),
            "real",
        )


class TestComposeInterpolationIsAvoided(unittest.TestCase):
    """
    Guards the actual failure mode: a "$" silently eaten by Compose, which
    produces a wrong credential and no error. Skipped without Compose v2.
    """

    def setUp(self):
        if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
            self.skipTest("docker compose v2 not available")

    def _compose_down(self, tmp):
        """Tear down the project this test created.

        Without this each run leaves a Docker network behind, and after enough
        runs the predefined address pools are exhausted -- at which point the
        suite fails for reasons that have nothing to do with the code.
        """
        subprocess.run(["docker", "compose", "down", "--remove-orphans"],
                       capture_output=True, text=True, cwd=tmp)

    def test_escaped_value_survives_compose(self):
        secret = "$Secret$@abcd"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(f"CHECKPW={secret.replace('$', '$$')}\n")
            (Path(tmp) / "docker-compose.yml").write_text(textwrap.dedent("""
                services:
                  t:
                    image: busybox
                    entrypoint: ["sh", "-c"]
                    command: ["printf %s \\\"$$CHECKPW\\\" | wc -c"]
                    env_file: [.env]
            """))
            out = subprocess.run(
                ["docker", "compose", "-f", str(Path(tmp) / "docker-compose.yml"), "run", "--rm", "t"],
                capture_output=True, text=True, cwd=tmp,
            )
            self._compose_down(tmp)
            self.assertEqual(out.returncode, 0, out.stderr[-300:])
            got = out.stdout.strip().splitlines()[-1]
            self.assertEqual(got, str(len(secret)),
                             f"compose delivered {got} chars, expected {len(secret)}: "
                             "the '$' was interpolated away")

    def test_unescaped_value_would_be_truncated(self):
        """
        Documents the bug being fixed: without "$$" the same secret arrives
        short. If this ever starts passing, Compose changed its escaping rules
        and the convention in .env should be revisited.
        """
        secret = "$Secret$@abcd"
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text(f"CHECKPW={secret}\n")
            (Path(tmp) / "docker-compose.yml").write_text(textwrap.dedent("""
                services:
                  t:
                    image: busybox
                    entrypoint: ["sh", "-c"]
                    command: ["printf %s \\\"$$CHECKPW\\\" | wc -c"]
                    env_file: [.env]
            """))
            out = subprocess.run(
                ["docker", "compose", "-f", str(Path(tmp) / "docker-compose.yml"), "run", "--rm", "t"],
                capture_output=True, text=True, cwd=tmp,
            )
            self._compose_down(tmp)
            got = out.stdout.strip().splitlines()[-1]
            self.assertNotEqual(got, str(len(secret)),
                                "unescaped '$' no longer breaks; revisit the $$ convention")


if __name__ == "__main__":
    unittest.main(verbosity=2)
