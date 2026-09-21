# Claude Whisperer (`/whisper`)

A Claude Code skill that rewrites your prompt into the form Claude works best from, executes it, and learns from
what happens next. It subtracts before it adds: filler, restated CLAUDE.md, pasted code that is already in the
repo; then adds only the specifics that would otherwise cost a follow-up turn (target, done-criteria, the check
to run, a reply budget). Every learned rule and shortcut lives in `memory/`, never in `SKILL.md`.

## Install

```bash
# the folder name is the slash command, so clone straight to it:
git clone https://github.com/bjaysingh/claudewhisperer ~/.claude/skills/whisper
python3 ~/.claude/skills/whisper/scripts/whisper.py setup
```

Restart Claude Code (or `/reload-plugins`), then `/whisper <prompt>`.

Requirements: Python 3.9+ (macOS system python is fine), no packages. Works in any repo; uses `git` when present.

## Use

| | |
|---|---|
| `/whisper fix the login bug, it 500s on expired tokens...` | rewrite, show it, execute |
| `/whisper quiet ...` | rewrite and execute, show nothing |
| `/whisper dry ...` / `/whisper audit ...` | show the rewrite (audit adds the changes and token delta); do not execute |
| `/whisper good` / `/whisper bad it dropped the year pagination` | label the last run; `bad` usually produces a rule |
| `/whisper stats` | reduction, correction rate, follow-up turns per task, causes, shortcut use |
| `/whisper learnings` | the rules and shortcuts it has learned |
| `/whisper learn` | consolidate the log into rules now (also runs on its own every 10 runs) |
| `/whisper restart` | one fresh prompt carrying everything settled in this session, for after `/clear` |
| `/whisper setup` | environment, Jev, and hook status |

The rewrite is shown in a 3-6 line block and then executed as if you had sent it. It pauses for a `go` only when
it had to guess your intent, dropped something that might matter, or the task is destructive.

## The opt-in hook (recommended after a few manual runs)

```bash
python3 ~/.claude/skills/whisper/scripts/whisper.py hook on     # or /whisper hook on
```

This adds a `UserPromptSubmit` and a `Stop` hook to `~/.claude/settings.json` (backup written next to it). With
the hook on:

- every prompt is triaged before it reaches Claude; prompts that are already tight, short replies, corrections,
  and routine follow-ups ("commit and push") pass through untouched; the rest get a nudge to apply the pass,
  with the analysis already done. On heuristics alone this costs 70-80 ms, and stays there:
  `prompts.jsonl` self-trims to the newest `max_prompts_retained` (5000) records, so the cost does not drift
  upward with use. `/whisper stats` reports the hook's own median/p95/max, measured rather than assumed. **With Jev enabled the hook makes up
  to three sequential API calls** - labelling your last run, triaging this prompt, classifying the follow-up -
  each bounded by `timeout_s` (2.5 s), so a stalled network can add several seconds before your prompt is sent.
  The hook entry is registered with an 8 s timeout. Lower `jev.timeout_s` in `memory/config.json` if you would
  rather lose the classification than wait for it;
- your next message after a Whisperer run is labeled automatically (correction / new task / approval), which is
  the learning signal; follow-up turns and questions Claude asked are counted;
- repeated prompts are detected from a content-word fingerprint (not the text) and offered as shortcuts;
- an exact shortcut key (`next`) is expanded before Claude sees it;
- the `Stop` hook records the length of Claude's final reply so `stats` can show whether replies shrink.

`hook off` removes both. Changes take effect in the next session. To make the hook session-scoped instead of
global (on only after the first `/whisper` in a session), move the two entries into a `hooks:` block in
`SKILL.md` frontmatter using `${CLAUDE_SKILL_DIR}/hooks/...` as the command paths.

## Jev (TypeSafe AI), optional

Jev is a decision model (typed answers with confidence, no text generation). Whisperer uses it for the
decisions in the loop, not for writing the rewrite:

| decision | question type | fallback when Jev is absent |
|---|---|---|
| does this prompt need a pass at all (skip / light / full) | choice | length + filler heuristics |
| task type | choice | keyword scores |
| is the task destructive or external | noul | keyword flags |
| did the rewrite drop intent (gate) | noul | Claude's own judgment only |
| is the next message a correction / new task / approval, and why | choice + choice | keyword patterns |
| which routine follow-up did the user send | choice | keyword patterns |

Enable it by exporting the key in the shell that launches Claude Code:

```bash
export TYPESAFE_API_KEY=sk-...
python3 ~/.claude/skills/whisper/scripts/whisper.py jev-check
```

