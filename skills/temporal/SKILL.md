---
name: memory-temporal
description: "Build a bitemporal memory: facts with validity intervals and belief time, point-in-time queries, invalidation instead of deletion"
---

# Temporal Memory

Most memory systems have one implicit timestamp — *when we wrote the row* — and
answer exactly one question: what do we believe now. Temporal memory separates
two clocks and can answer two more:

- **Valid time** — when the fact was true *in the world*. Arttu lived in
  Helsinki from 2019 to 2024.
- **Belief time** — when *we knew it*. We learned about the Berlin move in
  September 2026, three months after it happened.

With both, "what did the agent believe in March, and was it right?" is a query
rather than an archaeology project. With only the write timestamp it is
unanswerable, and every agent incident review eventually needs the answer.

```
  valid time  ──────────────────────────────────────────────►
              │  Helsinki  ────────────────►│ Berlin ───────►
              2019                        2024            now
  belief time ──────────────────────────────────────────────►
              │  we believed "Helsinki" ───────────────►│ we learned "Berlin"
                                                      2026-09

  as_of(valid=2025-01, belief=2025-06) ──► "Helsinki"   (what we thought then)
  as_of(valid=2025-01, belief=now)     ──► "Berlin"     (what we now know was true then)
```

Those two queries returning different answers is the entire product. If they
never differ for your data, you do not need this skill.

## Use this when

- You must explain a past decision: "why did the agent say that in March?"
  Audit, incident review, compliance, or debugging a production agent.
- Facts have genuine validity periods — employment, pricing, configuration,
  ownership, versions — and questions are asked about past states.
- Corrections arrive late and must not rewrite history. Learning in September
  that something changed in June is the normal case, and an ordinary store
  handles it by silently destroying the record of what you believed in July.

**Do not use this when** you can just overwrite. Bitemporal modelling doubles
the size of every query in your head and every index in the schema, and most
systems genuinely only need "what is true now". `memory-semantic`'s
supersession — one `valid_to` column and a lineage pointer — is the 80% version
and is the right answer far more often than this skill is.

The honest test: write down two questions your system must answer that differ
only in belief time. If you cannot, use supersession.

## Workflow

1. **Ask which clocks matter.** Do you need valid time, belief time, or both?
   Many systems need valid time only (facts with periods, no need to reconstruct
   past beliefs) — that is *uni*-temporal and is half the complexity. Confirm
   before building both.

2. **Ask where valid time comes from.** Explicit dates in the source? Extracted
   from prose ("since last March")? Assumed to be the write time? This decides
   how much of the extractor you need.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

4. **Write the five modules**: `interval.py` (the time algebra), `fact.py`
   (bitemporal record), `store.py` (the schema and the three query forms),
   `assert_.py` (the write path that closes intervals), `resolve.py` (overlap
   handling). Then `cli.py`.

5. **Verify against the contract** in `references/evaluation.md`, plus the
   temporal-specific tests below.

## The decisions that matter

### 1. Half-open intervals, always

`[valid_from, valid_to)` — inclusive start, exclusive end. Not negotiable, and
the source of most bugs when violated.

With closed intervals, a fact ending on the 5th and its successor starting on
the 5th both match a query for the 5th, and you get two contradictory answers
for one instant. With half-open, adjacent intervals tile the timeline exactly
once, and `valid_from == valid_to` is a clean empty interval rather than a
one-instant fact.

Use `None` for "still true" rather than a sentinel like `9999-12-31`. The
sentinel invites arithmetic on a date that does not exist and sorts correctly
only by accident.

```python
def contains(interval, t) -> bool:
    start, end = interval
    return start <= t and (end is None or t < end)
```

### 2. Belief time is set by the system, valid time by the world

A confusion worth stating explicitly because it causes real bugs:

- **Belief time (`asserted_at`) is never supplied by a caller.** It is the
  system clock at write. Letting callers set it destroys the audit property —
  the whole point is that you cannot retroactively change what you believed.
- **Valid time is data.** It comes from the source and can be in the past, the
  future, or unknown.

When valid time is unknown, **do not default it to now**. Store `valid_from =
None` and mark it `inferred`. A fact silently stamped with the write date
becomes a false claim about when something started, and it is indistinguishable
from a real one later.

### 3. Correction versus change — the distinction the schema must carry

Two things look identical in a naive store and mean opposite things:

