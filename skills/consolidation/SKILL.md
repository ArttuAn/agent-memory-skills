---
name: memory-consolidation
description: "Build the background pass that keeps memory healthy: dedupe, merge, promote episodes to facts, decay and prune - with lineage and a dry run"
---

# Consolidation

Every memory system that only writes eventually stops working — not with an
error, but with a slow decline in recall as useful records get outvoted by
accumulated sediment. Consolidation is the pump that runs against that: it
merges near-duplicates, promotes episodes into facts, resolves what can be
resolved, and prunes what has earned pruning.

It is also the only skill in this repo that **destroys information**. A merge
bug is silent, permanent, and discovered weeks later by someone asking why the
agent forgot something. So the design here is built around three properties
that matter more than the merging itself: **lineage** (every derived record
names its sources), **dry run by default** (nothing writes until a human has
read the plan), and **idempotence** (a pass that dies halfway can simply be
re-run).

```
  select candidates ──► blocking, not all-pairs
        │
        ▼
  propose operations ──► merge / promote / supersede / decay / prune
        │
        ▼
  ┌── DRY RUN ──► print the plan ──► human reads it ──┐
  │                                                   │
  └── APPLY ──► one transaction per operation ────────┘
                     │
                     ▼
              lineage written, sources kept until first successful use
```

## Use this when

- The store is growing without bound and recall is measurably declining.
- You have an episodic log and want the distillate — this is the pump between
  `memory-episodic` and `memory-semantic`.
- Near-duplicates are crowding the top-k: five phrasings of one fact occupying
  all five slots.

**Do not use this when** the store is small. Under a few thousand records, a
nightly pass is a cron job that mostly proves itself unnecessary, and you have
imported the riskiest component in the system to solve a problem you do not
have. Measure first: run the probe suite, look at `distinct@k`, and only build
this when duplicates are demonstrably displacing real answers.

## Workflow

1. **Ask what is allowed to be destroyed.** Which record types can be merged?
   Which must never be touched? Is anything under a retention obligation? If the
   user has no answer, default to: merge only near-identical facts, promote but
   never delete episodes, and prune nothing at all in the first version.

2. **Ask what triggers a pass.** Schedule, record count, or manual. Default to
   manual plus a documented cron line — a consolidation pass firing unattended
   on day one is how silent data loss happens.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

4. **Write the six modules**: `operations.py` (the operation types),
   `candidates.py` (blocking), `merge.py`, `promote.py`, `prune.py`,
   `runner.py` (planning, dry run, transactional apply). Then `cli.py`.

5. **Verify against the contract** in `references/evaluation.md`, plus the
   consolidation-specific tests below.

## The decisions that matter

### 1. Candidate selection — never compare all pairs

Naive deduplication is O(n²) embedding comparisons. At 50,000 records that is
1.25 billion, which is not a performance problem so much as a pass that never
finishes. Use **blocking**: only compare records that share a cheap key.

```python
def blocks(records):
    """Group by cheap keys. Only compare within a block."""
    by_key: dict[tuple, list] = defaultdict(list)
    for record in records:
        by_key[("subject", record.metadata.get("subject"))].append(record)
        by_key[("day", int(record.created_at // 86400))].append(record)
        by_key[("shingle", minhash_band(record.text))].append(record)
    return [group for group in by_key.values() if 2 <= len(group) <= 200]
```

Three blocking keys with different failure modes, which is the point of using
several: subject catches facts about the same entity, day catches episodes from
one session, and a MinHash band catches lexical near-duplicates that share
neither. A pair missed by all three survives to the next pass — blocking trades
recall for tractability, and that trade is correct here because a missed merge
costs one redundant record while a bad merge costs the truth.

Cap block size. A block of 5,000 records sharing `subject="user"` is not a
block, it is the whole store wearing a hat.

### 2. What may be merged automatically, and what may never

