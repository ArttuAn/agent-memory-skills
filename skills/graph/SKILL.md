---
name: memory-graph
description: "Build a knowledge-graph memory: entity extraction, conservative entity resolution, edge storage, and seed-and-traverse retrieval"
---

# Graph Memory

Top-k retrieval answers "what text is similar to this question". A graph answers
"what is connected to what". The difference only matters for **multi-hop**
questions — *"who else worked on the thing she shipped last quarter"* — where
the answer exists in no single record and vector search structurally cannot find
it, because no stored chunk is similar to the question.

That is the entire case for this skill, and it is narrower than it looks. Most
questions are single-hop. Buy the graph when you have demonstrated multi-hop
questions that top-k provably fails on, not when the diagram looks impressive.

```
   text ──► extract (entity, relation, entity) triples
              │
              ▼
        resolve entities ──► same node, or new node?
              │              (conservative: when in doubt, new)
              ▼
        upsert nodes + edges, each with provenance
              │
   query ─────┴──► seed: which nodes does the question name?
                        │
                        ▼
                   traverse: 1..N hops, budgeted, scored by path
                        │
                        ▼
                   render the subgraph as sentences, with the path shown
```

## Use this when

- Questions chain across entities: relationship lookups, "who/what connects
  these two", provenance chains, org and dependency structures.
- The domain has a natural entity structure that a human would draw as boxes and
  arrows — people, services, repos, tickets, papers and their citations.
- You need to answer aggregate questions over connections: "how many services
  depend on this one", which top-k cannot do at all because it sees only what it
  ranked.

**Do not use this when** your lookups are single-hop, which is most of them.
The cost is an extraction step that must decide entity identity, and getting
that wrong poisons every traversal that passes through the bad node. A graph
with confused entities is worse than no graph: it returns confidently connected
nonsense.

**Do not use this when** the entity vocabulary is open and enormous. Free text
about the whole world yields a graph with one edge per node and no traversal
worth doing. Graphs pay off in bounded domains.

Pair with `memory-semantic`: extract entities once, store the sentence form as a
fact and the triple as an edge, then traverse when the question is multi-hop and
fall back to top-k when it is not.

## Workflow

1. **Ask for the entity types and the relations.** What are the boxes, what are
   the arrows? A closed list of 5-15 relation types is the single strongest
   predictor of whether this works. If the user has no list, propose one from
   their domain and get it confirmed — do not default to an open vocabulary.

2. **Ask for the identity rule.** How do you know two mentions are the same
   entity? An ID field? An exact name? Only human judgement? This determines how
   aggressive resolution can be.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

4. **Write the six modules**: `graph.py` (nodes, edges), `store.py` (SQLite),
   `extractor.py` (text → triples), `resolver.py` (entity identity),
   `traverse.py` (seed and walk), `render.py`. Then `cli.py`.

5. **Verify against the contract** in `references/evaluation.md`, plus the
   graph-specific tests below.

## The decisions that matter

### 1. Entity resolution — the decision the whole skill rests on

Two mentions, one entity or two? Get it wrong in one direction and the graph
fragments (three "Arttu" nodes, none connected). Get it wrong in the other and
it *collapses* — two people merged into one node, every edge on both now false,
and a privacy incident besides.

**The two errors are not symmetric.** Fragmentation is a recall problem,
visible, and fixable at any time by adding an alias. Collapse is a correctness
problem, invisible, and hard to undo once edges have accumulated on the merged
node. So: **when in doubt, create a new node.**

A ladder of resolution strategies, in descending order of safety:

1. **Exact ID match.** A ticket number, a repo URL, an email. Always safe, and
   the reason to ask for the identity rule up front.
2. **Exact normalized name within a scope and type.** Case-folded, punctuation
   stripped. Safe enough in a bounded domain.
3. **Explicit alias table.** Populated by a human or by the user saying "that's
   the same person". Safe and auditable.
