---
name: memory-scratchpad
description: "Build file-backed working memory: a structured notes file the agent reads and rewrites each turn, atomic, diffable, and editable by a human"
---

# Scratchpad Memory

A scratchpad is a file the agent reads at the start of a turn and rewrites at
the end. No embeddings, no database, no retrieval. It is the cheapest memory
that survives a context reset, and for most agents it is the one that should
have been built first.

Its real advantage over a store is that it is **legible**. A human can open it,
see exactly what the agent thinks the state is, fix a wrong line, and the agent
picks up the correction on the next turn. No retrieval system offers that, and
no amount of vector search substitutes for it.

```
  turn start ──► read scratchpad ──► into the prompt verbatim
                                          │
                                       agent works
                                          │
  turn end   ◄── patch operations ◄───────┘
                     │
                     ▼
              validate ──► atomic write (tmp + rename)
                     │
                     └──► invalid? quarantine the bad version, keep the good one
```

## Use this when

- One agent, one working session, state that must survive a context reset:
  a task list, findings so far, the current plan, what has been ruled out.
- You want the user to be able to read and correct the agent's working state.
  This is the reason to prefer a scratchpad over a store even when a store
  would work.
- The agent is long-running and you need a crash-recovery point that is not the
  transcript.

**Do not use this when** more than one agent writes to it. File-backed memory
has no concurrency story beyond an advisory lock; two agents and you want
`memory-shared`.

**Do not use this when** the content needs retrieval. A scratchpad is read
whole, every turn, into the context. That caps it at a few thousand tokens by
construction. Anything that grows past that belongs in a store, with the
scratchpad holding a pointer to it.

Pairs naturally with `memory-working`: compaction discards the transcript, and
the scratchpad is where anything worth keeping should already have been written.
An agent with compaction and no scratchpad loses work at every compaction
boundary.

## Workflow

1. **Ask what the agent needs to remember between turns.** Get the actual
   sections — "current task", "files touched", "ruled out", "open questions".
   A scratchpad with the wrong sections is a file the agent ignores.

2. **Ask who else reads it.** If a human is expected to edit it, the format is
   Markdown and the structure must survive hand-editing. If it is agent-only,
   JSON is safer. Markdown is the better default and the one this skill builds.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

4. **Write the five modules**: `schema.py` (the sections), `parse.py`
   (Markdown ↔ structure), `patch.py` (the operations), `io.py` (atomic write,
   lock, quarantine), `pad.py` (the object the agent uses). Then `cli.py`.

5. **Verify against the contract** in `references/evaluation.md`, plus the
   scratchpad-specific tests below.

## The decisions that matter

### 1. Structured sections, not freeform

A freeform scratchpad decays. The agent appends, never prunes, and within twenty
turns it is a transcript with extra steps — the thing the scratchpad existed to
avoid.

Declare the sections up front, with a purpose and a size cap each:

```python
SECTIONS = [
    Section("Task",          cap=300,  purpose="What we are trying to do. Rarely changes."),
    Section("Plan",          cap=800,  purpose="Numbered steps, with [x] on the done ones."),
    Section("Findings",      cap=1500, purpose="What we learned. One line each, with a source."),
    Section("Ruled out",     cap=600,  purpose="What we tried that did not work, and why."),
    Section("Open questions", cap=400, purpose="What we still need to know."),
    Section("Next",          cap=200,  purpose="The single next action. One line."),
]
```

**"Ruled out" is the section people forget and the one that pays for the
scratchpad.** Without it the agent re-tries the approach that failed two
compactions ago, every time, and the user watches it happen.

Caps are in characters and they are enforced. A section over cap forces a
decision — prune or promote to a store — instead of quietly growing until the
file dominates the context window.

### 2. Patch operations, not full rewrites

Two ways for the agent to update the scratchpad:

| | How | Risk |
| --- | --- | --- |
| **Full rewrite** | Model emits the whole new file | Silent loss: it drops a section it did not think about, and nothing notices |
| **Patch ops** | Model emits `{section, op, content}` | Loss requires an explicit delete |