| Operation | Auto? | Why |
| --- | --- | --- |
| Merge facts with cosine > 0.95 and identical `(subject, predicate, object)` | **Yes** | They are the same claim |
| Merge facts with cosine > 0.9, same key, different object | **No** | That is a contradiction, not a duplicate |
| Compact N episodes into a summary | Yes, with sources kept | Recoverable |
| Promote a fact from 3+ corroborating episodes | Yes, at reduced confidence | Traceable via lineage |
| Merge two entity nodes | **Never automatically** | See `memory-graph`: collapse is unrecoverable |
| Delete anything a user wrote directly | **Never** without an explicit request | Not yours to delete |

The rule underneath: **automatic operations must be reversible or provably
information-preserving.** If undoing an operation requires information the
operation destroyed, a human approves it or it does not happen.

### 3. Lineage, on every derived record

```python
derived_from: list[str]      # the source record ids
derivation: str              # "merge" | "promote" | "compact"
derived_at: float
```

Lineage is what makes consolidation auditable, what makes `forget(scope)`
cascade correctly (see `references/forgetting.md`), and what lets you answer
"where did this come from" when a consolidated fact turns out to be wrong.

**Keep the sources until the derived record has been retrieved and used at least
once.** Consolidation bugs surface on first use, and that is the only moment
when rollback is still cheap. Mark sources `archived` rather than deleting them,
and let a later pass delete archived sources whose derived record has a non-zero
`access_count`.

### 4. Dry run is the default

```bash
<command> consolidate            # plans, prints, writes nothing
<command> consolidate --apply    # writes
```

Not a flag on a destructive default — a non-destructive default with an opt-in.
The plan output must be readable by a human who did not write the system:

```
MERGE  3 records -> 1
  keep:  f_a19c  "Arttu deploys arttuan.com on Vercel"
  merge: f_77de  "Arttu uses Vercel for arttuan.com"        sim 0.96
         f_2b01  "arttuan.com is deployed on Vercel"        sim 0.95
  reason: same (subject, predicate, object), cosine > 0.95

PROMOTE episode cluster -> fact
  from:  ep_4a2, ep_91f, ep_c03  (3 episodes, 2 verified successes)
  fact:  "The CI pipeline has no redis service"
  confidence: 0.6  (promoted, not user-stated)

PRUNE  0 records  (retention policy: none configured)

SKIPPED 4 operations - run with --explain to see why
```

`SKIPPED` in the summary matters as much as the operations. A pass that skips
nothing is not applying its safety rules.

### 5. Idempotence and crash safety

A consolidation pass will die halfway — a killed process, a full disk, a model
timeout. Design for it:

- **One transaction per operation**, not one per pass. A pass that dies after 40
  of 100 merges leaves 40 complete merges and 60 untouched records, which is a
  valid state.
- **Operations are keyed by their inputs.** A merge of `{f_a, f_b, f_c}` has a
  deterministic ID; re-running the pass finds the merge already recorded and
  skips it.
- **Never mutate in place.** Write the derived record, then mark the sources
  archived. In that order: a crash between them leaves a duplicate (harmless
  and fixed next pass), while the reverse order loses data.

```python
operation_id = "op_" + blake2b(
    (kind + "|" + "|".join(sorted(source_ids))).encode(), digest_size=8
).hexdigest()
```

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # thresholds, retention, what may be auto-merged
    ├── operations.py    # Operation types + deterministic ids
    ├── candidates.py    # blocking: subject / day / minhash
    ├── merge.py         # near-duplicate merging
    ├── promote.py       # episodes -> facts, with corroboration
    ├── prune.py         # retention, decay, archived-source cleanup
    ├── runner.py        # plan -> dry run -> transactional apply
    └── cli.py           # consolidate / plan / explain / undo / stats