| | What happened | What to do |
| --- | --- | --- |
| **Change** | The world changed. Helsinki → Berlin in 2024. | Close the old interval at the change date, open a new one. Both were true. |
| **Correction** | We were wrong. It was never Helsinki. | Mark the old assertion `retracted` in belief time. It was never true. |

A change writes `valid_to` on the old row. A correction writes `retracted_at`
and leaves `valid_to` alone. Get this wrong and "what was true in 2020" returns
a fact you know to be false, or "what did we believe in 2020" loses the belief
you actually held.

Both are non-destructive. **Nothing in this skill deletes.**

### 4. The three query forms

```python
current(key)                       # valid now, believed now - the common case
as_of(key, valid_at)               # what was true then, as best we know today
as_believed(key, valid_at, belief_at)   # what we thought then about then
```

The third is the expensive one and the reason the skill exists. Implement all
three; make `current()` the fast path with its own index, because it is 95% of
traffic and should not pay for the other 5%.

```sql
CREATE INDEX facts_current ON facts(scope, subject, predicate)
       WHERE valid_to IS NULL AND retracted_at IS NULL;
```

A partial index on the current slice keeps the common query cheap no matter how
much history accumulates.

### 5. Overlap policy, decided once

Two assertions on the same `(subject, predicate)` with overlapping valid
intervals. Three options, and you must pick one and enforce it in code:

- **Last-write-wins on overlap** — truncate the earlier interval to start where
  the later one begins. Simple, and correct when a later assertion means "and
  this is when it changed".
- **Reject the overlap** — refuse the write, surface the conflict. Correct when
  both sources are authoritative and a human must decide.
- **Allow overlap** — store both, return both, let the caller reconcile.
  Correct only when the predicate is genuinely multi-valued (`works_on` can have
  three simultaneous answers; `lives_in` cannot).

Mark predicates as single- or multi-valued in a small table. Applying
last-write-wins to a multi-valued predicate silently deletes true facts — the
second project someone works on truncates the first.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # predicate arity, overlap policy, db path
    ├── interval.py      # half-open interval algebra
    ├── fact.py          # the bitemporal record
    ├── store.py         # schema + current / as_of / as_believed
    ├── assert_.py       # the write path: close, open, retract
    ├── extract.py       # prose -> valid intervals (optional)
    └── cli.py           # assert / correct / current / as-of / history / timeline
```

### `interval.py`

```python
"""Half-open [start, end) algebra over epoch seconds. None = unbounded."""

from __future__ import annotations

Instant = float | None


def contains(start: Instant, end: Instant, t: float) -> bool:
    if start is not None and t < start:
        return False
    return end is None or t < end


def overlaps(a_start: Instant, a_end: Instant, b_start: Instant, b_end: Instant) -> bool:
    if a_end is not None and b_start is not None and a_end <= b_start:
        return False
    if b_end is not None and a_start is not None and b_end <= a_start:
        return False
    return True


def truncate(start: Instant, end: Instant, at: float) -> tuple[Instant, Instant]:
    """Close an interval at `at`. Returns an empty interval if `at` precedes it."""
    if start is not None and at <= start:
        return (start, start)                      # empty: start == end
    return (start, at if end is None else min(end, at))


def is_empty(start: Instant, end: Instant) -> bool:
    return start is not None and end is not None and start >= end
```

### `fact.py`

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


@dataclass
class TemporalFact:
    subject: str
    predicate: str
    object: str
    text: str = ""                       # the human-readable form the model reads

    # Valid time: when this was true in the world. Data.
    valid_from: float | None = None      # None = unknown, NOT "now"
    valid_to: float | None = None        # None = still true

    # Belief time: when we knew it. System-controlled, never caller-supplied.
    asserted_at: float = field(default_factory=time.time)
    retracted_at: float | None = None    # set when we learn it was never true

    scope: str = "default"
    source: str = "user"
    confidence: float = 0.9
    valid_from_inferred: bool = False    # True when valid_from was not in the source
    supersedes: str | None = None
    id: str = ""

    def __post_init__(self):
        if not self.id:
            payload = (f"{self.scope}\x1f{self.subject}\x1f{self.predicate}\x1f"
                       f"{self.object}\x1f{self.asserted_at}").encode()
            self.id = "tf_" + hashlib.blake2b(payload, digest_size=8).hexdigest()
        if not self.text:
            self.text = f"{self.subject} {self.predicate.replace('_', ' ')} {self.object}"

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scope, self.subject, self.predicate)
```

### `store.py`

