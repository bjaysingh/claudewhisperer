"""Saved prompt cases: the regression set for the analysis layer.

These are the inputs the rewrite is tuned against, ugly ones included. They pin the deterministic
half (task type, triage, risk, secrets, paths, pasted code, CLAUDE.md overlap) so a heuristic edit
has to be deliberate. `run_cases.py` replays the same set through Jev and diffs the answers, which
is what a pinned model version moving actually requires.
"""
import json
import os
import subprocess
import sys
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

    def test_offline_replay_has_no_drift(self):
        """`run_cases.py` without --jev is the offline gate. Diffed against a Jev-built baseline it reported
        every place Jev and the heuristics disagree as drift, failed on a clean checkout, and offered an
        `--update` that would have overwritten the Jev baseline. Each path keeps its own."""
        here = os.path.dirname(os.path.abspath(__file__))
        self.assertTrue(os.path.exists(os.path.join(here, "cases_snapshot_heuristics.json")),
                        "without a baseline run_cases.py writes one and passes by definition")
        p = subprocess.run([sys.executable, os.path.join(here, "run_cases.py")], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ, WHISPERER_HOME=tempfile.mkdtemp(prefix="whisperer-test-")))
        self.assertEqual(p.returncode, 0, p.stdout[-1500:])

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


class MultiTaskHeuristic(unittest.TestCase):
    """Several tasks are told apart by shape: a work verb, a sequence marker, another work verb. Each row pins
    one side of one rule, so loosening any of them fails a named prompt rather than a count."""

    SEVERAL = [
        "fix the flaky retry test in test_net.py; after that, add a request timeout to the http client",
        "add a --dry-run flag to the sync command. then add a --verbose flag to export.",
        "tag the release then publish it to pypi",
        "update the README install section and then regenerate the API reference",
    ]
    ONE = [
        # the verb after the marker narrates a symptom or specifies behaviour; it is not an imperative
        "fix the bug where the session timer resets, then fires twice",
        "write a migration that adds a nullable column, then backfills it in batches",
        # a contingency, not a second task
        "if the deploy fails after you update the config, then restore the previous build",
        # routine and check tails belong to the task before them
        "add the retry flag to the sync command, then commit",
        "fix the flaky upload test, then run the whole suite to make sure nothing else broke",
        # a follow-up that opens with the marker has no first task
        "then redeploy the api",
        "I ran the migration and then it crashed with a lock timeout, find out why",
        "explain how the scheduler picks the next job and then how retries get queued",
        # the older markers still need a prompt long enough to hold two tasks
        "also fix the typo in the footer",
    ]

    def test_chained_work_is_several_tasks(self):
        for p in self.SEVERAL:
            with self.subTest(prompt=p):
                self.assertTrue(W.multi_task_heuristic(p))

    def test_one_task_with_a_sequence_word_is_still_one(self):
        for p in self.ONE:
            with self.subTest(prompt=p):
                self.assertFalse(W.multi_task_heuristic(p))

    def test_several_tasks_are_full_triage_however_short(self):
        """Q_ANALYZE puts several tasks mixed together under `full`; before this a short chain read as tight."""
        a = analyse({"prompt": "tag the release then publish it to pypi"}, tempfile.mkdtemp())
        self.assertEqual(a["triage"]["value"], "full")


if __name__ == "__main__":
    unittest.main()