```

### `operations.py`

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum


class Kind(str, Enum):
    MERGE = "merge"
    PROMOTE = "promote"
    COMPACT = "compact"
    SUPERSEDE = "supersede"
    ARCHIVE = "archive"
    PRUNE = "prune"


AUTO_APPROVED = {Kind.MERGE, Kind.PROMOTE, Kind.COMPACT, Kind.ARCHIVE}
NEEDS_HUMAN = {Kind.PRUNE}          # and entity merges, which live in memory-graph


@dataclass
class Operation:
    kind: Kind
    sources: list[str]
    produces: object | None = None       # the derived record, if any
    reason: str = ""
    score: float = 0.0
    skipped: str | None = None           # set when a safety rule blocked it
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            payload = f"{self.kind.value}|" + "|".join(sorted(self.sources))
            self.id = "op_" + hashlib.blake2b(payload.encode(), digest_size=8).hexdigest()

    @property
    def auto(self) -> bool:
        return self.kind in AUTO_APPROVED and self.skipped is None

    def render(self) -> str:
        head = f"{self.kind.value.upper():9} {len(self.sources)} records"
        if self.skipped:
            return f"{head}  SKIPPED: {self.skipped}"
        lines = [f"{head}  ({self.reason})"]
        if self.produces is not None:
            lines.append(f"  -> {getattr(self.produces, 'text', self.produces)[:100]}")
        for source in self.sources[:5]:
            lines.append(f"     from {source}")
        return "\n".join(lines)
```

### `merge.py`

```python
"""Near-duplicate merging. Refuses anything that smells like a contradiction."""

from __future__ import annotations

from .operations import Kind, Operation

MERGE_AT = 0.95
CONTRADICTION_BAND = (0.85, 0.95)     # similar but not identical: suspicious


def plan_merges(block: list, *, similarity, keep_rule=None) -> list[Operation]:
    """One operation per cluster of near-identical records."""
    operations: list[Operation] = []
    used: set[str] = set()
    keep_rule = keep_rule or (lambda records: max(
        records, key=lambda r: (r.metadata.get("confidence", 0.5), r.created_at)))

    for i, record in enumerate(block):
        if record.id in used:
            continue
        cluster = [record]
        for other in block[i + 1:]:
            if other.id in used:
                continue
            score = similarity(record.text, other.text)
            if score >= MERGE_AT:
                if _contradicts(record, other):
                    operations.append(Operation(
                        Kind.MERGE, [record.id, other.id],
                        reason=f"sim {score:.2f}",
                        skipped="same key, different value - this is a contradiction, "
                                "not a duplicate; route it to the resolver"))
                    continue
                cluster.append(other)
            elif CONTRADICTION_BAND[0] <= score < CONTRADICTION_BAND[1] and _contradicts(record, other):
                operations.append(Operation(
                    Kind.MERGE, [record.id, other.id], reason=f"sim {score:.2f}",
                    skipped="near-duplicate with a differing value - needs a human"))

        if len(cluster) < 2:
            continue
        keeper = keep_rule(cluster)
        merged = _combine(keeper, cluster)
        used.update(r.id for r in cluster)
        operations.append(Operation(
            Kind.MERGE, [r.id for r in cluster], produces=merged,
            reason=f"{len(cluster)} records, cosine >= {MERGE_AT}",
            score=min(similarity(keeper.text, r.text) for r in cluster if r.id != keeper.id),
        ))
    return operations


def _contradicts(a, b) -> bool:
    key_a = (a.metadata.get("subject"), a.metadata.get("predicate"))
    key_b = (b.metadata.get("subject"), b.metadata.get("predicate"))
    if not all(key_a) or key_a != key_b:
        return False
    return a.metadata.get("object") != b.metadata.get("object")


def _combine(keeper, cluster):
    """The merged record keeps the best text and the union of everything else."""
    merged = replace(keeper)
    merged.metadata = dict(keeper.metadata)
    merged.metadata["derived_from"] = sorted({r.id for r in cluster})
    merged.metadata["derivation"] = "merge"
    merged.metadata["access_count"] = sum(r.metadata.get("access_count", 0) for r in cluster)
    # Corroboration raises confidence, but never past a user statement's own level.
    best = max(r.metadata.get("confidence", 0.5) for r in cluster)
    merged.metadata["confidence"] = min(0.99, best + 0.01 * (len(cluster) - 1))
    merged.created_at = min(r.created_at for r in cluster)      # keep the earliest claim date
    return merged
```