4. **Model adjudication with the surrounding context.** "Are these the same
   entity? Here are three sentences about each." Use only for candidates that
   already passed a similarity filter, and only to *confirm* — never let a
   positive verdict alone merge nodes that share no identifier.
5. **Embedding similarity.** **Never on its own.** "Arttu Antikainen" and "Anna
   Antikainen" are close vectors and different people.

Whatever merges, **record the merge** with both source IDs and the reason. It is
the only path back.

```python
MERGE_LOG = "node_merges(kept_id, merged_id, strategy, evidence, at)"
```

### 2. Closed relation vocabulary

Open extraction ("find any relations") produces `works_at`, `employed_by`,
`is_employed_at`, and `has_job_at` as four distinct predicates over the same
facts. Traversal then misses two-thirds of the graph.

Fix it at extraction: give the model the list and require it to pick, with an
explicit escape.

```python
RELATIONS = [
    "works_on", "depends_on", "owns", "created", "part_of",
    "located_in", "reports_to", "uses", "supersedes", "mentions",
]
```

And handle the escape honestly: a triple whose relation is not in the list is
stored with `relation="OTHER"` and the raw phrase in metadata, **not dropped**.
Review the `OTHER` bucket periodically — it is how the vocabulary grows, and a
bucket that is 40% of your edges means the vocabulary is wrong.

Decide direction once and normalize on write. `(a, depends_on, b)` and
`(b, required_by, a)` must not both exist; pick the canonical direction per
relation and store the inverse as a query-time view.

### 3. Traversal: seed, budget, score by path

```
seed  ── which nodes does the question actually name? ──► exact + fuzzy match
walk  ── expand 1 hop at a time, breadth-first, budgeted
score ── each path decays per hop, weighted by edge confidence
stop  ── max_hops (2, sometimes 3) or max_nodes, whichever first
```

**Two hops is the answer almost always.** One hop is a lookup you did not need a
graph for. Three hops in a connected domain reaches most of the graph and
returns noise with a path attached. Make `max_hops` configurable and default it
to 2.

Score paths so that distance costs something:

```python
path_score = product(edge.confidence for edge in path) * (decay ** (len(path) - 1))
```

with `decay ≈ 0.6`. A two-hop path through two confident edges beats a one-hop
path through a shaky one, which is the behaviour you want.

**Budget by nodes, not just hops.** A single hub node — "the company", "Python",
a shared dependency — can have thousands of edges, and expanding it once blows
the budget and floods the result. Cap the per-node fan-out (`max_fanout ≈ 20`,
highest-confidence edges first) and skip nodes whose degree exceeds a threshold
unless the question named them directly. Hub nodes are where naive traversal
dies.

### 4. Provenance on every edge

An edge with no source is an assertion nobody can check. Store, per edge: the
source record or document ID, the sentence it came from, the extraction
confidence, and the timestamp. It costs a few columns and buys you the ability
to answer "why do you think these are connected", to delete a source document
and have its edges go with it, and to debug a bad traversal by reading the
sentence that produced the wrong edge.

### 5. Do not reach for a graph database

Two SQLite tables and a breadth-first walk in Python handle graphs into the
hundreds of thousands of edges at interactive speed. Neo4j, Cypher, and a server
process buy you query expressiveness you will not use at this size and cost you
an operational dependency.

```sql
CREATE TABLE nodes (id TEXT PRIMARY KEY, scope TEXT, type TEXT, name TEXT,
                    aliases TEXT, attrs TEXT, created_at REAL, embedding BLOB);
CREATE TABLE edges (id TEXT PRIMARY KEY, scope TEXT, src TEXT, relation TEXT,
                    dst TEXT, confidence REAL, source_id TEXT, sentence TEXT,
                    created_at REAL, valid_to REAL);
CREATE INDEX edges_src ON edges(scope, src, relation);
CREATE INDEX edges_dst ON edges(scope, dst, relation);
```

