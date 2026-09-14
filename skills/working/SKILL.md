---
name: memory-working
description: "Build the agent's short-term memory: token budgeting, compaction, eviction policy, and pinned turns that survive it"
---

# Working Memory

Working memory is the transcript the model actually sees on this call. It is
not storage — nothing here survives the session — it is a **budget**, and the
job of this skill is to spend it deliberately instead of discovering the limit
by hitting it.

Every agent has working memory. Most have it by accident: a `messages` list that
grows until the provider returns a context-length error, or until the bill
does. The difference between an accidental one and a designed one is four
decisions — what a turn costs, what the ceiling is, what goes first, and what
can never go.

```
  messages ──► account ──► under budget? ──► send
                 │              │
                 │              no
                 │              ▼
                 │         select victims   (never the pinned ones)
                 │              │
                 │              ▼
                 │         compact them into one summary turn
                 │              │
                 └──────────────┘   leave a visible marker where they were
```

## Use this when

- Sessions run long enough to approach the context window, or costs scale with
  turn count in a way you can feel.
- Tool results are large and unpredictable — logs, file dumps, API payloads.
  One `cat` of the wrong file ends a session that had nothing else wrong.
- The agent "forgets the task" partway through a long run. Usually this is not
  a memory problem at all: it is the task definition scrolling out of the
  window because nothing pinned it.

**Do not use this when** transcripts stay under roughly 30% of the window.
Compaction below that line is pure loss — you pay a model call to discard detail
you were not being charged for. Measure `tokens_used / window` first; it is one
line, and it frequently ends the conversation.

Working memory pairs with `memory-scratchpad` (the agent's durable notes) and
`memory-episodic` (what happened, kept after the session). Compaction discards;
those two are where anything worth keeping should already have been written.

## Workflow

When the user asks for working memory / context management / compaction:

1. **Ask for the shape if it is not given.** What model and window? Roughly how
   long do sessions run? Are tool results large? Is there a task statement that
   must never be evicted? If vague, assume a 128k window, a chat loop, and
   propose the pinned + compaction default below.

2. **Scaffold the project** into the directory the user names (default: a folder
   named after the project). Kebab-case folder, snake_case package, `hatchling`
   + `pyproject.toml`, `src/` layout.

3. **Write the four modules**: `budget.py` (accounting), `policy.py` (what goes),
   `compactor.py` (how it is summarized), `window.py` (the managed transcript
   the agent talks to). Wire them into the existing loop if there is one.

4. **Verify against the contract** in `references/evaluation.md`, plus the
   working-memory-specific tests below.

## The decisions that matter

Four decisions. Everything else in this skill is consequence.

### 1. How you count tokens

The choice is between exact and free, and it is not close.

- **`tiktoken`** — exact for OpenAI models, a dependency, and wrong for every
  other provider anyway.
- **`len(text) / 4`** — free, no dependency, and about 10-15% off for English
  prose. Worse for code and JSON, which are denser.

Take the estimate, and **set your budget to 70% of the real window**. The
headroom absorbs the estimation error, the response you have not generated yet,
and the tool schemas you forgot to count. An exact counter with no headroom
fails; an estimate with headroom does not. Do not spend a dependency here.

Count *everything*: the system prompt, the tool schemas (these are large and
invisible — a dozen tools is easily 2,000 tokens on every single call), the
messages, and a reservation for the completion.

### 2. What can never be evicted

The pin list is the highest-value twenty lines in this skill. Without it,
FIFO eviction eventually removes the system prompt or the task statement, and
the agent's subsequent confusion looks like a model failure.

Always pinned:

- The system prompt.
- The original task or user goal.
- The current plan, if there is one.
- The most recent N turns (N=4 is a reasonable floor) — the model needs local
  coherence more than it needs distant history.

Usually pinned:

- Explicit corrections. "No, use the staging database" is a tiny, cheap,
  load-bearing turn that pure-relevance eviction throws away first precisely
  because it is short and off-topic.

### 3. Evict or compact

**Eviction** drops turns. Free, instant, lossy.
**Compaction** summarizes them into one turn. Costs a model call and latency,
keeps the thread.

Use both, at different thresholds:

| Pressure | Action |
| --- | --- |
| Tool result larger than `max_tool_tokens` | Truncate immediately, at write time, head+tail with a byte count in the middle |
| Above 70% of budget | Compact the oldest unpinned span into a summary |
| Above 90% after compacting | Hard-evict oldest unpinned turns, FIFO |
| A single message exceeds the budget alone | Refuse it with a clear error; do not silently truncate a user's input |

The tool-result rule does most of the work. Large tool results are the single
biggest cause of context blowout, they are detectable at the moment they are
produced, and truncating them costs nothing.

### 4. What the compaction summary must preserve

A compaction prompt that says "summarize the conversation" produces prose the
agent cannot act on. The summary is not for a reader — it is the only surviving
record of that span, for the model, and it must be structured:

```python
COMPACT_PROMPT = """Compress this span of an agent transcript into a dense note.
Preserve, as explicit lines:
- Decisions made, and what they rule out
- Facts discovered (file paths, names, values, errors) — verbatim, not paraphrased
- What was tried and failed, and why
- Anything the user corrected

Drop: pleasantries, restatement of the task, reasoning that led nowhere,
tool calls whose results you have already captured above.

Write under {limit} words. No preamble. If the span contains nothing
worth preserving, reply exactly: NOTHING

Span:
{span}"""
```

`NOTHING` is not a nicety — a span of five failed searches genuinely has nothing
in it, and a summary that invents significance for it is worse than the empty
string.

**Never compact a span that is still open.** If the last message in the span is
an assistant turn with tool calls, the matching tool results are in the *next*
span, and compacting the call away leaves orphaned results the provider will
reject. Always cut on a complete turn boundary.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # window size, budget ratio, pin rules, thresholds
    ├── budget.py        # token accounting for messages, tools, system
    ├── policy.py        # which messages are pinned, which span goes next
    ├── compactor.py     # span → summary turn (LLM, with a NOTHING escape)
    ├── window.py        # ManagedWindow: the list the agent appends to
    └── cli.py           # inspect / simulate / demo
```

### `budget.py`

```python
"""Token accounting. Estimates, deliberately — see 'How you count tokens'."""

from __future__ import annotations

import json
from dataclasses import dataclass

CHARS_PER_TOKEN = 4.0


def estimate(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN) + 1)


