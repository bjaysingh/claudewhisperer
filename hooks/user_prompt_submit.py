#!/usr/bin/env python3
"""Claude Whisperer - opt-in UserPromptSubmit hook.

Runs on every prompt when enabled (`/whisper hook on`). It never blocks a prompt and never rewrites
one silently, with one exception: an exact match on a shortcut the user created expands to its
full prompt. Everything else is context injected for Claude:

  1. closes the learning loop: classifies this message as correction / new task / ack / reply for the
     previous Whisperer run (Jev if available, keyword heuristics otherwise);
  2. records a fingerprint of the prompt (never the text) so repeated prompts can become shortcuts;
  3. triages the prompt (skip / light / full) and, when a pass is worth it, tells Claude to apply
     the Whisperer pass with the analysis already done.

Any failure exits 0 with no output: the hook must never get in the user's way.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


def _record_baseline(W, cfg, analysis, session, hook_ms) -> None:
    """This prompt went to Claude untouched. Log it as the control arm: the next message labels it
    through the same path, so `stats` can say whether the pass changes anything."""
    if not cfg.get("record_baseline", True):
        return
    try:
        rec = W.new_record(session, analysis.get("project", ""), analysis["task_type"]["value"], mode="baseline")
        rec["tokens_in"] = analysis["tokens"]
        rec["triage"] = analysis["triage"]["value"]
        rec["hook_ms"] = hook_ms
        rec["fp"] = analysis.get("fingerprint", "")
        with W.memory_lock(timeout_s=1.0):
            recs = W.read_log()
            changed = False
            for r in recs:   # a new prompt closes whatever was open for this session
                if not r.get("closed") and not r.get("dry") and (r.get("session") or "") == (session or ""):
                    r["closed"] = True
                    if r.get("outcome") is None:
                        r["outcome"], r["labeled_by"] = "ok_implicit", "next-prompt"
                    changed = True
            if changed:
                W.rewrite_log(recs)
            W.append_log(rec)
    except Exception:
        pass


def main() -> None:
    t0 = time.time()
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    prompt = (data.get("prompt") or data.get("prompt_text") or "").strip()
    session = data.get("session_id", "")
    cwd = data.get("cwd") or os.getcwd()
    if not prompt or prompt.startswith("/"):
        return
    try:
        import whisper_lib as W
        if W.is_harness_message(prompt):   # a subagent or task reporting back, not the user reacting
            return
        W.ensure_memory()
        cfg = W.load_config()
    except Exception:
        return

    out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit"}}
    notes = []
    kind = None        # how this message relates to the previous run, if there is one
    fk = None          # routine follow-up kind, when the user moved on
    log_records = None  # read once below, reused by analyze_prompt instead of a second read

    # 1. learning loop: label the previous open run
    try:
        # short lock timeout: a hook must never make the user wait on another session's write
        with W.memory_lock(timeout_s=1.0):
            recs = log_records = W.read_log()
            idx = W.find_pending(recs, session=session)
            if idx is not None:
                r = recs[idx]
                kind, cause = W.correction_heuristic(prompt)
                labeled_by = "hook-heuristic"
                if W.jev_available(cfg) and not W.is_ack(prompt, cfg["hook"]["skip_under_words"]):
                    ans = W.jev_ask({"PREVIOUS": {"task_type": r.get("task_type"), "gate": r.get("gate")}, "NEW": prompt[:6000]}, W.Q_OUTCOME, cfg)
                    k = W._answer(ans, "kind")
                    if k and k.get("choice") and (k.get("confidence") or 0) >= cfg["jev"]["label_min_confidence"]:
                        labeled_by = "hook-jev"
                        kind = {"correction": "correction", "acknowledgement": "ok_explicit", "new_task": "new_task", "reply": "reply"}[k["choice"]]
                        c = W._answer(ans, "cause")
                        if kind == "correction" and c and c.get("choice") and c["choice"] != "none":
                            cause = c["choice"]
                elif W.is_ack(prompt, cfg["hook"]["skip_under_words"]):
                    low = prompt.lower()
                    kind = "ok_explicit" if any(w in low for w in ("thank", "perfect", "great", "nice", "lgtm", "looks good")) else "reply"
                r["followups"] = int(r.get("followups", 0)) + 1
                if kind == "reply":
                    r["questions_asked"] = int(r.get("questions_asked", 0)) + 1
                elif kind == "correction":
                    if r.get("outcome") in (None, "ok_implicit", "uncertain"):
                        r["outcome"], r["cause"], r["labeled_by"], r["labeled_at"] = "correction", cause, labeled_by, W.now_iso()
                        W.bump_rules(r.get("rules", []), "corrections")
                elif kind == "ok_explicit":
                    if r.get("outcome") in (None, "ok_implicit", "uncertain"):
                        r["outcome"], r["labeled_by"], r["labeled_at"] = "ok", labeled_by, W.now_iso()
                elif kind == "new_task":
                    r["followups"] = int(r.get("followups", 0)) - 1  # the new task itself is not a follow-up turn
                    if r.get("outcome") is None:
                        r["outcome"], r["labeled_by"], r["labeled_at"] = "ok_implicit", labeled_by, W.now_iso()
                    fk = W.followup_heuristic(prompt)
                    if W.jev_available(cfg):
                        ans = W.jev_ask({"NEW": prompt[:4000]}, W.Q_FOLLOWUP, cfg)
                        f = W._answer(ans, "followup")
                        if f and f.get("choice") and (f.get("confidence") or 0) >= cfg["jev"]["label_min_confidence"]:
                            fk = f["choice"]
                    r["followup_kind"] = fk
                    r["closed"] = True
                W.rewrite_log(recs)
    except Exception:
        pass

    # 2. fingerprint for repetition detection (corrections and answers are not candidates for shortcuts)
    try:
        if kind not in ("correction", "reply"):
            W.record_prompt_seen(prompt, session, "hook")
    except Exception:
        pass

    # 3. shortcuts: exact match expands the prompt
    try:
        key = None
        with W.memory_lock(timeout_s=1.0):
            shortcuts = W.load_shortcuts()
            key = W.match_shortcut(prompt, shortcuts)
            if key:
                sc = shortcuts[key]
                sc["uses"] = int(sc.get("uses", 0)) + 1
                W.save_shortcuts(shortcuts)
        if key:
            out["hookSpecificOutput"]["updatedPromptText"] = sc["expands_to"]
            out["hookSpecificOutput"]["additionalContext"] = "[Claude Whisperer] shortcut `%s` expanded. Act on the expanded prompt; the reply budget in it applies." % key
            print(json.dumps(out))
            return
    except Exception:
        pass

    # 4. triage and nudge. Corrections, answers to Claude's questions and routine follow-ups
    #    (commit, run tests, ...) get no pass: rewriting those adds a turn instead of saving one.
    try:
        if W.is_ack(prompt, cfg["hook"]["skip_under_words"]) or len(prompt.split()) <= 3:
            return
        if kind in ("correction", "reply", "ok_explicit"):
            return
        if kind == "new_task" and fk not in (None, "unrelated"):
            return
        a = W.analyze_prompt(prompt, cwd, cfg, use_jev=True, log_records=log_records)
        a["preview"] = "" if not cfg.get("store_previews") else W.redact_secrets(prompt)[: cfg["preview_chars"]]
        a["hash"] = W.hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        actionable = bool(a["paths"]["missing"] or a["fenced_blocks"] or a["claude_md_overlap"] or a["secrets_detected"]
                          or a.get("seen_before", 0) >= 2 or a["risk"]["value"] or a["multi_task_hint"])
        if a["triage"]["value"] == "skip" or (a["triage"]["value"] == "light" and not actionable):
            _record_baseline(W, cfg, a, session, int((time.time() - t0) * 1000))
            return
        # the hook sits between enter and Claude seeing the prompt; carry the cost so stats can show it
        a["hook_ms"] = int((time.time() - t0) * 1000)
        W.reap_pending(lock_timeout_s=1.0)   # nudges nobody logged; nothing else ever removes them
        W.write_json(os.path.join(W.PENDING_DIR, a["id"] + ".json"), a)
        skill_md = os.path.join(W.SKILL_DIR, "SKILL.md")
        parts = [
            "[Claude Whisperer hook] Apply the Whisperer pass to this prompt before acting (instructions: %s; read once per session)." % skill_md,
            "analysis=%s session=%s triage=%s task=%s%s risk=%s est_tokens=%d filler=%d" % (
                a["id"], session, a["triage"]["value"], a["task_type"]["value"],
                "(%.2f)" % a["task_type"]["confidence"] if a["task_type"].get("confidence") else "",
                "yes:" + ",".join(a["risk"].get("flags", [])) if a["risk"]["value"] else "no",
                a["tokens"], a["filler"]["total"]),
        ]
        if a["paths"]["missing"]:
            parts.append("paths not found: " + ", ".join(a["paths"]["missing"][:5]))
        if a["fenced_blocks"]:
            fb = ["%d lines%s" % (b["lines"], " ~ " + b["looks_like"] if b.get("looks_like") else "") for b in a["fenced_blocks"][:3]]
            parts.append("pasted code: " + "; ".join(fb))
        if a["claude_md_overlap"]:
            parts.append("restates CLAUDE.md: %d sentence(s)" % len(a["claude_md_overlap"]))
        if a["verify_hints"]:
            parts.append("verify: " + ", ".join("%s=`%s`" % (k, v) for k, v in list(a["verify_hints"].items())[:3]))
        if a.get("seen_before", 0) >= 2:
            parts.append("seen %d times before: offer a shortcut (see SKILL.md > Shortcuts)" % a["seen_before"])
        if a["rules"]:
            parts.append("rules: " + " | ".join("%s %s" % (r["id"], r["text"]) for r in a["rules"][:8]))
        if a["learn_due"]:
            parts.append("learn_due=true: run `python3 %s/scripts/whisper.py learn` after this task" % W.SKILL_DIR)
        notes.append("\n".join(parts))
    except Exception:
        return

    if notes:
        out["hookSpecificOutput"]["additionalContext"] = "\n".join(notes)
        print(json.dumps(out))


if __name__ == "__main__":
    main()