```python
"""The schema, and the three query forms. Nothing here deletes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .fact import TemporalFact
from .interval import contains

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id                  TEXT PRIMARY KEY,
    scope               TEXT NOT NULL,
    subject             TEXT NOT NULL,
    predicate           TEXT NOT NULL,
    object              TEXT NOT NULL,
    text                TEXT NOT NULL,
    valid_from          REAL,
    valid_to            REAL,
    asserted_at         REAL NOT NULL,
    retracted_at        REAL,
    source              TEXT NOT NULL DEFAULT 'user',
    confidence          REAL NOT NULL DEFAULT 0.9,
    valid_from_inferred INTEGER NOT NULL DEFAULT 0,
    supersedes          TEXT
);
CREATE INDEX IF NOT EXISTS facts_key ON facts(scope, subject, predicate);
CREATE INDEX IF NOT EXISTS facts_current ON facts(scope, subject, predicate)
       WHERE valid_to IS NULL AND retracted_at IS NULL;
CREATE INDEX IF NOT EXISTS facts_belief ON facts(scope, asserted_at);

CREATE TABLE IF NOT EXISTS predicate_arity (
    predicate TEXT PRIMARY KEY,
    multi     INTEGER NOT NULL DEFAULT 0
);
"""


class TemporalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def put(self, fact: TemporalFact) -> str:
        self.db.execute(
            "INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fact.id, fact.scope, fact.subject, fact.predicate, fact.object, fact.text,
             fact.valid_from, fact.valid_to, fact.asserted_at, fact.retracted_at,
             fact.source, fact.confidence, int(fact.valid_from_inferred), fact.supersedes),
        )
        self.db.commit()
        return fact.id

    def is_multi_valued(self, predicate: str) -> bool:
        row = self.db.execute(
            "SELECT multi FROM predicate_arity WHERE predicate=?", (predicate,)
        ).fetchone()
        return bool(row["multi"]) if row else False

    # --- the three query forms -------------------------------------------

    def current(self, subject: str, predicate: str | None = None, *, scope: str = "default"):
        """Valid now, believed now. The fast path."""
        sql = ("SELECT * FROM facts WHERE scope=? AND subject=? "
               "AND valid_to IS NULL AND retracted_at IS NULL")
        args: list = [scope, subject]
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        return [self._hydrate(r) for r in self.db.execute(sql, args)]

    def as_of(self, subject: str, valid_at: float, predicate: str | None = None,
              *, scope: str = "default"):
        """What was true at `valid_at`, as best we know today."""
        rows = self._by_key(scope, subject, predicate)
        return [f for f in rows
                if f.retracted_at is None and contains(f.valid_from, f.valid_to, valid_at)]

    def as_believed(self, subject: str, valid_at: float, belief_at: float,
                    predicate: str | None = None, *, scope: str = "default"):
        """What we believed at `belief_at` about `valid_at`. The reason this store exists."""
        rows = self._by_key(scope, subject, predicate)
        return [
            f for f in rows
            if f.asserted_at <= belief_at                                  # we knew it by then
            and (f.retracted_at is None or f.retracted_at > belief_at)     # not yet retracted
            and contains(f.valid_from, f.valid_to, valid_at)
        ]

    def history(self, subject: str, predicate: str, *, scope: str = "default"):
        """Every assertion ever made on this key, in belief order."""
        return sorted(self._by_key(scope, subject, predicate), key=lambda f: f.asserted_at)

    def _by_key(self, scope, subject, predicate):
        sql = "SELECT * FROM facts WHERE scope=? AND subject=?"
        args: list = [scope, subject]
        if predicate:
            sql += " AND predicate=?"
            args.append(predicate)
        return [self._hydrate(r) for r in self.db.execute(sql, args)]

    def _hydrate(self, row) -> TemporalFact:
        return TemporalFact(
            id=row["id"], scope=row["scope"], subject=row["subject"],
            predicate=row["predicate"], object=row["object"], text=row["text"],
            valid_from=row["valid_from"], valid_to=row["valid_to"],
            asserted_at=row["asserted_at"], retracted_at=row["retracted_at"],
            source=row["source"], confidence=row["confidence"],
            valid_from_inferred=bool(row["valid_from_inferred"]),
            supersedes=row["supersedes"],
        )
```

Note what `as_believed` does *not* do: it never looks at `valid_to` set by a
later correction, because `valid_to` is part of the row, not a separate belief
event. That is the one simplification in this implementation. If you need
"we believed in March that it ended in June, then in July we learned it ended in
May", you need a full six-column bitemporal model with belief intervals on every
attribute — which is genuinely what banks do, and almost certainly more than an
agent needs. Say so in the README rather than half-implementing it.

