"""Memory store: concurrent writes, rule round-trip, rule ordering, shortcut matching.

These cover the paths where a defect silently destroys the user's learning data rather than
raising, which is why they are the ones worth a test at all.
"""
import json
import multiprocessing
import os
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (sets WHISPERER_HOME before whisper_lib is imported)
import whisper_lib as W


def _hammer_log(tag: str) -> None:
    """One process doing the read-modify-write cycle the hooks do, 40 times."""
    import whisper_lib as W
    for _ in range(40):
        with W.memory_lock():
            recs = W.read_log()
            recs[-1][tag] = recs[-1].get(tag, 0) + 1
            W.rewrite_log(recs)


def _hammer_rules(tag: str) -> None:
    import whisper_lib as W
    for _ in range(25):
        W.bump_rules(["R001"], "applied")


class ConcurrentWrites(unittest.TestCase):
    """Two Claude Code sessions share one memory dir, and both hooks fire per prompt."""

    def setUp(self):
        W.ensure_memory()
        W.rewrite_log([{"id": "w-%d" % i, "ts": W.now_iso(), "session": "s", "closed": False, "dry": False}
                       for i in range(50)])

    def test_concurrent_log_updates_are_not_lost(self):
        ps = [multiprocessing.Process(target=_hammer_log, args=(t,)) for t in ("A", "B")]
        for p in ps:
            p.start()
        for p in ps:
            p.join(60)
        for p in ps:
            self.assertEqual(p.exitcode, 0, "a writer crashed (shared temp path pulled out from under it)")
        last = W.read_log()[-1]
        self.assertEqual((last.get("A"), last.get("B")), (40, 40), "lost updates: read-modify-write is not serialised")

    def test_concurrent_log_updates_keep_every_record(self):
        ps = [multiprocessing.Process(target=_hammer_log, args=(t,)) for t in ("A", "B")]
        for p in ps:
            p.start()
        for p in ps:
            p.join(60)
        self.assertEqual(len(W.read_log()), 50)

    def test_concurrent_rule_counter_updates_are_not_lost(self):
        W.save_rules({"Rules": [{"id": "R001", "text": "a rule", "source": "user", "created": "2026-01-01",
                                 "applied": 0, "corrections": 0, "extra": []}],
                      "Candidates": [], "Retired": []})
        ps = [multiprocessing.Process(target=_hammer_rules, args=(t,)) for t in ("A", "B")]
        for p in ps:
            p.start()
        for p in ps:
            p.join(60)
        self.assertEqual(W.load_rules()["Rules"][0]["applied"], 50)


class Locking(unittest.TestCase):
    def test_lock_is_reentrant(self):
        """cmd_outcome takes the lock and then calls bump_rules, which takes it again."""
        with W.memory_lock():
            with W.memory_lock(timeout_s=0.5):
                self.assertEqual(W._LOCK_DEPTH, 2)
        self.assertEqual(W._LOCK_DEPTH, 0)

    def test_lock_released_after_an_exception(self):
        with self.assertRaises(ValueError):
            with W.memory_lock():
                raise ValueError("boom")
        self.assertEqual(W._LOCK_DEPTH, 0)


class RuleRoundTrip(unittest.TestCase):
    """learnings.md is line-oriented; anything that breaks a line loses the rule on the next save."""

    def setUp(self):
        W.save_rules({"Rules": [], "Candidates": [], "Retired": []})

    def _add(self, text):
        sections = W.load_rules()
        sections["Rules"].append({"id": "R001", "text": W.one_line(text), "source": "user", "created": "2026-01-01",
                                  "applied": 0, "corrections": 0, "extra": []})
        W.save_rules(sections)
        return W.load_rules()["Rules"]

    def test_multiline_rule_text_survives(self):
        rules = self._add("keep the exact string\nand never reflow it")
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["text"], "keep the exact string and never reflow it")

    def test_rule_text_with_parens_and_commas_survives(self):
        text = "task=bugfix: quote identifiers (names, paths) verbatim"
        self.assertEqual(self._add(text)[0]["text"], text)

    def test_counters_survive_a_round_trip(self):
        W.save_rules({"Rules": [{"id": "R001", "text": "t", "source": "user", "created": "2026-01-01",
                                 "applied": 7, "corrections": 3, "extra": ["from C002"]}],
                      "Candidates": [], "Retired": []})
        r = W.load_rules()["Rules"][0]
        self.assertEqual((r["applied"], r["corrections"], r["extra"]), (7, 3, ["from C002"]))


class RuleOrdering(unittest.TestCase):
    SECTIONS = {"Rules": [{"id": "R001", "text": "generic rule about wording"},
                          {"id": "R002", "text": "task=bugfix: end with a commit step"},
                          {"id": "R003", "text": "[] bracket rule"}],
                "Candidates": [], "Retired": []}

    def _order(self, tt):
        return [r["id"] for r in W.relevant_rules(self.SECTIONS, {"task_type": {"value": tt}, "project": "zz"})]

    def test_task_tagged_rule_sorts_first(self):
        self.assertEqual(self._order("bugfix")[0], "R002")

    def test_empty_task_type_does_not_promote_an_empty_bracket_match(self):
        self.assertEqual(self._order(""), ["R001", "R002", "R003"])