`created_at = min(...)` is deliberate: the merged fact has been true since the
*earliest* time anyone said it, not since the merge ran. Taking the max quietly
makes every consolidated fact look fresh, which then defeats recency decay.

### `promote.py`

```python
"""Episodes -> facts. The pump. Requires corroboration, caps confidence."""

from __future__ import annotations

from .operations import Kind, Operation

PROMOTE_PROMPT = """These episodes describe things that happened. Extract any
durable fact that MORE THAN ONE of them independently supports.

Rules:
- The fact must be supported by at least two separate episodes.
- Do not extract what happened (that is already recorded). Extract what is true.
- Do not extract anything an episode merely assumed.
- Empty array if nothing is corroborated. That is the common answer.

Reply as JSON: [{{"text": "...", "subject": "...", "predicate": "...",
                  "object": "...", "supported_by": [<episode indexes>]}}]

Episodes:
{episodes}"""

MIN_SUPPORT = 2
PROMOTED_CONFIDENCE = 0.6        # below any user-stated fact, deliberately


def plan_promotions(cluster: list, client, *, make_fact) -> list[Operation]:
    if len(cluster) < MIN_SUPPORT:
        return []
    rendered = "\n\n".join(f"[{i}] {e.render()}" for i, e in enumerate(cluster))
    reply = client.complete([{"role": "user", "content": PROMOTE_PROMPT.format(episodes=rendered)}])
    raw = (reply.content or "")
    raw = raw[raw.find("[") : raw.rfind("]") + 1] if "[" in raw else "[]"
    try:
        candidates = __import__("json").loads(raw)
    except Exception:
        return []

    operations = []
    for candidate in candidates if isinstance(candidates, list) else []:
        support = [i for i in candidate.get("supported_by", []) if 0 <= i < len(cluster)]
        if len(set(support)) < MIN_SUPPORT:
            operations.append(Operation(
                Kind.PROMOTE, [cluster[i].id for i in support], reason="candidate fact",
                skipped=f"only {len(set(support))} supporting episode(s)"))
            continue
        fact = make_fact(candidate, confidence=PROMOTED_CONFIDENCE,
                         derived_from=[cluster[i].id for i in support])
        operations.append(Operation(
            Kind.PROMOTE, [cluster[i].id for i in support], produces=fact,
            reason=f"corroborated by {len(set(support))} episodes"))
    return operations
```

Promotion **never deletes the episodes**. The log is the evidence; the fact is
the claim. Deleting the evidence when you derive a claim is how a system becomes
unable to explain itself.

### `runner.py`

```python
"""Plan, print, apply. One transaction per operation. Resumable."""

from __future__ import annotations

from dataclasses import dataclass, field

from .operations import Kind, Operation


@dataclass
class Plan:
    operations: list[Operation] = field(default_factory=list)

    @property
    def applicable(self) -> list[Operation]:
        return [o for o in self.operations if o.auto]

    @property
    def skipped(self) -> list[Operation]:
        return [o for o in self.operations if o.skipped]

    def render(self, *, explain: bool = False) -> str:
        lines = [o.render() for o in self.applicable]
        if self.skipped:
            lines.append(f"\nSKIPPED {len(self.skipped)} operations"
                         + (":" if explain else " - run with --explain to see why"))
            if explain:
                lines += [f"  {o.render()}" for o in self.skipped]
        by_kind = {}
        for operation in self.applicable:
            by_kind[operation.kind.value] = by_kind.get(operation.kind.value, 0) + 1
        lines.append("\n" + ", ".join(f"{v} {k}" for k, v in sorted(by_kind.items())) or "nothing to do")
        return "\n".join(lines)


class Runner:
    def __init__(self, store, *, max_compression: float = 10.0):
        self.store = store
        self.max_compression = max_compression

    def apply(self, plan: Plan, *, dry_run: bool = True) -> dict:
        if dry_run:
            return {"planned": len(plan.applicable), "applied": 0, "dry_run": True}

        applied = skipped = 0
        for operation in plan.applicable:
            if self.store.operation_done(operation.id):
                skipped += 1                       # idempotent: already applied
                continue
            ratio = len(operation.sources) / 1.0
            if operation.kind is Kind.COMPACT and ratio > self.max_compression:
                skipped += 1                       # not summarising, discarding
                continue
            with self.store.transaction():
                if operation.produces is not None:
                    self.store.put(operation.produces)      # derived record FIRST
                if operation.kind in (Kind.MERGE, Kind.COMPACT):
                    for source in operation.sources:
                        self.store.archive(source, superseded_by=getattr(operation.produces, "id", None))
                self.store.record_operation(operation)
            applied += 1
        return {"planned": len(plan.applicable), "applied": applied, "skipped": skipped}
```