Both indexes matter: traversal goes in both directions, and a graph that can
only be walked forwards answers half the questions.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # relation vocabulary, max_hops, fanout, decay
    ├── graph.py         # Node, Edge, Path
    ├── store.py         # SQLite: nodes, edges, merges; both-direction indexes
    ├── extractor.py     # text -> triples against the closed vocabulary
    ├── resolver.py      # the identity ladder + merge log
    ├── traverse.py      # seed, budgeted BFS, path scoring
    ├── render.py        # subgraph -> sentences with the path shown
    └── cli.py           # ingest / node / neighbours / path / ask / stats
```

### `graph.py`

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field


def node_id(scope: str, type_: str, name: str) -> str:
    payload = f"{scope}\x1f{type_}\x1f{name.strip().lower()}".encode()
    return "n_" + hashlib.blake2b(payload, digest_size=8).hexdigest()


@dataclass
class Node:
    name: str
    type: str = "thing"
    scope: str = "default"
    aliases: list[str] = field(default_factory=list)
    attrs: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            self.id = node_id(self.scope, self.type, self.name)


@dataclass
class Edge:
    src: str
    relation: str
    dst: str
    scope: str = "default"
    confidence: float = 0.8
    source_id: str | None = None        # the record this came from
    sentence: str = ""                  # the text that produced it
    created_at: float = field(default_factory=time.time)
    valid_to: float | None = None
    id: str = ""

    def __post_init__(self):
        if not self.id:
            payload = f"{self.scope}\x1f{self.src}\x1f{self.relation}\x1f{self.dst}".encode()
            self.id = "e_" + hashlib.blake2b(payload, digest_size=8).hexdigest()


@dataclass
class Path:
    nodes: list[Node]
    edges: list[Edge]
    score: float

    def render(self) -> str:
        parts = [self.nodes[0].name]
        for edge, node in zip(self.edges, self.nodes[1:]):
            parts.append(f" -[{edge.relation}]-> {node.name}")
        return "".join(parts)
```

### `extractor.py`

```python
"""Text -> triples, against a closed vocabulary, with an honest escape."""

from __future__ import annotations

import json

from .graph import Edge, Node

EXTRACT_PROMPT = """Extract entity relationships from the text.

Use ONLY these relations: {relations}
If a relationship does not fit any of them, use "OTHER" and put the actual
phrase in "raw_relation". Do not invent new relation names.

Rules:
- Only relationships stated in the text. Do not infer, do not use world knowledge.
- Use the most specific name for each entity as it appears in the text.
- Give each entity a type from: {types}
- Empty array if the text states no relationships.

Reply as a JSON array:
  [{{"src": "...", "src_type": "...", "relation": "...", "raw_relation": null,
     "dst": "...", "dst_type": "...", "sentence": "<the sentence it came from>",
     "confidence": 0.0-1.0}}]

Text:
{text}"""


class TripleExtractor:
    def __init__(self, client, *, relations: list[str], types: list[str], scope: str = "default"):
        self.client, self.relations, self.types, self.scope = client, relations, types, scope
        self.other_bucket: list[str] = []          # raw phrases that missed the vocabulary

    def extract(self, text: str, *, source_id: str | None = None):
        reply = self.client.complete([{"role": "user", "content": EXTRACT_PROMPT.format(
            relations=", ".join(self.relations), types=", ".join(self.types), text=text)}])
        raw = (reply.content or "").strip()
        raw = raw[raw.find("[") : raw.rfind("]") + 1] if "[" in raw else "[]"
        try:
            triples = json.loads(raw)
        except json.JSONDecodeError:
            return [], []
        if not isinstance(triples, list):
            return [], []

        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        for triple in triples:
            if not isinstance(triple, dict) or not all(triple.get(k) for k in ("src", "dst", "relation")):
                continue
            relation = triple["relation"]
            if relation not in self.relations and relation != "OTHER":
                # Model invented a relation. Keep the edge, bucket the phrase.
                self.other_bucket.append(relation)
                triple["raw_relation"], relation = relation, "OTHER"
            elif relation == "OTHER":
                self.other_bucket.append(triple.get("raw_relation") or "?")

            src = Node(name=triple["src"], type=triple.get("src_type", "thing"), scope=self.scope)
            dst = Node(name=triple["dst"], type=triple.get("dst_type", "thing"), scope=self.scope)
            nodes[src.id], nodes[dst.id] = src, dst
            edges.append(Edge(
                src=src.id, relation=relation, dst=dst.id, scope=self.scope,
                confidence=float(triple.get("confidence", 0.8)),
                source_id=source_id, sentence=triple.get("sentence", "")[:500],
            ))
        return list(nodes.values()), edges
```

