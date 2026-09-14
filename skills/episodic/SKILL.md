---
name: memory-episodic
description: "Build an episodic memory: an append-only log of what the agent did, with outcomes, recalled by similarity and recency"
---

# Episodic Memory

Episodic memory answers one question: **"have I been here before, and how did it
go?"** It is a log of *events* — this task, on this date, with this outcome —
not a store of truths. The difference is the whole design. A fact store that
says "the deploy script lives at `bin/deploy`" is asserting something. An
episode that says "on March 4th I tried `bin/deploy` and it failed with
`ENOENT`" is reporting something, and remains true even after the claim inside
it stops being.

That is why episodes are append-only and never corrected. When a later episode
contradicts an earlier one, both are kept: the contradiction *is* the signal,
and it is what `memory-consolidation` reads to work out what is actually true.

```
  run ends ──► did anything worth remembering happen?
                   │ yes
                   ▼
              distil one episode:  what / how / outcome / lesson
                   │
                   ▼
              append  (never update, never delete)
                   │
  new task ────────┴──► recall: similarity × recency × outcome-fit
                             │
                             ▼
                   "Last time you did something like this: ..."
```

## Use this when

- The agent repeats a mistake it has already made. This is the canonical
  symptom, and it is the one thing episodic memory is uniquely good at.
- The user asks time-shaped questions: "what did we do last Tuesday", "when did
  this start failing", "have we tried this before".
- You want a substrate for `memory-consolidation` or `memory-procedural` to
  learn from. Both read episodes; neither works without a log to read.
- You need an audit trail of agent behaviour that is not the raw transcript
  (too large, too noisy) and not a summary of the whole session (too coarse).

**Do not use this when** nothing in your system depends on *when* something
happened. If recency never changes the answer, you have written a vector store
with a timestamp column and paid for the timestamp. And do not use episodes as
a source of facts: episodes record what was believed and tried at the time,
including everything that was wrong. Distil facts out with
`memory-semantic`; keep the episodes as the evidence.

## Workflow

When the user asks for episodic memory / "remember what happened" / run history:

1. **Ask for the granularity if it is not given.** What is one episode — a
   turn, a task, a session, a tool call? What counts as a good outcome, and can
   the system tell? If vague, propose *one episode per completed task, outcome
   labelled by the agent's own final state*, and confirm.

2. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

3. **Write the five modules**: `episode.py` (the record), `store.py`
   (append-only SQLite), `recorder.py` (run → episode), `recall.py` (ranked
   retrieval), `render.py` (episodes → prompt block). Then `cli.py`.

4. **Verify against the contract** in `references/evaluation.md`, plus the
   episodic-specific tests below.

## The decisions that matter

### 1. Granularity — what exactly is one episode

This determines whether the store is useful or unusable, and it cannot be
changed later without re-deriving everything.

| Grain | Episodes/day | Good for | Fails at |
| --- | --- | --- | --- |
| Per tool call | Hundreds | Debugging one tool's reliability | Recall — everything matches everything |
| **Per task** | A handful | "Have I done this before" | Very long tasks compress to uselessness |
| Per session | One or two | Coarse history | Answering anything specific |

**Default to per-task**, where a task is one user goal pursued to a terminal
state. It is the grain at which "have I done this before" has an answer, and
the grain a human would use describing their own day.

A task that runs for hours is the failure case: one episode cannot hold it.
Split on *phase transitions* — when the agent's plan changes or a subgoal
completes — rather than on a turn count.

### 2. What the episode text actually says

The `text` field is the entire retrievable surface. Raw transcripts are the
wrong thing to put in it: they are enormous, they embed poorly (dominated by
boilerplate), and they bury the one line that mattered.

Distil to a fixed shape, and keep the raw transcript separately, referenced by
path:

```
Task:     Add rate limiting to the /search endpoint
Approach: Edited middleware.py, added a token-bucket limiter, wrote 3 tests
Outcome:  failure - tests passed locally, CI failed on a missing redis fixture
Lesson:   CI has no redis; limiter tests need the fakeredis fixture
```

