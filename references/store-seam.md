# The store seam

Every skill in this repo writes against the same four-type vocabulary. That is
deliberate: it is what makes a semantic store and a graph store and a
scratchpad interchangeable behind one retriever, and it is what makes all of
them testable without a network, an API key, or a vector database.

Read this once. The skills assume it.

## The problem

The natural way to write agent memory is the untestable way:

```python
class Memory:
    def __init__(self):
        import chromadb
        self.db = chromadb.Client()          # constructed inside
        self.embed = OpenAI().embeddings     # network on every write

    def remember(self, text):
        self.db.add(documents=[text], embeddings=[self.embed(text)])
```

Now every test of your recall logic needs a network and a running vector DB,
and still cannot reproduce the cases that actually break memory systems: the
near-duplicate write, the stale fact that outranks the fresh one, the deletion
that leaves a dangling reference. So those paths go untested, which in practice
means they go unwritten.

## The four types

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Protocol


@dataclass
class Record:
    """One unit of memory. Every store in this repo holds these."""

    id: str
    kind: str                                   # "episode" | "fact" | "procedure" | "note" | "edge"
    text: str                                   # what a model will actually read
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    embedding: list[float] | None = None


@dataclass
class Hit:
    """A retrieval result, with its reasoning attached."""

    record: Record
    score: float
    why: str                                    # "cosine=0.81" | "recency" | "graph hop 2 via works_at"


class MemoryStore(Protocol):
    def put(self, record: Record) -> str: ...
    def get(self, record_id: str) -> Record | None: ...
    def delete(self, record_id: str) -> bool: ...
    def all(self, *, kind: str | None = None) -> Iterator[Record]: ...
    def search(
        self,
        query: str,
        *,
        k: int = 5,
        kind: str | None = None,
        where: dict[str, Any] | None = None,
    ) -> list[Hit]: ...


class Embedder(Protocol):
    dimensions: int

    def embed(self, texts: Iterable[str]) -> list[list[float]]: ...
```

`why` is not decoration. When a memory system returns the wrong thing — and it
will — `why` is the difference between a five-minute fix and an afternoon of
print statements. Carry it through every ranker, every fusion step, every
rerank. It costs one string.

## The offline embedder

Ship this in every project. It is deterministic, dependency-free, and makes the
entire test suite run in milliseconds.

```python
import hashlib
import math
import re


class HashEmbedder:
    """Deterministic bag-of-words hashing. No network, no model, no drift.

    Not semantically good — "car" and "automobile" land nowhere near each other.
    That is fine: tests assert on ranking *mechanics*, not on synonym quality.
    """

    def __init__(self, dimensions: int = 512) -> None:
        self.dimensions = dimensions

    def embed(self, texts):
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector] if norm else vector
```

Select it with an env var (`EMBEDDING_BACKEND=hash|ollama|openai`) and default
to `hash` when no API key is present, so a fresh clone works before the reader
has configured anything.

> **Never mix backends against one persisted store.** `hash` is 512-dim,
> `nomic-embed-text` is 768, `text-embedding-3-small` is 1536. Cosine across
> mismatched dimensions is not an error — it silently returns `0.0`, and your
> memory system quietly stops recalling anything. Stamp the backend and the
> dimension into the store on creation and refuse to open it with a different
> one.

## Persistence: SQLite, and why not a vector DB

Every skill here persists to a single SQLite file, with embeddings as raw
`float32` bytes and cosine hand-rolled over Python lists.

```python
from array import array


def vec_to_blob(vector) -> bytes:
    return array("f", [float(x) for x in vector]).tobytes()


def blob_to_vec(blob) -> list[float]:
    if not blob:
        return []
    out = array("f")
    out.frombytes(blob)
    return list(out)


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
```

A brute-force scan over 50k records takes tens of milliseconds in pure Python.
Most agent memory never gets that big; the ones that do have an operations team.
Until then a vector DB buys you an index you do not need and costs you a
process to run, a schema you do not control, and a backup story. When you
outgrow the scan, swap the `search` method — the `MemoryStore` protocol does not
change, which is the entire reason it exists.

SQLite also gives you two things the vector DBs make awkward and agent memory
needs constantly: **arbitrary metadata filters** in the same query as the
similarity scan, and **transactional writes** so a consolidation pass that dies
halfway does not leave half a merge behind.

## The fake store

For testing anything that *consumes* memory rather than implements it:

```python
class FakeStore:
    """In-memory MemoryStore. Substring search, insertion order, no embeddings."""

    def __init__(self, records=()):
        self.records = {r.id: r for r in records}
        self.searches: list[tuple[str, int]] = []

    def put(self, record):
        self.records[record.id] = record
        return record.id

    def get(self, record_id):
        return self.records.get(record_id)

    def delete(self, record_id):
        return self.records.pop(record_id, None) is not None

    def all(self, *, kind=None):
        return (r for r in self.records.values() if kind is None or r.kind == kind)

    def search(self, query, *, k=5, kind=None, where=None):
        self.searches.append((query, k))
        hits = [
            Hit(record=r, score=1.0, why="fake substring")
            for r in self.all(kind=kind)
            if query.lower() in r.text.lower()
        ]
        return hits[:k]
```

`self.searches` is the point. Half the bugs in a memory-backed agent are not
"retrieval returned the wrong thing" — they are "retrieval was never called",
or "retrieval was called with the raw user turn instead of the rewritten
query". Recording the calls lets a test assert on that directly.

## Identity: content hashing, not autoincrement

Give records deterministic IDs derived from their content and scope:

```python
def record_id(kind: str, text: str, scope: str = "default") -> str:
    payload = f"{kind}\x1f{scope}\x1f{text.strip().lower()}".encode()
    return f"{kind}_{hashlib.blake2b(payload, digest_size=8).hexdigest()}"
```

This makes `put` idempotent for free: replaying the same conversation does not
double the store, a crashed consolidation pass can be re-run, and tests can
assert on specific IDs without fixture ordering. Autoincrement gives you none
of that and buys nothing in return.

Two caveats. Content hashing catches *exact* duplicates only — "Arttu lives in
Helsinki" and "Arttu is based in Helsinki" hash differently, which is what
`consolidation` exists to handle. And `scope` must be in the hash, or two users'
identical facts collide into one record; see `privacy.md`.
