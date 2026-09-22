"""The Jev client: model-version capture, drift detection, and failure behaviour.

No network and no API key. urlopen is stubbed, because what matters here is what the code does
with the response, not that TypeSafe is reachable.
"""
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401
import whisper_lib as W

PINNED = "jev-1.13.0"


def cfg_with(model=PINNED, **over):
    cfg = W.load_config()
    cfg["jev"].update({"model": model, "enabled": True}, **over)
    return cfg


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def reply(body):
    return lambda *a, **k: _Resp(json.dumps(body).encode())


ANSWER = {"answers": {"triage": {"type": "choice", "choice": "full", "confidence": 0.9}},
          "usage": {"input_tokens": 296, "output_tokens": 20}}


class ModelVersionCapture(unittest.TestCase):
    def setUp(self):
        for p in (W.JEV_STATUS_PATH,):
            if os.path.exists(p):
                os.remove(p)
        self.env = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_config_ships_a_pinned_version_not_an_alias(self):
        model = W.DEFAULT_CONFIG["jev"]["model"]
        self.assertFalse(model.endswith(("latest", "preview")),
                         "an alias moves under the tuned thresholds without a code change")

    def test_the_answering_model_is_recorded(self):
        with mock.patch("urllib.request.urlopen", reply(dict(ANSWER, model=PINNED))):
            W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with())
        st = W.read_jev_status()
        self.assertEqual(st["model_reported"], PINNED)
        self.assertEqual(st["models_seen"], {PINNED: 1})
        self.assertFalse(st["version_drift"])

    def test_drift_is_flagged_when_a_pinned_request_answers_as_another_version(self):
        with mock.patch("urllib.request.urlopen", reply(dict(ANSWER, model="jev-1.14.0"))):
            W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with())
        st = W.read_jev_status()
        self.assertTrue(st["version_drift"])
        self.assertEqual((st["model_requested"], st["model_reported"]), (PINNED, "jev-1.14.0"))

    def test_an_alias_resolving_elsewhere_is_not_drift(self):
        with mock.patch("urllib.request.urlopen", reply(dict(ANSWER, model=PINNED))):
            W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with(model="jev-latest"))
        self.assertFalse(W.read_jev_status()["version_drift"])

    def test_token_usage_accumulates(self):
        with mock.patch("urllib.request.urlopen", reply(dict(ANSWER, model=PINNED))):
            for _ in range(3):
                W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with())
        st = W.read_jev_status()
        self.assertEqual((st["tokens_in_total"], st["tokens_out_total"]), (888, 60))

    def test_the_request_asks_for_the_configured_model(self):
        seen = {}

        def capture(req, *a, **k):
            seen["body"] = json.loads(req.data)
            return _Resp(json.dumps(dict(ANSWER, model=PINNED)).encode())

        with mock.patch("urllib.request.urlopen", capture):
            W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with())
        self.assertEqual(seen["body"]["model"], PINNED)


class FailureBehaviour(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_failed_call_returns_none_and_is_counted(self):
        before = W.read_jev_status().get("failures", 0)

        def boom(*a, **k):
            raise TimeoutError("timed out")

        with mock.patch("urllib.request.urlopen", boom):
            self.assertIsNone(W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with()))
        self.assertEqual(W.read_jev_status()["failures"], before + 1)

    def test_no_api_key_means_no_call_at_all(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=AssertionError("must not call")):
                self.assertIsNone(W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with()))

    def test_malformed_response_does_not_raise(self):
        with mock.patch("urllib.request.urlopen", reply({"unexpected": True})):
            self.assertIsNone(W.jev_ask({"prompt": "x"}, W.Q_ANALYZE, cfg_with()))


class MultiTaskCutComesFromConfig(unittest.TestCase):
    """The cut is 0.7, not 0.5, and it has to stay a config value.

    Measured over the saved cases, genuinely multi-task prompts answer >= 0.89 and single ones
    <= 0.50. A cut at 0.5 sat on one case's mode and flipped run to run; these pin both the
    default and the fact that moving it in config.json actually moves the decision.
    """

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sk-test"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cwd = tempfile.mkdtemp()

    def hint_for(self, p, cut=None):
        cfg = cfg_with()
        if cut is not None:
            cfg["jev"]["multi_task_at"] = cut
        body = {"answers": {"multi_task": {"type": "noul", "noul": p}}}
        # not a one-liner: the heuristic triages a short prompt as skip, and skip never asks Jev
        prompt = ("add a health check endpoint to the api and update the openapi spec for it, "
                  "then explain what changed")
        with mock.patch("urllib.request.urlopen", reply(body)):
            a = W.analyze_prompt(prompt, self.cwd, cfg, use_jev=True)
        self.assertTrue(a["jev"]["used"], "the stubbed answer never reached the code")
        return a["multi_task_hint"]

    def test_the_default_cut_ships_at_the_gap_in_the_answers(self):
        self.assertEqual(W.DEFAULT_CONFIG["jev"]["multi_task_at"], 0.7)

    def test_an_answer_at_the_old_cut_is_not_several_tasks(self):
        # 0.5 is where "and also explain it" lands; it is one task
        self.assertFalse(self.hint_for(0.5))
        self.assertFalse(self.hint_for(0.69))

    def test_an_answer_above_the_cut_is_several_tasks(self):
        self.assertTrue(self.hint_for(0.7))
        self.assertTrue(self.hint_for(0.94))

    def test_config_moves_the_decision_so_the_cut_is_not_hardcoded(self):
        self.assertTrue(self.hint_for(0.8), "0.8 is over the default cut")
        self.assertFalse(self.hint_for(0.8, cut=0.9),
                         "raising multi_task_at in config.json must move the boundary")
        self.assertTrue(self.hint_for(0.55, cut=0.5),
                        "lowering it must move the boundary too")


if __name__ == "__main__":
    unittest.main()
