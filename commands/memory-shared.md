# Build Shared Memory (a multi-agent blackboard)

Given the user's spec, build a blackboard: namespaced writes, work leases with
fencing, compare-and-set conflict detection, and bounded per-worker views.

**Ask what is actually shared** (findings? claims? a plan?) and **what happens
when a worker dies mid-task** — that determines lease duration and whether
partial work is recoverable.

**Check it is needed**: under three agents, or when workers could just return
results to a coordinator, a blackboard imports lease expiry, stale reads, and
write-write conflicts into a system that had none. Say so rather than building.

## What to build

```
<project>/
└── src/<package>/
    ├── namespace.py # patterns + modes: append / owned / leased / single
    ├── board.py     # get / append / set (CAS) — SQLite in WAL mode
    ├── lease.py     # claim / heartbeat / release / reclaim, with generation
    ├── view.py      # per-worker rendered view, token-capped
    └── cli.py       # dump / watch / claims / release / stats
```

## Core pieces

1. **Namespaces with modes, declared up front.** `facts/*` append-only,
   `claims/*` leased, `results/<worker>/*` owned, `plan/*` single-writer.
   **Most of the board should be append-only** — it eliminates a whole class of
   bug. Several contended keys means the agents are sharing mutable state where
   they should be sharing conclusions. Enforce permissions in the API.
2. **Compare-and-set on everything mutable.** A write supplies the version it
   read and fails if the value moved. Last-write-wins loses the work of whichever
   agent was thinking hardest. A failed CAS is a **normal outcome** — re-read,
   reconcile, retry, capped at 3 — not an exception nobody catches.
3. **Leases need three things**: a duration that is ~3× the p95 task time (too
   short → two workers doing the same job and writing contradictory results),
   **heartbeats** so long tasks extend themselves, and a **`generation` fencing
   token**. The fencing token is the part everyone skips: a worker whose lease
   expired may still be alive and about to write, and its write must be rejected.
4. **Expiry must not lose work.** Partial results stay under the original
   worker's `results/` namespace so the next claimant resumes rather than restarts.
5. **Bounded views.** Subscribe by pattern, cap in tokens, render each entry with
   its author and timestamp. Giving every worker the whole board defeats the
   context isolation that justified multiple agents.
6. **Another agent's writes are untrusted input.** Fence the view in a labelled
   block, never in the system prompt, and never let a board entry become an
   unreviewed tool call.
7. **SQLite with `journal_mode=WAL`, `busy_timeout=5000`,
   `synchronous=NORMAL`.** The missing `busy_timeout` is the cause of most
   "database is locked" reports.

## Verify before finishing

1. Install, import, `--help`, `dump` on an empty board.
2. `pytest -q`, **deterministic — no real threads.** Drive concurrency by
   interleaving two `Board` handles. CAS rejects a stale write; an expired lease
   is reclaimable; a live lease blocks a second claimant; **an expired owner
   cannot write (fencing)**; heartbeat extends and fails after reclaim; partial
   work survives expiry; owned and single-writer namespaces are enforced;
   undeclared keys are refused; append namespaces never conflict; the view
   excludes own writes and caps size; readers never see a partial write.
3. A real concurrency run: N **processes** for 60s. Assert no work item was
   completed twice and no result was lost. Report conflict and reclaim rates.
4. Run the real multi-agent system with `watch` open. Look for workers reading
   each other's writes, and for leases actually being reclaimed.

If the feed showed no cross-worker reads, say so plainly: the honest
recommendation is then to drop the blackboard and return results to a coordinator.