The derived record is written **before** the sources are archived, inside one
transaction, and the operation is recorded last. A crash at any point leaves
either nothing done or everything done for that operation, and re-running skips
what is already recorded.

### `cli.py`

- `<command> plan [--explain]` — the dry run. The default. Prints the plan and
  writes nothing.
- `<command> consolidate --apply` — runs it. Prints the same plan plus a summary.
- `<command> undo <operation_id>` — restore archived sources, remove the derived
  record. Possible only while sources are archived rather than pruned, which is
  why the archive stage exists.
- `<command> stats` — store size over time, duplicate estimate, promotion count,
  oldest record. The growth curve is the number that tells you whether this is
  working.

## Failure modes

- **A merge that was a contradiction.** Two facts with the same key and
  different values, cosine 0.96 because the wording matches, merged into
  whichever one the keep-rule picked. The disagreement is now gone and the agent
  asserts one side with raised confidence. The `_contradicts` check exists for
  exactly this; never merge on text similarity without comparing the structured
  key.

- **Compaction that is deletion in disguise.** Forty records into one paragraph
  is not a summary. Cap the compression ratio and alert rather than accepting it.

- **Consolidating across scopes.** Two users' records merged into one. A data
  leak wearing a tidy-up costume. Block on scope first, always, before any other
  blocking key.

- **Lost lineage.** A consolidated fact turns out to be wrong and there is no way
  to find what produced it or to re-derive it correctly. Lineage is three fields
  and it is the difference between a bug and an unexplainable system.

- **A pass that deletes its own evidence.** Promoting episodes into facts and
  then pruning the episodes leaves claims with no support, and a later
  `forget(scope)` that cannot cascade.

- **Non-idempotent operations.** A re-run after a crash double-merges, or
  merges a record into a record that was itself merged. Deterministic operation
  IDs plus a `record_operation` table; check before applying.

- **Unattended first run.** A consolidation pass on a cron schedule, on day one,
  against a store nobody has inspected. Run it manually, with `--explain`, for
  at least a week before it gets a schedule.

- **Confidence inflation.** Merging five copies of a rumour into one fact at
  confidence 0.99. Corroboration from independent sources is evidence; five
  copies of the same source is not. Cap the lift, and prefer to count distinct
  `derived_from` sources rather than record count.

## Required tests

All offline. See `references/evaluation.md` for the universal set; these are
mandatory:

