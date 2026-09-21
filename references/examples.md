# Before / after

Token counts are chars/4 estimates. The point of each example is the line between trimming and dropping intent.

## 1. Verbose bug report with pasted code (bugfix, ~172 -> ~95 tokens)

Before:
> Hey Claude, I hope you're doing well. I've been working on this project for a while and I have a bug that's
> been driving me crazy. Basically when a user logs in with an expired token, instead of getting a 401 they get
> a 500 error. I think it's somewhere in src/auth.js but I'm not totally sure. Could you please take a look and
> fix it? Please be careful not to break anything else, and make sure to run tests with npm test before
> finishing. Also please explain what you did in detail so I can understand it. Thanks so much!
> ```js
> export function refreshToken(token) { if (!token || token.expired) { throw new Error("token expired ..."); } return token; }
> ```

After:
```
Fix: login with an expired token returns 500; it should return 401 with the existing error body.
Start in @src/auth.js (refreshToken throws instead of mapping to 401); confirm before assuming, the user was not sure.
Reproduce with a failing test first, then fix. Done when: the new test and `npm test` pass.
Scope: the auth code path and its tests.
Reply: root cause (1 line), files as path:line, test result, then <= 10 lines explaining the fix for someone new to this middleware. Nothing else.
```

What moved: greeting, backstory, "driving me crazy" (no decision depends on them); "be careful / don't break
anything" became the check; "run tests with npm test" was already in CLAUDE.md and is now the done-criterion;
the pasted block became `@src/auth.js` because it is that file; "explain in detail" was *kept* and bounded
(<= 10 lines) because it is an explicit output request. Gate: proceed (nothing guessed).

## 2. Terse prompt that needed more, not less (feature, ~15 -> ~55 tokens)

Before:
> add a calendar widget to the home page like the other widgets

After:
```
Add a calendar widget to the home page following the existing widget pattern (look at how the current widgets are built; pick the simplest one as the template and say which).
Behavior: month view, previous/next month, and a way to jump years.
Assumed: no new dependencies; only what the existing widgets use.
Done when: it renders on the home page and `npm test` passes.
Reply: files as path:line, the check result, anything left out. <= 8 lines.
```

What moved: nothing was cut; the rewrite added the pattern pointer, done-criteria, and a stated assumption
so Claude does not stop to ask about libraries. Gate: pause? No: the assumption is reversible and visible.
Expanding a terse prompt is expected; `stats` reports these separately from shrinks.

## 3. Two tasks and a question in one message (multi-step, ~110 -> ~70 tokens)

Before:
> can you look at why the nightly job is slow, I think it's the query in reports/daily.py, and also we need to
> add the CSV export button on the reports page. oh and what's the difference between our two logger configs?

After:
```
Steps:
1) Profile the nightly job; start with the query in @reports/daily.py (the user's guess; confirm). Fix the dominant cost only. Done when: runtime on the sample data drops and `pytest tests/reports` passes.
2) Add a CSV export button to the reports page, following the existing export pattern on that page. Done when: `pytest tests/reports` passes.
3) Answer: the difference between the two logger configs. Read both first; <= 5 lines.
Reply: one line per step with its result, then the final check. <= 10 lines.
```

What moved: three asks became three numbered steps in one prompt, each with its own check or budget. The
logger question is unrelated to the other two, but the user asked for it, so it stays (as the last step, with a
small budget) rather than being queued; deferring an ask is dropping it, and it costs the turn where they ask
again. "I think it's the query" became a starting point to confirm. Task type for the log: `multi`.

## 4. Correction turn: not a Whisperer target

> no, you dropped the year pagination I asked for

Do not rewrite corrections. The hook labels this as `correction / dropped_detail` on the previous run and
stays silent; Claude just acts on it. Rewriting a correction adds a turn instead of saving one.

## 5. Repeated status prompt -> shortcut

Seen three times: "what's pending and what's next?", "what is pending, what should we do next", "whats pending, whats next".

```
shortcut add next "Status: what is still pending in the current task and the single next step. <= 5 lines, no recap of finished work." --from "what's pending and what's next?"
```

From then on `next` (hook on) or `/whisper next` expands to it.

## 6. Risky ops prompt (ops, ~60 -> ~65 tokens)

Before:
> clean up the old feature-flag tables in the db and deploy the migration to prod tonight, the flags were removed last sprint

After:
```
Write a migration that drops the feature-flag tables removed last sprint (find them from the flag removal commits; list them before writing anything).
Run it against the local/dev database and `make test`.
Ask before: running it against any shared or production database, or deploying.
Reply: the tables you found (with the commit that removed each), the migration file path, the dev-run result. <= 8 lines.
```

What moved: the destructive action got a guardrail because `risk` flagged schema + deploy; "tonight" is the
user's schedule, not Claude's instruction, so it was dropped; "find them from the commits" replaced a guess.
Gate: pause (irreversible action in the original).