### `assert_.py`

```python
"""The write path. Three operations: assert, change, correct. None delete."""

from __future__ import annotations

import time

from .fact import TemporalFact
from .interval import is_empty, overlaps, truncate


class Asserter:
    def __init__(self, store, *, overlap_policy: str = "last_write_wins"):
        assert overlap_policy in ("last_write_wins", "reject", "allow")
        self.store, self.policy = store, overlap_policy

    def assert_fact(self, fact: TemporalFact) -> TemporalFact:
        """Belief time is ours. Never accept it from the caller."""
        fact.asserted_at = time.time()

        if self.store.is_multi_valued(fact.predicate):
            self.store.put(fact)                 # multi-valued: overlap is normal
            return fact

        existing = [
            f for f in self.store._by_key(fact.scope, fact.subject, fact.predicate)
            if f.retracted_at is None
            and overlaps(f.valid_from, f.valid_to, fact.valid_from, fact.valid_to)
            and f.object != fact.object
        ]

        if existing and self.policy == "reject":
            raise ValueError(
                f"{fact.subject}.{fact.predicate} already has an overlapping value: "
                f"{[f.object for f in existing]}. Resolve it explicitly."
            )

        if existing and self.policy == "last_write_wins":
            boundary = fact.valid_from if fact.valid_from is not None else fact.asserted_at
            for old in existing:
                start, end = truncate(old.valid_from, old.valid_to, boundary)
                if is_empty(start, end):
                    # The new fact starts at or before the old one: the old was never
                    # true as recorded. That is a correction, not a change.
                    old.retracted_at = fact.asserted_at
                else:
                    old.valid_to = end
                self.store.put(old)
            fact.supersedes = existing[0].id

        self.store.put(fact)
        return fact

    def correct(self, fact_id: str, *, reason: str = "") -> TemporalFact:
        """We were wrong: this was never true. Belief-time retraction, nothing deleted."""
        fact = next(f for f in self.store._by_key_any(fact_id))
        fact.retracted_at = time.time()
        fact.text = f"{fact.text}  [retracted: {reason}]" if reason else fact.text
        self.store.put(fact)
        return fact
```

The `is_empty` branch is the subtle one and worth reading twice: when a new
assertion starts before the old one did, the old interval truncates to nothing,
which means the old fact was never true as recorded. That is a correction
discovered through a change, and treating it as a change would leave a
zero-length interval in the store that matches no query and confuses every
later reader.

### `cli.py`

- `<command> assert "<subject> <predicate> <object>" [--from 2024-06-01] [--to ...]`
- `<command> correct <id> --reason "..."`
- `<command> current <subject>`
- `<command> as-of <subject> --at 2025-01-01 [--believed-at 2025-06-01]` — the
  command that demonstrates the whole point. Print both answers side by side
  when they differ.
- `<command> history <subject> <predicate>` — every assertion in belief order,
  with a timeline. This is the audit view.

Accept dates as ISO strings and print them as ISO strings. Epoch seconds in a
CLI make temporal bugs impossible to see.

## Failure modes

- **Closed intervals.** Two facts match one instant, queries return
  contradictions at every boundary, and the bug only appears on the exact dates
  in your fixtures. Half-open, everywhere, tested at the boundary.

- **Valid time defaulted to now.** A fact with no date in the source, stamped
  with the write time, becomes a false claim about when something started — and
  is indistinguishable from a real one afterwards. `valid_from = None` plus the
  `inferred` flag.

- **Caller-supplied belief time.** Someone adds `asserted_at` to the API "for
  backfill", and the audit property is gone: history can now be rewritten. If
  you genuinely must backfill, use a separate, clearly-named ingestion path that
  records the real write time in a second column.

- **Corrections modelled as changes.** "It was never Helsinki" recorded by
  closing the Helsinki interval leaves a period where the store still asserts
  Helsinki was true. `as_of` then returns a fact you know to be false.

- **Last-write-wins on a multi-valued predicate.** Someone works on two
  projects; the second assertion truncates the first. Silent data loss, and the
  hardest failure here to notice. Declare arity per predicate before the first
  write.

- **Retracted facts leaking into recall.** Every query path must filter
  `retracted_at`. Miss it in one code path and the agent asserts something you
  explicitly marked false. Test each of the three query forms separately.

