#!/usr/bin/env python3
"""Replay the saved cases and diff the analysis against a stored snapshot.

Why it exists: the thresholds in memory/config.json are tuned against whatever model answered
last. When the Jev model moves, this is what says out loud what changed before a user sees it.

    python3 tests/run_cases.py                 # heuristics only, diff against cases_snapshot_heuristics.json
    python3 tests/run_cases.py --jev           # include Jev's answers, diff against cases_snapshot_jev.json
    python3 tests/run_cases.py --update        # accept the current output as that path's new baseline
    python3 tests/run_cases.py --jev --samples 8   # majority of 8 draws, and what was unstable

Exit code 1 when anything drifted, so it can gate a release.

Use the same --samples the baseline was written with: a majority-of-8 row carries an _unstable
field that a single draw cannot produce, so mixing the two reports drift that is not there.
"""
import argparse
import collections
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

# deliberately NOT inside cases/: load_cases() globs every .json in there.
# One baseline per path: a heuristics replay diffed against the Jev baseline reports every place the two
# disagree as drift, and its --update would overwrite the baseline the model-version check needs.
_HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOTS = {False: os.path.join(_HERE, "cases_snapshot_heuristics.json"),
             True: os.path.join(_HERE, "cases_snapshot_jev.json")}


def _row(case, use_jev):
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


def snapshot_of(case, use_jev, samples=1):
    """Run the case `samples` times and keep the majority answer per field.

    Jev is non-deterministic: the same prompt can come back `skip` on one run and `light` on the
    next, so a single draw makes the gate report drift that is only sampling noise, and it hides a
    case that is answered wrong a third of the time. The majority is what the user mostly gets;
    `_unstable` names the fields that did not agree with themselves, which is its own defect.
    """
    rows = [_row(case, use_jev) for _ in range(samples)]
    if samples == 1:
        return rows[0], []
    row, unstable = {}, []
    # a row omits *_confidence_band entirely when the heuristic answered, so the key sets differ
    keys = [k for k in rows[0]] + [k for r in rows[1:] for k in r if k not in rows[0]]
    for k in dict.fromkeys(keys):
        seen = collections.Counter(json.dumps(r.get(k), sort_keys=True) for r in rows)
        top, n = seen.most_common(1)[0]
        top = json.loads(top)
        # Which of Jev/heuristic answered can flap while both give the same value: that is
        # provenance, not a decision. Recording the majority of a coin flip would make the
        # baseline disagree with itself run after run, so say "mixed" and stay stable.
        provenance = k.endswith(("_source", "_confidence_band")) or k == "jev_used"
        if n < samples and provenance:
            row[k] = "mixed"
        elif top is not None:
            row[k] = top
        if n < samples and not provenance:
            unstable.append("%s %d/%d" % (k, n, samples))
    return row, sorted(unstable)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jev", action="store_true", help="ask Jev too (records confidences)")
    ap.add_argument("--update", action="store_true", help="write the current output as the baseline")
    ap.add_argument("--samples", type=int, default=1, metavar="N",
                    help="run each case N times and keep the majority answer (Jev is non-deterministic)")
    args = ap.parse_args()

    cfg = W.load_config()
    if args.jev and not W.jev_available(cfg):
        print("TYPESAFE_API_KEY not set; nothing to compare.", file=sys.stderr)
        return 2

    cases = load_cases()
    current = {"model": cfg["jev"]["model"] if args.jev else "heuristics",
               "cases": {}}
    shaky = []
    for c in cases:
        row, unstable = snapshot_of(c, args.jev, args.samples)
        current["cases"][c["name"]] = row
        if unstable:
            shaky.append((c["name"], unstable))

    if shaky:
        print("unstable across %d samples (the model disagreed with itself):" % args.samples)
        for name, fields in shaky:
            print("  %s: %s" % (name, ", ".join(fields)))

    snapshot = SNAPSHOTS[args.jev]
    if args.update or not os.path.exists(snapshot):
        W.write_json(snapshot, current)
        print("baseline written: %d cases (%s, %d sample(s))"
              % (len(cases), current["model"], args.samples))
        return 0

    with open(snapshot, encoding="utf-8") as f:
        base = json.load(f)
    if base.get("model") != current["model"]:
        print("baseline model %r, this run %r" % (base.get("model"), current["model"]))

    drift, provenance = [], []
    for name, row in current["cases"].items():
        old = base["cases"].get(name)
        if old is None:
            drift.append("%s: new case, no baseline" % name)
            continue
        for k, v in row.items():
            if old.get(k) == v:
                continue
            line = "%s: %s %r -> %r" % (name, k, old.get(k), v)
            # Whether Jev or the heuristic supplied an answer can differ run to run while the
            # answer itself is identical. Worth printing, never worth failing: a release gate
            # that cries every run is one nobody reads.
            if k.endswith(("_source", "_confidence_band")) or k == "jev_used":
                provenance.append(line)
            else:
                drift.append(line)
    for name in base["cases"]:
        if name not in current["cases"]:
            drift.append("%s: case removed" % name)

    gaps = [c["name"] for c in cases if c.get("known_gap")]
    if gaps:
        print("known gaps still pinned: %s" % ", ".join(gaps))
    if provenance:
        print("provenance changed (same answers, different source) - not gating:")
        for d in provenance:
            print("  " + d)
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
