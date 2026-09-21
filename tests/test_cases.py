"""Saved prompt cases: the regression set for the analysis layer.

These are the inputs the rewrite is tuned against, ugly ones included. They pin the deterministic
half (task type, triage, risk, secrets, paths, pasted code, CLAUDE.md overlap) so a heuristic edit
has to be deliberate. `run_cases.py` replays the same set through Jev and diffs the answers, which
is what a pinned model version moving actually requires.
"""
import json
import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
import whisper_lib as W

CASES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases")


def load_cases():
    cases = []
    for name in sorted(os.listdir(CASES_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(CASES_DIR, name), encoding="utf-8") as f:
                cases.extend(json.load(f))
    return cases


def analyse(case, cwd):
    """Hermetic analysis: no repo, and only the CLAUDE.md the case declares."""
    real = W.claude_md_files
    md = []
    if case.get("claude_md"):
        p = os.path.join(cwd, "CLAUDE.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(case["claude_md"])
        md = [p]
    W.claude_md_files = lambda _cwd: md
    try:
        return W.analyze_prompt(case["prompt"], cwd, W.load_config(), use_jev=False)
    finally:
        W.claude_md_files = real


class SavedCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()

    def test_cases_dir_holds_only_case_files(self):
        """A stray .json in cases/ is loaded as a case list; the snapshot lives one level up."""
        for name in os.listdir(CASES_DIR):
            with self.subTest(file=name):
                self.assertTrue(name.endswith(".json"))
                with open(os.path.join(CASES_DIR, name), encoding="utf-8") as f:
                    self.assertIsInstance(json.load(f), list)

    def test_case_set_is_big_enough_to_be_worth_something(self):
        self.assertGreaterEqual(len(self.cases), 10)

    def test_every_case_is_documented(self):
        for c in self.cases:
            with self.subTest(case=c.get("name")):
                self.assertTrue(c.get("why"), "a case without a reason cannot be judged when it fails")
                self.assertIn("must_keep", c, "must_keep is what a model bump is reviewed against")
                for s in c["must_keep"]:
                    self.assertIn(s, c["prompt"], "must_keep must quote the prompt verbatim")

    def test_expectations_hold(self):
        for c in self.cases:
            with self.subTest(case=c["name"]):
                cwd = tempfile.mkdtemp()
                a = analyse(c, cwd)
                e = c["expect"]
                if "task_type" in e:
                    self.assertEqual(a["task_type"]["value"], e["task_type"])
                if "triage" in e:
                    self.assertEqual(a["triage"]["value"], e["triage"])
                if "secrets_detected" in e:
                    self.assertEqual(a["secrets_detected"], e["secrets_detected"])
                if "multi_task_hint" in e:
                    self.assertEqual(a["multi_task_hint"], e["multi_task_hint"])
                for flag in e.get("risk_flags_include", []):
                    self.assertIn(flag, a["risk"]["flags"])
                for p in e.get("paths_missing_include", []):
                    self.assertIn(p, a["paths"]["missing"])
                if "filler_at_least" in e:
                    self.assertGreaterEqual(a["filler"]["total"], e["filler_at_least"])
                if "fenced_blocks_at_least" in e:
                    self.assertGreaterEqual(len(a["fenced_blocks"]), e["fenced_blocks_at_least"])
                if "claude_md_overlap_at_least" in e:
                    self.assertGreaterEqual(len(a["claude_md_overlap"]), e["claude_md_overlap_at_least"])

    def test_known_gaps_are_explained(self):
        """A pinned-but-wrong expectation has to say so, or the set quietly blesses a defect."""
        for c in self.cases:
            if "known_gap" in c:
                with self.subTest(case=c["name"]):
                    self.assertRegex(c["known_gap"], r"[Ss]hould be", "say what the right answer is")

    def test_no_case_leaks_a_secret_into_memory(self):
        """A prompt carrying a key must not reach prompts.jsonl, even as a fingerprint."""
        for c in self.cases:
            if c["expect"].get("secrets_detected"):
                with self.subTest(case=c["name"]):
                    before = len(W.read_prompts_seen())
                    W.record_prompt_seen(c["prompt"], "s", "test")
                    self.assertEqual(len(W.read_prompts_seen()), before)


if __name__ == "__main__":
    unittest.main()