### `resolver.py`

```python
"""The identity ladder. Biased towards creating rather than merging."""

from __future__ import annotations

import re

CONFIRM_PROMPT = """Are these two mentions the same real-world entity?
Answer exactly YES or NO. Answer NO if you are not sure.

A: {a_name} ({a_type}) - known facts: {a_facts}
B: {b_name} ({b_type}) - known facts: {b_facts}"""


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", "", name.strip().lower()))


class Resolver:
    def __init__(self, store, client=None, *, aliases: dict[str, str] | None = None,
                 id_fields: tuple[str, ...] = ("email", "url", "ticket")):
        self.store, self.client = store, client
        self.aliases = {normalize(k): v for k, v in (aliases or {}).items()}
        self.id_fields = id_fields

    def resolve(self, node, *, allow_model: bool = False):
        """Returns the node to use: an existing one, or `node` itself."""
        # 1. Exact identifier.
        for field_ in self.id_fields:
            value = node.attrs.get(field_)
            if value:
                existing = self.store.by_attr(field_, value, scope=node.scope)
                if existing:
                    return self._merge(existing, node, "identifier", f"{field_}={value}")

        # 2. Exact normalized name, same type, same scope.
        existing = self.store.by_name(normalize(node.name), type_=node.type, scope=node.scope)
        if existing:
            return self._merge(existing, node, "exact-name", normalize(node.name))

        # 3. Explicit alias table.
        target = self.aliases.get(normalize(node.name))
        if target:
            existing = self.store.by_name(normalize(target), type_=node.type, scope=node.scope)
            if existing:
                return self._merge(existing, node, "alias", f"{node.name} -> {target}")

        # 4. Model confirmation, only for a same-type near-name candidate.
        if allow_model and self.client is not None:
            candidate = self.store.nearest_name(node.name, type_=node.type, scope=node.scope)
            if candidate and self._confirm(candidate, node):
                return self._merge(candidate, node, "model-confirmed", node.name)

        # 5. Default: a new node. Fragmentation is recoverable; collapse is not.
        self.store.put_node(node)
        return node

    def _confirm(self, a, b) -> bool:
        reply = self.client.complete([{"role": "user", "content": CONFIRM_PROMPT.format(
            a_name=a.name, a_type=a.type, a_facts=self.store.facts_about(a.id)[:3],
            b_name=b.name, b_type=b.type, b_facts=[])}])
        return (reply.content or "").strip().upper().startswith("YES")

    def _merge(self, kept, merged, strategy: str, evidence: str):
        if kept.id == merged.id:
            return kept
        if normalize(merged.name) not in {normalize(a) for a in kept.aliases}:
            kept.aliases.append(merged.name)
            self.store.put_node(kept)
        self.store.log_merge(kept.id, merged.id, strategy, evidence)
        return kept
```

### `traverse.py`

