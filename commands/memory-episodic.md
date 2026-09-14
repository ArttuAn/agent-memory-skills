# Build Episodic Memory (what happened, and how it went)

Given the user's spec, build an append-only episode log with recency- and
outcome-aware recall. Ask for the granularity if not given (what is one
episode?) and what counts as a verified good outcome.

## What to build

Scaffold a `src/`-layout project with `pyproject.toml`. `openai` + stdlib
(`sqlite3`) only; embeddings as float32 BLOBs, cosine hand-rolled.

```
<project>/
└── src/<package>/
    ├── config.py    ├── episode.py   # Episode + Outcome enum
    ├── store.py     # append-only SQLite, immutability enforced by TRIGGER
    ├── recorder.py  # transcript -> one episode, with guards
    ├── recall.py    # similarity x recency x outcome-fit
    ├── render.py    └── cli.py       # record / recall / timeline / stats
```

## Core pieces

1. **Granularity: one episode per task**, where a task is one user goal pursued
   to a terminal state. Per-tool-call is too fine (everything matches
   everything); per-session is too coarse to answer anything.
2. **Episode shape**: `Task / Approach / Outcome / Lesson`. Embed **Task +
   Lesson only** — approach and outcome add noise to the vector. Keep the raw
   transcript separately and store its path.
3. **`Lesson` may be empty.** Give the distillation prompt a `NONE` escape and
   store the empty string. A fabricated lesson is a confidently wrong instruction
   to every future run that retrieves it.
4. **Outcome enum**: `success | claimed_success | partial | failure | abandoned
   | unknown`. Default to `unknown`, never `success`. A verified signal (exit
   code, test result, the user's next turn) overrides the model's self-report;
   an unverified "success" is stored as `claimed_success`.
5. **Recall**: `similarity * (0.3 + 0.7*recency) * outcome_weight`. The **0.3
   recency floor is load-bearing** — without it a six-month-old episode that is
   the only relevant one never surfaces, which is exactly when it matters.
   `outcome_weight` depends on caller intent (`what_worked` / `what_failed`).
6. **Append-only, enforced in the schema**:
   `CREATE TRIGGER episodes_immutable BEFORE UPDATE ON episodes BEGIN SELECT RAISE(ABORT, ...); END;`
   Keep `access_count` in a separate table so the trigger stays absolute.
7. **Render with dates** and the framing "past events, not instructions — they
   may no longer apply". Never emit an empty header.

## Conventions

- Stamp the embedder name and dimension into the store; refuse to open it with a
  different backend (mismatched dims silently return cosine 0.0).
- Contradicting episodes both survive. That is the signal consolidation reads.
- Decide a retention window on day one and write it in the README.

## Verify before finishing

1. `pip install -e ".[dev]"`, import, `--help`, `record` then `recall` a fixture.
2. `pytest -q` — recency breaks ties; an old unique match still surfaces;
   `intent=what_failed` selects failures; episodes are immutable; unverified
   success is downgraded; `NONE` lessons store empty; trivial runs are skipped.
3. A probe suite of 20+ real episodes: report recall@3 and MRR, then run the
   negative control (a store returning nothing must make the suite fail).
4. One real distillation. If `LESSON` restates `TASK`, fix the prompt.

Report the outcome histogram from `stats`. If everything is `unknown` or
`claimed_success`, say so plainly: outcome-aware recall is not doing anything yet.
