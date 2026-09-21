# Reply budgets by task type

Attach one of these as the last block of every rewrite. Visible length is a requirement: effort settings do not
change it, and Claude mirrors the register of the prompt, so state the shape and the cap plainly. Raise a cap
only when the user asked for depth, teaching, or a full report; then say the new cap instead of removing it.

Common tail, present in every contract (fold it into the wording, do not paste it as a separate list):
work silently between tool calls and report once at the end; no restating the plan, no echo of code already
written to files, no list of files inspected, no generic next steps, no second summary, no offer at the end.
Stop at the check, not at the next improvement.

## explain / question
```
Reply: the answer first, in <= 150 words, prose. Cite path:line for anything you claim about the code, after reading it.
```

## bugfix
```
Reply: root cause (1 line), files changed as path:line, the check you ran and its result line. <= 8 lines.
Anything left unverified: one line saying exactly what.
```
Normally preceded by: "Reproduce with a failing test first, then fix, then run `<check>`."

## feature
```
Reply: files changed as path:line, the check and its result, what you intentionally left out. <= 10 lines.
Scope: only the changes requested; no cleanup, refactors, or new dependencies.
```

## refactor
```
Reply: moved/renamed symbols as a list, confirmation of no behavior change with the check that shows it. <= 10 lines.
Scope: the named files only; behavior identical.
```

## test
```
Reply: test names added and what each covers (one line each), run result. <= 10 lines.
No mocks unless the existing tests use them.
```

## review
```
Reply: findings only, ordered by severity, each with path:line and why it matters; gaps that affect correctness or the stated requirements, not style. Max 8. No praise, no summary paragraph.
```

## research
```
Reply: the decision-driving answer first (<= 3 sentences), then the evidence as <= 5 bullets with sources. <= 300 words. Flag anything you could not verify.
```

## ops / config
```
Reply: the exact commands run and their exit status, anything that now needs my action (env vars, secrets, restarts). <= 8 lines.
Ask before anything that touches production or shared infrastructure.
```

## docs
```
Reply: the document itself, nothing around it.
```

## plan
```
Reply: numbered steps, each one line with the file it touches; max 7. Open decisions as one batched question at the top, or none.
```

## multi-step (several related tasks in one prompt)
```
Steps: 1) ... 2) ... 3) ...  Run the check after each step that changes code.
Reply: one line per step (done / result), then the final check. <= 12 lines.
```

## Wording that makes the budget stick

- Say what the reply *contains*, not only what it must not contain.
- Put the cap in lines or words, not adjectives ("concise" does nothing; "<= 8 lines" does).
- Say the reply ends after the last required item; models otherwise append offers.
- If the user wants progress updates during a long task, ask for them explicitly ("one line per completed step"); Fable-class models write fewer updates by default, so silence is the default, not the exception.