`Lesson` is the field that earns the entire skill. It is the one line a future
run needs, and it is why an episode beats a transcript. When the model produces
nothing for it, store the episode with the field empty rather than inventing a
lesson — a fabricated lesson is a confidently wrong instruction to every future
run that retrieves it.

Embed `Task` plus `Lesson`, not the whole block. `Approach` and `Outcome` are
worth reading but they add noise to the vector: two unrelated tasks that both
edited `middleware.py` should not look similar.

### 3. Outcome labelling — where the truth comes from

Ranking by outcome is most of episodic memory's value ("show me what worked"),
and it is only as good as the label. Sources, best to worst:

1. **A test suite, exit code, or verifier.** Ground truth. Use it if it exists.
2. **The user's next turn.** "thanks, that works" versus "no, still broken" is
   a strong and free signal, one turn late.
3. **The agent's own claim.** Weak. Agents report success generously. Store it
   as `outcome="claimed_success"`, distinct from verified success, and rank it
   lower.
4. **An LLM judging the transcript.** Adds cost and correlated error. Only
   worth it when 1 and 2 are both unavailable.

Model outcome as a small enum — `success | failure | partial | abandoned |
unknown` — and **default to `unknown`**, not `success`. A store where everything
is `success` because nothing set the label is worse than no labels, because it
looks informative.

### 4. Recency weighting — and the trap in it

Episodic recall without recency is just vector search. Recency with too much
weight is a system that always returns yesterday, no matter what you asked.

Score multiplicatively so a match must be both relevant *and* reasonably fresh,
then floor the recency term so old-but-perfect matches still surface:

```python
score = similarity * (0.3 + 0.7 * recency) * outcome_weight
```

The `0.3` floor is load-bearing. Without it, a six-month-old episode that is the
only relevant one in the store scores near zero and never appears — which is
exactly the case where episodic memory would have been most valuable.

`outcome_weight` should depend on what the caller asked for. Planning a new
approach wants successes. Avoiding a repeat mistake wants failures — pass the
intent in rather than hard-coding a preference.

### 5. Append-only, enforced

No `UPDATE`. No `DELETE` outside a user-requested forget. Corrections are new
episodes. This is not purity — it is what makes the log admissible evidence for
consolidation, and what lets you answer "when did the agent's behaviour change"
by reading the store instead of guessing.

Enforce it in the schema, not in a code review:

```sql
CREATE TRIGGER episodes_immutable BEFORE UPDATE ON episodes
BEGIN SELECT RAISE(ABORT, 'episodes are append-only'); END;
```

The one legitimate mutation is `access_count` for reinforcement. Keep it in a
separate table so the trigger can stay absolute.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # half-life, k, embedding backend, db path
    ├── episode.py       # the Episode record + Outcome enum
    ├── store.py         # append-only SQLite + embeddings + FTS
    ├── recorder.py      # transcript -> Episode (LLM distillation, with guards)
    ├── recall.py        # similarity x recency x outcome ranking
    ├── render.py        # episodes -> prompt block
    └── cli.py           # record / recall / timeline / stats
```

### `episode.py`

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict
from enum import Enum


class Outcome(str, Enum):
    SUCCESS = "success"                  # verified by a test, exit code, or the user
    CLAIMED_SUCCESS = "claimed_success"  # the agent says so; nothing checked it
    PARTIAL = "partial"
    FAILURE = "failure"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"                  # the default, deliberately


VERIFIED = {Outcome.SUCCESS, Outcome.FAILURE}


@dataclass
class Episode:
    task: str
    approach: str = ""
    outcome: Outcome = Outcome.UNKNOWN
    lesson: str = ""                     # may be empty; never invent one
    scope: str = "default"
    tags: list[str] = field(default_factory=list)
    transcript_ref: str | None = None    # path to the raw log
    created_at: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self):
        if not self.id:
            payload = f"{self.scope}\x1f{self.task}\x1f{self.created_at}".encode()
            self.id = "ep_" + hashlib.blake2b(payload, digest_size=8).hexdigest()

    def embed_text(self) -> str:
        """Task + lesson only. Approach and outcome add noise to the vector."""
        return f"{self.task}\n{self.lesson}".strip()

    def render(self) -> str:
        lines = [f"Task:     {self.task}"]
        if self.approach:
            lines.append(f"Approach: {self.approach}")
        lines.append(f"Outcome:  {self.outcome.value}")
        if self.lesson:
            lines.append(f"Lesson:   {self.lesson}")
        return "\n".join(lines)

    def to_row(self) -> dict:
        row = asdict(self)
        row["outcome"] = self.outcome.value
        return row
```

