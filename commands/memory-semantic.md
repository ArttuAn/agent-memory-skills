# Build Semantic Memory (distilled facts, with a gate)

Given the user's spec, build a fact store: extraction behind a gate, entity
keying, contradiction resolution, confidence and provenance.

**Ask the one question that cannot be defaulted**: what counts as a fact here?
Get 5-10 examples of things to remember forever and 3-5 things that must never
be stored. If the user cannot produce the second list, draft one and confirm it —
without it you are building a store of hallucinations with good recall.

## What to build

```
<project>/
└── src/<package>/
    ├── fact.py       # Fact + Status + source-seeded confidence
    ├── extractor.py  # candidates + passes_gate()
    ├── store.py      # SQLite: facts, aliases, lineage, FTS5
    ├── resolver.py   # duplicate / refinement / update / conflict
    ├── recall.py     # entity-exact + vector + lexical, fused by RRF
    ├── render.py     └── cli.py  # remember/recall/list/forget/export/conflicts
```

## Core pieces

1. **The gate** — a candidate must be durable, specific, sourced, and asserted
   (not hedged). Deny-list in the prompt: emotions, opinions attributed to
   others, speculation, hedges, transient state, restatements of your own
   answers, and anything the user asked you not to remember. Add a code-level
   `passes_gate()` too. Keep `extractor.rejected` and expose `--show-rejected`:
   gates are tuned by reading what they threw away.
2. **Store both shapes**: the sentence as `text` (what the model reads) and a
   best-effort `(subject, predicate, object)` key (what drives contradiction
   detection). This is the one place where doing both beats picking one.
3. **Four resolution outcomes, not two**: duplicate → reinforce; refinement →
   replace with lineage; update → supersede (old kept, `valid_to` set); conflict
   → **keep both, flag both, surface both at recall**. Never silently resolve a
   conflict — an agent that picks one and presents it as settled is worse than
   one that asks.
4. **Confidence by source**: user 0.9, tool 0.85, imported 0.75, inferred **≤
   0.5 at birth**. Inference compounds; cap it. Carry `derived_from`.
5. **Entity normalization, conservatively**: case-fold, resolve `the user/you/me`
   to the scope's canonical subject, use an explicit alias table. **Never merge
   on embedding similarity** — "Arttu Antikainen" and "Anna Antikainen" are close
   vectors and different people. Two records for one person is a recall problem;
   one record for two people is a privacy incident.
6. **Render with dates and hedges**, and when conflicted facts are present, tell
   the model not to pick one silently.
7. **Ship the privacy triad**: `list`, `forget`, `export`. `forget` must cascade
   into derived facts.

## Conventions

- Scope is a constructor argument, not a search filter. Bind a `ScopedStore` so
  no call site can forget it, and put the scope in the content hash.
- Extract on episode boundaries or explicit "remember this", not every turn.
- Superseded facts stay; never `UPDATE` in place.

## Verify before finishing

1. Install, import, `--help`, `remember` then `recall`.
2. `pytest -q` — gate rejects affect and hedges; duplicate reinforces without
   adding; update supersedes with lineage; conflict keeps both and both reach
   the prompt; superseded facts are not recalled; inferred confidence is capped;
   self-reference normalizes but similar names do not merge.
3. Probe suite of 25+ real facts, including 3 deliberate contradictions and 2
   scope-leak probes in `forbid`. **Leak rate is a gate: any non-zero value fails.**
4. Run the extractor over a real 30-turn conversation and read both the accepted
   and rejected lists.

Report the accept/reject ratio and the unresolved-conflict count. Zero conflicts
after real use usually means the resolver is silently choosing.