- **The store never shrinks.** Bitemporal stores grow monotonically by design.
  That is correct, but it needs a stated retention policy for the history slice
  — archive assertions older than N years, keep the current slice forever — and
  a scheduled job that actually runs it.

## Required tests

All offline. See `references/evaluation.md` for the universal set; these are
mandatory:

```python
def test_as_of_and_as_believed_diverge(store, asserter):
    helsinki = TemporalFact(subject="arttu", predicate="lives_in", object="Helsinki",
                            valid_from=iso("2019-01-01"))
    asserter.assert_fact(helsinki)
    freeze(iso("2026-09-01"))
    asserter.assert_fact(TemporalFact(subject="arttu", predicate="lives_in",
                                      object="Berlin", valid_from=iso("2024-06-01")))
    assert store.as_of("arttu", iso("2025-01-01"))[0].object == "Berlin"
    assert store.as_believed("arttu", iso("2025-01-01"), iso("2025-06-01"))[0].object == "Helsinki"


def test_half_open_boundary_returns_exactly_one(store):
    put(store, "a", "p", "x", valid_from=iso("2020-01-01"), valid_to=iso("2021-01-01"))
    put(store, "a", "p", "y", valid_from=iso("2021-01-01"))
    assert [f.object for f in store.as_of("a", iso("2021-01-01"))] == ["y"]


def test_unknown_valid_from_is_none_not_now(extractor):
    fact = extractor.parse("arttu uses postgres")
    assert fact.valid_from is None and fact.valid_from_inferred is True


def test_belief_time_is_not_caller_supplied(asserter):
    fact = TemporalFact(subject="a", predicate="p", object="x", asserted_at=0.0)
    stored = asserter.assert_fact(fact)
    assert stored.asserted_at > 1_700_000_000


def test_correction_retracts_without_closing_validity(store, asserter):
    fact = asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="wrong",
                                             valid_from=iso("2020-01-01")))
    asserter.correct(fact.id, reason="never true")
    assert store.get(fact.id).retracted_at is not None
    assert store.get(fact.id).valid_to is None
    assert store.as_of("a", iso("2021-01-01")) == []


def test_change_closes_the_old_interval(store, asserter):
    asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="old",
                                      valid_from=iso("2020-01-01")))
    asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="new",
                                      valid_from=iso("2022-01-01")))
    old = [f for f in store.history("a", "p") if f.object == "old"][0]
    assert old.valid_to == iso("2022-01-01")
    assert old.retracted_at is None


def test_multi_valued_predicate_keeps_both(store, asserter):
    store.db.execute("INSERT INTO predicate_arity VALUES ('works_on', 1)")
    asserter.assert_fact(TemporalFact(subject="a", predicate="works_on", object="herdr"))
    asserter.assert_fact(TemporalFact(subject="a", predicate="works_on", object="gateway"))
    assert len(store.current("a", "works_on")) == 2


def test_reject_policy_surfaces_the_conflict(store):
    asserter = Asserter(store, overlap_policy="reject")
    asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="x"))
    with pytest.raises(ValueError, match="overlapping value"):
        asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="y"))


def test_retracted_facts_are_excluded_from_all_three_queries(store, asserter):
    fact = asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="x",
                                             valid_from=iso("2020-01-01")))
    asserter.correct(fact.id)
    assert store.current("a", "p") == []
    assert store.as_of("a", iso("2021-01-01")) == []
    assert store.as_believed("a", iso("2021-01-01"), time.time()) == []


def test_nothing_is_ever_deleted(store, asserter):
    fact = asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="x"))
    asserter.assert_fact(TemporalFact(subject="a", predicate="p", object="y"))
    asserter.correct(fact.id)
    assert len(store.history("a", "p")) == 2
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, then assert a fact and read it back
   with `current`.
2. **Tier 1** — `pytest -q`, offline. Every test above, especially the boundary
   test — off-by-one at an interval edge is the defining bug of this skill.
3. **Tier 2** — a probe suite where at least five probes have *different*
   answers for `as_of` and `as_believed`. If none do, the bitemporal model is
   not earning its complexity, and the honest recommendation is `memory-semantic`
   supersession instead. Report that finding rather than shipping the complexity.
4. **Tier 3** — replay a real sequence of assertions and corrections, then run
   `history` and read the timeline. Every row should be explicable: what we
   learned, when, and what it changed.

Then tell the user what actually ran, including whether the `as_of` /
`as_believed` divergence probes actually diverged. That number is the honest
answer to "did we need this".
