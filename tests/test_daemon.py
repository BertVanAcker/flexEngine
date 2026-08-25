import shutil
import tempfile
import time
import unittest
from pathlib import Path

from flexEngine.__main__ import build_parser

FAKE_AGENT = """\
import time
from pathlib import Path
STOP_FILE = Path("agent.stop")
STOP_FILE.unlink(missing_ok=True)
try:
    while not STOP_FILE.exists():
        time.sleep(0.1)
finally:
    STOP_FILE.unlink(missing_ok=True)
"""


class TestDaemonLifecycle(unittest.TestCase):
    """Exercises `flexEngine start/status/stop` against a fake agent.py
    (a stand-in for the real supervisor, which needs Redis) so the
    background-process management itself is covered."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        (self.workspace / "logs").mkdir()
        (self.workspace / "agent.py").write_text(FAKE_AGENT)
        self.parser = build_parser()

    def tearDown(self):
        self.run_cmd("stop", str(self.workspace))
        shutil.rmtree(self.workspace, ignore_errors=True)

    def run_cmd(self, *argv):
        args = self.parser.parse_args(list(argv))
        return args.func(args)

    def wait_until(self, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_status_when_not_started(self):
        self.assertEqual(self.run_cmd("status", str(self.workspace)), 1)

    def test_start_status_stop_lifecycle(self):
        self.assertEqual(self.run_cmd("start", str(self.workspace)), 0)
        self.assertTrue((self.workspace / "agent.pid").exists())
        self.assertEqual(self.run_cmd("status", str(self.workspace)), 0)

        # starting again while already running is a no-op, not an error
        self.assertEqual(self.run_cmd("start", str(self.workspace)), 0)

        self.assertEqual(self.run_cmd("stop", str(self.workspace)), 0)
        self.assertFalse((self.workspace / "agent.pid").exists())
        self.assertFalse((self.workspace / "agent.stop").exists())
        self.assertEqual(self.run_cmd("status", str(self.workspace)), 1)

    def test_stop_when_not_running_is_a_noop(self):
        self.assertEqual(self.run_cmd("stop", str(self.workspace)), 0)

    def test_stale_pid_file_is_ignored(self):
        (self.workspace / "agent.pid").write_text("1")
        self.assertEqual(self.run_cmd("status", str(self.workspace)), 1)


if __name__ == "__main__":
    unittest.main()
