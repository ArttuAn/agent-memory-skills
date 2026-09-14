# Forgetting

Every memory system that only writes eventually stops working. Not with an
error — with a slow decline in recall as the useful records get outvoted by
the accumulated sediment of everything that was ever said.

Forgetting is a feature you design, not an outage you recover from.

## Four mechanisms, in order of danger

| Mechanism | What it does | Reversible? |
| --- | --- | --- |
| **Decay** | Down-weights old records at retrieval time | Yes — nothing is touched |
| **Eviction** | Drops records from the working set, not the store | Yes — the store still has them |
| **Supersession** | Marks a record obsolete, keeps the row | Yes — see `memory-temporal` |
| **Deletion** | Removes the row | **No** |

Reach for them in that order. Decay solves most of what people reach for
deletion to solve, and cannot lose anything. Deletion is only the right answer
for three cases: the user asked, the law requires it, or the record is provably
garbage (empty, malformed, a duplicate of a record you are keeping).

## Decay

Covered mechanically in `retrieval-quality.md`. The design decision it forces:
**not everything decays at the same rate**, and a single global half-life is
wrong for any real system.

```python
HALF_LIFE_DAYS = {
    "preference": float("inf"),   # "I'm vegetarian" does not become less true
    "identity":   float("inf"),   # names, roles, relationships
    "fact":       180.0,          # world facts drift
    "state":      7.0,            # "the branch is currently failing"
    "episode":    30.0,           # what happened, fading into what usually happens
    "note":       3.0,            # scratch
}
```

Assign the class at write time. Guessing it at read time means re-deriving it on
every query, and getting a different answer each time the prompt changes.

An important asymmetry: **decay should never zero out.** Floor it. A record at
0.0 is invisible and might as well be deleted; a record at 0.05 still surfaces
when nothing else matches, which is exactly when a five-year-old memory is
valuable.

## Eviction from the working set

This is `memory-working`'s job and the only forgetting most agents need. The
policies, in ascending order of how much they cost you:

- **FIFO** — drop the oldest turns. One line, and wrong the moment the first
  turn contained the task definition.
- **Pinned + FIFO** — never evict turns marked pinned (the system prompt, the
  task, the current plan). This is the sweet spot for most agents.
- **Summarize-and-drop** — compact the evicted span into a paragraph and keep
  the paragraph. Costs a model call, keeps the thread.
- **Relevance eviction** — keep the turns most similar to the current goal.
  Sounds right, behaves badly: it silently discards the turn where the user
  corrected you, because the correction is off-topic from the current step.

Whatever you choose, **the eviction must be visible in the transcript**. Leave a
marker:

```
[12 turns compacted — 4,100 tokens → 380. Full transcript at .memory/session-3f2a.jsonl]
```

Without it, the model sees a conversation that inexplicably jumps, and the user
has no idea why the agent forgot. With it, both can ask for the original.

## Supersession over deletion

When a fact changes, the instinct is `UPDATE`. Resist it. Write the new fact,
mark the old one superseded, and keep both:

```sql
UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ?;
```

Costs one row. Buys you: an audit trail, the ability to answer "why did you say
that last month", a recovery path from a bad extraction, and the ability to
detect flapping (a fact that gets rewritten every session is not a fact, it is a
bad extraction rule). `memory-temporal` is this idea taken seriously.

## Deletion: doing it properly

When deletion is genuinely required, a `DELETE` on one table is not enough.

**Delete by scope, not by row.** The user asks to forget a person, a project, a
session — never "record 4471". Design for `delete_where(scope=...)` from the
start and make `scope` a first-class indexed column.

**Cascade, and know what cascades.** One fact can be referenced by a graph edge,
a consolidation lineage pointer, a procedure's precondition, and an eval
fixture. An orphaned edge pointing at a deleted node is a retrieval that returns
`None` and a traversal that crashes three weeks later.

```python
def forget(store, *, scope: str) -> dict[str, int]:
    """Delete everything in a scope, and everything that points into it."""
    ids = {r.id for r in store.all() if r.metadata.get("scope") == scope}
    removed = {"records": 0, "edges": 0, "derived": 0}
    for record in list(store.all()):
        meta = record.metadata
        if record.id in ids:
            store.delete(record.id); removed["records"] += 1
        elif meta.get("source_id") in ids or meta.get("derived_from") in ids:
            store.delete(record.id); removed["derived"] += 1
        elif meta.get("src") in ids or meta.get("dst") in ids:
            store.delete(record.id); removed["edges"] += 1
    return removed
```

**Deletion must survive consolidation.** If episodes are the source and facts are
derived, deleting the episodes leaves the derived facts standing — the system
"forgot" nothing that matters. Either carry `derived_from` on every consolidated
record (above), or re-derive the affected scope after a delete. Choose one and
write the test.

**Log the deletion, not the deleted.** Keep a tombstone with the scope, the
count, the reason, and the timestamp. Never keep the text you just deleted in a
log file next to the store; that is the same data in a place nobody audits.

## Compaction

Separate from forgetting, frequently confused with it: compaction *reshapes*
without losing. Ten episodes of "the user asked about deployment and I checked
the pipeline" become one record: "the user regularly asks about deployment
status; the pipeline check is the useful response."

Rules that keep compaction from becoming lossy deletion in disguise:

1. **Keep the source records** until the compacted record has been retrieved and
   used at least once. Compaction bugs surface on first use, and that is the
   only moment when rollback is still cheap.
2. **Never compact across scopes.** Two users' episodes merged into one summary
   is a data leak wearing a tidy-up costume.
3. **Never compact unresolved contradictions.** If two episodes disagree, the
   summary picks one and the disagreement is gone. Surface it; do not average it.
4. **Cap the compression ratio.** A pass that turns 40 records into 1 has not
   summarized, it has discarded. Alert above roughly 10:1 rather than accepting
   it silently.

## What good forgetting looks like

- Store size is roughly flat over time, while recall on the probe suite holds.
- Deleting a scope and re-running the probe suite shows exactly the expected
  probes failing, and no others.
- The oldest record in the store is old. A system whose maximum record age
  equals its retention window has no long-term memory, only a rolling buffer
  with extra steps.
