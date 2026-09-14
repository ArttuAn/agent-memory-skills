# Choosing a memory

Start here before invoking a skill. The most expensive mistake in this repo is
building a knowledge graph for a problem that wanted a 40-line summarizer.

## Default: you probably want working memory plus a scratchpad

Most agents that "need memory" need two unglamorous things:

1. A **context budget** that compacts the transcript instead of letting it
   overflow (`memory-working`), and
2. A **file the agent writes notes into** that survives the turn
   (`memory-scratchpad`).

Together that is a few hundred lines, no embeddings, no database, and it solves
the complaint that produced the request — "it forgets what we decided" — in the
overwhelming majority of cases. Every other skill here is that, plus specific
machinery, bought to solve a specific problem. Do not buy the machinery until
you have the problem.

## The map

| Symptom you actually have | Skill | What it adds |
| --- | --- | --- |
| The context window overflows mid-task; costs scale with turn count | **memory-working** | Token budget, compaction, eviction policy, pinned turns |
| "What did we do last Tuesday?" / the agent repeats a mistake it already made | **memory-episodic** | Timestamped episode log, recency-weighted retrieval |
| The agent re-asks things the user already told it | **memory-semantic** | Distilled fact store, entity-keyed, contradiction resolution |
| The agent re-derives the same multi-step routine every run | **memory-procedural** | Learned playbooks, matched by precondition, replayed as steps |
| Questions are multi-hop: "who else worked on what she shipped?" | **memory-graph** | Entity–relation graph, traversal retrieval |
| Facts change and old answers must stay explicable | **memory-temporal** | Bitemporal validity, invalidation instead of deletion |
| The store grows forever; near-duplicates crowd out real recall | **memory-consolidation** | Background merge, promotion, decay, forgetting |
| The agent needs a durable notepad it can read and rewrite | **memory-scratchpad** | File-backed working set, diffable, human-editable |
| Several agents must see each other's findings | **memory-shared** | Namespaced blackboard, leases, conflict resolution |
| You cannot tell whether any of the above is working | **memory-eval** | Probe suites, recall@k, staleness, contradiction rate |

## When each one is the wrong choice

Worth reading before you commit a weekend.

**memory-working** is wrong when your transcripts never approach the window.
Under about 30% of the context budget, compaction is pure loss: you spend a
model call to throw away detail you were not paying for. Measure first; the
`tokens_used / window` ratio is one line.

**memory-episodic** is wrong when nothing depends on *when* something happened.
If recency never changes the answer, you have written a vector store with an
extra column. It is also wrong as a primary fact source — episodes record what
was said, including the things that were wrong at the time.

**memory-semantic** is wrong without an extraction gate you trust. An LLM asked
"what facts are in this turn?" will happily produce "the user is frustrated"
and store it forever. Every distilled fact is a claim your agent will later
assert with confidence. If you cannot articulate what does *not* qualify as a
fact, you are building a store of hallucinations with good recall.

**memory-procedural** is wrong when the task varies more than it repeats. A
playbook is a bet that the next instance looks like the last one; when it does
not, the agent follows a confidently wrong script instead of thinking. Needs
real repetition — the same shape of task a dozen-plus times — before it pays.

**memory-graph** is wrong for single-hop lookups, which is most lookups. The
cost is an extraction step that must decide entity identity ("Arttu", "the
author", "@ArttuAn" — one node or three?), and getting that wrong poisons every
traversal. Buy it when you have demonstrated multi-hop questions that top-k
retrieval provably fails, not when the diagram looks impressive.

**memory-temporal** is wrong when you can just overwrite. Bitemporal modelling
doubles the size of every query in your head. It earns its keep when you must
answer "why did the agent say that in March?" — audit, compliance, debugging a
production agent — and nowhere else.

**memory-consolidation** is wrong before you have a store worth consolidating.
Under a few thousand records, a nightly merge pass is a cron job that mostly
proves itself unnecessary. It is also the most dangerous skill here: it is the
only one that deletes, and a merge bug is silent, permanent, and discovered
weeks later.

**memory-scratchpad** is wrong for anything more than one agent writes to.
File-backed memory has no concurrency story beyond an advisory lock; two agents
and you want `memory-shared`.

**memory-shared** is wrong for fewer than three agents, and wrong whenever
workers could just return their results to a coordinator. A blackboard is
coordination infrastructure — it brings lease expiry, stale reads, and
write-write conflicts into a system that may not have needed any of them.

**memory-eval** is never wrong, and is the one people skip. A memory system
without probes is a system whose failures are invisible by construction: it
returns *something* for every query, and nothing in normal operation tells you
the something was the wrong thing.

## How they stack

The types are not alternatives. A mature memory system runs several tiers at
once, with different lifetimes:

```
turn      working memory      compacted every N turns, nothing survives the session
session   scratchpad          written by the agent, read next turn, diffable
days      episodic            append-only log of what happened
forever   semantic + graph    distilled, deduplicated, contradiction-resolved
          procedural          how to do the thing, learned from episodes
```

Consolidation is the pump between the tiers: episodes in, facts out. Temporal
is the discipline applied to the bottom tier so "forever" does not mean "wrong
forever".

## Combinations that work

- **working + scratchpad** — the default. Start here, add nothing else until it
  visibly fails.
- **episodic + consolidation → semantic** — the classic pipeline: log
  everything cheaply, distil on a schedule, query the distillate.
- **semantic + temporal** — facts that change (prices, roles, preferences) with
  the old value kept and marked superseded rather than destroyed.
- **procedural + eval** — the playbook's own test is whether replaying it
  succeeds; the eval harness is the only honest judge of that.
- **graph + semantic** — extract entities once, store facts keyed by entity,
  traverse edges when the question is multi-hop and fall back to top-k when it
  is not.

## Combinations that do not

- **graph + consolidation, unsupervised** — entity merges cascade. One bad
  "these two nodes are the same person" collapses two people permanently, and
  every edge on both now lies. Gate node merges behind a human or a
  high-precision rule; never behind a model's judgement alone.
- **procedural + self-modifying playbooks without eval** — a playbook that
  rewrites itself against a metric it also reports will optimize the metric.
  Same failure as a self-evolving harness with no external test suite.
- **every tier at once, on day one** — you will not be able to attribute a bad
  answer to a tier, which means you cannot fix it. Add one, measure with
  `memory-eval`, then add the next.
