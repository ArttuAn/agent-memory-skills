# Build Memory Consolidation (the background pass)

Given the user's spec, build the pass that keeps a memory store healthy: merge
near-duplicates, promote episodes into facts, decay and prune — with lineage, a
dry run, and crash-safe idempotence.

**Ask what is allowed to be destroyed** and what triggers a pass. If the user has
no answer, default to: merge only near-identical facts, promote but never delete
episodes, prune nothing at all in the first version, and manual invocation with a
documented cron line. **This is the only skill here that destroys information.**

**Check it is needed**: under a few thousand records this is a cron job proving
itself unnecessary. Measure `distinct@k` first.

## What to build

```
<project>/
└── src/<package>/
    ├── operations.py # Operation + deterministic ids + what is auto-approved
    ├── candidates.py # blocking: subject / day / minhash band
    ├── merge.py      ├── promote.py   ├── prune.py
    ├── runner.py     # plan -> dry run -> transactional apply
    └── cli.py        # plan / consolidate --apply / undo / stats
```

## Core pieces

1. **Blocking, never all-pairs.** Three keys with different failure modes —
   subject, day, MinHash band. Cap block size; a block of 5,000 records sharing
   `subject="user"` is the whole store wearing a hat. A missed merge costs one
   redundant record; a bad merge costs the truth.
2. **Auto-approve only what is reversible or information-preserving.** Merge on
   cosine >0.95 **and identical `(subject, predicate, object)`** — yes. Merge on
   high similarity with a *different* object — never: that is a contradiction,
   route it to the resolver. Entity node merges: never automatic. Deleting
   anything the user wrote: never without an explicit request.
3. **Lineage on every derived record**: `derived_from`, `derivation`,
   `derived_at`. Keep sources **archived, not deleted**, until the derived record
   has been retrieved at least once — consolidation bugs surface on first use,
   which is the only moment rollback is cheap.
4. **Dry run is the default**, `--apply` is the opt-in. The plan must be readable
   by someone who did not write the system, and must list `SKIPPED` operations
   with reasons. A pass that skips nothing is not applying its safety rules.
5. **Idempotence and crash safety**: deterministic operation ids from the sorted
   source ids; one transaction **per operation**, not per pass; write the derived
   record **before** archiving sources. A crash then leaves either a harmless
   duplicate or a completed operation — never a loss.
6. **Promotion caps confidence at 0.6** and requires 2+ independently supporting
   episodes. It never deletes the episodes: the log is the evidence, the fact is
   the claim.
7. **Merged records keep the earliest `created_at`.** Taking the max makes every
   consolidated fact look fresh and defeats recency decay.
8. **Cap the compression ratio** (~10:1). Forty records into one paragraph is not
   a summary.

## Verify before finishing

1. Install, import, `--help`, `plan` on an empty store prints "nothing to do".
2. `pytest -q` — merging preserves every source id; contradictions are skipped
   not merged; dry run writes nothing; apply is idempotent; a crash between
   operations leaves a valid, resumable state; sources are archived not deleted;
   `undo` restores them; promotion requires two episodes and caps confidence;
   promotion does not delete episodes; blocking never crosses scopes;
   over-compression is refused; merged records keep the earliest date.
3. **The before/after measurement, which is the only real proof**: run the probe
   suite, consolidate, re-run. recall@5 **must not drop**; distinct@5 should
   rise; store size should fall and the growth curve should flatten.
4. `plan --explain` against the real store — read **every** operation, the first
   time. This is the last cheap moment to catch a wrong merge rule.

Report operations planned/applied/skipped with reasons plus the three numbers.
If recall@5 fell at all, lead with that and recommend against scheduling it.
