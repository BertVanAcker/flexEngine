import shutil
import subprocess
import unittest


class TestDockerInstallation(unittest.TestCase):
    """Docker is an optional dependency (see scripts/install.sh / install.ps1),
    only needed to deploy applications as containers. These tests verify that,
    when Docker is present on the machine running the suite, the installation
    is actually functional. The whole class is skipped (not failed) when
    Docker isn't installed, since it's optional."""

    @classmethod
    def setUpClass(cls):
        cls.docker_path = shutil.which("docker")
        if cls.docker_path is None:
            raise unittest.SkipTest("Docker is not installed (optional dependency), skipping.")

    def test_docker_cli_reports_version(self):
        result = subprocess.run(
            [self.docker_path, "--version"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, "`docker --version` did not exit cleanly.")
        self.assertIn("Docker version", result.stdout)

    def test_docker_daemon_is_reachable(self):
        result = subprocess.run(
            [self.docker_path, "info"], capture_output=True, text=True, timeout=10
        )
        self.assertEqual(
            result.returncode, 0,
            "Docker is installed but the daemon isn't reachable. Start Docker "
            "Desktop (or the docker service) and re-run this test.\n"
            f"stderr: {result.stderr}"
        )


if __name__ == "__main__":
    unittest.main()
