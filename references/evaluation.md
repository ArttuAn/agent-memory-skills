# Verification contract

"It imports and `--help` renders" proves the packaging works. It says nothing
about whether the store recalls the fact it was given, notices that the fact
changed, or stops growing. A generated memory system is not done until it
passes all three tiers below.

Tiers 0 and 1 need **no API key and no network**. That is the point: they run in
CI, on a plane, and in the seconds after the agent finishes writing code.

## Tier 0 — it is real code

```bash
uv pip install -e ".[dev]"     # or: pip install -e ".[dev]"
python -c "import <package>"   # imports clean
<command> --help               # CLI renders
<command> remember "the sky is blue" && <command> recall "sky"
```

Catches: bad packaging, a missing `__init__.py`, an entry point pointing at a
function that does not exist, a schema that does not create on first run. The
last line is the one that matters — it is a full write-then-read round trip
with the `hash` embedder, so it needs nothing configured.

## Tier 1 — the memory behaves (offline, `HashEmbedder` + `FakeChat`)

This is the tier that matters and the one that is usually missing. Every test is
deterministic and runs in milliseconds. See `store-seam.md` for the fakes.

### The universal set — every skill in this repo

| Test | Asserts |
| --- | --- |
| `test_write_then_read_round_trips` | What went in comes back out, metadata intact |
| `test_put_is_idempotent` | Same content twice → one record, not two |
| `test_search_respects_k` | Never returns more than `k` |
| `test_empty_store_returns_nothing` | No hits, no crash, no injected header |
| `test_scope_isolation` | Scope A never sees scope B (`privacy.md`) |
| `test_delete_removes_from_search` | Deleted records stop being retrievable |
| `test_reopen_preserves_records` | Close, reopen the file, everything is still there |
| `test_dimension_mismatch_is_refused` | Reopening with a different embedder raises, not returns 0.0 |

### The per-type set

| Skill | Must also prove |
| --- | --- |
| **working** | Compaction triggers at the threshold; pinned turns survive; token count strictly decreases; a compaction marker is left in the transcript |
| **episodic** | Recency changes ranking with similarity held constant; episodes are append-only; a failed episode is retrievable by outcome |
| **semantic** | A contradicting fact supersedes rather than duplicates; extraction rejects non-facts; confidence is carried through |
| **procedural** | A playbook matches only when its preconditions hold; a failed replay demotes it; step ordering survives a round trip |
| **graph** | Two-hop traversal reaches what one-hop cannot; entity resolution merges aliases; deleting a node removes its edges |
| **temporal** | `as_of(t)` returns what was believed at `t`, not what is believed now; invalidation never deletes; open intervals close on supersession |
| **consolidation** | Merging N near-duplicates yields 1 and preserves every source ID in lineage; a dry run writes nothing; the pass is re-runnable after a crash |
| **scratchpad** | Concurrent writers do not interleave; a malformed file is quarantined, not silently reset; the diff between turns is inspectable |
| **shared** | An expired lease is reclaimed; two writers to one key produce a detected conflict; readers never see a partial write |
| **eval** | The harness fails when memory is deliberately broken (see below) |

### The negative control

The most important test in a memory repo, and the one nobody writes:

```python
def test_probe_suite_fails_on_a_broken_store():
    """A suite that passes against a store that recalls nothing is measuring nothing."""
    results = run_probes(PROBES, store=NullStore())
    assert results.recall_at_k < 0.2
```

If your probe suite still passes when you swap in a store that returns an empty
list for every query, the probes are being answered by the model's own priors
and you have learned nothing about your memory system.

## Tier 2 — the metrics, not the assertions

Tests prove mechanics. Metrics prove quality, and they need a probe suite: a
list of `(setup_records, query, expected_ids)` fixtures that encode what this
system is supposed to recall.

```python
@dataclass
class Probe:
    name: str
    given: list[Record]        # written before the query
    query: str
    expect: list[str]          # record ids that must appear
    forbid: list[str] = field(default_factory=list)   # must NOT appear (stale, other-scope)
```

Report these five, per run, to a file you can diff across commits:

| Metric | Definition | A number to beat |
| --- | --- | --- |
| **recall@k** | Share of probes where every `expect` id is in the top k | > 0.85 at k=5 |
| **MRR** | Mean of 1/rank of the first expected hit | > 0.7 |
| **precision@k** | Share of returned records that were expected | context-dependent; track the trend |
| **staleness** | Share of returned facts superseded by a newer record | < 0.05 |
| **leak rate** | Any `forbid` id returned | **0. Not a threshold — a gate.** |

Thirty probes hand-written from real usage beat three hundred generated ones.
The generated probes test the generator.

**Track the numbers over time.** A single run tells you almost nothing; the
value is the diff. Write `eval-results.json` into the repo and fail CI on a
regression beyond a tolerance, the same way you would for a performance budget.

## Tier 3 — one real round trip

Exactly one end-to-end run against a live endpoint and a real embedding model,
to prove the adapter speaks the provider's dialect and the dimensions line up:

```bash
EMBEDDING_BACKEND=ollama <command> remember "<a fact from the spec>"
EMBEDDING_BACKEND=ollama <command> recall "<a paraphrase of that fact>" -v
```

The paraphrase matters. Recalling by the exact words proves string matching;
recalling by a paraphrase proves the embeddings are doing their job. With the
`hash` backend this test is *expected* to fail, which is a useful demonstration
of what the offline tiers do and do not cover.

Check by eye: the right memory came back, the score breakdown in `why` is
sensible, and nothing from another scope appeared.

If the endpoint is local, confirm the model is actually resident:

```bash
ollama ps          # loaded, and at what context size?
free -g            # headroom, or about to be OOM-killed?
```

## The long-run check

Some memory failures only appear over time and none of the tiers above will
catch them. Once, before calling it done, simulate a few hundred sessions:

```bash
<command> simulate --sessions 200 --seed 7
```

Then look at three numbers:

- **Store growth** — linear in sessions is expected early; if it is still linear
  after consolidation runs, consolidation is not working.
- **Recall@5 on the probe suite, before and after.** It should hold. If it drops
  as the store grows, your ranker is being outvoted by volume and you need MMR,
  decay, or both.
- **Oldest surviving record.** If it equals your retention window, you have a
  rolling buffer, not a memory.

## What to tell the user when you finish

Report what actually ran, not what you intended to run. State the numbers: how
many tests, which tiers, the recall@k on how many probes, and the growth figure
from the simulation. If Tier 3 was skipped because there was no key, say so
explicitly rather than implying an end-to-end run happened — and say which
numbers came from `HashEmbedder`, because those numbers do not transfer.
