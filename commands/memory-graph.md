# Build Graph Memory (entities, relations, traversal)

Given the user's spec, build a knowledge-graph memory: triple extraction against
a closed vocabulary, conservative entity resolution, and budgeted seed-and-
traverse retrieval.

**Ask for the entity types and the relation list.** A closed list of 5-15
relation types is the single strongest predictor of whether this works — do not
default to open extraction. Also ask the identity rule: how do you know two
mentions are the same entity?

**Check it is needed first.** Graphs only beat top-k on multi-hop questions. If
the user's lookups are single-hop, say so rather than building.

## What to build

```
<project>/
└── src/<package>/
    ├── graph.py      # Node, Edge, Path
    ├── store.py      # SQLite nodes+edges, indexed in BOTH directions
    ├── extractor.py  # text -> triples, closed vocabulary + OTHER bucket
    ├── resolver.py   # the identity ladder + a merge log
    ├── traverse.py   # budgeted BFS with path scoring
    ├── render.py     └── cli.py  # ingest / node / neighbours / path / stats
```

## Core pieces

1. **Entity resolution, biased toward creating.** The two errors are not
   symmetric: fragmentation is a visible recall problem fixable with an alias;
   collapse merges two people into one node, makes every edge on both false, and
   is unrecoverable. The ladder: exact identifier → exact normalized name within
   scope+type → explicit alias table → model confirmation (only to *confirm* a
   similarity candidate, and NO when unsure) → **new node**. Never merge on
   embedding similarity alone. Log every merge with both ids and the reason.
2. **Closed relation vocabulary.** Give the model the list; a relation outside it
   is stored as `OTHER` with the raw phrase in metadata — **not dropped**. Watch
   the `OTHER` share in `stats`; above ~40% the vocabulary is wrong. Normalize
   edge direction on write so `depends_on` and `required_by` never both exist.
3. **Traversal: default `max_hops=2`.** One hop did not need a graph; three hops
   in a connected domain reaches everything and returns noise with a path
   attached. Score `product(edge.confidence) * decay**(hops-1)` with decay ~0.6.
4. **Hub nodes are what kills naive traversal.** Cap per-node fan-out (~20,
   highest confidence first) and skip high-degree nodes reached indirectly.
   Budget by node count as well as hops.
5. **Provenance on every edge**: source id, the sentence it came from, extraction
   confidence, timestamp. "Only relationships stated in the text" in the prompt —
   an edge with an empty `sentence` is an inferred edge and should score low.
6. **Render the path, not the endpoint.** "Anna works_on Herdr, which depends_on
   the gateway, which Arttu owns" is auditable; "Anna is related to Arttu" is not.
7. **Two SQLite tables, not a graph database.** Index `(scope, src, relation)`
   and `(scope, dst, relation)` — a graph that only walks forwards answers half
   the questions.

## Verify before finishing

1. Install, import, `--help`, ingest a 3-sentence fixture and run `neighbours`.
2. `pytest -q` — two hops reach what one cannot; aliases resolve; similar names
   do **not** merge; every merge is logged; a hub does not flood the result; a
   dangling edge does not crash traversal; deleting a node removes its edges;
   unknown relations go to `OTHER`; path score decays with distance.
3. A probe suite with 8+ genuinely multi-hop questions, **run against a plain
   top-k store as the control**. If top-k answers them too, you did not need the
   graph — that is the most useful finding this suite can produce.
4. Ingest a real document, read `stats` (node count vs real entities →
   fragmentation; top degrees → hubs; `OTHER` share → vocabulary fit), and
   spot-check five edges against their `sentence` field.

Report the graph-versus-top-k comparison. If top-k matched, say so plainly.
