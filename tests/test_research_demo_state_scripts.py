"""The demo state scripts never read from or write into this repository.
Named to match the research test pattern the public CI runs."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(script, *args):
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG")}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run([PY, "-B", str(REPO / "scripts" / script), *map(str, args)],
                          capture_output=True, text=True, env=env, cwd=str(REPO))


class DemoStateScriptTests(unittest.TestCase):
    def test_export_refuses_a_destination_inside_the_repository(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            r = run("export_demo_state.py", "--source", src, "--dest", REPO / "data" / "snapshot")
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("inside this repository", r.stderr)

    def test_export_refuses_a_destination_that_is_a_symlink_into_the_repository(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            link = Path(td) / "dest"
            link.symlink_to(REPO / "data")
            before = sorted(p.name for p in (REPO / "data").iterdir())
            r = run("export_demo_state.py", "--source", src, "--dest", link)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertEqual(before, sorted(p.name for p in (REPO / "data").iterdir()))

    def test_export_refuses_an_existing_destination_folder(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            dest = Path(td) / "dest"
            dest.mkdir()
            (dest / "research.json").symlink_to(REPO / "README.md")
            readme = (REPO / "README.md").read_bytes()
            r = run("export_demo_state.py", "--source", src, "--dest", dest)
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("must not exist yet", r.stderr)
            self.assertEqual(readme, (REPO / "README.md").read_bytes())

    def test_seed_refuses_a_source_inside_the_repository(self):
        with tempfile.TemporaryDirectory() as td:
            r = run("seed_demo_state.py", "--source", REPO / "data", "--dest", Path(td) / "state")
            self.assertEqual(r.returncode, 1, r.stderr)
            self.assertIn("outside this repository", r.stderr)


if __name__ == "__main__":
    unittest.main()