class ShortcutMatching(unittest.TestCase):
    SC = {"next": {"expands_to": "Status: ..."}}

    def test_exact_and_punctuated_keys_match(self):
        self.assertEqual(W.match_shortcut("next", self.SC), "next")
        self.assertEqual(W.match_shortcut("Next.", self.SC), "next")

    def test_slash_command_prefix_is_stripped(self):
        self.assertEqual(W.match_shortcut("/whisper next", self.SC), "next")

    def test_unrelated_prompt_does_not_match(self):
        self.assertIsNone(W.match_shortcut("next steps for the parser", self.SC))


if __name__ == "__main__":
    unittest.main()


class RiskDetection(unittest.TestCase):
    """Risk flags decide whether the rewrite carries a guardrail, so a missed destructive
    phrasing is a safety hole, not a classification nit."""

    def test_schema_drop_in_plain_english(self):
        for prompt in ("drop the legacy_sessions table from production",
                       "drop table users",
                       "delete the audit column",
                       "truncate the events table"):
            with self.subTest(prompt=prompt):
                self.assertIn("schema", W.find_risks(prompt))

    def test_schema_flag_does_not_fire_on_lookalikes(self):
        for prompt in ("drop the ball on this feature", "style the dropdown component"):
            with self.subTest(prompt=prompt):
                self.assertNotIn("schema", W.find_risks(prompt))


class TaskTypeHeuristic(unittest.TestCase):
    """Task type picks the reply budget and sorts the rules. With Jev off — the path anyone
    installing without a TypeSafe key is on — this heuristic is the only classifier there is."""

    def kind(self, prompt, flags=None):
        return W.task_type_heuristic(prompt, flags if flags is not None else W.find_risks(prompt))[0]

    def test_a_negated_keyword_does_not_set_the_type(self):
        self.assertEqual(self.kind("review this branch, do not refactor anything while you are in there"), "review")
        self.assertEqual(self.kind("add the endpoint without refactoring the router"), "feature")
        self.assertEqual(self.kind("explain the cache, don't add tests for it"), "explain")

    def test_a_real_keyword_still_counts(self):
        self.assertEqual(self.kind("refactor the router into two modules"), "refactor")
        self.assertEqual(self.kind("add tests for the parser"), "test")

    def test_a_trailing_output_request_does_not_become_the_task(self):
        self.assertEqual(self.kind("cache the user lookup in the profile endpoint, and also explain "
                                   "what you changed and why it is safe"), "feature")

    def test_a_genuine_explain_task_is_still_explain(self):
        self.assertEqual(self.kind("the scheduler is confusing, explain how the startup tick works"), "explain")
        self.assertEqual(self.kind("walk me through the auth flow"), "explain")

    def test_destructive_work_reads_as_ops_not_a_tidy_up(self):
        self.assertEqual(self.kind("clean up the old release branches and drop the legacy_sessions "
                                   "table from production, then redeploy"), "ops")

    def test_risk_flags_do_not_override_a_confident_bugfix(self):
        self.assertEqual(self.kind("fix the crash when the migration runs twice"), "bugfix")


class PromptCachePruning(unittest.TestCase):
    """prompts.jsonl is a rebuildable cache read on every hook run; it has to stay bounded."""

    def setUp(self):
        W.ensure_memory()
        open(W.PROMPTS_PATH, "w").close()
        self.cfg = W.load_config()

    def _fill(self, n):
        with open(W.PROMPTS_PATH, "w", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"ts": W.now_iso(), "session": "s", "source": "hook",
                                    "fp": "alpha beta gamma %d" % i, "preview": "", "words": 12}) + "\n")

    def test_a_small_cache_is_left_alone(self):
        self._fill(100)
        self.assertEqual(W.prune_prompts(self.cfg), 0)
        self.assertEqual(len(W.read_prompts_seen()), 100)

    def test_an_oversized_cache_is_trimmed_to_the_window(self):
        cfg = dict(self.cfg, max_prompts_retained=500, prompts_max_bytes=1000)
        self._fill(2000)
        self.assertEqual(W.prune_prompts(cfg), 1500)
        kept = W.read_prompts_seen()
        self.assertEqual(len(kept), 500)
        self.assertEqual(kept[-1]["fp"], "alpha beta gamma 1999", "the newest records are the ones kept")

    def test_pruning_survives_a_missing_file(self):
        os.remove(W.PROMPTS_PATH)
        self.assertEqual(W.prune_prompts(self.cfg), 0)

    def test_recording_a_prompt_prunes_as_it_goes(self):
        W.write_json(W.CONFIG_PATH, dict(self.cfg, max_prompts_retained=50, prompts_max_bytes=1000))
        try:
            self._fill(400)
            W.record_prompt_seen("what is still pending on the parser work", "s", "test")
            self.assertLessEqual(len(W.read_prompts_seen()), 51)
        finally:
            W.write_json(W.CONFIG_PATH, self.cfg)


class LearnDueReadsTheLogOnce(unittest.TestCase):
    def test_caller_supplied_records_are_used(self):
        """The hook has already read the log; analyze_prompt must not read it again."""
        cfg = W.load_config()
        with mock.patch.object(W, "read_log", side_effect=AssertionError("read the log twice")):
            self.assertFalse(W.learn_due(cfg, []))