### `store.py`

```python
"""Append-only episode store. SQLite, immutability enforced by trigger."""

from __future__ import annotations

import json
import sqlite3
from array import array
from pathlib import Path

from .episode import Episode, Outcome

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id             TEXT PRIMARY KEY,
    scope          TEXT NOT NULL,
    task           TEXT NOT NULL,
    approach       TEXT NOT NULL DEFAULT '',
    outcome        TEXT NOT NULL DEFAULT 'unknown',
    lesson         TEXT NOT NULL DEFAULT '',
    tags           TEXT NOT NULL DEFAULT '[]',
    transcript_ref TEXT,
    created_at     REAL NOT NULL,
    embedding      BLOB
);
CREATE INDEX IF NOT EXISTS episodes_scope_time ON episodes(scope, created_at DESC);
CREATE INDEX IF NOT EXISTS episodes_outcome ON episodes(scope, outcome);

CREATE TRIGGER IF NOT EXISTS episodes_immutable BEFORE UPDATE ON episodes
BEGIN SELECT RAISE(ABORT, 'episodes are append-only'); END;

CREATE TABLE IF NOT EXISTS episode_uses (
    id     TEXT PRIMARY KEY,
    uses   INTEGER NOT NULL DEFAULT 0,
    last   REAL
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def vec_to_blob(vector) -> bytes:
    return array("f", [float(x) for x in vector]).tobytes()


def blob_to_vec(blob) -> list[float]:
    if not blob:
        return []
    out = array("f")
    out.frombytes(blob)
    return list(out)


class EpisodeStore:
    def __init__(self, path: str | Path, embedder):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedder = embedder
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._check_dimensions()

    def _check_dimensions(self) -> None:
        """A store opened with a different embedder silently returns 0.0 forever."""
        stamp = f"{type(self.embedder).__name__}:{self.embedder.dimensions}"
        row = self.db.execute("SELECT value FROM meta WHERE key='embedder'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('embedder', ?)", (stamp,))
            self.db.commit()
        elif row["value"] != stamp:
            raise RuntimeError(
                f"store was built with {row['value']}, opened with {stamp}. "
                f"Re-embed the store or use the original backend."
            )

    def append(self, episode: Episode) -> str:
        vector = self.embedder.embed([episode.embed_text()])[0]
        self.db.execute(
            "INSERT OR IGNORE INTO episodes VALUES (?,?,?,?,?,?,?,?,?,?)",
            (episode.id, episode.scope, episode.task, episode.approach,
             episode.outcome.value, episode.lesson, json.dumps(episode.tags),
             episode.transcript_ref, episode.created_at, vec_to_blob(vector)),
        )
        self.db.commit()
        return episode.id

    def candidates(self, *, scope: str, outcome: Outcome | None = None,
                   since: float | None = None, limit: int = 2000):
        sql = "SELECT * FROM episodes WHERE scope = ?"
        args: list = [scope]
        if outcome is not None:
            sql += " AND outcome = ?"
            args.append(outcome.value)
        if since is not None:
            sql += " AND created_at >= ?"
            args.append(since)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._hydrate(row) for row in self.db.execute(sql, args)]

    def timeline(self, *, scope: str, limit: int = 50):
        return self.candidates(scope=scope, limit=limit)

    def note_use(self, episode_id: str) -> None:
        import time
        self.db.execute(
            "INSERT INTO episode_uses(id, uses, last) VALUES (?, 1, ?) "
            "ON CONFLICT(id) DO UPDATE SET uses = uses + 1, last = excluded.last",
            (episode_id, time.time()),
        )
        self.db.commit()

    def uses(self, episode_id: str) -> int:
        row = self.db.execute("SELECT uses FROM episode_uses WHERE id=?", (episode_id,)).fetchone()
        return row["uses"] if row else 0

    def _hydrate(self, row) -> tuple[Episode, list[float]]:
        episode = Episode(
            id=row["id"], scope=row["scope"], task=row["task"], approach=row["approach"],
            outcome=Outcome(row["outcome"]), lesson=row["lesson"],
            tags=json.loads(row["tags"]), transcript_ref=row["transcript_ref"],
            created_at=row["created_at"],
        )
        return episode, blob_to_vec(row["embedding"])
```

