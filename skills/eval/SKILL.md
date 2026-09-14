---
name: memory-eval
description: "Build the probe harness that proves a memory system works: recall@k, staleness, leak gates, negative controls, and longitudinal drift"
---

# Memory Evaluation

A memory system fails quietly. It returns five records for every query,
plausible-looking, and the model writes a confident answer around whichever ones
it got. Nothing throws, nothing logs, and the first signal is a user saying "it
used to be better at this."

This skill builds the instrument that makes those failures visible: a probe
suite, five metrics, a negative control, and a longitudinal simulation that
catches the decline no single-point measurement can.

It is the only skill in this repo that is never the wrong choice, and the one
people skip.

```
  probes ──► write `given` into a clean store ──► run the query
                                                      │
                                                      ▼
                             compare against `expect` and `forbid`
                                                      │
   ┌──────────────────────────────────────────────────┤
   ▼                    ▼                  ▼          ▼
 recall@k             MRR            staleness    LEAK GATE
   │                                                  │
   └──► negative control: does the suite FAIL         └── any leak = fail,
        against a store that recalls nothing?             not a threshold
```

## Use this when

- You have built any of the other skills in this repo. All of them end with
  "report recall@k on a probe suite", and this is the thing that produces it.
- You are about to tune a ranker, change a chunking strategy, or add a retrieval
  stage — none of which can be justified by the one query you happened to try.
- Recall feels like it is degrading as the store grows, and you need to know
  whether that is real.

There is no "do not use this when". The only bad version is a suite so thin it
measures nothing, and the negative control below is how you find out whether
yours is.

## Workflow

1. **Collect real probes.** Not generated ones — generated probes test the
   generator. Ask the user for 20-30 things their agent should remember and be
   asked about, from actual usage. If they have a transcript log, mine it.

2. **Ask what "correct" means.** For each probe: which specific record must be
   retrieved? Probes with fuzzy expectations produce fuzzy metrics.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.
   This one can also be dropped into an existing memory project as a `evals/`
   package — ask which the user wants.

4. **Write the five modules**: `probe.py` (the fixture), `metrics.py`,
   `runner.py`, `simulate.py` (longitudinal), `report.py`. Then `cli.py` and a
   CI workflow.

5. **Run it against a deliberately broken store first.** If the suite passes,
   the suite is the thing that is broken.

## The decisions that matter

### 1. A probe is a fixture, not a question

The mistake is writing probes as questions and grading the agent's *answer*.
That measures the model and the memory system together, with no way to attribute
a failure to either.

A probe fixes the store contents, runs the retrieval, and grades the retrieval:

```python
@dataclass
class Probe:
    name: str
    given: list[Record]       # written into a CLEAN store before the query
    query: str
    expect: list[str]         # record ids that MUST appear in the top k
    forbid: list[str] = ()    # ids that must NOT appear: stale, other-scope, superseded
    k: int = 5
    scope: str = "default"
    tags: list[str] = ()      # "multi-hop", "temporal", "negative", "paraphrase"
```

`given` is what makes probes reproducible. The store is rebuilt from scratch for
each probe (or each probe group), so results do not depend on the order tests
ran in or on what a previous probe left behind.

**`forbid` is the half people omit and the half that catches the serious bugs.**
Cross-scope leaks and superseded facts are both silent, and neither shows up in
recall@k — a suite with no `forbid` entries cannot see either.

### 2. Five metrics, and one of them is a gate

| Metric | Definition | Target |
| --- | --- | --- |
| **recall@k** | Share of probes where every `expect` id is in the top k | > 0.85 at k=5 |
| **MRR** | Mean reciprocal rank of the first expected hit | > 0.7 |
| **distinct@k** | Distinct information units in the k slots (duplicates collapse) | > 0.8 |
| **staleness** | Share of returned records superseded by a newer one | < 0.05 |
| **leak rate** | Any `forbid` id returned | **0 — a gate, not a threshold** |

The distinction in that last row matters. Four of these are numbers you trade
off against each other. The leak rate is not tunable: a single cross-scope hit
is a failed run, regardless of how good recall was.