Patches, always. The vocabulary is small enough to fit in a prompt:

```json
[{"section": "Findings", "op": "append", "content": "CI has no redis service (from job #4412)"},
 {"section": "Plan",     "op": "check",  "content": "2"},
 {"section": "Next",     "op": "replace", "content": "Add the fakeredis fixture"},
 {"section": "Ruled out","op": "append", "content": "Mocking at the client level - the limiter calls redis directly"}]
```

Five operations cover everything real: `append`, `replace`, `check` (tick a
numbered plan item), `remove`, `clear`. `replace` is the only one that can lose
content, and it is scoped to one section.

**An empty patch list is a valid turn.** Most turns should not change the
scratchpad. A prompt that demands an update every turn produces churn, and churn
in a file the human is reading is worse than no updates.

### 3. Atomic writes, and a lock you actually take

The scratchpad is read at the start of a turn and written at the end. A crash in
between, or a second process, corrupts it — and the corrupted file is then read
into the next prompt.

```python
import fcntl
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename. Never partial."""
    directory = path.parent
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".scratch-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)          # atomic on POSIX and Windows
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
```

`os.replace` is the whole trick: the reader sees either the old file or the new
one, never half of either. Writing in place does not have this property, and the
failure only shows up under a crash you cannot reproduce on demand.

For the lock, an advisory `flock` on a sidecar file, held across read-modify-
write. It does not protect against a process that ignores it, which is fine —
it protects against your own two processes, which is the actual risk.

### 4. Quarantine, never reset

The file is hand-editable, which means it will eventually be hand-broken: a
deleted heading, a stray fence, an editor that mangled it.

The wrong response is to reset to a blank scratchpad. That discards the user's
work *and* the agent's, silently, at the moment they most need it.

```python
def load(path: Path) -> Scratchpad:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except ParseError as error:
        quarantine = path.with_suffix(f".broken-{int(time.time())}")
        path.rename(quarantine)
        last_good = recover_last_good(path)          # from .history/
        pad = last_good or Scratchpad.empty()
        pad.note(f"Previous scratchpad could not be parsed ({error}); "
                 f"moved to {quarantine.name}. Recovered from history.")
        return pad
```

Keep a rolling history — the last N versions in a `.history/` directory, written
on every successful save. It costs a few kilobytes and it is what makes the
scratchpad safe to let an agent write to.

### 5. The diff is the observability

After every turn, the diff between the previous and current scratchpad is a
complete record of what the agent decided that turn — far more readable than the
transcript and far shorter.

```python
def turn_diff(before: str, after: str) -> str:
    return "\n".join(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile="before", tofile="after", lineterm="", n=1))
```

Log it. Print it in verbose mode. If the scratchpad lives in a git repo, commit
it once per turn with the turn number as the message — then `git log -p` on one
file is a complete, navigable history of the agent's reasoning, for free.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # path, sections, caps, history depth
    ├── schema.py        # Section definitions + the render order
    ├── parse.py         # markdown <-> Scratchpad, strict, with ParseError
    ├── patch.py         # the five operations + validation
    ├── io.py            # atomic_write, lock, history, quarantine
    ├── pad.py           # Scratchpad: load / render / apply / save
    └── cli.py           # show / edit / diff / history / restore / validate
```

### `schema.py`

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    name: str
    cap: int                    # characters; enforced, not advisory
    purpose: str                # goes into the prompt so the agent knows what belongs here
    numbered: bool = False      # Plan-style, supports the `check` operation


SECTIONS = (
    Section("Task", 300, "What we are trying to do. Rarely changes."),
    Section("Plan", 800, "Numbered steps. Tick with [x] as they complete.", numbered=True),
    Section("Findings", 1500, "What we learned. One line each, with a source."),
    Section("Ruled out", 600, "What we tried that did not work, and why. Prevents re-trying."),
    Section("Open questions", 400, "What we still need to know."),
    Section("Next", 200, "The single next action. One line."),
)

BY_NAME = {s.name.lower(): s for s in SECTIONS}


def prompt_block() -> str:
    """The section contract, for the system prompt. The agent needs this verbatim."""
    lines = ["Your scratchpad has these sections. Keep each one within its purpose:"]
    for section in SECTIONS:
        lines.append(f"- {section.name} (max {section.cap} chars): {section.purpose}")
    return "\n".join(lines)
```