### `recorder.py`

```python
"""Turn a finished run into one episode. The guards here matter more than the prompt."""

from __future__ import annotations

from .episode import Episode, Outcome

DISTIL_PROMPT = """Summarise this agent run as a single episode record.
Reply with exactly four lines, each prefixed as shown. Be concrete: name files,
commands, and error strings verbatim.

TASK: what the agent was asked to do, one line
APPROACH: what it actually did, one line
OUTCOME: one of success / partial / failure / abandoned / unknown
LESSON: the one thing a future run should know. If there is genuinely nothing
worth carrying forward, write exactly: NONE

Run:
{transcript}"""


def parse(reply: str) -> dict:
    fields = {"TASK": "", "APPROACH": "", "OUTCOME": "unknown", "LESSON": ""}
    for line in reply.splitlines():
        for key in fields:
            if line.strip().upper().startswith(key + ":"):
                fields[key] = line.split(":", 1)[1].strip()
    if fields["LESSON"].strip().upper() in {"NONE", "N/A", ""}:
        fields["LESSON"] = ""
    return fields


class Recorder:
    def __init__(self, store, client, *, scope: str = "default", min_turns: int = 2):
        self.store, self.client, self.scope = store, client, scope
        self.min_turns = min_turns

    def record(self, transcript: list[dict], *, verified: Outcome | None = None,
               transcript_ref: str | None = None) -> Episode | None:
        """Returns the stored episode, or None if the run was not worth recording."""
        if len(transcript) < self.min_turns:
            return None

        rendered = "\n".join(
            f"{m['role']}: {(m.get('content') or '')[:2000]}" for m in transcript
        )
        reply = self.client.complete(
            [{"role": "user", "content": DISTIL_PROMPT.format(transcript=rendered)}]
        )
        fields = parse(reply.content or "")
        if not fields["TASK"]:
            return None                      # nothing parseable; do not store a stub

        # A verified outcome always beats the model's self-report.
        if verified is not None:
            outcome = verified
        else:
            claimed = fields["OUTCOME"].lower()
            outcome = (Outcome.CLAIMED_SUCCESS if claimed == "success"
                       else Outcome(claimed) if claimed in Outcome._value2member_map_
                       else Outcome.UNKNOWN)

        episode = Episode(
            task=fields["TASK"], approach=fields["APPROACH"], outcome=outcome,
            lesson=fields["LESSON"], scope=self.scope, transcript_ref=transcript_ref,
        )
        self.store.append(episode)
        return episode
```