```python
def test_merging_preserves_every_source_id(store):
    plan = plan_merges(three_near_identical(), similarity=exact_similarity)
    merged = plan[0].produces
    assert set(merged.metadata["derived_from"]) == {"f_1", "f_2", "f_3"}


def test_contradiction_is_skipped_not_merged():
    a = fact("The deploy target is staging", subject="deploy", predicate="target", object="staging")
    b = fact("The deploy target is prod", subject="deploy", predicate="target", object="prod")
    operations = plan_merges([a, b], similarity=lambda x, y: 0.96)
    assert all(o.skipped for o in operations)
    assert "contradiction" in operations[0].skipped


def test_dry_run_writes_nothing(store, plan):
    before = snapshot(store)
    Runner(store).apply(plan, dry_run=True)
    assert snapshot(store) == before


def test_apply_is_idempotent(store, plan):
    first = Runner(store).apply(plan, dry_run=False)
    second = Runner(store).apply(plan, dry_run=False)
    assert first["applied"] > 0
    assert second["applied"] == 0 and second["skipped"] == first["applied"]


def test_crash_between_operations_leaves_a_valid_state(store, plan):
    runner = Runner(store)
    with crash_after(operations=2):
        with contextlib.suppress(Crash):
            runner.apply(plan, dry_run=False)
    assert store.integrity_ok()
    runner.apply(plan, dry_run=False)          # resumes cleanly
    assert store.operation_done(plan.applicable[-1].id)


def test_sources_are_archived_not_deleted(store, plan):
    Runner(store).apply(plan, dry_run=False)
    for source in plan.applicable[0].sources:
        assert store.get(source) is not None
        assert store.get(source).metadata["status"] == "archived"


def test_undo_restores_the_sources(store, plan):
    Runner(store).apply(plan, dry_run=False)
    store.undo(plan.applicable[0].id)
    for source in plan.applicable[0].sources:
        assert store.get(source).metadata["status"] == "active"


def test_promotion_requires_two_episodes(promoter):
    promoter.client = FakeChat(['[{"text":"x","supported_by":[0]}]'])
    operations = plan_promotions(two_episodes(), promoter.client, make_fact=make_fact)
    assert operations[0].skipped and "1 supporting" in operations[0].skipped


def test_promoted_facts_have_capped_confidence(promoter):
    operations = plan_promotions(three_episodes(), promoter.client, make_fact=make_fact)
    assert operations[0].produces.metadata["confidence"] <= 0.6


def test_promotion_does_not_delete_episodes(store, plan):
    before = set(store.ids(kind="episode"))
    Runner(store).apply(promotion_plan(), dry_run=False)
    assert set(store.ids(kind="episode")) == before


def test_blocking_never_crosses_scopes(store):
    groups = blocks(store.all())
    for group in groups:
        assert len({r.metadata["scope"] for r in group}) == 1


def test_over_compression_is_refused(store):
    plan = Plan([Operation(Kind.COMPACT, [f"ep_{i}" for i in range(40)], produces=one_summary())])
    result = Runner(store, max_compression=10.0).apply(plan, dry_run=False)
    assert result["applied"] == 0


def test_merged_record_keeps_the_earliest_created_at():
    merged = plan_merges(records_dated(["2020-01-01", "2024-01-01"]),
                         similarity=lambda a, b: 0.99)[0].produces
    assert merged.created_at == iso("2020-01-01")
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, `<command> plan` on an empty store
   (should print "nothing to do", not crash).
2. **Tier 1** — `pytest -q`, offline. Every test above, especially the crash and
   idempotence tests — those are the ones that matter in production.
3. **Tier 2** — the before/after measurement, which is the only real proof this
   skill works. Run the probe suite, run consolidation, run it again:
   - **recall@5 must not drop.** If it does, consolidation destroyed something
     the probes needed. Stop and find out what.
   - **distinct@5 should rise.** That is the duplicates clearing out.
   - **Store size should fall**, and the growth curve over repeated
     simulated sessions should flatten rather than stay linear.
   Report all three numbers.
4. **Tier 3** — run `plan --explain` against the user's real store and read
   every operation. Every single one, the first time. This is the skill where
   reading the plan is not optional, and it is the last cheap moment to catch a
   merge rule that is wrong.

Then tell the user what actually ran: operations planned, applied, skipped and
why, plus the three Tier 2 numbers. If recall@5 fell at all, say so first and
recommend against scheduling the pass until it is understood.