The scripts call `POST https://api.typesafe.ai/v1/systemone` with the stdlib; the `typesafe-sdk` package and the
`typesafe@typesafe-ai` Claude plugin are not required (the plugin is detected and reported by `setup` for
information only). Thresholds live in `memory/config.json`: `task_min_confidence` (0.55), `label_min_confidence`
(0.6), `intent_drop_pause_at` (0.35, deliberately low: silently dropping intent costs more than an extra `go`).
A slow (>2.5 s) or failed call is ignored and counted in `stats`.

The model is **pinned** (`jev-1.13.0`), not set to the `jev-latest` alias: every threshold above is tuned
against whatever model answered last, and an alias moves without a change on your side. Whisperer records the
`model` each response reports, and `/whisper setup` warns when it stops matching the pinned id. After a
deliberate bump, run `python3 tests/run_cases.py --jev` and read the diff before trusting the thresholds.

## What it learns, and where

- `memory/learnings.md`: rules, one per line, with applied/corrections counts. Candidates from `learn` sit in
  their own section until promoted or dismissed. Cap: 25 active rules (more than that and Claude starts ignoring
  them, which defeats the purpose). Hand-edit freely; keep the `[Rnnn]` ids.
- `memory/shortcuts.json`: key -> expanded prompt.
- `memory/log.jsonl`: one record per run: task type, tokens before/after, transforms applied, rules applied, gate,
  outcome, cause, follow-up turns, reply length. No prompt text by default.
- `memory/prompts.jsonl`: content-word fingerprints of prompts seen by the hook, for repetition detection.
  A rolling window, not a history: it trims to the newest 5000 records (`max_prompts_retained`) once it passes
  `prompts_max_bytes`. That is deliberate - a prompt you repeated months ago is not a current habit worth a
  shortcut - and it keeps the per-prompt hook cost flat. The run log is never trimmed; it is the evidence.
- `memory/config.json`: thresholds and privacy switches (`store_previews`, `store_prompts` default off).

Evidence rules: only explicit corrections and approvals drive rule promotion. "The user moved on" is recorded
as `ok_implicit` and shown in stats, but it is not evidence of success.

Set `WHISPERER_HOME=/some/dir` to keep `memory/` outside the skill folder (survives reinstalling the skill).

## Layout

```
whisper/
├── SKILL.md                     the pass, commands, learning loop (read by Claude)
├── README.md
├── scripts/whisper.py           CLI: analyze, log, outcome, learn, rule, shortcut, stats, setup, hook, jev-check
├── scripts/whisper_lib.py       heuristics, memory io, Jev client (stdlib only)
├── hooks/user_prompt_submit.py  opt-in: triage, labeling, fingerprints, shortcut expansion
├── hooks/stop.py                opt-in: reply length
├── references/claude-prompt-rules.md   what Claude responds to, with sources
├── references/output-contracts.md      reply budgets per task type
├── references/examples.md              before/after
└── memory/                      learnings.md, shortcuts.json, log.jsonl, prompts.jsonl, config.json
```

`memory/` is not in the repository: it is created with defaults on the first run and holds only your own
learning state.

## Development

```bash
cd tests && python3 -m unittest discover -p 'test_*.py'   # ~1s, stdlib only
cd tests && python3 run_cases.py                          # replay the saved prompt cases
```

`tests/cases/prompts.json` holds the saved prompts the analysis layer is tuned against, ugly ones included.
`run_cases.py` diffs a replay against `tests/cases_snapshot.json` and exits 1 on drift; run it with `--jev`
after changing the Jev model or any threshold in `memory/config.json`, because those are tuned against
whatever model answered last. Tests write to a temp `WHISPERER_HOME`, never your real `memory/`.

## Design notes

- `disable-model-invocation: true`: it runs only when you call it or when the hook you enabled nudges it.
- `allowed-tools: Bash(python3 *)` lets the skill run its own scripts without a permission prompt. Claude Code
  cannot scope a Bash rule to a script path, so this is the narrowest documented form; remove the line if you
  would rather approve once per session.
- The rewrite is never grading itself: with Jev on, an independent read of the two prompts can force a pause.
- Sources: Anthropic prompting best practices (platform.claude.com), Claude Code best practices and hooks
  reference (code.claude.com), TypeSafe API and confidence docs (docs.typesafe.ai). Community inputs:
  nidhinjs/prompt-master (intent extraction, diagnostics, credential handling) and a ChatGPT-authored
  prompt-optimizer (priority order, evidence taxonomy, reply budgets).

## License

MIT. See [LICENSE](LICENSE).
