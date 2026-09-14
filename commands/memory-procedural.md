# Build Procedural Memory (learned playbooks)

Given the user's spec, build procedural memory: playbooks induced from repeated
episodes, matched by precondition, replayed, and demoted when they stop working.

**Ask what actually repeats.** Get 2-3 concrete routines the user watches the
agent redo. If they cannot name any, this is premature — say so and point at
episodic memory to gather the evidence first. Also ask for the replay mode:
advisory (default) or strict.

## What to build

```
<project>/
└── src/<package>/
    ├── procedure.py  # Procedure + Step + smoothed, age-decayed trust
    ├── store.py      ├── inducer.py   # episode clusters -> playbook
    ├── matcher.py    # 3-stage precondition matching
    ├── replayer.py   # advisory render + strict driver
    ├── trust.py      └── cli.py  # induce / list / show / match / retire
```

## Core pieces

1. **A procedure has four parts**: `goal`, `preconditions`, `steps` (each with a
   `do`, a **`check`**, and `on_fail`), and `anti_preconditions`. The `check`
   field is what makes it safe — a playbook of `do` steps with no verification
   is a macro that executes into a broken state. A step with no expressible
   check needs judgement and must say so (`check: null`).
2. **Promotion thresholds**: ≥3 episodes of the same task shape, ≥2 **verified**
   successes (not `claimed_success`), and consistent step structure. Give the
   induction prompt a `NO_COMMON_PROCEDURE` escape — three different routes to
   one goal averaged into one playbook is a procedure nobody has ever executed.
   Never induce from a single brilliant run; that promotes luck to policy.
3. **Trust** = `(successes + 1) / (uses + 2)`, with the evidence decayed by a
   90-day half-life so an untouched playbook re-earns its standing. Bands:
   ≥0.7 offered; 0.4–0.7 offered **with an explicit caveat**; <0.4 after 5+ uses
   retired. **Retire, never delete** — a retired playbook is the best evidence
   for why the current approach is what it is.
4. **Matching, cheapest first**: lexical/vector against `goal` → structured
   checks in code → one model call for prose conditions on the best survivor
   only. **Unknown counts as failed.** Not offering a playbook costs one ordinary
   run; offering the wrong one costs a confident wrong sequence.
5. **Advisory replay is the default.** Strict replay must raise if any step
   lacks a check, and must halt at the first failed check without continuing.
6. **Wire the failure path first.** If `record(..., succeeded=False)` is never
   called, every playbook drifts to trust 1.0 and nothing is ever retired.

## Conventions

- `induce` defaults to `--dry-run`; writing playbooks is a deliberate act.
- Print the skip list with reasons. Induction should refuse most clusters.
- Above ~30 playbooks, matching degrades — merge related ones behind branching
  steps rather than adding more.

## Verify before finishing

1. Install, import, `--help`, `list` on an empty store.
2. `pytest -q` — induction refuses below threshold and on unverified successes;
   `NO_COMMON_PROCEDURE` is honoured; anti-preconditions block; unknown
   preconditions fail; structured checks short-circuit without a model call;
   repeated failure retires but does not delete; trust decays with age; strict
   replay refuses unchecked steps and halts at the first failed check.
3. `induce --dry-run` over the real episode log. **The skip list should be
   longer than the induced list.**
4. Run `match` on one task that should fire and one that should not, and read
   the `why`. A playbook that matches everything is worse than none.

Report clusters seen, induced, refused (with reasons), and the trust
distribution. All playbooks at 0.5 means no outcome was ever recorded.