### `parse.py`

```python
"""Markdown <-> structure. Strict: a file it cannot parse is quarantined, not guessed at."""

from __future__ import annotations

import re

from .schema import BY_NAME, SECTIONS

HEADING = re.compile(r"^##\s+(.+?)\s*$")


class ParseError(Exception):
    pass


def parse(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {s.name: [] for s in SECTIONS}
    current: str | None = None
    seen: set[str] = set()

    for number, line in enumerate(text.splitlines(), 1):
        match = HEADING.match(line)
        if match:
            name = match.group(1).strip()
            known = BY_NAME.get(name.lower())
            if known is None:
                raise ParseError(f"line {number}: unknown section {name!r}")
            if known.name in seen:
                raise ParseError(f"line {number}: duplicate section {name!r}")
            seen.add(known.name)
            current = known.name
            continue
        if line.strip() and current is None:
            raise ParseError(f"line {number}: content before the first section heading")
        if current and line.strip():
            sections[current].append(line.rstrip())
    return sections


def render(sections: dict[str, list[str]]) -> str:
    parts = []
    for section in SECTIONS:
        parts.append(f"## {section.name}")
        body = sections.get(section.name) or []
        parts.extend(body if body else ["_(empty)_"])
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
```

Rendering empty sections rather than omitting them keeps the file's shape stable
across turns, which keeps the diffs small and keeps the agent from "discovering"
that a section is missing and inventing a different name for it.

### `patch.py`

```python
"""Five operations. Validated before anything is written."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .schema import BY_NAME

OPS = {"append", "replace", "check", "remove", "clear"}


@dataclass
class Patch:
    section: str
    op: str
    content: str = ""


class PatchError(Exception):
    pass


def validate(patches: list[Patch]) -> list[Patch]:
    for patch in patches:
        if patch.section.lower() not in BY_NAME:
            raise PatchError(f"unknown section {patch.section!r}")
        if patch.op not in OPS:
            raise PatchError(f"unknown op {patch.op!r}")
        if patch.op == "check" and not patch.content.strip().isdigit():
            raise PatchError("check requires the item number")
    return patches


def apply(sections: dict[str, list[str]], patches: list[Patch]) -> tuple[dict, list[str]]:
    """Returns (new_sections, warnings). Never mutates the input."""
    out = {name: list(lines) for name, lines in sections.items()}
    warnings: list[str] = []

    for patch in validate(patches):
        schema = BY_NAME[patch.section.lower()]
        name = schema.name
        if patch.op == "append":
            line = patch.content.strip()
            if schema.numbered:
                line = f"{len(out[name]) + 1}. [ ] {line}"
            elif not line.startswith("-"):
                line = f"- {line}"
            out[name].append(line)
        elif patch.op == "replace":
            out[name] = [l.strip() for l in patch.content.splitlines() if l.strip()]
        elif patch.op == "check":
            index = int(patch.content.strip()) - 1
            if 0 <= index < len(out[name]):
                out[name][index] = re.sub(r"\[ \]", "[x]", out[name][index], count=1)
            else:
                warnings.append(f"check: {name} has no item {patch.content}")
        elif patch.op == "remove":
            before = len(out[name])
            out[name] = [l for l in out[name] if patch.content.strip() not in l]
            if len(out[name]) == before:
                warnings.append(f"remove: nothing in {name} matched {patch.content!r}")
        elif patch.op == "clear":
            out[name] = []

        size = sum(len(l) + 1 for l in out[name])
        if size > schema.cap:
            warnings.append(
                f"{name} is {size} chars, over its {schema.cap} cap - "
                f"prune it or promote the content to long-term memory"
            )
    return out, warnings
```