```python
"""Seed, then a budgeted breadth-first walk. Hub nodes are the thing to fear."""

from __future__ import annotations

from collections import deque

from .graph import Path


def traverse(store, seed_ids: list[str], *, scope: str = "default", max_hops: int = 2,
             max_nodes: int = 60, max_fanout: int = 20, hub_degree: int = 200,
             decay: float = 0.6) -> list[Path]:
    seen: set[str] = set(seed_ids)
    paths: list[Path] = []
    queue = deque(
        (node_id, [store.get_node(node_id)], [], 1.0) for node_id in seed_ids
        if store.get_node(node_id)
    )

    while queue and len(seen) < max_nodes:
        current, nodes, edges, score = queue.popleft()
        if len(edges) >= max_hops:
            continue

        outgoing = store.edges_of(current, scope=scope, limit=max_fanout)
        for edge in outgoing:
            other_id = edge.dst if edge.src == current else edge.src
            if other_id in {n.id for n in nodes}:          # no cycles in one path
                continue
            # A hub reached indirectly floods everything downstream of it.
            if store.degree(other_id) > hub_degree and len(edges) > 0:
                continue

            other = store.get_node(other_id)
            if other is None:
                continue                                    # dangling edge; see failure modes
            new_score = score * edge.confidence * (decay if edges else 1.0)
            path = Path(nodes=[*nodes, other], edges=[*edges, edge], score=new_score)
            paths.append(path)
            seen.add(other_id)
            queue.append((other_id, path.nodes, path.edges, new_score))

    paths.sort(key=lambda p: p.score, reverse=True)
    return paths
```

### `render.py`

```python
def render(paths, *, limit: int = 15) -> str:
    if not paths:
        return ""
    lines = ["Related information from the knowledge graph "
             "(derived from stored text; verify before relying on it):"]
    for path in paths[:limit]:
        sentence = path.edges[-1].sentence
        lines.append(f"- {path.render()}   [score {path.score:.2f}]")
        if sentence:
            lines.append(f"    source: \"{sentence[:160]}\"")
    return "\n".join(lines)
```

Rendering the **path**, not just the endpoint, is what makes graph memory
debuggable and what makes the model's answer checkable. "Anna works on Herdr,
which depends on the gateway, which Arttu owns" is auditable; "Anna is related
to Arttu" is not.

### `cli.py`

- `<command> ingest <file|->` — extract and store, printing new nodes, new
  edges, merges, and the `OTHER` bucket.
- `<command> node <name>` — the node, its aliases, its degree, its edges.
- `<command> neighbours <name> [--hops 2]` — the subgraph, rendered as paths.
- `<command> path <a> <b>` — shortest scored path between two nodes. This is
  the command that demonstrates why you built a graph.
- `<command> stats` — node and edge counts, degree distribution, the `OTHER`
  share, the top hub nodes. Run this after every ingest: a sudden hub or a
  growing `OTHER` share is the early warning for both failure modes.

## Failure modes

- **Node collapse.** Two entities merged into one. Every edge on both is now
  wrong, and the traversal output is confident nonsense. This is the failure to
  design against: keep the merge log, prefer new nodes, and never merge on
  embedding similarity alone.

- **Entity fragmentation.** Three nodes for one person, none connected, so
  traversal finds nothing. Visible in `stats` as a node count far above the
  number of real entities. Fix with aliases, not with looser matching.

- **Hub explosion.** One node with 5,000 edges — "the company", a shared
  library — turns every two-hop traversal into a scan of the whole graph. Cap
  fan-out, skip high-degree nodes reached indirectly, and check the degree
  distribution in `stats`.

- **Dangling edges.** A node deleted, its edges left behind. Traversal then hits
  `None` and either crashes or silently drops paths. Cascade deletes, and have
  the traversal tolerate a missing node rather than assuming referential
  integrity.

- **Vocabulary drift.** Six synonymous relations for one concept because
  extraction was open. Traversal misses most of the graph. The closed list plus
  a monitored `OTHER` bucket is the fix; enforce it at extraction, not in review.

- **Inferred edges stored as observed.** The model connects two entities because
  it knows about them from pretraining, not because your text said so. "Only
  relationships stated in the text" in the prompt, and a low confidence on
  anything whose `sentence` field is empty.

- **Three-hop defaults.** In a connected domain, three hops reaches almost
  everything and the result is noise with a path attached. Default to two and
  make the user ask for more.

## Required tests