`distinct@k` is the one that catches near-duplicate pile-up — five phrasings of
one fact filling all five slots while the second fact the answer needed never
surfaces. Recall@k can look fine while this is happening, because the one fact
you probed for *is* there.

### 3. The negative control is the most important test

```python
def test_suite_detects_a_broken_store():
    """If this passes, the probes are being answered by the model's priors."""
    results = run(PROBES, store=NullStore())     # returns [] for every query
    assert results.recall_at_k < 0.2
```

Run it with three broken stores, because each catches a different flavour of
worthless suite:

- **`NullStore`** — returns nothing. Recall must collapse.
- **`RandomStore`** — returns k random records. Recall must collapse.
- **`ShuffledStore`** — returns the right records in scrambled order. Recall@k
  holds (correctly — they are in the top k) but **MRR must drop**. If it does
  not, your probes have `k` so large that ranking is not being measured at all.

A suite that survives any of these is measuring nothing, and every number it
produces afterwards is noise.

### 4. Longitudinal, not point-in-time

Most memory failures are gradual: recall holds at 100 records and degrades at
10,000 as duplicates accumulate and decay misfires. A single run cannot see it.

```python
for session in range(200):
    write_synthetic_session(store, session)
    if session % 20 == 0:
        record(session, run(PROBES, store))
```

Plot three curves and read them together:

- **recall@5 over store size** — should be flat. A downward slope means the
  ranker is being outvoted by volume; you need MMR, decay, or consolidation.
- **store size over sessions** — linear early is fine. Still linear after
  consolidation runs means consolidation is not working.
- **p95 query latency over store size** — the point where a brute-force scan
  stops being acceptable, which is the only honest signal that it is time for a
  real index.

Seed the synthetic sessions deterministically. A drift measurement you cannot
reproduce is an anecdote.

### 5. Results are a file in the repo, and CI fails on a regression

```json
{"commit": "a3f91c2", "at": "2026-09-14T10:22:00Z", "probes": 34,
 "recall_at_5": 0.88, "mrr": 0.74, "distinct_at_5": 0.83,
 "staleness": 0.02, "leaks": 0, "p95_ms": 41}
```

Commit `eval-results.json`. Fail CI when recall drops by more than a tolerance
(0.03 is reasonable) or when `leaks > 0`, exactly as you would for a performance
budget.

Without this, the numbers get produced, admired once, and never compared. The
diff across commits is the entire value; a single run tells you almost nothing.

Keep a per-probe breakdown too, not just aggregates. "recall fell from 0.88 to
0.84" is not actionable; "these three probes started failing" is.

## Build it

```
<project>/
├── pyproject.toml
├── README.md
├── probes/
│   ├── core.yaml          # the hand-written probes, in a format humans can edit
│   └── negative.yaml      # probes where returning nothing is the right answer
└── src/<package>/
    ├── __init__.py
    ├── probe.py           # Probe + loading from yaml
    ├── stores.py          # NullStore, RandomStore, ShuffledStore - the controls
    ├── metrics.py         # the five metrics
    ├── runner.py          # rebuild store, run probes, collect
    ├── simulate.py        # longitudinal drift
    ├── report.py          # json + a readable table + the regression check
    └── cli.py             # run / control / simulate / compare / add
```

### `probe.py`

```python
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Probe:
    name: str
    query: str
    given: list = field(default_factory=list)
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    k: int = 5
    scope: str = "default"
    tags: list[str] = field(default_factory=list)

    @property
    def is_negative(self) -> bool:
        """Retrieving nothing is the correct answer. These are easy to forget
        and they are what keep a system from retrieving on every single turn."""
        return not self.expect


def load(path: str | Path) -> list[Probe]:
    import yaml                                   # the one non-stdlib dep, for editability
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []
    probes = [Probe(**item) for item in raw]
    names = [p.name for p in probes]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"duplicate probe names: {sorted(duplicates)}")
    return probes
```

### `stores.py`