The cap produces a **warning that reaches the agent's next prompt**, not a
silent truncation. Truncating drops the oldest finding, which is usually the
most important one; telling the agent it is over budget makes pruning a decision
it takes deliberately.

### `pad.py`

```python
"""The object the agent uses. Load, render into the prompt, apply patches, save."""

from __future__ import annotations

import difflib
from pathlib import Path

from .io import atomic_write, lock, quarantine, save_history
from .parse import ParseError, parse, render
from .patch import Patch, apply
from .schema import prompt_block


class Scratchpad:
    def __init__(self, path: str | Path, *, history_depth: int = 20):
        self.path = Path(path)
        self.history_depth = history_depth
        self.sections = self._load()
        self.warnings: list[str] = []

    def _load(self) -> dict:
        if not self.path.exists():
            return parse("")
        try:
            return parse(self.path.read_text(encoding="utf-8"))
        except ParseError as error:
            recovered = quarantine(self.path, reason=str(error))
            return recovered if recovered is not None else parse("")

    def for_prompt(self) -> str:
        """What goes into the context. Contract first, then the content."""
        return f"{prompt_block()}\n\nCurrent scratchpad:\n\n{render(self.sections)}"

    def update(self, patches: list[Patch]) -> str:
        """Apply, save atomically, return the diff. An empty patch list is a no-op."""
        if not patches:
            return ""
        before = render(self.sections)
        self.sections, self.warnings = apply(self.sections, patches)
        after = render(self.sections)
        if after == before:
            return ""
        with lock(self.path):
            save_history(self.path, before, depth=self.history_depth)
            atomic_write(self.path, after)
        return "\n".join(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile="before", tofile="after", lineterm="", n=1))
```

### `cli.py`

- `<command> show` — the rendered scratchpad.
- `<command> validate` — parse it and report, without changing anything. The
  command to run when the agent starts behaving strangely.
- `<command> diff [-n 1]` — the last turn's diff, from history.
- `<command> history` / `<command> restore <n>` — the recovery path.
- `<command> edit` — open in `$EDITOR`, validate on close, refuse to save an
  unparseable file rather than letting the user break it by accident.

## Failure modes

- **Freeform growth.** No sections, no caps, and by turn thirty the scratchpad
  is longer than the conversation it was meant to compress. Sections with
  enforced caps, from the first version.

- **The agent stops reading it.** Usually because the scratchpad is stale and
  the agent has learned it is not worth attending to. Root cause is almost
  always the update path failing silently — check that `update` is actually
  called at the end of every turn, and that warnings reach the prompt.

- **Full-rewrite drift.** With full rewrites, a section vanishes because the
  model did not think about it that turn, and nothing detects the loss. Patch
  operations make a deletion explicit.

- **Reset on parse failure.** A blank scratchpad after a bad hand-edit destroys
  everything both parties built. Quarantine and restore from history.

- **In-place writes.** A crash mid-write leaves a half-file that then gets read
  into the next prompt. `os.replace`, always.

- **Two agents, one file.** Interleaved writes, last-writer-wins, silent loss.
  If a second writer appears, move to `memory-shared` rather than adding a
  second lock.

- **The scratchpad as a database.** Findings accumulate until it is 8,000 tokens
  read in full on every turn. The caps are the forcing function: when a section
  is chronically over, that content belongs in `memory-semantic` and the
  scratchpad should hold a pointer.

- **No history.** The agent wrote something wrong, the user did not notice for
  ten turns, and there is nothing to go back to. Twenty versions is a few
  kilobytes.

## Required tests

All offline, no model needed — this skill's tests are unusually cheap. See
`references/evaluation.md` for the universal set; these are mandatory:

