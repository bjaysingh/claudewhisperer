#!/usr/bin/env python3
"""Claude Whisperer CLI - deterministic helpers for the /whisper skill.

Usage (all output is JSON unless noted):
  whisper.py analyze [--cwd DIR] [--no-jev]           prompt on stdin -> analysis json (saved under memory/pending/)
  whisper.py log --analysis ID --gate proceed|pause [--gate-reason TXT] [--transforms a,b] [--rules R001,R002]
                 [--task-type T] [--session SID] [--dry]   stdin: <<<ORIGINAL>>> ... <<<REWRITTEN>>> ...
  whisper.py outcome ok|correction|uncertain [--cause C] [--note TXT] [--run ID] [--by user|claude|hook]
  whisper.py learn                                     consolidate the log into candidate rules
  whisper.py rule add "text" [--source user|auto|claude] | promote C001 "text" | dismiss C001 | retire R001 | list
  whisper.py learnings                                 print learnings.md (text)
  whisper.py stats                                     numbers for /whisper stats
  whisper.py setup                                     environment + Jev + hook status
  whisper.py hook on|off|status                        manage the opt-in hooks in ~/.claude/settings.json
  whisper.py jev-check                                 one live Jev call to confirm the key works
"""
import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import whisper_lib as W  # noqa: E402