```python
"""The controls. A suite that passes against these is measuring nothing."""

from __future__ import annotations

import random


class NullStore:
    def put(self, record): return record.id
    def search(self, query, *, k=5, **kwargs): return []


class RandomStore:
    def __init__(self, seed: int = 0):
        self.records, self.rng = [], random.Random(seed)

    def put(self, record):
        self.records.append(record)
        return record.id

    def search(self, query, *, k=5, **kwargs):
        sample = self.rng.sample(self.records, min(k, len(self.records)))
        return [Hit(record=r, score=0.5, why="random control") for r in sample]


class ShuffledStore:
    """Wraps a real store and scrambles the order. Recall@k survives; MRR must not."""

    def __init__(self, inner, seed: int = 0):
        self.inner, self.rng = inner, random.Random(seed)

    def put(self, record):
        return self.inner.put(record)

    def search(self, query, *, k=5, **kwargs):
        hits = self.inner.search(query, k=k, **kwargs)
        self.rng.shuffle(hits)
        return hits
```

### `metrics.py`

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProbeResult:
    probe: object
    returned: list[str]
    hit_all: bool
    first_rank: int | None
    leaked: list[str]
    stale: list[str]
    distinct: float
    latency_ms: float


def evaluate(probe, hits, *, is_stale) -> ProbeResult:
    returned = [h.record.id for h in hits[: probe.k]]
    expected = set(probe.expect)

    hit_all = expected.issubset(set(returned)) if expected else not returned
    first_rank = next((i + 1 for i, rid in enumerate(returned) if rid in expected), None)
    leaked = [rid for rid in returned if rid in set(probe.forbid)]
    stale = [h.record.id for h in hits[: probe.k] if is_stale(h.record)]

    texts = {h.record.text.strip().lower() for h in hits[: probe.k]}
    distinct = len(texts) / len(returned) if returned else 1.0

    return ProbeResult(probe, returned, hit_all, first_rank, leaked, stale, distinct, 0.0)


@dataclass
class Summary:
    probes: int
    recall_at_k: float
    mrr: float
    distinct_at_k: float
    staleness: float
    leaks: int
    negative_correct: float          # share of negative probes that correctly returned nothing
    p95_ms: float

    @property
    def passed(self) -> bool:
        return self.leaks == 0       # the gate. Everything else is a threshold.


def summarize(results: list[ProbeResult]) -> Summary:
    total = len(results) or 1
    positives = [r for r in results if r.probe.expect]
    negatives = [r for r in results if not r.probe.expect]
    latencies = sorted(r.latency_ms for r in results)

    return Summary(
        probes=len(results),
        recall_at_k=sum(r.hit_all for r in positives) / (len(positives) or 1),
        mrr=sum(1.0 / r.first_rank for r in positives if r.first_rank) / (len(positives) or 1),
        distinct_at_k=sum(r.distinct for r in results) / total,
        staleness=sum(len(r.stale) for r in results) / max(1, sum(len(r.returned) for r in results)),
        leaks=sum(len(r.leaked) for r in results),
        negative_correct=sum(not r.returned for r in negatives) / (len(negatives) or 1),
        p95_ms=latencies[int(len(latencies) * 0.95)] if latencies else 0.0,
    )
```

### `runner.py`

```python
"""Rebuild the store per probe group, run, time, collect."""

from __future__ import annotations

import time

from .metrics import evaluate, summarize


def run(probes, *, make_store, is_stale=lambda r: False, retrieve=None):
    """`make_store` returns a FRESH store. `retrieve(store, probe) -> list[Hit]`."""
    retrieve = retrieve or (lambda store, probe: store.search(
        probe.query, k=probe.k, scope=probe.scope))

    results = []
    for probe in probes:
        store = make_store()                       # clean slate: probes cannot interfere
        for record in probe.given:
            store.put(record)
        started = time.perf_counter()
        hits = retrieve(store, probe)
        elapsed = (time.perf_counter() - started) * 1000
        result = evaluate(probe, hits, is_stale=is_stale)
        result.latency_ms = elapsed
        results.append(result)
    return summarize(results), results


