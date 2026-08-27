import unittest

from flexEngine import __version__, __author__, __email__
from flexEngine.utils.banner import render_banner


class TestBanner(unittest.TestCase):
    def _render(self, **kwargs):
        return render_banner(color=False, workdir="/work/space", **kwargs)

    def test_plain_banner_is_a_rectangular_box(self):
        lines = self._render().splitlines()
        widths = {len(line) for line in lines}
        self.assertEqual(len(widths), 1, f"box lines have mismatched widths: {widths}")
        self.assertTrue(lines[0].startswith("╭─") and lines[0].endswith("╮"))
        self.assertTrue(lines[-1].startswith("╰") and lines[-1].endswith("╯"))
        for line in lines[1:-1]:
            self.assertTrue(line.startswith("│") and line.endswith("│"))
            self.assertEqual(line.count("│"), 3, "expected left column, divider and right column")

    def test_version_sits_in_the_top_border(self):
        self.assertIn(f"flexEngine v{__version__}", self._render().splitlines()[0])

    def test_left_column_carries_identity_and_workdir(self):
        text = self._render()
        self.assertIn(__author__, text)
        self.assertIn(__email__, text)
        self.assertIn("/work/space", text)

    def test_agent_status_reflects_argument(self):
        self.assertIn("agent running", self._render(agent_running=True, agent_pid=4242))
        self.assertIn("4242", self._render(agent_running=True, agent_pid=4242))
        self.assertIn("agent stopped", self._render(agent_running=False))
        self.assertIn("unknown", self._render(agent_running=None))

    def test_right_column_has_getting_started(self):
        text = self._render()
        self.assertIn("Getting started", text)
        self.assertIn("flexEngine init", text)

    def test_long_workdir_is_clipped_to_the_box(self):
        deep = "/" + "/".join(f"segment{i}" for i in range(40))
        lines = render_banner(color=False, workdir=deep).splitlines()
        self.assertEqual(len({len(line) for line in lines}), 1)
        self.assertIn("…", render_banner(color=False, workdir=deep))

    def test_color_toggle(self):
        self.assertIn("\033[", render_banner(color=True))
        self.assertNotIn("\033[", render_banner(color=False))


if __name__ == "__main__":
    unittest.main()
