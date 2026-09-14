# Retrieval quality

A memory system fails quietly. It returns five records for every query,
plausible-looking, and the model writes a confident answer around whichever
ones it got. Nothing throws. This file is the set of mechanics that separate a
store that recalls from a store that merely responds.

## Cosine alone is a weak ranker

Pure vector similarity has three structural blind spots in agent memory:

1. **No recency.** A preference the user stated in January outranks the one
   that overrode it in June, because both are equally "about" the topic.
2. **No importance.** "The user's name is Arttu" and "the user mentioned the
   weather" embed as equally retrievable.
3. **Near-duplicates fill the window.** Five phrasings of one fact occupy all
   five slots, and the second fact the answer needed never surfaces.

The fix is a composite score and a diversity pass. Neither needs a model call.

## The composite score

```python
import math
import time


def score(hit_similarity: float, record, *, now: float | None = None,
          half_life_days: float = 30.0) -> tuple[float, str]:
    """Similarity, decayed by age, lifted by importance and access count."""
    now = now or time.time()
    age_days = max(0.0, (now - record.created_at) / 86400.0)
    recency = 0.5 ** (age_days / half_life_days)

    importance = float(record.metadata.get("importance", 0.5))   # 0..1, set at write time
    uses = int(record.metadata.get("access_count", 0))
    reinforcement = math.log1p(uses) / 4.0                       # saturating, caps ~0.5

    total = (0.55 * hit_similarity) + (0.20 * recency) + (0.15 * importance) + (0.10 * reinforcement)
    why = (f"cos={hit_similarity:.2f} rec={recency:.2f} "
           f"imp={importance:.2f} use={reinforcement:.2f}")
    return total, why
```

Three things to hold on to:

- **The weights are a starting point, not a result.** Tune them against a probe
  suite (`evaluation.md`), never against the one query you happened to try.
- **`half_life_days` is domain-specific.** A coding agent's memory of a file
  layout decays in days. A user's dietary restriction does not decay at all —
  give records a `half_life` in metadata and let permanent facts opt out with
  `float("inf")`.
- **Reinforcement is a feedback loop.** Records that get retrieved get
  retrieved more. That is the desired behaviour for genuinely useful memories
  and a trap for one early mistake that keeps resurfacing. Cap it (the `log1p`
  above saturates), and let consolidation demote records that were retrieved but
  did not end up cited.

## Maximal marginal relevance

Stop the near-duplicate pile-up. Select greedily, penalizing similarity to what
you have already chosen:

```python
def mmr(candidates, *, k: int = 5, lambda_: float = 0.7, similarity):
    """candidates: list[Hit] sorted by score. similarity(a, b) -> float."""
    selected: list = []
    pool = list(candidates)
    while pool and len(selected) < k:
        best, best_value = None, -1e9
        for hit in pool:
            redundancy = max(
                (similarity(hit.record, chosen.record) for chosen in selected),
                default=0.0,
            )
            value = lambda_ * hit.score - (1 - lambda_) * redundancy
            if value > best_value:
                best, best_value = hit, value
        selected.append(best)
        pool.remove(best)
    return selected
```

`lambda_=0.7` is a reasonable default: strongly relevance-driven, but a
candidate that repeats an already-selected record has to be substantially
better to win a slot. At `1.0` you have plain top-k back.

## Query rewriting is the highest-leverage fix

The single most common retrieval bug is embedding the raw user turn. "What
about the other one?" embeds to nothing useful. Before searching, resolve the
query against the recent transcript:

```python
REWRITE = """Rewrite the user's message as a standalone search query.
Resolve pronouns and references using the conversation. Keep it short.
If the message needs no memory lookup at all, reply exactly: SKIP

Conversation:
{history}

Message: {message}
Query:"""
```

Two payoffs. Pronouns get resolved, and — more valuable — `SKIP` gives the
agent a way to *not* retrieve. An agent that searches memory on every turn
injects noise into turns that needed none, and pays for it in both tokens and
accuracy.

Cache rewrites by `(message, history_tail)` hash. In a chat loop the same
rewrite recurs constantly.

## Hybrid: lexical plus vector

Embeddings lose exactly the things agent memory most needs to match verbatim:
identifiers, error codes, file paths, version numbers, names. `ORA-01555` and
`ORA-01843` are near-identical vectors and completely different problems.

SQLite ships FTS5 in the standard build. Run both and fuse with reciprocal rank:

```python
def rrf(*ranked_lists, k: int = 60):
    """Reciprocal rank fusion. Rank-based, so scores from different
    scales never need normalizing — the main reason to prefer it."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, hit in enumerate(ranked, start=1):
            scores[hit.record.id] = scores.get(hit.record.id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
```

If you add one thing to a plain cosine store, add this. It is roughly thirty
lines and it fixes the failure mode users notice first.

## Chunking: the boring decisions that dominate

- **Chunk on semantic boundaries, not character counts.** Paragraphs, list
  items, one fact per record. A fact split across two chunks is retrievable by
  neither.
- **Overlap 10–15%** when chunking prose. Zero overlap loses anything spanning a
  boundary; heavy overlap inflates the store and worsens the duplicate problem.
- **Store the chunk, return the neighbourhood.** Embed a tight chunk for
  precision, then hand the model that chunk plus its adjacent siblings for
  context. Retrieval precision and generation context want different sizes.
- **Keep the parent pointer.** Every chunk should name its source document, its
  position, and its ingest time. Without that you cannot cite, cannot
  invalidate a whole document, and cannot debug a bad hit.

## Inject retrieved memory honestly

How memory enters the prompt determines how the model treats it:

```python
def render(hits) -> str:
    if not hits:
        return ""
    lines = ["Relevant memories (may be outdated; prefer the conversation if they conflict):"]
    for hit in hits:
        stamp = time.strftime("%Y-%m-%d", time.localtime(hit.record.created_at))
        lines.append(f"- [{stamp}] {hit.record.text}")
    return "\n".join(lines)
```

- **Timestamp every memory.** Without dates the model cannot reason about
  staleness, and will assert a superseded fact in the present tense.
- **Say they may be wrong.** Retrieved text presented as ground truth turns a
  retrieval miss into a confident false claim.
- **Never inject on an empty result.** A header with nothing under it invites
  the model to invent entries beneath it.
- **Retrieved memory is data, not instructions.** If a user can write into the
  store, then "ignore previous instructions" can be written into the store.
  Keep memory in a user-role block or a clearly fenced section — never
  concatenated into the system prompt as if you had authored it.

## What to measure

Ranking changes must be justified by numbers, not by the one query you tried.
See `evaluation.md` for the harness. The minimum:

| Metric | Question it answers |
| --- | --- |
| recall@k | Is the needed record in the top k at all? |
| MRR | How far down is it, on average? |
| Distinct@k | How many of the k slots carry distinct information? |
| Staleness rate | Share of returned facts that a newer record supersedes |
| Null rate | Share of queries where retrieving nothing was correct, and you did |