All offline. See `references/evaluation.md` for the universal set; these are
mandatory:

```python
def test_two_hops_reach_what_one_cannot(store):
    build_chain(store, "anna", "works_on", "herdr", "depends_on", "gateway")
    one = traverse(store, [node_id_for("anna")], max_hops=1)
    two = traverse(store, [node_id_for("anna")], max_hops=2)
    assert "gateway" not in rendered(one)
    assert "gateway" in rendered(two)


def test_aliases_resolve_to_one_node(resolver, store):
    resolver.aliases = {"@arttuan": "Arttu Antikainen"}
    store.put_node(Node(name="Arttu Antikainen", type="person"))
    resolved = resolver.resolve(Node(name="@ArttuAn", type="person"))
    assert resolved.name == "Arttu Antikainen"


def test_similar_names_are_not_merged(resolver, store):
    store.put_node(Node(name="Arttu Antikainen", type="person"))
    resolved = resolver.resolve(Node(name="Anna Antikainen", type="person"))
    assert resolved.id != node_id("default", "person", "Arttu Antikainen")
    assert len(list(store.all_nodes())) == 2


def test_model_says_no_when_unsure(resolver):
    resolver.client = FakeChat(["NO"])
    resolved = resolver.resolve(Node(name="A. Antikainen", type="person"), allow_model=True)
    assert resolved.name == "A. Antikainen"      # not merged


def test_every_merge_is_logged(resolver, store):
    ...
    assert store.merges()[-1]["strategy"] == "alias"


def test_hub_node_does_not_flood_the_result(store):
    make_hub(store, "python", edges=3000)
    paths = traverse(store, [node_id_for("anna")], max_hops=2, max_nodes=60)
    assert len(paths) <= 60
    assert sum(1 for p in paths if "python" in p.render()) < 10


def test_dangling_edge_does_not_crash_traversal(store):
    store.put_edge(Edge(src=node_id_for("anna"), relation="owns", dst="n_missing"))
    assert traverse(store, [node_id_for("anna")], max_hops=2) is not None


def test_deleting_a_node_removes_its_edges(store):
    store.delete_node(node_id_for("herdr"))
    assert not [e for e in store.all_edges()
                if node_id_for("herdr") in (e.src, e.dst)]


def test_unknown_relation_goes_to_other_not_dropped(extractor):
    extractor.client = FakeChat(['[{"src":"a","relation":"befriended","dst":"b"}]'])
    _, edges = extractor.extract("a befriended b")
    assert edges[0].relation == "OTHER"
    assert "befriended" in extractor.other_bucket


def test_path_score_decays_with_distance(store):
    build_chain(store, "a", "r", "b", "r", "c")
    paths = traverse(store, [node_id_for("a")], max_hops=2)
    one_hop = next(p for p in paths if len(p.edges) == 1)
    two_hop = next(p for p in paths if len(p.edges) == 2)
    assert two_hop.score < one_hop.score


def test_empty_graph_renders_nothing(store):
    assert render(traverse(store, [], max_hops=2)) == ""
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, ingest a three-sentence fixture and
   run `neighbours`.
2. **Tier 1** — `pytest -q`, offline. Every test above.
3. **Tier 2** — a probe suite with at least 8 genuinely multi-hop questions.
   **Run each one against a plain top-k store as the control.** If top-k answers
   them too, you did not need the graph, and that is the most useful finding
   this suite can produce. Report recall for both.
4. **Tier 3** — ingest a real document and read `stats`. Check the node count
   against the number of real entities you expect (fragmentation), the top-5
   degrees (hubs), and the `OTHER` share (vocabulary fit). Then spot-check five
   extracted edges against their `sentence` field: any edge whose sentence does
   not state the relationship is an inferred edge, and inferred edges compound.

Then tell the user what actually ran, including the graph-versus-top-k
comparison. If top-k matched the graph on the multi-hop probes, say so plainly —
that is a result worth acting on, and it usually means the simpler system wins.
