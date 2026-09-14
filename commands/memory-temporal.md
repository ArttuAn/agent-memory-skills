# Build Temporal Memory (bitemporal facts)

Given the user's spec, build a bitemporal fact store: validity intervals, belief
time, point-in-time queries, and invalidation instead of deletion.

**Ask which clocks matter.** Many systems need valid time only (uni-temporal) —
that is half the complexity. Confirm before building both. Also ask where valid
time comes from: explicit dates, extracted from prose, or unknown.

**The honest test before building**: can the user name two questions that differ
only in belief time? If not, recommend `memory-semantic` supersession instead —
one `valid_to` column and a lineage pointer is the 80% version and is right far
more often.

## What to build

```
<project>/
└── src/<package>/
    ├── interval.py  # half-open [start, end) algebra
    ├── fact.py      # TemporalFact: valid_from/to + asserted_at/retracted_at
    ├── store.py     # current() / as_of() / as_believed() / history()
    ├── assert_.py   # the write path: assert, change, correct
    └── cli.py       # assert / correct / current / as-of / history
```

## Core pieces

1. **Half-open intervals `[valid_from, valid_to)`, always.** With closed
   intervals a fact ending on the 5th and its successor starting on the 5th both
   match a query for the 5th. Use `None` for "still true", never a `9999` sentinel.
2. **Belief time is system-controlled; valid time is data.** Never accept
   `asserted_at` from a caller — that destroys the audit property. When valid
   time is unknown, store `valid_from = None` with an `inferred` flag; **do not
   default it to now**, which manufactures a false claim about when something started.
3. **Distinguish change from correction.** A change (the world moved) closes the
   old interval and opens a new one — both were true. A correction (we were
   wrong) sets `retracted_at` and leaves `valid_to` alone — it was never true.
   Getting this wrong makes `as_of` return facts you know to be false.
4. **Three query forms**: `current()` (fast path, with a partial index on the
   current slice), `as_of(valid_at)`, and `as_believed(valid_at, belief_at)`.
   The third is why the skill exists. Every one must filter `retracted_at`.
5. **Declare predicate arity before the first write.** Last-write-wins on a
   multi-valued predicate (`works_on`) silently truncates true facts. Pick one
   overlap policy — `last_write_wins`, `reject`, or `allow` — and enforce it.
6. **Nothing deletes.** The store grows monotonically by design; state a
   retention policy for the history slice and schedule it.
7. **CLI takes and prints ISO dates.** Epoch seconds make temporal bugs invisible.
   Print `as_of` and `as_believed` side by side when they differ — that is the demo.

## Verify before finishing

1. Install, import, `--help`, assert a fact and read it back with `current`.
2. `pytest -q` — `as_of` and `as_believed` diverge; the half-open boundary
   returns exactly one; unknown `valid_from` is `None` not now; caller-supplied
   belief time is overridden; a correction retracts without closing validity; a
   change closes the old interval without retracting; multi-valued predicates
   keep both; retracted facts are excluded from **all three** query forms;
   nothing is ever deleted. The boundary test is the defining bug of this skill.
3. A probe suite where 5+ probes have different `as_of` / `as_believed` answers.
   **If none do, report that the bitemporal model is not earning its complexity**
   and recommend supersession instead.
4. Replay a real sequence of assertions and corrections, then read `history`.

Report whether the divergence probes actually diverged. That number is the
honest answer to "did we need this".