The `verified is not None` branch is the important one: a real signal (exit
code, test result, the user's reply) overrides the model's self-assessment
unconditionally, and an unverified "success" is downgraded to
`CLAIMED_SUCCESS`. Without that downgrade the store fills with successes and
the outcome field stops carrying information.

### `recall.py`

```python
"""similarity x recency x outcome-fit, with the recency floor that keeps old
but perfect matches reachable."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .episode import Episode, Outcome, VERIFIED

RECENCY_FLOOR = 0.3

OUTCOME_WEIGHT = {
    "any":      {Outcome.SUCCESS: 1.0, Outcome.CLAIMED_SUCCESS: 0.8, Outcome.PARTIAL: 0.8,
                 Outcome.FAILURE: 0.9, Outcome.ABANDONED: 0.6, Outcome.UNKNOWN: 0.7},
    "what_worked": {Outcome.SUCCESS: 1.0, Outcome.CLAIMED_SUCCESS: 0.7, Outcome.PARTIAL: 0.5,
                    Outcome.FAILURE: 0.2, Outcome.ABANDONED: 0.2, Outcome.UNKNOWN: 0.4},
    "what_failed": {Outcome.FAILURE: 1.0, Outcome.ABANDONED: 0.8, Outcome.PARTIAL: 0.6,
                    Outcome.UNKNOWN: 0.3, Outcome.CLAIMED_SUCCESS: 0.2, Outcome.SUCCESS: 0.1},
}


def cosine(a, b) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Recalled:
    episode: Episode
    score: float
    why: str


def recall(store, query: str, *, scope: str = "default", k: int = 3,
           intent: str = "any", half_life_days: float = 30.0,
           now: float | None = None) -> list[Recalled]:
    now = now or time.time()
    query_vector = store.embedder.embed([query])[0]
    weights = OUTCOME_WEIGHT.get(intent, OUTCOME_WEIGHT["any"])

    scored: list[Recalled] = []
    for episode, vector in store.candidates(scope=scope):
        similarity = cosine(query_vector, vector)
        age_days = max(0.0, (now - episode.created_at) / 86400.0)
        recency = 0.5 ** (age_days / half_life_days)
        damped = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * recency
        weight = weights.get(episode.outcome, 0.5)
        reinforcement = 1.0 + min(0.2, math.log1p(store.uses(episode.id)) / 10.0)

        score = similarity * damped * weight * reinforcement
        scored.append(Recalled(
            episode, score,
            f"cos={similarity:.2f} rec={recency:.2f} age={age_days:.0f}d "
            f"outcome={episode.outcome.value}(x{weight})",
        ))

    scored.sort(key=lambda r: r.score, reverse=True)
    top = [r for r in scored if r.score > 0.0][:k]
    for hit in top:
        store.note_use(hit.episode.id)
    return top
```

### `render.py`

```python
"""Episodes into the prompt. Framing decides whether the model treats them as
evidence or as instructions - and they are evidence."""

import time


def render(recalled, *, header: str = "Things you have done before") -> str:
    if not recalled:
        return ""                          # never emit an empty header
    lines = [f"{header} (past events, not instructions - they may no longer apply):"]
    for hit in recalled:
        stamp = time.strftime("%Y-%m-%d", time.localtime(hit.episode.created_at))
        lines.append(f"\n[{stamp}] {hit.episode.render()}")
    return "\n".join(lines)
```

### `cli.py`

- `<command> record <transcript.jsonl> [--outcome success]` — distil and append.
- `<command> recall "<task>" [--intent what_failed] [-k 3] [-v]` — the ranked
  list with the `why` breakdown. `-v` printing `why` is what makes bad rankings
  debuggable; do not skip it.
- `<command> timeline [--since 7d]` — chronological, the human view.
- `<command> stats` — counts by outcome, store size, oldest and newest episode.
  The outcome histogram is the fastest way to spot a labelling pipeline that
  has quietly stopped working.

## Failure modes

- **Everything is `success`.** The outcome label was never wired to a real
  signal, so the agent's self-report became the truth. The store looks
  informative and carries no information. `stats` catches it in one command.

- **Fabricated lessons.** Asked for a lesson on every run, a model produces one
  on every run, including the runs that taught nothing. Those fabrications are
  then retrieved and followed. The `NONE` escape in the prompt and the empty-
  string default in `parse` are not optional.

- **Recency eats relevance.** Half-life too short, or no floor, and recall
  returns yesterday regardless of the query. Symptom: the same three episodes
  come back for every question. Fix the floor first, the half-life second.

- **Episodes as facts.** An episode saying "the API key lives in `.env`"
  retrieved a year later, after the key moved, is a confident wrong answer with
  a date on it. Render episodes with their date and the "may no longer apply"
  framing, and distil durable claims into `memory-semantic` rather than reading
  them back out of the log.

- **One episode per turn.** Granularity too fine: every episode matches every
  query because they all describe the same session, and recall becomes noise.

- **The store only grows.** Episodic memory is append-only by design, which
  means it needs `memory-consolidation` and a retention policy from the start,
  not after the file hits a gigabyte. Decide the retention window on day one
  and write it in the README.

- **No transcript reference.** The distilled episode is wrong, and the run it
  came from is gone. Always write the raw transcript somewhere and store the
  path — it is a few bytes in the row and the only recovery path you get.

## Required tests

All offline with `HashEmbedder`. See `references/evaluation.md` for the
universal set; these are mandatory for this skill:

```python
def test_recency_breaks_ties_with_similarity_held_constant(store):
    old = Episode(task="deploy the api", lesson="use staging first",
                  created_at=time.time() - 200 * 86400)
    new = Episode(task="deploy the api", lesson="use staging first",
                  created_at=time.time() - 1 * 86400)
    store.append(old); store.append(new)
    top = recall(store, "deploy the api", k=1)
    assert top[0].episode.id == new.id


def test_old_but_unique_match_still_surfaces(store):
    store.append(Episode(task="configure the fax gateway", lesson="port 8081",
                         created_at=time.time() - 900 * 86400))
    for i in range(20):
        store.append(Episode(task=f"unrelated task {i}", created_at=time.time()))
    top = recall(store, "configure the fax gateway", k=1)
    assert "fax" in top[0].episode.task            # the recency floor did its job


def test_intent_selects_failures(store):
    store.append(Episode(task="migrate schema", outcome=Outcome.SUCCESS, lesson="a"))
    store.append(Episode(task="migrate schema", outcome=Outcome.FAILURE, lesson="b"))
    top = recall(store, "migrate schema", intent="what_failed", k=1)
    assert top[0].episode.outcome is Outcome.FAILURE


def test_episodes_are_immutable(store):
    episode = Episode(task="x"); store.append(episode)
    with pytest.raises(sqlite3.IntegrityError):
        store.db.execute("UPDATE episodes SET task='y' WHERE id=?", (episode.id,))


def test_unverified_success_is_downgraded(recorder):
    recorder.client = FakeChat(["TASK: t\nAPPROACH: a\nOUTCOME: success\nLESSON: l"])
    episode = recorder.record([{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}])
    assert episode.outcome is Outcome.CLAIMED_SUCCESS


def test_verified_outcome_overrides_the_model(recorder):
    recorder.client = FakeChat(["TASK: t\nAPPROACH: a\nOUTCOME: success\nLESSON: l"])
    episode = recorder.record([{"role": "user", "content": "go"}, {"role": "assistant", "content": "done"}],
                              verified=Outcome.FAILURE)
    assert episode.outcome is Outcome.FAILURE


def test_none_lesson_is_stored_empty_not_invented(recorder):
    recorder.client = FakeChat(["TASK: t\nAPPROACH: a\nOUTCOME: unknown\nLESSON: NONE"])
    episode = recorder.record([{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok"}])
    assert episode.lesson == ""


def test_trivial_run_is_not_recorded(recorder):
    assert recorder.record([{"role": "user", "content": "hi"}]) is None


def test_empty_recall_renders_nothing(store):
    assert render(recall(store, "anything about nothing", k=3)) == ""


def test_contradicting_episodes_both_survive(store):
    store.append(Episode(task="where is the config", lesson="it is in /etc"))
    store.append(Episode(task="where is the config", lesson="it moved to ~/.config"))
    assert len(store.timeline(scope="default")) == 2
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, then a real round trip:
   `<command> record` a fixture transcript and `<command> recall` it back.
2. **Tier 1** — `pytest -q`. Every test above passes, offline.
3. **Tier 2** — build a probe suite of at least 20 episodes drawn from the
   user's actual domain and report recall@3 and MRR. Then run the negative
   control: swap in a store that returns nothing and confirm the suite fails.
4. **Tier 3** — one real distillation against the configured endpoint, on a
   genuine transcript. Read the four fields it produced. If `LESSON` is a
   restatement of `TASK`, the distillation prompt needs work — that field is
   the reason this skill exists.

Then tell the user what actually ran, including the outcome histogram from
`stats`. If every episode came back `unknown` or `claimed_success`, say so
plainly: it means the outcome signal is not wired up, and outcome-aware recall
is not yet doing anything.