def run_controls(probes, *, make_store, **kwargs) -> dict:
    """The negative control. Every one of these must FAIL to recall."""
    from .stores import NullStore, RandomStore, ShuffledStore

    null_summary, _ = run(probes, make_store=NullStore, **kwargs)
    random_summary, _ = run(probes, make_store=lambda: RandomStore(seed=7), **kwargs)
    shuffled_summary, _ = run(probes, make_store=lambda: ShuffledStore(make_store()), **kwargs)

    return {
        "null_recall": null_summary.recall_at_k,             # must be < 0.2
        "random_recall": random_summary.recall_at_k,         # must be < 0.2
        "shuffled_mrr": shuffled_summary.mrr,                # must be well below the real mrr
        "valid": (null_summary.recall_at_k < 0.2
                  and random_summary.recall_at_k < 0.2),
    }
```

### `report.py`

```python
"""JSON for CI, a table for humans, and the regression check."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

TOLERANCE = 0.03


def write(summary, results, path: Path, *, commit: str = "") -> None:
    payload = {
        "commit": commit,
        **{k: round(v, 4) if isinstance(v, float) else v for k, v in asdict(summary).items()},
        "per_probe": {r.probe.name: {"hit": r.hit_all, "rank": r.first_rank,
                                     "leaked": r.leaked} for r in results},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check_regression(current: dict, baseline_path: Path) -> tuple[bool, list[str]]:
    if not baseline_path.exists():
        return True, ["no baseline; recording this run as the baseline"]
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = []

    if current["leaks"] > 0:
        problems.append(f"LEAK GATE: {current['leaks']} forbidden record(s) returned")
    for metric, direction in (("recall_at_k", 1), ("mrr", 1), ("distinct_at_k", 1)):
        delta = current[metric] - baseline.get(metric, 0)
        if delta * direction < -TOLERANCE:
            problems.append(f"{metric}: {baseline[metric]:.3f} -> {current[metric]:.3f}")
    if current["staleness"] - baseline.get("staleness", 0) > TOLERANCE:
        problems.append(f"staleness rose to {current['staleness']:.3f}")

    # Per-probe: the actionable part.
    newly_failing = [
        name for name, entry in current["per_probe"].items()
        if not entry["hit"] and baseline.get("per_probe", {}).get(name, {}).get("hit")
    ]
    if newly_failing:
        problems.append("newly failing probes: " + ", ".join(sorted(newly_failing)))
    return not problems, problems
```

### `cli.py`

- `<command> run [--probes probes/core.yaml]` — the table plus the JSON.
- `<command> control` — the three broken stores. **Run this first, on a new
  suite, before trusting any number it produces.**
- `<command> simulate --sessions 200 --seed 7` — the drift curves.
- `<command> compare <baseline.json>` — the regression check, exit 1 on failure.
  This is the CI entry point.
- `<command> add` — an interactive helper to append a probe from a real query
  that just went wrong. The suite grows from failures; make that one command.

### CI

```yaml
- run: python -m <package>.cli control     # the suite is valid
- run: python -m <package>.cli run --out eval-results.json
- run: python -m <package>.cli compare eval-results.baseline.json
```

`control` running first is deliberate. A suite that has silently rotted into
uselessness would otherwise pass every subsequent step.

## Failure modes

- **The suite passes against a null store.** The probes are being answered by
  the model's priors, or `expect` is so loose that anything satisfies it. Every
  number the suite has ever produced is noise. Run `control` first, always.

- **Generated probes.** An LLM asked to write probes writes probes an LLM can
  answer, from the same distribution as the system under test. Hand-write them
  from real failures — thirty real probes beat three hundred generated ones.

- **No `forbid` entries.** Cross-scope leaks and stale facts are invisible to
  recall@k. Without `forbid`, the suite cannot see the two failure modes that
  actually hurt users.

- **No negative probes.** Every probe expecting a hit trains you toward a system
  that retrieves on every turn, injecting noise into turns that needed none. At
  least 15% of probes should expect nothing.

- **Probes that share a store.** Probe 7 passes because probe 3 left something
  behind. Rebuild per probe; the cost is milliseconds.

- **k too large.** At k=20, recall@k is trivially high and ranking is not being
  measured. The `ShuffledStore` control catches this — if MRR barely moves when
  order is scrambled, k is doing the work.

- **Point-in-time only.** Everything looks fine at 100 records. The failure is at
  10,000, and only the longitudinal run sees it coming.

- **Metrics with no baseline.** Numbers produced, admired once, never compared.
  The diff is the value; commit the results file.

- **Tuning against the suite until it passes.** At that point the suite is a
  training set, not a test set. Keep a holdout: 20% of probes that you look at
  only when you think you are done.

## Required tests

The harness needs its own tests — a broken measuring instrument is worse than
none. All offline:

```python
def test_null_store_fails_the_suite():
    summary, _ = run(PROBES, make_store=NullStore)
    assert summary.recall_at_k < 0.2


def test_random_store_fails_the_suite():
    summary, _ = run(PROBES, make_store=lambda: RandomStore(seed=1))
    assert summary.recall_at_k < 0.2


def test_shuffled_store_keeps_recall_but_drops_mrr(real_store_factory):
    real, _ = run(PROBES, make_store=real_store_factory)
    shuffled, _ = run(PROBES, make_store=lambda: ShuffledStore(real_store_factory()))
    assert shuffled.recall_at_k >= real.recall_at_k - 0.05
    assert shuffled.mrr < real.mrr - 0.1


def test_a_leak_fails_the_run_regardless_of_recall():
    summary, _ = run([leaking_probe()], make_store=leaky_store_factory)
    assert summary.leaks > 0
    assert summary.passed is False


def test_negative_probe_scores_correctly_on_empty_result():
    probe = Probe(name="n1", query="something nobody said", expect=[], given=[])
    summary, _ = run([probe], make_store=NullStore)
    assert summary.negative_correct == 1.0


def test_probes_do_not_share_state():
    first = Probe(name="a", query="x", given=[record("r1", "x")], expect=["r1"])
    second = Probe(name="b", query="x", given=[], expect=[])
    _, results = run([first, second], make_store=fresh_store)
    assert results[1].returned == []


def test_duplicate_probe_names_are_rejected(tmp_path):
    (tmp_path / "p.yaml").write_text("- {name: a, query: q}\n- {name: a, query: r}\n")
    with pytest.raises(ValueError, match="duplicate probe names"):
        load(tmp_path / "p.yaml")


def test_distinct_at_k_penalizes_near_duplicates():
    result = evaluate(probe_k3(), three_hits_same_text(), is_stale=lambda r: False)
    assert result.distinct < 0.4


def test_regression_check_fails_on_a_drop(tmp_path):
    baseline = {"recall_at_k": 0.90, "mrr": 0.75, "distinct_at_k": 0.8,
                "staleness": 0.01, "per_probe": {"a": {"hit": True}}}
    (tmp_path / "b.json").write_text(json.dumps(baseline))
    ok, problems = check_regression(
        {"recall_at_k": 0.80, "mrr": 0.75, "distinct_at_k": 0.8, "staleness": 0.01,
         "leaks": 0, "per_probe": {"a": {"hit": False, "leaked": []}}},
        tmp_path / "b.json")
    assert not ok
    assert any("recall_at_k" in p for p in problems)
    assert any("newly failing probes: a" in p for p in problems)


def test_regression_check_fails_on_any_leak(tmp_path):
    ok, problems = check_regression({"leaks": 1, "recall_at_k": 0.99, "mrr": 0.9,
                                     "distinct_at_k": 0.9, "staleness": 0.0,
                                     "per_probe": {}}, baseline_at(tmp_path))
    assert not ok and "LEAK GATE" in problems[0]
```

## Verify

Follow `references/evaluation.md` — this skill *implements* that contract, so
verification here is mostly self-referential and unusually strict:

1. **Tier 0** — install, import, `--help`, `run` against the bundled example
   probes.
2. **Tier 1** — `pytest -q`, offline. Every test above.
3. **Tier 2** — `<command> control` against the user's real memory system. All
   three controls must fail to recall. **If they do not, stop: report that the
   probe suite is invalid and do not present any other numbers from it.** A
   number from an invalid suite is worse than no number, because it will be
   believed.
4. **Tier 3** — `<command> simulate --sessions 200` and read the three curves.
   Report the recall slope explicitly: flat is the pass condition, and a
   downward slope is a finding that should change what the user builds next.

Then tell the user what actually ran: the probe count, the split between
positive and negative probes, the control results, the five metrics, and the
drift slope. Lead with the control result — every other number depends on it.