def message_tokens(message: dict) -> int:
    """One message, including the role and tool-call envelope."""
    total = 4  # per-message overhead: role, delimiters
    total += estimate(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        total += estimate(call["function"]["name"])
        total += estimate(call["function"].get("arguments") or "")
        total += 8
    return total


def tools_tokens(tools: list[dict] | None) -> int:
    """Tool schemas are sent on every call and are easy to forget."""
    return estimate(json.dumps(tools)) if tools else 0


@dataclass
class Budget:
    window: int = 128_000
    ratio: float = 0.70              # headroom for estimation error + completion
    reserve_completion: int = 4_000

    @property
    def limit(self) -> int:
        return int(self.window * self.ratio) - self.reserve_completion

    def used(self, messages: list[dict], tools: list[dict] | None = None) -> int:
        return sum(message_tokens(m) for m in messages) + tools_tokens(tools)

    def pressure(self, messages, tools=None) -> float:
        """0.0 = empty, 1.0 = at the limit. The number to log every turn."""
        return self.used(messages, tools) / max(1, self.limit)
```

### `policy.py`

```python
"""What is pinned, and which span goes next."""

from __future__ import annotations

from dataclasses import dataclass, field

CORRECTION_MARKERS = ("actually", "no,", "not ", "instead", "i meant", "wrong")


@dataclass
class PinPolicy:
    keep_recent: int = 4
    pin_system: bool = True
    pin_first_user: bool = True          # the task statement
    pin_corrections: bool = True
    explicit: set[int] = field(default_factory=set)   # indices pinned by the caller

    def pinned(self, messages: list[dict]) -> set[int]:
        keep: set[int] = set(self.explicit)
        for index, message in enumerate(messages):
            if self.pin_system and message["role"] == "system":
                keep.add(index)
        if self.pin_first_user:
            for index, message in enumerate(messages):
                if message["role"] == "user":
                    keep.add(index)
                    break
        if self.pin_corrections:
            for index, message in enumerate(messages):
                text = (message.get("content") or "").lower()
                if message["role"] == "user" and any(m in text for m in CORRECTION_MARKERS):
                    keep.add(index)
        keep.update(range(max(0, len(messages) - self.keep_recent), len(messages)))
        return keep


def complete_turn_boundary(messages: list[dict], end: int) -> int:
    """Walk `end` back until cutting there leaves no tool call unanswered.

    An assistant message with tool_calls must stay with its tool results, or
    the provider rejects the request.
    """
    while end > 0:
        previous = messages[end - 1]
        if previous.get("tool_calls"):
            end -= 1
            continue
        if end < len(messages) and messages[end]["role"] == "tool":
            end -= 1
            continue
        break
    return end


def next_span(messages: list[dict], pinned: set[int], *, max_span: int = 30) -> tuple[int, int]:
    """The oldest contiguous unpinned run. Returns (start, end); empty if none."""
    start = None
    for index in range(len(messages)):
        if index in pinned:
            if start is not None:
                break
            continue
        if start is None:
            start = index
    if start is None:
        return (0, 0)
    end = start
    while end < len(messages) and end not in pinned and (end - start) < max_span:
        end += 1
    end = complete_turn_boundary(messages, end)
    return (start, end) if end - start >= 2 else (0, 0)
```

### `compactor.py`

```python
"""Span → one summary message. The only LLM call in this skill."""

from __future__ import annotations

COMPACT_PROMPT = """Compress this span of an agent transcript into a dense note.
Preserve, as explicit lines:
- Decisions made, and what they rule out
- Facts discovered (file paths, names, values, errors) - verbatim, not paraphrased
- What was tried and failed, and why
- Anything the user corrected

Drop: pleasantries, restatement of the task, reasoning that led nowhere,
tool calls whose results you have already captured above.

Write under {limit} words. No preamble. If the span contains nothing
worth preserving, reply exactly: NOTHING

Span:
{span}"""


def render_span(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        role = message["role"]
        content = (message.get("content") or "").strip()
        for call in message.get("tool_calls") or []:
            lines.append(f"[{role} calls {call['function']['name']}({call['function']['arguments']})]")
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


class Compactor:
    def __init__(self, client, *, word_limit: int = 200, archive=None):
        self.client = client              # anything with .complete(messages) -> ModelResponse
        self.word_limit = word_limit
        self.archive = archive            # callable(messages) -> str path, or None

    def compact(self, span: list[dict]) -> dict | None:
        """Returns the replacement message, or None if the span carried nothing."""
        prompt = COMPACT_PROMPT.format(limit=self.word_limit, span=render_span(span))
        response = self.client.complete([{"role": "user", "content": prompt}])
        summary = (response.content or "").strip()
        if not summary or summary == "NOTHING":
            return None

        reference = self.archive(span) if self.archive else None
        marker = f" Full text: {reference}." if reference else ""
        return {
            "role": "user",
            "content": (
                f"[{len(span)} earlier turns compacted.{marker}]\n"
                f"Summary of what happened:\n{summary}"
            ),
            "_compacted": len(span),
        }
```

Two details that matter more than they look.

`role="user"` — not `system`. A compaction summary is a record of what
happened, and putting it in a system message gives the model's own summary the
authority of your instructions. If the summary is wrong, a system role makes it
unarguable.

`archive` — write the original span to a JSONL file before replacing it.
Compaction is the only irreversible thing in this skill, and a path in the
marker turns "the agent forgot" into "here is exactly what it dropped".

### `window.py`

```python
"""ManagedWindow: the messages list the agent appends to, that manages itself."""

from __future__ import annotations

from dataclasses import dataclass, field

from .budget import Budget, estimate
from .policy import PinPolicy, next_span

TRUNCATION_NOTE = "\n... [{dropped} chars dropped] ...\n"


def truncate_tool_result(text: str, max_tokens: int) -> str:
    """Head + tail. The middle of a log is the least informative part of it."""
    limit = max_tokens * 4
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    dropped = len(text) - head - tail
    return text[:head] + TRUNCATION_NOTE.format(dropped=dropped) + text[-tail:]


@dataclass
class ManagedWindow:
    budget: Budget = field(default_factory=Budget)
    pins: PinPolicy = field(default_factory=PinPolicy)
    compactor: object | None = None
    max_tool_tokens: int = 2_000
    compact_at: float = 0.70
    evict_at: float = 0.90
    messages: list[dict] = field(default_factory=list)
    tools: list[dict] | None = None
    events: list[str] = field(default_factory=list)   # what happened, for the CLI and tests

    def append(self, message: dict) -> None:
        if message["role"] == "tool":
            message = {**message, "content": truncate_tool_result(
                message.get("content") or "", self.max_tool_tokens)}
        if self.budget.used([message]) > self.budget.limit:
            raise ValueError(
                f"single message is {self.budget.used([message])} tokens, over the "
                f"{self.budget.limit} budget - it cannot be sent at all"
            )
        self.messages.append(message)

    def pressure(self) -> float:
        return self.budget.pressure(self.messages, self.tools)

    def prepare(self) -> list[dict]:
        """Call immediately before every model call. Returns the list to send."""
        if self.pressure() > self.compact_at and self.compactor is not None:
            self._compact_once()
        while self.pressure() > self.evict_at:
            if not self._evict_once():
                break
        return self.messages

    def _compact_once(self) -> bool:
        pinned = self.pins.pinned(self.messages)
        start, end = next_span(self.messages, pinned)
        if end - start < 2:
            return False
        before = self.pressure()
        replacement = self.compactor.compact(self.messages[start:end])
        self.messages[start:end] = [replacement] if replacement else []
        self.events.append(
            f"compacted {end - start} turns at pressure {before:.2f} -> {self.pressure():.2f}"
        )
        return True

    def _evict_once(self) -> bool:
        pinned = self.pins.pinned(self.messages)
        for index in range(len(self.messages)):
            if index not in pinned:
                dropped = self.messages.pop(index)
                self.events.append(f"evicted {dropped['role']} turn ({estimate(dropped.get('content') or '')} tok)")
                return True
        return False       # everything is pinned; nothing more can be done
```

`prepare()` returning the list, rather than the agent reading `.messages`
directly, is what makes this safe to bolt onto an existing loop: one call site
changes, `client.complete(window.prepare(), tools=...)`, and the rest of the
agent is untouched.

`_evict_once` returning `False` when everything is pinned is a real state, not
an error — it means the pin policy itself is over budget, and the honest
response is to send anyway and let the provider complain, with `events` showing
exactly why.

### `cli.py`

Three subcommands, all offline:

- `<command> inspect <transcript.jsonl>` — token counts per message, cumulative
  pressure, which messages the pin policy protects. This is the command people
  actually use; make its output a readable table.
- `<command> simulate --turns 200` — synthetic turns through a `ManagedWindow`
  with a fake compactor, printing the `events` log and the pressure curve.
  Proves the thing never blows the budget without spending a token.
- `<command> demo` — a short real session, if a key is present.

## Failure modes

- **Compaction that drops the task.** The single worst outcome, and it comes
  from a pin policy that only pins `system`. If the task arrived as a user turn
  — which it did — pin the first user message. Test it explicitly.

- **Orphaned tool calls.** An assistant message with `tool_calls` whose results
  were compacted away is rejected by the provider with a confusing error about
  message ordering. `complete_turn_boundary` exists for this; do not skip it.

- **Compaction thrash.** Compacting at 70% when a summary only frees 5% means
  you compact again two turns later, and again, each time summarizing a
  summary. Require a minimum span (2+ turns) and a minimum gain — if a pass does
  not drop pressure by at least 0.05, stop trying and evict instead.

- **Summaries of summaries.** Three compaction rounds and the oldest content has
  been through the model three times, each pass lossier than the last. Mark
  compacted messages (`_compacted`), and prefer to *evict* an old summary
  outright rather than re-compact it — the second summary of a summary is worth
  less than the tokens it costs.

- **Counting the prompt but not the tools.** A dozen tool schemas is a couple of
  thousand tokens on every call, invisible in the `messages` list. Systems blow
  their budget here and nobody can find the tokens.

- **Truncating in the middle of JSON.** A tool result cut at a character offset
  yields invalid JSON that the model then tries to parse and reason about.
  Truncate with the explicit `[N chars dropped]` marker so the model can see
  that it is looking at a fragment.

- **Pressure that is never logged.** If you do not print `pressure` every turn,
  you find out about the budget from a 400 response in production. One line.

## Required tests

All offline, no key. See `references/evaluation.md` for the universal set;
these are mandatory for this skill:

```python
def test_pinned_task_survives_heavy_pressure(window):
    window.append({"role": "system", "content": "you are an agent"})
    window.append({"role": "user", "content": "TASK: migrate the billing module"})
    for i in range(80):
        window.append({"role": "assistant", "content": f"step {i} " * 200})
    window.prepare()
    surviving = " ".join(m.get("content") or "" for m in window.messages)
    assert "TASK: migrate the billing module" in surviving


def test_pressure_strictly_decreases_after_compaction(window):
    fill(window, turns=60)
    before = window.pressure()
    window.prepare()
    assert window.pressure() < before
    assert any("compacted" in e for e in window.events)


def test_compaction_leaves_a_visible_marker(window):
    fill(window, turns=60)
    window.prepare()
    assert any("turns compacted" in (m.get("content") or "") for m in window.messages)


def test_tool_results_are_truncated_at_write_time(window):
    window.append({"role": "tool", "tool_call_id": "c0", "content": "x" * 400_000})
    content = window.messages[-1]["content"]
    assert "chars dropped" in content
    assert len(content) < 400_000


def test_tool_calls_are_never_orphaned(window):
    window.append({"role": "assistant", "content": None,
                   "tool_calls": [{"id": "c0", "function": {"name": "f", "arguments": "{}"}}]})
    window.append({"role": "tool", "tool_call_id": "c0", "content": "result"})
    fill(window, turns=60)
    window.prepare()
    ids = {c["id"] for m in window.messages for c in (m.get("tool_calls") or [])}
    answered = {m.get("tool_call_id") for m in window.messages if m["role"] == "tool"}
    assert ids <= answered


def test_empty_span_summary_removes_rather_than_inserts(window):
    window.compactor = FakeCompactor(returns=None)     # model said NOTHING
    fill(window, turns=60)
    count_before = len(window.messages)
    window.prepare()
    assert len(window.messages) < count_before
    assert not any("Summary of what happened" in (m.get("content") or "") for m in window.messages)


def test_oversized_single_message_is_refused_not_truncated(window):
    with pytest.raises(ValueError, match="cannot be sent"):
        window.append({"role": "user", "content": "y" * 10_000_000})


def test_all_pinned_stops_cleanly(window):
    window.pins.keep_recent = 1000
    fill(window, turns=50)
    window.prepare()          # nothing is evictable
    assert window.messages    # did not loop forever, did not empty the window
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — `pip install -e ".[dev]"`, import, `--help`.
2. **Tier 1** — `pytest -q`. Every test above passes, no network, no key.
3. **Tier 2** — `<command> simulate --turns 500`, then read the output:
   pressure never exceeds `evict_at`, the event log shows compaction firing at
   a steady cadence rather than every turn, and the pinned task statement is
   present in the final window.
4. **Tier 3** — one real session against the configured endpoint, long enough
   to trigger one compaction. Read the summary it produced. If it is prose about
   how the conversation went rather than a list of decisions and verbatim
   facts, the prompt needs work — that summary is all the model will have.

Then tell the user what actually ran, including the measured pressure curve.
If Tier 3 was skipped for lack of a key, say so — the compaction prompt is the
one part of this skill that offline tests cannot judge.
