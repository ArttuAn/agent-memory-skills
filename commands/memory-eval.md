# Build a Memory Eval Harness (probes, metrics, controls)

Given the user's spec, build the instrument that proves a memory system works:
a probe suite, five metrics, negative controls, longitudinal drift, and a CI
regression gate.

**Collect real probes, not generated ones** — generated probes test the
generator. Ask for 20-30 things the agent should remember and be asked about,
from actual usage, and for each: which specific record must be retrieved. Ask
whether this is a standalone project or an `evals/` package inside an existing one.

## What to build

```
<project>/
├── probes/core.yaml      # hand-written, human-editable
├── probes/negative.yaml  # probes where returning nothing is correct
└── src/<package>/
    ├── probe.py    ├── stores.py   # NullStore, RandomStore, ShuffledStore
    ├── metrics.py  ├── runner.py   ├── simulate.py
    ├── report.py   └── cli.py      # run / control / simulate / compare / add
```

## Core pieces

1. **A probe is a fixture, not a question**: `given` (records written into a
   CLEAN store), `query`, `expect` (ids that must appear in top k), `forbid` (ids
   that must not — stale, superseded, other-scope), `k`, `tags`. Grading the
   agent's *answer* measures the model and the memory together with no way to
   attribute a failure. **Rebuild the store per probe** so results do not depend
   on what a previous probe left behind.
2. **`forbid` is the half people omit and the half that catches serious bugs.**
   Cross-scope leaks and stale facts are invisible to recall@k.
3. **Five metrics, one of which is a gate**: recall@k (>0.85), MRR (>0.7),
   distinct@k (>0.8, catches near-duplicate pile-up that recall@k cannot see),
   staleness (<0.05), and **leak rate — 0, a gate, not a threshold**.
4. **The negative control is the most important test.** Three broken stores,
   each catching a different flavour of worthless suite: `NullStore` (recall must
   collapse), `RandomStore` (recall must collapse), `ShuffledStore` (recall@k
   holds but **MRR must drop** — if it does not, k is so large that ranking is
   not being measured). A suite that survives any of these is measuring nothing.
5. **Negative probes**: at least 15% should expect *nothing*. Without them you
   tune toward a system that retrieves on every turn, injecting noise into turns
   that needed none.
6. **Longitudinal, not point-in-time.** Write 200 seeded synthetic sessions and
   plot recall@5 over store size (must be flat), store size over sessions
   (flattening means consolidation works), and p95 latency over store size.
7. **Commit `eval-results.json` and fail CI on a regression** beyond ~0.03, or on
   any leak. Keep the **per-probe** breakdown: "recall fell 0.88 → 0.84" is not
   actionable; "these three probes started failing" is.
8. **Keep a 20% holdout** you only look at when you think you are done —
   otherwise the suite becomes a training set.

## CI order

```yaml
- run: python -m <package>.cli control    # validate the suite FIRST
- run: python -m <package>.cli run --out eval-results.json
- run: python -m <package>.cli compare eval-results.baseline.json
```

`control` first is deliberate: a suite that has rotted into uselessness would
otherwise pass every subsequent step.

## Verify before finishing

1. Install, import, `--help`, `run` against the bundled example probes.
2. `pytest -q` — null and random stores fail the suite; the shuffled store keeps
   recall but drops MRR; a leak fails the run regardless of recall; negative
   probes score correctly on an empty result; probes do not share state;
   duplicate probe names are rejected; distinct@k penalizes near-duplicates; the
   regression check fails on a drop and on any leak, and names newly failing probes.
3. `control` against the user's real memory system. **If the controls do not
   fail, stop: report that the suite is invalid and present no other numbers
   from it.** A number from an invalid suite is worse than none — it gets believed.
4. `simulate --sessions 200` and read the three curves. Report the recall slope
   explicitly: flat is the pass condition.

Lead the report with the control result — every other number depends on it.
