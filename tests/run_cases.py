#!/usr/bin/env python3
"""Replay the saved cases and diff the analysis against a stored snapshot.

Why it exists: the thresholds in memory/config.json are tuned against whatever model answered
last. When the Jev model moves, this is what says out loud what changed before a user sees it.

    python3 tests/run_cases.py                 # heuristics only, diff against the snapshot
    python3 tests/run_cases.py --jev           # include Jev's answers (needs TYPESAFE_API_KEY)
    python3 tests/run_cases.py --update        # accept the current output as the new baseline

Exit code 1 when anything drifted, so it can gate a release.
"""
import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _bootstrap  # noqa: E402
# _bootstrap drops the key so the unit suite stays offline; this script is the Jev path.
if _bootstrap._STASHED_JEV_KEY:
    os.environ["TYPESAFE_API_KEY"] = _bootstrap._STASHED_JEV_KEY
import whisper_lib as W  # noqa: E402
from test_cases import CASES_DIR, analyse, load_cases  # noqa: E402

# deliberately NOT inside cases/: load_cases() globs every .json in there
SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases_snapshot.json")


def snapshot_of(case, use_jev):
    cwd = tempfile.mkdtemp()
    if use_jev:
        real = W.claude_md_files
        W.claude_md_files = lambda _c: []
        try:
            a = W.analyze_prompt(case["prompt"], cwd, W.load_config(), use_jev=True)
        finally:
            W.claude_md_files = real
    else:
        a = analyse(case, cwd)
    row = {
        "task_type": a["task_type"]["value"],
        "task_type_source": a["task_type"]["source"],
        "triage": a["triage"]["value"],
        "triage_source": a["triage"]["source"],
        "risk_flags": sorted(a["risk"]["flags"]),
        "risk": a["risk"]["value"],
        "secrets_detected": a["secrets_detected"],
        "multi_task_hint": a["multi_task_hint"],
        "filler_total": a["filler"]["total"],
        "tokens": a["tokens"],
        "paths_missing": a["paths"]["missing"],
        "fenced_blocks": len(a["fenced_blocks"]),
    }
    if use_jev:
        row["jev_used"] = a["jev"]["used"]
        # Jev's confidence wobbles a few points between identical runs, so an exact float would
        # report drift every time and the gate would mean nothing. What matters is which side of
        # the threshold it lands on, because that is what decides heuristic vs jev.
        floor = W.load_config()["jev"]["task_min_confidence"]
        for k in ("task_type", "triage"):
            c = a[k].get("confidence")
            if c is not None:
                row[k + "_confidence_band"] = ">=floor" if c >= floor else "<floor"
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jev", action="store_true", help="ask Jev too (records confidences)")
    ap.add_argument("--update", action="store_true", help="write the current output as the baseline")
    args = ap.parse_args()

    cfg = W.load_config()
    if args.jev and not W.jev_available(cfg):
        print("TYPESAFE_API_KEY not set; nothing to compare.", file=sys.stderr)
        return 2

    cases = load_cases()
    current = {"model": cfg["jev"]["model"] if args.jev else "heuristics",
               "cases": {c["name"]: snapshot_of(c, args.jev) for c in cases}}

    if args.update or not os.path.exists(SNAPSHOT):
        W.write_json(SNAPSHOT, current)
        print("baseline written: %d cases (%s)" % (len(cases), current["model"]))
        return 0

    with open(SNAPSHOT, encoding="utf-8") as f:
        base = json.load(f)
    if base.get("model") != current["model"]:
        print("baseline model %r, this run %r" % (base.get("model"), current["model"]))

    drift = []
    for name, row in current["cases"].items():
        old = base["cases"].get(name)
        if old is None:
            drift.append("%s: new case, no baseline" % name)
            continue
        for k, v in row.items():
            if old.get(k) != v:
                drift.append("%s: %s %r -> %r" % (name, k, old.get(k), v))
    for name in base["cases"]:
        if name not in current["cases"]:
            drift.append("%s: case removed" % name)

    gaps = [c["name"] for c in cases if c.get("known_gap")]
    if gaps:
        print("known gaps still pinned: %s" % ", ".join(gaps))
    if not drift:
        print("%d cases, no drift (%s)" % (len(cases), current["model"]))
        return 0
    print("\n%d change(s) against the baseline:" % len(drift))
    for d in drift:
        print("  " + d)
    print("\nRe-read these before shipping; `--update` accepts them.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
