"""The hooks end to end, as Claude Code runs them: JSON on stdin, JSON or nothing on stdout.

Runs them as subprocesses on purpose. Every failure inside a hook is swallowed by design, so a
unit test of the internals cannot tell a working hook from one whose body never executes.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
import whisper_lib as W

HOOKS = os.path.join(_bootstrap.ROOT, "hooks")


def run_hook(name, payload):
    env = dict(os.environ, WHISPERER_HOME=os.environ["WHISPERER_HOME"])
    p = subprocess.run([sys.executable, os.path.join(HOOKS, name)], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30, env=env)
    return p


def seed_open_run(**over):
    rec = {"id": "w-test", "ts": W.now_iso(), "session": "s1", "task_type": "bugfix", "gate": "proceed",
           "closed": False, "dry": False, "outcome": None, "followups": 0, "questions_asked": 0, "rules": []}
    rec.update(over)
    W.rewrite_log([rec])
    return rec


class UserPromptSubmitHook(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp()
        W.ensure_memory()
        W.save_shortcuts({})

    def _send(self, prompt, session="s1"):
        return run_hook("user_prompt_submit.py", {"prompt": prompt, "session_id": session, "cwd": self.cwd})

    def test_correction_labels_the_open_run(self):
        seed_open_run()
        self._send("that's not what i asked for, you dropped the pagination")
        r = W.read_log()[0]
        self.assertEqual(r["outcome"], "correction")
        self.assertEqual(r["cause"], "dropped_detail")

    def test_approval_labels_the_open_run_ok(self):
        seed_open_run()
        self._send("perfect, thanks")
        self.assertEqual(W.read_log()[0]["outcome"], "ok")

    def test_new_task_closes_the_run_as_implicit(self):
        seed_open_run()
        self._send("now add a --json flag to the export command and wire it through the CLI")
        r = W.read_log()[0]
        self.assertEqual((r["outcome"], r["closed"]), ("ok_implicit", True))

    def test_an_answer_to_a_question_counts_as_a_follow_up_turn(self):
        seed_open_run()
        self._send("yes")
        r = W.read_log()[0]
        self.assertEqual((r["followups"], r["questions_asked"]), (1, 1))

    def test_a_bare_question_reads_as_a_new_task_without_jev(self):
        """Known limit of the keyword fallback: CORRECTION_PATTERNS has no question form, so
        `correction_heuristic` returns new_task and the run closes as ok_implicit. Jev classifies
        it as `reply`. Pinned so a heuristic change has to be deliberate."""
        seed_open_run()
        self._send("which file did you change?")
        r = W.read_log()[0]
        self.assertEqual((r["outcome"], r["closed"], r["followups"]), ("ok_implicit", True, 0))

    def test_shortcut_key_is_expanded(self):
        W.save_shortcuts({"next": {"expands_to": "Status: pending work and the single next step.", "uses": 0}})
        p = self._send("next")
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["updatedPromptText"],
                         "Status: pending work and the single next step.")
        self.assertEqual(W.load_shortcuts()["next"]["uses"], 1)

    def test_hook_is_silent_and_clean_for_a_slash_command(self):
        p = self._send("/clear")
        self.assertEqual((p.returncode, p.stdout.strip(), p.stderr.strip()), (0, "", ""))

    def test_hook_never_fails_on_a_malformed_payload(self):
        p = subprocess.run([sys.executable, os.path.join(HOOKS, "user_prompt_submit.py")],
                           input="not json", capture_output=True, text=True, timeout=30)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))

    def test_a_full_prompt_gets_a_nudge_with_the_analysis_id(self):
        p = self._send("hey, when you get a chance could you please fix the login thing? it 500s on expired "
                       "tokens and it should 401. it's driving me crazy. be careful not to break anything.")
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[Claude Whisperer hook]", ctx)
        self.assertIn("analysis=a-", ctx)

    def test_a_subagent_report_is_not_the_user_reacting(self):
        """A hand-back reaches UserPromptSubmit as if typed. Read as a reply it labelled the user's last run:
        both baseline runs of one session became corrections while only subagent reports were arriving."""
        seed_open_run()
        pending, seen = set(os.listdir(W.PENDING_DIR)), len(W.read_prompts_seen())
        p = self._send('<agent-message from="a4ccf0b0f0b63ce96">\n[Subagent hand-back] The report follows:\n'
                       "  that's not what i asked for, you dropped the pagination. fix the login bug and update "
                       "the readme with the new setup steps\n</agent-message>")
        self.assertEqual(p.stdout, "", "no nudge")
        log = W.read_log()
        self.assertEqual(len(log), 1, "no baseline run")
        self.assertEqual((log[0]["outcome"], log[0]["followups"]), (None, 0), "the open run is untouched")
        self.assertEqual(set(os.listdir(W.PENDING_DIR)), pending, "no pending analysis")
        self.assertEqual(len(W.read_prompts_seen()), seen, "no fingerprint")

    def test_a_task_notification_is_not_the_user_reacting(self):
        seed_open_run()
        p = self._send('<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>\n'
                       '<summary>Agent "fix the login bug" finished</summary>\n</task-notification>')
        self.assertEqual(p.stdout, "")
        r = W.read_log()[0]
        self.assertEqual((r["outcome"], r["followups"], r["closed"]), (None, 0, False))

    def test_a_prompt_about_agent_messages_is_still_a_prompt(self):
        self.assertFalse(W.is_harness_message("why does the agent-message parser choke on nested tags"))

    def test_a_nudge_reaps_analyses_nobody_logged(self):
        """Only `log` removed a pending analysis, so every nudge Claude ignored left a file behind for good.
        The file goes; the count of it stays, because stats reports it."""
        stale = os.path.join(W.PENDING_DIR, "a-ignored.json")
        W.write_json(stale, {"id": "a-ignored"})
        t = time.time() - W.PENDING_TTL_S - 60
        os.utime(stale, (t, t))
        counted = W.never_logged_count()
        p = self._send("hey, when you get a chance could you please fix the login thing? it 500s on expired "
                       "tokens and it should 401. it's driving me crazy. be careful not to break anything.")
        new_id = re.search(r"analysis=(a-\S+)", p.stdout).group(1)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(os.path.join(W.PENDING_DIR, new_id + ".json")), "the new nudge is not stale")
        self.assertEqual(W.never_logged_count(), counted)


class StopHook(unittest.TestCase):
    def test_reply_length_is_recorded_on_the_open_run(self):
        seed_open_run()
        run_hook("stop.py", {"last_assistant_message": "line one\nline two", "session_id": "s1"})
        r = W.read_log()[0]
        self.assertEqual((r["reply_chars"], r["reply_lines"]), (17, 2))

    def test_no_open_run_is_not_an_error(self):
        seed_open_run(closed=True)
        p = run_hook("stop.py", {"last_assistant_message": "hi", "session_id": "s1"})
        self.assertEqual(p.returncode, 0)
        self.assertIsNone(W.read_log()[0].get("reply_chars"))


if __name__ == "__main__":
    unittest.main()
