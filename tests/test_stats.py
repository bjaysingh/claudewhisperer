"""stats: the control arm, the trend, and the refusal to draw a conclusion from too little data.

The point of the baseline arm is that "33% correction rate" means nothing on its own. These tests
pin the comparison and, more importantly, pin the guard that stops it being read too early.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import _bootstrap  # noqa: F401
import whisper_lib as W

CLI = os.path.join(_bootstrap.ROOT, "scripts", "whisper.py")
HOOK = os.path.join(_bootstrap.ROOT, "hooks", "user_prompt_submit.py")


def cli(*args):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, WHISPERER_HOME=os.environ["WHISPERER_HOME"]))
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def run(mode, outcome, followups=1, reply=500, task="bugfix", **over):
    r = W.new_record("s", "p", task, mode=mode)
    r.update({"outcome": outcome, "closed": True, "followups": followups, "reply_chars": reply,
              "transforms": ["strip_filler"] if mode == "whispered" else [],
              "cause": "too_verbose" if outcome == "correction" else None})
    r.update(over)
    return r


class ControlArm(unittest.TestCase):
    def test_no_verdict_until_both_arms_are_big_enough(self):
        W.rewrite_log([run("whispered", "ok") for _ in range(30)] + [run("baseline", "ok") for _ in range(3)])
        d = cli("stats")["does_it_help"]
        self.assertIn("not enough data", d["verdict"])
        self.assertNotIn("delta_vs_baseline", d, "a delta printed here would be read as a result")

    def test_the_arms_are_compared_once_there_is_enough(self):
        W.rewrite_log([run("whispered", "ok", followups=1, reply=500) for _ in range(12)]
                      + [run("baseline", "correction", followups=3, reply=900) for _ in range(12)])
        d = cli("stats")["does_it_help"]
        self.assertEqual(d["whispered"]["correction_rate_pct"], 0.0)
        self.assertEqual(d["baseline"]["correction_rate_pct"], 100.0)
        self.assertEqual(d["delta_vs_baseline"]["avg_followup_turns"], -2.0)
        self.assertEqual(d["delta_vs_baseline"]["avg_reply_chars"], -400)

    def test_the_verdict_names_the_self_selection(self):
        """The triage sends messier prompts to the treatment arm. Reading the delta as a clean
        experiment would overstate it, so the wording has to carry the caveat."""
        W.rewrite_log([run("whispered", "ok") for _ in range(12)] + [run("baseline", "ok") for _ in range(12)])
        self.assertIn("self-selected", cli("stats")["does_it_help"]["verdict"])

    def test_baseline_runs_stay_out_of_the_pass_metrics(self):
        W.rewrite_log([run("whispered", "ok", task="bugfix")] + [run("baseline", "correction", task="feature")])
        s = cli("stats")
        self.assertEqual(s["outcomes"], {"ok": 1}, "a baseline correction is not the pass being corrected")
        self.assertEqual(list(s["by_task_type"]), ["bugfix"])
        self.assertEqual(s["causes"], {})

    def test_records_written_before_mode_existed_count_as_whispered(self):
        legacy = run("whispered", "ok")
        legacy.pop("mode")
        W.rewrite_log([legacy])
        self.assertEqual(cli("stats")["whispered_runs"], 1)

    def test_baseline_runs_are_not_evidence_for_learning(self):
        W.rewrite_log([run("baseline", "correction", task="feature") for _ in range(8)])
        self.assertEqual(cli("learn")["strong_signals"], 0)


class Trend(unittest.TestCase):
    def test_no_trend_until_there_are_two_halves_worth(self):
        W.rewrite_log([run("whispered", "ok") for _ in range(10)])
        self.assertIn("not enough", cli("stats")["trend"]["verdict"])

    def test_trend_compares_recent_against_earlier(self):
        W.rewrite_log([run("whispered", "correction", followups=3) for _ in range(10)]
                      + [run("whispered", "ok", followups=1) for _ in range(10)])
        t = cli("stats")["trend"]
        self.assertEqual(t["earlier"]["correction_rate_pct"], 100.0)
        self.assertEqual(t["recent"]["correction_rate_pct"], 0.0)
        self.assertEqual(t["change"]["correction_rate_pct"], -100.0)


class HookWritesTheControlArm(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        W.rewrite_log([])
        # a fingerprint left by an earlier test makes the same prompt "seen before", which makes it
        # actionable and so no longer a baseline candidate - correct behaviour, ruinous for isolation
        open(W.PROMPTS_PATH, "w").close()

    def send(self, prompt):
        subprocess.run([sys.executable, HOOK], input=json.dumps({"prompt": prompt, "session_id": "s1", "cwd": self.cwd}),
                       capture_output=True, text=True, timeout=30,
                       env=dict(os.environ, WHISPERER_HOME=os.environ["WHISPERER_HOME"]))

    def test_an_untouched_prompt_is_logged_as_baseline(self):
        self.send("auth middleware rejects valid tokens after midnight UTC")
        recs = W.read_log()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["mode"], "baseline")
        self.assertIsNone(recs[0]["tokens_out"], "nothing was rewritten")
        self.assertIsNotNone(recs[0]["hook_ms"])

    def test_the_baseline_run_is_then_labelled_like_any_other(self):
        self.send("auth middleware rejects valid tokens after midnight UTC")
        self.send("that's not what i asked for, you dropped the pagination")
        self.assertEqual(W.read_log()[0]["outcome"], "correction")

    def test_a_repeated_prompt_stops_being_a_baseline_candidate(self):
        """Seeing the same prompt again is itself a reason to act on it (offer a shortcut), so it
        leaves the control arm. Worth pinning: it is also what breaks naive test isolation."""
        for _ in range(3):
            self.send("auth middleware rejects valid tokens after midnight UTC")
        self.assertEqual(len(W.read_log()), 1, "only the first sighting is a baseline run")

    def test_recording_can_be_turned_off(self):
        cfg = W.load_config()
        W.write_json(W.CONFIG_PATH, dict(cfg, record_baseline=False))
        try:
            self.send("auth middleware rejects valid tokens after midnight UTC")
            self.assertEqual(W.read_log(), [])
        finally:
            W.write_json(W.CONFIG_PATH, cfg)


if __name__ == "__main__":
    unittest.main()
