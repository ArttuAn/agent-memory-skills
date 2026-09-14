# Build a Scratchpad (file-backed working memory)

Given the user's spec, build a structured notes file the agent reads at the start
of a turn and patches at the end: atomic, diffable, quarantined on corruption,
and editable by a human.

**Ask what the agent needs to remember between turns** — get the actual sections.
And ask who else reads it: if a human edits it, the format is Markdown.

## What to build

```
<project>/
└── src/<package>/
    ├── schema.py  # Section definitions with purposes and enforced caps
    ├── parse.py   # markdown <-> structure, strict, raises ParseError
    ├── patch.py   # append / replace / check / remove / clear + validation
    ├── io.py      # atomic_write, flock, history, quarantine
    ├── pad.py     # Scratchpad: load / for_prompt / update
    └── cli.py     # show / validate / diff / history / restore / edit
```

## Core pieces

1. **Structured sections with character caps**, declared up front with a purpose
   each: Task, Plan (numbered), Findings, **Ruled out**, Open questions, Next.
   "Ruled out" is the section people forget and the one that pays for the
   scratchpad — without it the agent re-tries what failed two compactions ago.
2. **Patch operations, never full rewrites.** A full rewrite loses a section the
   model did not think about, silently. Five ops cover everything real. **An
   empty patch list is a valid turn** — most turns should not change the file.
3. **Atomic writes**: temp file in the same directory, `fsync`, then
   `os.replace`. The reader sees the old file or the new one, never half. Take an
   advisory `flock` on a sidecar across the read-modify-write.
4. **Quarantine, never reset.** A hand-broken file gets renamed to
   `.broken-<ts>`, the last good version is restored from a rolling `.history/`,
   and the pad notes what happened. Resetting to blank destroys the user's work
   and the agent's at the moment they most need it.
5. **Caps warn, they do not truncate.** Truncating drops the oldest finding,
   usually the most important. Put the warning in the next prompt so pruning is a
   decision the agent takes deliberately — or a signal to promote to a store.
6. **Render empty sections** as `_(empty)_` so the file shape is stable and the
   diffs stay small.
7. **The diff is the observability.** Log the per-turn unified diff. If the file
   is in a git repo, commit once per turn — `git log -p` is then a complete
   navigable history of the agent's reasoning, free.

## Conventions

- Put the section contract (names, caps, purposes) in the prompt verbatim.
- If a second writer appears, move to `memory-shared` rather than adding a lock.
- If a section is chronically over cap, that content belongs in a store and the
  scratchpad should hold a pointer.

## Verify before finishing

1. Install, import, `--help`, `show` on a fresh path.
2. `pytest -q` — round trip is stable; an empty patch list does not touch the
   file; over-cap warns without truncating; a malformed file is quarantined and
   recovered; unknown sections are rejected; `check` marks the right item and
   warns out of range; writes are atomic under a simulated crash with no temp
   files left; history allows restore; diffs are small.
3. A 50-turn simulated session: no section chronically over cap, "Ruled out" is
   non-empty, and the file is under ~3,000 tokens.
4. Hand-edit the file mid-session and continue. The agent must pick up the
   correction next turn — if not, it is being cached and the main advantage is gone.

Report the final size in tokens and the per-turn diff sizes. Large diffs every
turn mean the agent is rewriting rather than patching.