def out(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


# ------------------------------------------------------------------ analyze

def cmd_analyze(args) -> None:
    W.ensure_memory()
    cfg = W.load_config()
    text = W.read_stdin_text()
    parts = W.split_sections(text)
    prompt = parts.get("ORIGINAL", "").strip()
    if not prompt:
        out({"error": "no prompt on stdin"})
        sys.exit(2)
    a = W.analyze_prompt(prompt, args.cwd or os.getcwd(), cfg, use_jev=not args.no_jev)
    a["preview"] = W.redact_secrets(prompt)[: cfg["preview_chars"]] if cfg.get("store_previews") else ""
    a["hash"] = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
    if cfg.get("store_prompts"):
        a["original"] = W.redact_secrets(prompt)
    W.reap_pending()
    W.write_json(os.path.join(W.PENDING_DIR, a["id"] + ".json"), a)
    # trim noise for the model: keep what changes the rewrite
    view = {k: a[k] for k in ("id", "project", "tokens", "words", "filler", "risk", "task_type", "triage", "secrets_detected",
                              "paths", "fenced_blocks", "claude_md_files", "claude_md_overlap", "verify_hints", "multi_task_hint",
                              "questions_in_prompt", "shortcut_match", "seen_before", "rules", "learn_due", "hook_on", "jev")}
    if a["shortcut_match"]:
        view["shortcut_expands_to"] = W.load_shortcuts()[a["shortcut_match"]].get("expands_to")
    W.record_prompt_seen(prompt, "", "whisper")
    out(view)


# ------------------------------------------------------------------ log

def _gate_with_jev(original: str, rewritten: str, cfg) -> dict:
    ans = W.jev_ask({"ORIGINAL": original[:9000], "REWRITTEN": rewritten[:3000]}, W.Q_GATE, cfg)
    a = W._answer(ans, "intent_drop")
    if not a or a.get("noul") is None:
        return {"used": False}
    return {"used": True, "intent_drop": round(a["noul"], 3), "pause_at": cfg["jev"]["intent_drop_pause_at"]}


def cmd_log(args) -> None:
    W.ensure_memory()
    cfg = W.load_config()
    parts = W.split_sections(W.read_stdin_text())
    original = parts.get("ORIGINAL", "")
    rewritten = parts.get("REWRITTEN", "")
    analysis = {}
    if args.analysis:
        p = os.path.join(W.PENDING_DIR, args.analysis + ".json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                analysis = json.load(f)
    tokens_in = analysis.get("tokens") or (W.estimate_tokens(original) if original else None)
    tokens_out = W.estimate_tokens(rewritten) if rewritten else None

    gate = args.gate or "proceed"
    gate_reason = args.gate_reason or ""
    jev = {"used": False}
    if original and rewritten and not args.no_jev:
        jev = _gate_with_jev(original, rewritten, cfg)
        if jev.get("used") and jev["intent_drop"] >= cfg["jev"]["intent_drop_pause_at"] and gate == "proceed":
            gate = "pause"
            gate_reason = (gate_reason + "; " if gate_reason else "") + "jev: rewrite may drop intent (p=%.2f)" % jev["intent_drop"]

    rules = [r.strip() for r in (args.rules or "").split(",") if r.strip()]
    rec = {
        "id": "w-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1((original or rewritten or str(os.getpid())).encode()).hexdigest()[:4],
        "ts": W.now_iso(),
        "session": args.session or "",
        "project": analysis.get("project") or os.path.basename(W.git_root(os.getcwd()) or os.getcwd()),
        "mode": "whispered",
        "analysis": args.analysis or "",
        "task_type": args.task_type or analysis.get("task_type", {}).get("value", "other"),
        "triage": analysis.get("triage", {}).get("value", ""),
        "risk": bool(analysis.get("risk", {}).get("value", False)),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "reduction": round(1 - tokens_out / tokens_in, 3) if tokens_in and tokens_out else None,
        "transforms": [t.strip() for t in (args.transforms or "").split(",") if t.strip()],
        "rules": rules,
        "gate": gate,
        "gate_reason": gate_reason,
        "jev": jev,
        "outcome": None,
        "cause": None,
        "note": "",
        "labeled_by": "",
        "reply_chars": None,
        "hook_ms": analysis.get("hook_ms"),   # how long the hook held the prompt, when it ran
        "followups": 0,          # user turns before the user moved on to a new task
        "questions_asked": 0,    # of those, turns that were answers to a question Claude asked
        "followup_kind": None,   # what the user asked for right after (commit/tests/docs/...) - learnable
        "closed": False,
        "dry": bool(args.dry),
        "fp": analysis.get("fingerprint") or W.fingerprint(original),
        "preview": (analysis.get("preview") or W.redact_secrets(original)[: cfg["preview_chars"]]) if cfg.get("store_previews") else "",
    }
    if cfg.get("store_prompts"):
        rec["original"] = W.redact_secrets(original)
        rec["rewritten"] = W.redact_secrets(rewritten)
    # a new run closes any previous open run in this session (the user moved on)
    with W.memory_lock():
        if not args.dry:
            recs = W.read_log()
            changed = False
            for r in recs:
                if not r.get("closed") and not r.get("dry") and (r.get("session") or "") == (args.session or ""):
                    r["closed"] = True
                    if r.get("outcome") is None:
                        r["outcome"], r["labeled_by"] = "ok_implicit", "next-run"
                    changed = True
            if changed:
                W.rewrite_log(recs)
        W.append_log(rec)
    W.bump_rules(rules, "applied")
    if args.analysis:
        try:
            os.remove(os.path.join(W.PENDING_DIR, args.analysis + ".json"))
        except Exception:
            pass
    out({"run_id": rec["id"], "gate": gate, "gate_reason": gate_reason, "jev": jev, "tokens_in": tokens_in,
         "tokens_out": tokens_out, "reduction": rec["reduction"], "learn_due": W.learn_due(cfg)})


# ------------------------------------------------------------------ outcome

def cmd_outcome(args) -> None:
    W.ensure_memory()
    with W.memory_lock():
        _label_run(args)


def _label_run(args) -> None:
    recs = W.read_log()
    idx = None
    if args.run:
        for i, r in enumerate(recs):
            if r["id"] == args.run:
                idx = i
    else:
        # the user is talking about the last rewrite they saw: most recent run, dry included, this session first
        for want_session in ([args.session] if args.session else []) + [None]:
            for i in range(len(recs) - 1, -1, -1):
                if want_session is None or recs[i].get("session") == want_session:
                    idx = i
                    break
            if idx is not None:
                break
    if idx is None:
        out({"error": "no run to label"})
        sys.exit(1)
    r = recs[idx]
    already_counted = r.get("outcome") == "correction"
    r["outcome"] = args.value
    r["cause"] = args.cause or (None if args.value.startswith("ok") else r.get("cause"))
    if args.note:
        r["note"] = args.note[:300]
    r["labeled_by"] = args.by or "user"
    r["labeled_at"] = W.now_iso()
    W.rewrite_log(recs)
    if args.value == "correction" and not already_counted:
        W.bump_rules(r.get("rules", []), "corrections")
    out({"run_id": r["id"], "outcome": r["outcome"], "cause": r["cause"], "rules_on_run": r.get("rules", []), "transforms": r.get("transforms", [])})


# ------------------------------------------------------------------ shortcuts

def cmd_shortcut(args) -> None:
    with W.memory_lock():
        _apply_shortcut_op(args)


def _apply_shortcut_op(args) -> None:
    sc = W.load_shortcuts()
    if args.op == "list":
        out({k: {"expands_to": v.get("expands_to"), "uses": v.get("uses", 0), "created": v.get("created")} for k, v in sc.items()})
        return
    if args.op == "add":
        key = (args.key or "").strip().lower()
        text = (args.text or W.read_stdin_text()).strip()
        if not key or not text:
            out({"error": "usage: shortcut add <key> \"<expanded prompt>\" [--from \"original wording\"]"})
            sys.exit(2)
        if len(key.split()) > 3:
            out({"error": "shortcut keys are 1-3 words"})
            sys.exit(2)
        entry = sc.get(key, {"created": datetime.now().strftime("%Y-%m-%d"), "uses": 0, "learned_from": []})
        entry["expands_to"] = text
        if args.source_text:
            entry["learned_from"] = list(dict.fromkeys(entry.get("learned_from", []) + [args.source_text[:120]]))
        sc[key] = entry
        W.save_shortcuts(sc)
        out({"added": key, "expands_to": text})
    elif args.op == "rm":
        removed = sc.pop((args.key or "").strip().lower(), None) is not None
        W.save_shortcuts(sc)
        out({"removed": args.key, "ok": removed})
    elif args.op == "suggest":
        out({"repeated_prompts": W.repeated_prompts(min_count=args.min_count)})


# ------------------------------------------------------------------ learn (consolidation)

def cmd_learn(args) -> None:
    W.ensure_memory()
    with W.memory_lock():
        _consolidate(args)


def _consolidate(args) -> None:
    cfg = W.load_config()
    all_recs = [r for r in W.read_log() if W.is_whispered(r)]   # baseline runs are the control, not evidence
    recs = [r for r in all_recs if not r.get("dry")]
    # Only strong signals count as evidence: explicit ok / explicit correction.
    # "The user moved on" (ok_implicit) is not evidence of success. A dry/audit run the user labeled counts too:
    # the rewrite was judged even though nothing ran.
    labeled = [r for r in all_recs if r.get("outcome") in ("ok", "correction")]
    sections = W.load_rules()
    existing = {e["text"] for sec in sections.values() for e in sec}
    existing_stems = {e["text"].split(";")[0] for sec in sections.values() for e in sec}
    candidates = []

    def add(text, n):
        if text not in existing and text.split(";")[0] not in existing_stems:
            candidates.append({"text": text, "n": n})

    # 0. routine follow-ups the user keeps sending after a task type -> fold them into the rewrite
    fu = defaultdict(Counter)
    for r in recs:
        if r.get("followup_kind") and r["followup_kind"] not in ("unrelated", None):
            fu[r.get("task_type", "other")][r["followup_kind"]] += 1
    for tt, c in fu.items():
        total_tt = sum(1 for r in recs if r.get("task_type") == tt and r.get("closed"))
        for kind, n in c.items():
            if n >= 3 and n / max(1, total_tt) >= 0.5:
                add("task=%s: user asked for `%s` right after in %d/%d runs; include that step in the rewrite by default" % (tt, kind, n, total_tt), n)

    # 0b. tasks that took many turns -> front-load decisions
    turns = defaultdict(list)
    for r in recs:
        if r.get("closed"):
            turns[r.get("task_type", "other")].append(int(r.get("followups", 0)))
    for tt, xs in turns.items():
        if len(xs) >= 3 and sum(xs) / len(xs) >= 2:
            add("task=%s: averaged %.1f follow-up turns over %d runs; state assumptions inline and batch any questions into one AskUserQuestion at the start" % (tt, sum(xs) / len(xs), len(xs)), len(xs))
    q_total = sum(int(r.get("questions_asked", 0)) for r in recs if r.get("closed"))
    if q_total >= 3:
        add("Claude asked %d questions the user then answered across runs; look up test/lint commands, paths and conventions before asking, and ask only when irreversible" % q_total, q_total)

    # 0c. repeated prompts -> shortcuts
    for rp in W.repeated_prompts(min_count=3):
        add("repeated prompt (%d times, words: %s): create a shortcut with `whisper.py shortcut add <key> \"<expanded prompt>\"`; examples: %s" % (rp["count"], rp["fp"], " / ".join(rp["examples"])), rp["count"])

    # 1. transforms that precede dropped-detail corrections
    t_applied, t_bad = Counter(), Counter()
    for r in labeled:
        for t in r.get("transforms", []):
            t_applied[t] += 1
            if r["outcome"] == "correction" and r.get("cause") == "dropped_detail":
                t_bad[t] += 1
    for t, n in t_applied.items():
        if n >= 3 and t_bad[t] / n >= 0.4:
            add("transform `%s` preceded dropped_detail corrections in %d/%d runs; before applying it, check the rewrite still carries every explicit ask" % (t, t_bad[t], n), n)

    # 2. task types with repeated verbosity or scope complaints
    by_tt = defaultdict(Counter)
    for r in labeled:
        if r["outcome"] == "correction" and r.get("cause"):
            by_tt[r.get("task_type", "other")][r["cause"]] += 1
    for tt, c in by_tt.items():
        if c["too_verbose"] >= 2:
            add("task=%s: reply still too long in %d runs; tighten the output contract (fewer lines, no narration)" % (tt, c["too_verbose"]), c["too_verbose"])
        if c["scope_creep"] >= 2:
            add("task=%s: scope creep in %d runs; always add an explicit scope line naming what not to touch" % (tt, c["scope_creep"]), c["scope_creep"])
        if c["needed_clarification"] >= 2:
            add("task=%s: should have asked first in %d runs; pause at the gate when the target file or behavior is not named" % (tt, c["needed_clarification"]), c["needed_clarification"])
        if c["dropped_detail"] >= 2:
            add("task=%s: dropped a requested detail in %d runs; keep explicit output requests verbatim, only bound their length" % (tt, c["dropped_detail"]), c["dropped_detail"])

    # 3. rules that look harmful (contradicted twice unless support is 3x higher)
    for e in sections["Rules"]:
        c, a_ = e.get("corrections", 0), e.get("applied", 0)
        if (c >= 2 and a_ < 3 * c) or (a_ >= 5 and c / max(1, a_) >= 0.5):
            add("rule %s may be harmful: corrections in %d/%d runs where it was applied; retire it or narrow its scope" % (e["id"], c, a_), a_)

    # 4. repeated user notes with the same cause
    notes = Counter()
    for r in labeled:
        if r.get("labeled_by") == "user" and r["outcome"] == "correction" and r.get("cause"):
            notes[r["cause"]] += 1
    for cause, n in notes.items():
        if n >= 2:
            add("user flagged `%s` %d times via /whisper bad; re-read those notes (`/whisper stats`) and encode the pattern as a rule" % (cause, n), n)

    # 5. stale rules
    total = len(recs)
    stale = []
    for e in sections["Rules"]:
        if total >= cfg["stale_after_runs"] and e.get("applied", 0) == 0 and e.get("source") != "user":
            stale.append(e["id"])

    for c in candidates:
        cid = W.next_id(sections, "C")
        sections["Candidates"].append({"id": cid, "text": W.one_line(c["text"]), "source": "auto", "created": datetime.now().strftime("%Y-%m-%d"),
                                       "applied": c["n"], "corrections": 0, "extra": []})
    W.save_rules(sections)
    W.mark_learned()
    out({"runs": total, "strong_signals": len(labeled),
         "new_candidates": [{"id": e["id"], "text": e["text"]} for e in sections["Candidates"][-len(candidates):]] if candidates else [],
         "open_candidates": [{"id": e["id"], "text": e["text"]} for e in sections["Candidates"]],
         "stale_rules": stale,
         "active_rules": len(sections["Rules"]),
         "over_cap": max(0, len(sections["Rules"]) - cfg["max_active_rules"]),
         "next": "promote each candidate as a concrete rule (`rule promote C00x \"<rule>\"`) or dismiss it; retire stale/harmful rules"})


# ------------------------------------------------------------------ rules

def cmd_rule(args) -> None:
    W.ensure_memory()
    with W.memory_lock():
        _apply_rule_op(args)


def _apply_rule_op(args) -> None:
    sections = W.load_rules()
    today = datetime.now().strftime("%Y-%m-%d")
    if args.op == "add":
        rid = W.next_id(sections, "R")
        sections["Rules"].append({"id": rid, "text": W.one_line(args.text), "source": args.source or "user", "created": today,
                                  "applied": 0, "corrections": 0, "extra": []})
        W.save_rules(sections)
        out({"added": rid, "text": W.one_line(args.text)})
    elif args.op == "promote":
        cand = next((e for e in sections["Candidates"] if e["id"] == args.id), None)
        if not cand:
            out({"error": "no candidate " + str(args.id)})
            sys.exit(1)
        sections["Candidates"].remove(cand)
        rid = W.next_id(sections, "R")
        sections["Rules"].append({"id": rid, "text": W.one_line(args.text or cand["text"]), "source": "auto", "created": today,
                                  "applied": 0, "corrections": 0, "extra": ["from " + cand["id"]]})
        W.save_rules(sections)
        out({"promoted": cand["id"], "as": rid})
    elif args.op == "dismiss":
        before = len(sections["Candidates"])
        sections["Candidates"] = [e for e in sections["Candidates"] if e["id"] != args.id]
        W.save_rules(sections)
        out({"dismissed": args.id, "removed": before - len(sections["Candidates"])})
    elif args.op == "retire":
        rule = next((e for e in sections["Rules"] if e["id"] == args.id), None)
        if not rule:
            out({"error": "no rule " + str(args.id)})
            sys.exit(1)
        sections["Rules"].remove(rule)
        rule["extra"].append("retired " + today)
        sections["Retired"].append(rule)
        W.save_rules(sections)
        out({"retired": args.id})
    else:
        out({k: [{"id": e["id"], "text": e["text"], "applied": e.get("applied", 0), "corrections": e.get("corrections", 0)} for e in v]
             for k, v in sections.items()})


def cmd_learnings(args) -> None:
    print(W.load_learnings_text())


# ------------------------------------------------------------------ stats

MIN_PER_ARM = 10          # below this a difference between the arms is noise, and saying otherwise is worse than silence


def _arm(recs) -> dict:
    """The metrics that say whether a prompt worked, for one arm of the comparison."""
    labeled = [r for r in recs if r.get("outcome") in ("ok", "correction")]
    closed = [r for r in recs if r.get("closed")]
    replies = [r["reply_chars"] for r in recs if r.get("reply_chars")]
    fups = [int(r.get("followups", 0)) for r in closed]
    return {
        "runs": len(recs),
        "labeled_runs": len(labeled),
        "correction_rate_pct": round(100 * sum(1 for r in labeled if r["outcome"] == "correction") / len(labeled), 1) if labeled else None,
        "avg_followup_turns": round(sum(fups) / len(fups), 2) if fups else None,
        "avg_reply_chars": int(sum(replies) / len(replies)) if replies else None,
        "questions_per_run": round(sum(int(r.get("questions_asked", 0)) for r in closed) / len(closed), 2) if closed else None,
    }


def _delta(whispered, baseline) -> dict:
    """Whispered minus baseline. Negative is better for every metric here."""
    out_ = {}
    for k in ("correction_rate_pct", "avg_followup_turns", "avg_reply_chars", "questions_per_run"):
        a, b = whispered.get(k), baseline.get(k)
        out_[k] = round(a - b, 2) if a is not None and b is not None else None
    return out_


def _effect(recs) -> dict:
    """Does the pass change anything? Answered against the prompts it chose not to touch.

    The hook logs those as `baseline` runs and labels them through the same path, so the two arms
    differ in the treatment rather than in how they were measured. Self-selected, not randomised:
    the triage sends the messier prompts to the treatment arm, which biases against the pass."""
    whispered = _arm([r for r in recs if W.is_whispered(r)])
    baseline = _arm([r for r in recs if not W.is_whispered(r)])
    state = {"whispered": whispered, "baseline": baseline}
    if min(whispered["labeled_runs"], baseline["labeled_runs"]) < MIN_PER_ARM:
        state["verdict"] = ("not enough data: %d labelled whispered runs and %d labelled baseline runs, "
                            "need %d of each before a difference means anything"
                            % (whispered["labeled_runs"], baseline["labeled_runs"], MIN_PER_ARM))
    else:
        state["delta_vs_baseline"] = _delta(whispered, baseline)
        state["verdict"] = "compare delta_vs_baseline; negative is better on every metric, and the split is self-selected"
    return state


def _trend(recs) -> dict:
    """Recent half against the earlier half: is the learning loop actually moving anything?"""
    labeled = [r for r in recs if W.is_whispered(r) and r.get("outcome") in ("ok", "correction")]
    if len(labeled) < 2 * MIN_PER_ARM:
        return {"verdict": "not enough labelled runs yet (%d of %d)" % (len(labeled), 2 * MIN_PER_ARM)}
    half = len(labeled) // 2
    earlier, recent = _arm(labeled[:half]), _arm(labeled[half:])
    return {"earlier": earlier, "recent": recent, "change": _delta(recent, earlier)}


def _latency(recs) -> dict:
    """What the hook costs the user, measured. Prune/optimise when p95 climbs, not before."""
    xs = sorted(int(r["hook_ms"]) for r in recs if r.get("hook_ms") is not None)
    if not xs:
        return {"samples": 0}
    return {"samples": len(xs), "median": xs[len(xs) // 2], "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))],
            "max": xs[-1]}


def cmd_stats(args) -> None:
    W.ensure_memory()
    all_recs = W.read_log()
    if args.session:
        all_recs = [r for r in all_recs if r.get("session") == args.session]
    recs = [r for r in all_recs if not r.get("dry")]
    dry = [r for r in all_recs if r.get("dry")]
    n = len(recs)
    whispered = [r for r in recs if W.is_whispered(r)]
    # nudged or analysed, never whispered: in neither arm. Pending files carry no session, so no per-session count.
    never_logged = None if args.session else W.never_logged_count()
    if n == 0 and not dry:
        out({"runs": 0, "message": "no runs yet", "analyses_never_logged": never_logged})
        return
    outcomes = Counter(r.get("outcome") or "pending" for r in whispered)
    causes = Counter(r.get("cause") for r in whispered if r.get("outcome") == "correction")
    reductions = [r["reduction"] for r in whispered if r.get("reduction") is not None]
    shrunk = sorted(x for x in reductions if x > 0)
    expanded = sum(1 for x in reductions if x < 0)
    tin = sum(r.get("tokens_in") or 0 for r in whispered)
    tout = sum(r.get("tokens_out") or 0 for r in whispered)
    replies = [r["reply_chars"] for r in recs if r.get("reply_chars")]
    by_tt = defaultdict(lambda: {"runs": 0, "corrections": 0})
    for r in whispered:
        b = by_tt[r.get("task_type", "other")]
        b["runs"] += 1
        b["corrections"] += 1 if r.get("outcome") == "correction" else 0
    tf = defaultdict(lambda: {"applied": 0, "corrections": 0})
    for r in whispered:
        for t in r.get("transforms", []):
            tf[t]["applied"] += 1
            tf[t]["corrections"] += 1 if r.get("outcome") == "correction" else 0
    jev_st = {}
    if os.path.exists(W.JEV_STATUS_PATH):
        with open(W.JEV_STATUS_PATH, encoding="utf-8") as f:
            jev_st = json.load(f)
    labeled = outcomes["ok"] + outcomes["correction"]
    closed = [r for r in whispered if r.get("closed")]
    fups = [int(r.get("followups", 0)) for r in closed]
    out({
        "runs": n,
        "whispered_runs": sum(1 for r in recs if W.is_whispered(r)),
        "baseline_runs": sum(1 for r in recs if not W.is_whispered(r)),
        "analyses_never_logged": never_logged,
        "does_it_help": _effect(recs),
        "trend": _trend(recs),
        "dry_or_audit_runs": len(dry),
        "dry_runs_labeled": {k: v for k, v in Counter(r.get("outcome") for r in dry if r.get("outcome")).items()},
        "prompt_tokens_saved_total": tin - tout,
        "median_reduction_pct_when_shrunk": round(100 * shrunk[len(shrunk) // 2], 1) if shrunk else None,
        "runs_shrunk": len(shrunk),
        "runs_expanded": expanded,   # terse prompts that needed done-criteria or a check added; expected, not a failure
        "outcomes": dict(outcomes),
        "correction_rate_pct": round(100 * outcomes["correction"] / labeled, 1) if labeled else None,
        "strong_signals": labeled,
        "causes": dict(causes),
        "gate": dict(Counter(r.get("gate") for r in recs)),
        "avg_reply_chars": int(sum(replies) / len(replies)) if replies else None,
        "hook_ms": _latency(recs),
        "avg_followup_turns": round(sum(fups) / len(fups), 2) if fups else None,
        "questions_claude_asked": sum(int(r.get("questions_asked", 0)) for r in closed),
        "followup_kinds": dict(Counter(r.get("followup_kind") for r in closed if r.get("followup_kind"))),
        "shortcuts": {k: v.get("uses", 0) for k, v in W.load_shortcuts().items()},
        "by_task_type": dict(by_tt),
        "by_transform": dict(tf),
        "recent_user_notes": [{"run": r["id"], "cause": r.get("cause"), "note": r.get("note")} for r in all_recs if r.get("note")][-8:],
        "jev": jev_st or {"calls": 0},
        "learn_due": W.learn_due(W.load_config()),
    })


# ------------------------------------------------------------------ setup / hook / jev-check

SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
HOOK_MARK = "claude-whisperer"


def _hook_entries():
    hooks_dir = os.path.join(W.SKILL_DIR, "hooks")
    return {
        "UserPromptSubmit": {"type": "command", "command": "python3 \"%s\" # %s" % (os.path.join(hooks_dir, "user_prompt_submit.py"), HOOK_MARK), "timeout": 8},
        "Stop": {"type": "command", "command": "python3 \"%s\" # %s" % (os.path.join(hooks_dir, "stop.py"), HOOK_MARK), "timeout": 5},
    }


def _load_settings():
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _hook_installed(settings) -> dict:
    status = {}
    for ev in ("UserPromptSubmit", "Stop"):
        found = False
        for group in settings.get("hooks", {}).get(ev, []):
            for h in group.get("hooks", []):
                if HOOK_MARK in str(h.get("command", "")):
                    found = True
        status[ev] = found
    return status


def cmd_hook(args) -> None:
    settings = _load_settings()
    if args.op == "status":
        out({"settings": SETTINGS_PATH, "installed": _hook_installed(settings)})
        return
    hooks = settings.setdefault("hooks", {})
    if args.op == "on":
        for ev, entry in _hook_entries().items():
            groups = hooks.setdefault(ev, [])
            if not any(HOOK_MARK in str(h.get("command", "")) for g in groups for h in g.get("hooks", [])):
                groups.append({"hooks": [entry]})
        msg = "hooks added; they take effect in new Claude Code sessions (or after /hooks reload)"
    else:
        for ev in ("UserPromptSubmit", "Stop"):
            groups = hooks.get(ev, [])
            for g in groups:
                g["hooks"] = [h for h in g.get("hooks", []) if HOOK_MARK not in str(h.get("command", ""))]
            hooks[ev] = [g for g in groups if g.get("hooks")]
            if not hooks[ev]:
                hooks.pop(ev, None)
        if not hooks:
            settings.pop("hooks", None)
        msg = "hooks removed; takes effect in new sessions"
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    if os.path.exists(SETTINGS_PATH):
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            backup = f.read()
        with open(SETTINGS_PATH + ".whisperer.bak", "w", encoding="utf-8") as f:
            f.write(backup)
    W.write_json(SETTINGS_PATH, settings)
    out({"ok": True, "message": msg, "installed": _hook_installed(settings), "backup": SETTINGS_PATH + ".whisperer.bak"})


def _plugin_hint() -> dict:
    root = os.path.expanduser("~/.claude/plugins")
    hits = []
    if os.path.isdir(root):
        for dirpath, dirnames, filenames in os.walk(root):
            if dirpath.count(os.sep) - root.count(os.sep) > 4:
                dirnames[:] = []
                continue
            if "typesafe" in os.path.basename(dirpath).lower():
                hits.append(dirpath)
                dirnames[:] = []
    return {"typesafe_plugin_dirs": hits[:5]}


def cmd_setup(args) -> None:
    W.ensure_memory()
    cfg = W.load_config()
    try:
        import typesafe_sdk  # noqa: F401
        sdk = True
    except Exception:
        sdk = False
    out({
        "python": sys.version.split()[0],
        "skill_dir": W.SKILL_DIR,
        "memory_dir": W.MEMORY_DIR,
        "memory_writable": os.access(W.MEMORY_DIR, os.W_OK),
        "runs_logged": len(W.read_log()),
        "rules": len(W.load_rules()["Rules"]),
        "jev": {
            "enabled_in_config": cfg["jev"].get("enabled", True),
            "api_key_present": bool(os.environ.get("TYPESAFE_API_KEY")),
            "python_sdk_importable": sdk,
            "model_pinned": cfg["jev"]["model"],
            "model_is_an_alias": cfg["jev"]["model"].endswith(("latest", "preview")),
            **_version_state(cfg),
            **_plugin_hint(),
            "how_to_enable": "export TYPESAFE_API_KEY=... (the skill calls the HTTP API directly; the SDK and plugin are optional)",
        },
        "hooks": _hook_installed(_load_settings()),
        "config": cfg,
    })


def _version_state(cfg) -> dict:
    """What actually answered last, versus what config.json asks for.

    Thresholds are tuned per model, so a silent version change invalidates them."""
    st = W.read_jev_status()
    state = {
        "model_answered_last": st.get("model_reported", ""),
        "calls": st.get("calls", 0),
        "failures": st.get("failures", 0),
        "last_ms": st.get("last_ms"),
        "models_seen": st.get("models_seen", {}),
    }
    if st.get("version_drift"):
        state["warning"] = ("model drift: config asks for %s, the API answered as %s. Every threshold in "
                            "memory/config.json was tuned against the old one - run "
                            "`python3 tests/run_cases.py --jev` and read the diff before trusting them."
                            % (st.get("model_requested"), st.get("model_reported")))
    elif cfg["jev"]["model"].endswith(("latest", "preview")):
        state["warning"] = ("model is the floating alias `%s`; it moves without a change on your side and "
                            "silently invalidates the tuned thresholds. Pin the version it reports."
                            % cfg["jev"]["model"])
    return state


def cmd_jev_check(args) -> None:
    cfg = W.load_config()
    if not W.jev_available(cfg):
        out({"ok": False, "reason": "TYPESAFE_API_KEY not set or jev disabled in memory/config.json"})
        return
    ans = W.jev_ask({"prompt": "hey could you please fix the failing login test, it's driving me crazy, thanks!"}, W.Q_ANALYZE, cfg)
    out({"ok": bool(ans), "requested_model": cfg["jev"]["model"], **_version_state(cfg), "answers": ans})


# ------------------------------------------------------------------ main

def main() -> None:
    p = argparse.ArgumentParser(prog="whisper.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze"); s.add_argument("--cwd"); s.add_argument("--no-jev", action="store_true"); s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("log")
    s.add_argument("--analysis"); s.add_argument("--gate", choices=["proceed", "pause"]); s.add_argument("--gate-reason")
    s.add_argument("--transforms"); s.add_argument("--rules"); s.add_argument("--task-type"); s.add_argument("--session")
    s.add_argument("--dry", action="store_true"); s.add_argument("--no-jev", action="store_true"); s.set_defaults(fn=cmd_log)

    s = sub.add_parser("outcome"); s.add_argument("value", choices=["ok", "ok_implicit", "correction", "uncertain"]); s.add_argument("--cause")
    s.add_argument("--note"); s.add_argument("--run"); s.add_argument("--by"); s.add_argument("--session"); s.set_defaults(fn=cmd_outcome)

    s = sub.add_parser("shortcut"); s.add_argument("op", choices=["add", "rm", "list", "suggest"])
    s.add_argument("key", nargs="?"); s.add_argument("text", nargs="?"); s.add_argument("--from", dest="source_text")
    s.add_argument("--min-count", type=int, default=3); s.set_defaults(fn=cmd_shortcut)

    s = sub.add_parser("learn"); s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("rule"); s.add_argument("op", choices=["add", "promote", "dismiss", "retire", "list"])
    s.add_argument("id", nargs="?"); s.add_argument("text", nargs="?"); s.add_argument("--source"); s.set_defaults(fn=cmd_rule)

    s = sub.add_parser("learnings"); s.set_defaults(fn=cmd_learnings)
    s = sub.add_parser("stats"); s.add_argument("--session"); s.set_defaults(fn=cmd_stats)
    s = sub.add_parser("setup"); s.set_defaults(fn=cmd_setup)
    s = sub.add_parser("hook"); s.add_argument("op", choices=["on", "off", "status"]); s.set_defaults(fn=cmd_hook)
    s = sub.add_parser("jev-check"); s.set_defaults(fn=cmd_jev_check)

    args = p.parse_args()
    if args.cmd == "rule" and args.op == "add":
        # `rule add "text"` -> argparse puts the text in `id`
        args.text = args.text or args.id
        if not args.text:
            p.error("rule add needs the rule text")
    args.fn(args)


if __name__ == "__main__":
    main()
