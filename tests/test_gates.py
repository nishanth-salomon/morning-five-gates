"""Behavioural tests for the three gates. Stdlib only: python -m unittest discover tests"""
import os, subprocess, sys, unittest

CLIENTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "clients")
sys.path.insert(0, CLIENTS)
import dedupe_audit as da  # noqa: E402


def run(script, *args):
    return subprocess.run([sys.executable, os.path.join(CLIENTS, script), *args],
                          capture_output=True, text=True, encoding="utf-8", cwd=CLIENTS)


class ParserTests(unittest.TestCase):
    def test_current_heading_parses(self):
        r = da.parse_role_line("### 1. Data Analyst — Acme Corp (Chennai) — Score: 88 — *NEW*")
        self.assertIsNotNone(r)
        self.assertEqual(r[1], "Acme Corp")
        self.assertEqual(r[4], "Chennai")

    def test_legacy_heading_parses(self):
        self.assertIsNotNone(da.parse_role_line("### 83 — Data Analyst @ Acme Corp"))

    def test_tier_c_table_row_parses(self):
        self.assertIsNotNone(da.parse_role_line("| 52 | BI Developer | Initech | Chennai |"))

    def test_section_heading_is_not_a_role(self):
        self.assertIsNone(da.parse_role_line("## Tier A — Apply today (75-100)"))


class GateTests(unittest.TestCase):
    def test_dedupe_fails_on_within_window_repeat(self):
        self.assertEqual(run("dedupe_audit.py", "demo-client", "--window", "14").returncode, 1)

    def test_brief_audit_enforces_size_cap(self):
        r = run("brief_audit.py", "demo-client", "--max-a", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("SIZE", r.stdout)

    def test_brief_audit_passes_within_cap(self):
        self.assertEqual(run("brief_audit.py", "demo-client", "--max-a", "3").returncode, 0)


if __name__ == "__main__":
    unittest.main()