```python
def test_round_trip_is_stable(tmp_path):
    pad = Scratchpad(tmp_path / "s.md")
    pad.update([Patch("Findings", "append", "CI has no redis")])
    reloaded = Scratchpad(tmp_path / "s.md")
    assert reloaded.sections == pad.sections


def test_empty_patch_list_writes_nothing(tmp_path):
    path = tmp_path / "s.md"
    pad = Scratchpad(path)
    pad.update([Patch("Task", "replace", "do the thing")])
    before = path.stat().st_mtime_ns
    assert pad.update([]) == ""
    assert path.stat().st_mtime_ns == before


def test_over_cap_warns_and_does_not_truncate(tmp_path):
    pad = Scratchpad(tmp_path / "s.md")
    pad.update([Patch("Next", "replace", "x" * 500)])     # cap is 200
    assert any("over its 200 cap" in w for w in pad.warnings)
    assert len(pad.sections["Next"][0]) == 500            # nothing was cut


def test_malformed_file_is_quarantined_not_reset(tmp_path):
    path = tmp_path / "s.md"
    path.write_text("## Findings\n- a real finding\n## Nonsense\nbroken")
    pad = Scratchpad(path)
    assert list(path.parent.glob("*.broken-*"))
    assert "a real finding" in "\n".join(pad.sections["Findings"])


def test_unknown_section_in_a_patch_is_rejected(tmp_path):
    with pytest.raises(PatchError, match="unknown section"):
        Scratchpad(tmp_path / "s.md").update([Patch("Vibes", "append", "good")])


def test_check_marks_the_right_plan_item(tmp_path):
    pad = Scratchpad(tmp_path / "s.md")
    pad.update([Patch("Plan", "append", "first"), Patch("Plan", "append", "second")])
    pad.update([Patch("Plan", "check", "2")])
    assert "[x] second" in pad.sections["Plan"][1]
    assert "[ ] first" in pad.sections["Plan"][0]


def test_check_out_of_range_warns_and_changes_nothing(tmp_path):
    pad = Scratchpad(tmp_path / "s.md")
    pad.update([Patch("Plan", "append", "only one")])
    pad.update([Patch("Plan", "check", "7")])
    assert any("no item 7" in w for w in pad.warnings)


def test_writes_are_atomic_under_a_crash(tmp_path, monkeypatch):
    path = tmp_path / "s.md"
    Scratchpad(path).update([Patch("Task", "replace", "good state")])
    monkeypatch.setattr("os.replace", boom)
    with contextlib.suppress(Exception):
        Scratchpad(path).update([Patch("Task", "replace", "half written")])
    assert "good state" in path.read_text()               # old file intact
    assert not list(tmp_path.glob(".scratch-*.tmp"))      # temp cleaned up


def test_history_allows_restore(tmp_path):
    path = tmp_path / "s.md"
    pad = Scratchpad(path)
    pad.update([Patch("Findings", "append", "important thing")])
    pad.update([Patch("Findings", "clear")])
    assert "important thing" in read_history(path, -1)


def test_diff_shows_exactly_the_turn_change(tmp_path):
    pad = Scratchpad(tmp_path / "s.md")
    diff = pad.update([Patch("Next", "replace", "add the fixture")])
    assert "+add the fixture" in diff.replace("+- ", "+")
    assert diff.count("\n+") <= 3                         # small, readable diffs
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, then `show` on a fresh path (creates
   nothing, prints the empty structure).
2. **Tier 1** — `pytest -q`, offline. Every test above. The atomicity and
   quarantine tests are the ones that matter under real use.
3. **Tier 2** — run a 50-turn simulated session and read the final file. Check
   three things: no section is chronically over cap, "Ruled out" is non-empty
   (if it never fills, the agent is not recording failures and will repeat
   them), and the file is under roughly 3,000 tokens.
4. **Tier 3** — hand-edit the scratchpad mid-session, then continue. The agent
   should pick up the correction on the next turn. If it does not, the pad is
   being cached somewhere and the file's main advantage is gone.

Then tell the user what actually ran, plus the final scratchpad size in tokens
and the per-turn diff sizes. Large diffs every turn mean the agent is rewriting
rather than patching, and the file will stop being readable.
