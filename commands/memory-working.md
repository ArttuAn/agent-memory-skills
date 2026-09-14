# Build Working Memory (context budget + compaction)

Given the user's spec, build a working-memory layer: token budgeting, pinned
turns, compaction, and eviction for an agent's transcript. Ask for the shape if
they did not give one (model + window size, session length, whether tool results
are large, whether there is a task statement that must never be evicted).

**First, check whether it is needed.** If transcripts stay under ~30% of the
context window, compaction is pure loss. Measure `tokens_used / window` and say
so rather than building.

## What to build

Scaffold a `src/`-layout project with `pyproject.toml` into the requested
directory. `openai` + stdlib only.

```
<project>/
├── pyproject.toml
├── .env.example
└── src/<package>/
    ├── config.py      # window, budget ratio, pin rules, thresholds
    ├── budget.py      # token estimation for messages, tool schemas, system
    ├── policy.py      # pinned turns; next compaction span; turn boundaries
    ├── compactor.py   # span -> one summary message, with a NOTHING escape
    ├── window.py      # ManagedWindow: append() and prepare()
    └── cli.py         # inspect / simulate / demo
```

## Core pieces

1. **Budget** (`budget.py`): estimate at `len(text)/4`; do not add `tiktoken`.
   Set the limit to **70% of the real window** minus a completion reserve. Count
   the system prompt, the messages, **and the tool schemas** — a dozen tools is
   ~2,000 tokens on every call and is where budgets vanish.
2. **Pins** (`policy.py`): never evict the system prompt, the first user turn
   (the task), the current plan, the last 4 turns, or explicit corrections. The
   first-user-turn pin is the one that prevents "the agent forgot the task".
3. **Turn boundaries**: an assistant message with `tool_calls` must stay with
   its tool results. Walk the span end backwards until cutting there orphans
   nothing, or the provider rejects the request.
4. **Compaction** (`compactor.py`): the summary prompt must ask for decisions,
   verbatim facts, what failed, and user corrections — and must allow the model
   to reply `NOTHING`. Insert the summary as `role="user"`, not `system`; it is
   a record of what happened, not your instruction. Archive the original span to
   JSONL and put the path in the marker.
5. **Thresholds**: truncate tool results at write time (head 60% + tail 40% with
   a `[N chars dropped]` marker); compact above 70% pressure; hard-evict FIFO
   above 90%; refuse a single message that exceeds the budget alone rather than
   silently cutting the user's input.
6. **`prepare()`** returns the list to send, so bolting this onto an existing
   loop is a one-line change: `client.complete(window.prepare(), tools=...)`.

## Conventions

- Log `pressure` every turn. Without it you learn about the budget from a 400.
- Leave a visible marker where turns were compacted, with the archive path.
- Mark compacted messages; prefer evicting an old summary over re-compacting it.
- Require a minimum span (2+ turns) and a minimum gain (0.05 pressure) per pass,
  or compaction thrashes.

## Verify before finishing

1. `pip install -e ".[dev]"`, import, `--help`.
2. `pytest -q` — pinned task survives heavy pressure; pressure strictly
   decreases after compaction; tool calls are never orphaned; oversized single
   message is refused; all-pinned stops cleanly.
3. `<command> simulate --turns 500` — pressure never exceeds the evict
   threshold, compaction fires at a steady cadence, the task is still present.
4. One real session long enough to trigger a compaction. **Read the summary.**
   If it is prose about how the conversation went rather than decisions and
   verbatim facts, fix the prompt — that summary is all the model will have.

Report the measured pressure curve. If there was no key and the real run was
skipped, say so: the compaction prompt is the one part offline tests cannot judge.
