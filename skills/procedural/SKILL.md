---
name: memory-procedural
description: "Build procedural memory: playbooks the agent learns from repeated episodes, matched by precondition, replayed and demoted on failure"
---

# Procedural Memory

Semantic memory holds *that* something is true. Procedural memory holds *how* to
do something — the sequence the agent worked out the hard way, promoted to a
playbook so the next run does not have to rediscover it.

It is the only memory type here that changes the agent's **behaviour** rather
than its context, and that is what makes it the highest-value and the most
dangerous. A stale fact produces a wrong sentence. A stale playbook produces a
wrong sequence of actions, confidently, at speed.

So the design has two halves of equal weight: learning procedures, and
**distrusting them** — precondition matching, success tracking, and automatic
demotion when the world moves.

```
  episodes ──► cluster by task shape ──► 3+ successes, same steps?
                                              │ yes
                                              ▼
                                      induce a playbook
                                      (preconditions + steps + checks)
                                              │
  new task ──► match preconditions ──► trust above floor? ──► offer it
                     │ no match                   │ no
                     ▼                            ▼
                 think normally              think normally
                                              │
                        replay ──► succeeded? ──► trust up
                                       │ no
                                       ▼
                                   trust down; below floor ⇒ retired
```

## Use this when

- The agent re-derives the same multi-step routine every run — the same six
  commands to cut a release, the same diagnostic ladder for a class of bug.
- You have an episodic log with genuine repetition in it. Procedural memory is
  induced from episodes; without `memory-episodic` or an equivalent, there is
  nothing to induce from.
- The routine is longer than the agent reliably remembers and shorter than a
  program worth writing. That band — roughly 4 to 20 steps, with judgement at a
  few of them — is where playbooks earn their keep.

**Do not use this when** the task varies more than it repeats. A playbook is a
bet that the next instance resembles the last; when it does not, the agent
follows a confidently wrong script instead of thinking, and the failure is
harder to spot than an ordinary mistake because the steps look deliberate.

**Do not use this when** the routine is fully deterministic either. If there is
no judgement in it, write a script and give the agent a tool that calls it. A
playbook for `git add -A && git commit && git push` is a worse shell alias.

## Workflow

1. **Ask what repeats.** Get 2-3 concrete routines the user watches the agent
   redo. If they cannot name any, this skill is premature — say so and point at
   `memory-episodic` to gather the evidence first.

2. **Ask the replay mode.** Advisory (the playbook is injected as a suggestion
   the agent may deviate from) or strict (the steps are executed in order with
   checks between). Advisory is the default and the right answer for almost
   everything; see the decision below.

3. **Scaffold the project** into the directory the user names. Kebab-case
   folder, snake_case package, `hatchling` + `pyproject.toml`, `src/` layout.

4. **Write the six modules**: `procedure.py`, `store.py`, `inducer.py`
   (episodes → playbook), `matcher.py` (preconditions), `replayer.py`,
   `trust.py`. Then `cli.py`.

5. **Verify against the contract** in `references/evaluation.md`, plus the
   procedural-specific tests below.

## The decisions that matter

### 1. What a procedure actually contains

Four parts. Drop any one and the skill stops working:

```yaml
name: cut-a-release
goal: Publish a new version to PyPI
preconditions:            # ALL must hold, or the playbook does not apply
  - repo has a pyproject.toml
  - working tree is clean
  - user asked to release / publish / ship
steps:
  - do: run the full test suite
    check: exit code 0                     # how to know this step worked
    on_fail: stop and report; never release on red
  - do: bump the version in pyproject.toml
    check: the new version is greater than the published one
  - do: build with `python -m build`
    check: dist/ contains a wheel and an sdist
  - do: upload with twine
    check: the new version resolves on PyPI
anti_preconditions:       # ANY of these and the playbook is wrong
  - the repo is a monorepo with per-package versioning
```

**`check` is the field that makes this safe.** A playbook of `do` steps with no
verification is a macro — it executes the whole sequence into a broken state
without noticing. Every step must say how the agent knows it worked, and a step
whose check cannot be expressed is a step that needs judgement and should say so
explicitly rather than pretending to be mechanical.

**`anti_preconditions` catch the cases that look right and are not.** They are
where you encode the failures: every time a playbook fires wrongly, the fix is
usually one more anti-precondition, not a rewrite.

### 2. Advisory or strict replay

| Mode | What the agent gets | Use when |
| --- | --- | --- |
| **Advisory** (default) | The playbook in the prompt as "here is how this went last time", steps as guidance | Almost always |
| **Strict** | A driver executes steps in order, running each `check`, halting on failure | The routine is safety-critical *and* fully checkable |

Advisory keeps the model in the loop, which is the entire reason you hired one.
It handles "step 3 does not apply this time" gracefully, and its failure mode is
the agent ignoring good advice — recoverable.

Strict replay's failure mode is executing step 4 against a world that changed
after step 2. Choose it only when every step has a real check, and always with
`on_fail` behaviour defined per step. If you cannot fill in `check` for every
step, you do not have a strict-replay candidate.

### 3. Promotion — when an episode pattern becomes a playbook

The threshold decides whether the store is useful or full of one-off noise.
Require all of:

- **At least 3 episodes** with the same task shape. Two is a coincidence.
- **At least 2 verified successes** (`Outcome.SUCCESS`, not `claimed_success` —
  see `memory-episodic`). A playbook induced from self-reported success encodes
  whatever the agent believed it did.
- **Consistent step structure.** If three successes took three different routes,
  there is no procedure, only a goal. Storing a playbook here averages three
  approaches into one that has never been executed.

Never induce from a single brilliant run. The one-shot success is exactly the
case where the agent got lucky, and promoting it turns luck into policy.

### 4. Trust, and how a playbook dies

Every procedure carries `uses`, `successes`, and a derived trust score. Use a
smoothed rate so one early failure does not retire a good playbook and three
early successes do not enshrine a bad one:

```python
trust = (successes + 1) / (uses + 2)        # Laplace smoothing: starts at 0.5
```

Three bands, and the middle one matters most:

- **trust ≥ 0.7** — offered normally.
- **0.4 ≤ trust < 0.7** — offered with an explicit caveat: "this worked 3 of 6
  times; verify each step." Still useful, honestly labelled.
- **trust < 0.4 after 5+ uses** — retired. Kept in the store, not offered.

**Retire, never delete.** A retired playbook is the best possible evidence for
why the current approach is what it is, and the record you want when the same
idea gets proposed again. It is also recoverable when the environment that broke
it gets fixed.

**Decay trust with age.** A playbook untouched for six months is describing an
environment that may no longer exist. Halve the effective `uses` count every 90
days so an old playbook has to re-earn its trust rather than coasting on a
record set against a previous version of the world.

### 5. Matching preconditions without a model call

Precondition matching runs on every task. A model call there is a tax on the
whole system, so make matching cheap and let the model adjudicate only the
survivors:

1. **Cheap filter** — lexical and vector similarity between the incoming task
   and the playbook `goal`, top 5.
2. **Structured checks** — preconditions expressible as code (`file_exists`,
   `git_clean`, `tool_available`) run directly. Any failure eliminates the
   candidate.
3. **Model adjudication** — only for the remaining 1-2, and only for
   preconditions written in prose. One call, one clear question: "do these
   conditions hold for this task? yes/no per condition."

**Unknown is not yes.** A precondition that cannot be evaluated must count as
failed. The cost of not offering a playbook is one ordinary run; the cost of
offering the wrong one is a confident wrong sequence.

## Build it

```
<project>/
├── pyproject.toml
├── .env.example
├── README.md
└── src/<package>/
    ├── __init__.py
    ├── config.py        # thresholds: min_episodes, trust floor, decay
    ├── procedure.py     # Procedure + Step, trust math, status
    ├── store.py         # SQLite: procedures, uses, lineage to episodes
    ├── inducer.py       # episode clusters -> candidate playbook
    ├── matcher.py       # 3-stage precondition matching
    ├── replayer.py      # advisory render + strict driver
    ├── trust.py         # record outcomes, decay, retire
    └── cli.py           # induce / list / show / match / replay / retire
```

### `procedure.py`

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum


class ProcStatus(str, Enum):
    ACTIVE = "active"
    PROVISIONAL = "provisional"     # induced, not yet used successfully
    RETIRED = "retired"


@dataclass
class Step:
    do: str
    check: str | None = None        # None means "needs judgement" - say so out loud
    on_fail: str = "stop and report"

    @property
    def mechanical(self) -> bool:
        return bool(self.check)


@dataclass
class Procedure:
    name: str
    goal: str
    steps: list[Step]
    preconditions: list[str] = field(default_factory=list)
    anti_preconditions: list[str] = field(default_factory=list)
    scope: str = "default"
    induced_from: list[str] = field(default_factory=list)   # episode ids
    uses: int = 0
    successes: int = 0
    status: ProcStatus = ProcStatus.PROVISIONAL
    created_at: float = field(default_factory=time.time)
    last_used: float | None = None
    id: str = ""

    def __post_init__(self):
        if not self.id:
            payload = f"{self.scope}\x1f{self.name}".encode()
            self.id = "proc_" + hashlib.blake2b(payload, digest_size=8).hexdigest()

    def trust(self, *, now: float | None = None, half_life_days: float = 90.0) -> float:
        """Laplace-smoothed success rate, with the evidence decayed by age."""
        now = now or time.time()
        reference = self.last_used or self.created_at
        age_days = max(0.0, (now - reference) / 86400.0)
        weight = 0.5 ** (age_days / half_life_days)
        uses = self.uses * weight
        successes = self.successes * weight
        return (successes + 1.0) / (uses + 2.0)

    @property
    def fully_mechanical(self) -> bool:
        """Strict replay requires this. If it is False, strict mode is unsafe."""
        return bool(self.steps) and all(s.mechanical for s in self.steps)
```

### `inducer.py`

```python
"""Episodes -> a candidate playbook. Refuses more often than it produces."""

from __future__ import annotations

import json
from collections import defaultdict

from .procedure import Procedure, Step

INDUCE_PROMPT = """You are given several past runs that accomplished the same
kind of task. Induce the procedure they share.

Rules:
- Only include steps that appear in EVERY successful run. A step from one run is
  not part of the procedure.
- For each step give a `check`: an observable that proves the step worked. If a
  step needs human judgement and has no observable check, set check to null.
- `preconditions`: what must be true for this procedure to apply.
- `anti_preconditions`: situations where following it would be wrong.
- If the runs do not actually share a procedure - different routes to the same
  goal - reply exactly: NO_COMMON_PROCEDURE

Reply as JSON:
{{"name": "kebab-case", "goal": "...", "preconditions": [...],
  "anti_preconditions": [...],
  "steps": [{{"do": "...", "check": "..." | null, "on_fail": "..."}}]}}

Runs:
{runs}"""


def cluster_episodes(episodes, *, similarity, threshold: float = 0.75) -> list[list]:
    """Greedy clustering on task text. Cheap, and good enough at this scale."""
    clusters: list[list] = []
    for episode in episodes:
        for cluster in clusters:
            if similarity(episode.task, cluster[0].task) >= threshold:
                cluster.append(episode)
                break
        else:
            clusters.append([episode])
    return clusters


class Inducer:
    def __init__(self, client, *, min_episodes: int = 3, min_verified_successes: int = 2):
        self.client = client
        self.min_episodes = min_episodes
        self.min_verified = min_verified_successes
        self.skipped: list[tuple[str, str]] = []       # (cluster task, reason)

    def induce(self, cluster: list) -> Procedure | None:
        if len(cluster) < self.min_episodes:
            self.skipped.append((cluster[0].task, f"only {len(cluster)} episodes"))
            return None

        verified = [e for e in cluster if e.outcome.value == "success"]
        if len(verified) < self.min_verified:
            self.skipped.append((cluster[0].task,
                                 f"only {len(verified)} verified successes"))
            return None

        runs = "\n\n".join(f"--- run {i + 1} ---\n{e.render()}" for i, e in enumerate(verified))
        reply = self.client.complete([{"role": "user", "content": INDUCE_PROMPT.format(runs=runs)}])
        raw = (reply.content or "").strip()
        if "NO_COMMON_PROCEDURE" in raw:
            self.skipped.append((cluster[0].task, "no shared procedure across runs"))
            return None

        raw = raw[raw.find("{") : raw.rfind("}") + 1] if "{" in raw else ""
        try:
            spec = json.loads(raw)
        except json.JSONDecodeError:
            self.skipped.append((cluster[0].task, "unparseable induction"))
            return None

        steps = [Step(do=s["do"], check=s.get("check"),
                      on_fail=s.get("on_fail", "stop and report"))
                 for s in spec.get("steps", []) if s.get("do")]
        if len(steps) < 2:
            self.skipped.append((cluster[0].task, "fewer than 2 steps - not a procedure"))
            return None

        return Procedure(
            name=spec["name"], goal=spec["goal"], steps=steps,
            preconditions=spec.get("preconditions", []),
            anti_preconditions=spec.get("anti_preconditions", []),
            induced_from=[e.id for e in verified],
        )
```

`self.skipped` is the diagnostic that matters. Induction should refuse most
clusters; if it never refuses, the thresholds are not being applied and you are
building a library of one-offs.

### `matcher.py`

```python
"""Three stages, cheapest first. Unknown counts as failed."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .procedure import ProcStatus

ADJUDICATE_PROMPT = """For the task below, decide whether each condition holds.
Reply with a JSON object mapping each condition to true, false, or "unknown".
Use "unknown" when the task does not say - do not guess.

Task: {task}
Context: {context}

Conditions: {conditions}"""


@dataclass
class Match:
    procedure: object
    trust: float
    caveat: str = ""
    why: str = ""


class Matcher:
    def __init__(self, store, client, *, similarity, structured_checks: dict | None = None,
                 trust_floor: float = 0.4, caveat_below: float = 0.7):
        self.store, self.client, self.similarity = store, client, similarity
        self.checks = structured_checks or {}       # name -> callable(context) -> bool | None
        self.trust_floor, self.caveat_below = trust_floor, caveat_below

    def match(self, task: str, context: dict, *, scope: str = "default") -> Match | None:
        candidates = [
            p for p in self.store.all(scope=scope)
            if p.status is not ProcStatus.RETIRED and p.trust() >= self.trust_floor
        ]
        candidates.sort(key=lambda p: self.similarity(task, p.goal), reverse=True)
        candidates = [p for p in candidates[:5] if self.similarity(task, p.goal) > 0.4]
        if not candidates:
            return None

        # Stage 2: structured checks, free and exact.
        survivors = []
        for procedure in candidates:
            prose = []
            failed = False
            for condition in procedure.preconditions:
                check = self.checks.get(condition)
                if check is None:
                    prose.append(condition)
                elif check(context) is not True:       # None (unknown) also fails
                    failed = True
                    break
            if not failed:
                survivors.append((procedure, prose))
        if not survivors:
            return None

        # Stage 3: one model call, for the best survivor's prose conditions only.
        procedure, prose = survivors[0]
        if prose or procedure.anti_preconditions:
            verdicts = self._adjudicate(task, context, prose + procedure.anti_preconditions)
            for condition in prose:
                if verdicts.get(condition) is not True:
                    return None                        # unknown counts as failed
            for condition in procedure.anti_preconditions:
                if verdicts.get(condition) is True:
                    return None

        trust = procedure.trust()
        caveat = ""
        if trust < self.caveat_below:
            caveat = (f"This worked {procedure.successes} of {procedure.uses} times. "
                      f"Verify each step rather than assuming it applies.")
        return Match(procedure, trust, caveat, why=f"goal match, {len(prose)} prose conditions held")

    def _adjudicate(self, task, context, conditions) -> dict:
        if not conditions:
            return {}
        reply = self.client.complete([{"role": "user", "content": ADJUDICATE_PROMPT.format(
            task=task, context=json.dumps(context, default=str)[:2000],
            conditions=json.dumps(conditions))}])
        raw = (reply.content or "")
        raw = raw[raw.find("{") : raw.rfind("}") + 1] if "{" in raw else "{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}                                   # unparseable = nothing holds
```

### `replayer.py`

```python
"""Advisory render (the default) and the strict driver (rarely the right choice)."""

from __future__ import annotations


def render_advisory(match) -> str:
    procedure = match.procedure
    lines = [
        f"You have done this before. Playbook '{procedure.name}' "
        f"(worked {procedure.successes}/{procedure.uses} times, trust {match.trust:.2f}):",
        f"Goal: {procedure.goal}",
        "Steps that worked last time - adapt them, do not follow blindly:",
    ]
    for index, step in enumerate(procedure.steps, 1):
        check = f"  (confirm: {step.check})" if step.check else "  (needs your judgement)"
        lines.append(f"  {index}. {step.do}{check}")
    if procedure.anti_preconditions:
        lines.append("Do NOT use this playbook if: " + "; ".join(procedure.anti_preconditions))
    if match.caveat:
        lines.append(match.caveat)
    return "\n".join(lines)


class StrictReplayer:
    """Executes steps in order, running each check. Halts on the first failure."""

    def __init__(self, executor, checker):
        self.executor, self.checker = executor, checker     # both callable(str) -> (ok, detail)

    def replay(self, procedure) -> dict:
        if not procedure.fully_mechanical:
            raise ValueError(
                f"'{procedure.name}' has steps without checks; strict replay is unsafe. "
                f"Use advisory mode."
            )
        trace = []
        for index, step in enumerate(procedure.steps, 1):
            ok, detail = self.executor(step.do)
            trace.append({"step": index, "do": step.do, "executed": ok, "detail": detail})
            if not ok:
                return {"ok": False, "failed_at": index, "reason": detail,
                        "on_fail": step.on_fail, "trace": trace}
            ok, detail = self.checker(step.check)
            trace[-1]["checked"] = ok
            if not ok:
                return {"ok": False, "failed_at": index, "reason": f"check failed: {detail}",
                        "on_fail": step.on_fail, "trace": trace}
        return {"ok": True, "trace": trace}
```

### `trust.py`

```python
"""Record what happened, and retire what stopped working."""

from __future__ import annotations

from .procedure import ProcStatus

RETIRE_BELOW = 0.4
RETIRE_AFTER_USES = 5


def record(store, procedure_id: str, *, succeeded: bool, note: str = "") -> None:
    procedure = store.get(procedure_id)
    procedure.uses += 1
    procedure.successes += int(succeeded)
    procedure.last_used = __import__("time").time()

    if procedure.status is ProcStatus.PROVISIONAL and succeeded:
        procedure.status = ProcStatus.ACTIVE
    if procedure.uses >= RETIRE_AFTER_USES and procedure.trust() < RETIRE_BELOW:
        procedure.status = ProcStatus.RETIRED          # kept, not deleted

    store.put(procedure)
    store.log_use(procedure_id, succeeded=succeeded, note=note)
```

### `cli.py`

- `<command> induce [--from-episodes <db>] [--dry-run]` — cluster, induce,
  print what it would add and everything it skipped with the reason. Default
  to dry-run; writing playbooks should be a deliberate act.
- `<command> list [--include-retired]` — name, trust, uses, last used.
- `<command> show <name>` — the full playbook, with checks.
- `<command> match "<task>"` — which playbook fires and why, or why none did.
  This is the debugging command.
- `<command> retire <name>` / `<command> revive <name>`.

## Failure modes

- **A playbook fires in the wrong situation.** The single worst outcome, and
  almost always a missing anti-precondition rather than a bad match score. When
  it happens, add the anti-precondition; do not loosen the trust floor.

- **Induction from claimed successes.** The agent said it worked, nobody
  checked, and the playbook encodes a routine that never actually succeeded.
  Require verified outcomes — this is why `memory-episodic` distinguishes
  `success` from `claimed_success`.

- **Averaged procedures.** Three different routes to one goal, induced into a
  single playbook that is none of them. The `NO_COMMON_PROCEDURE` escape exists
  for this and the model will use it if you let it.

- **Steps without checks in strict mode.** The driver executes step 4 into a
  state step 2 already broke. `fully_mechanical` should raise, not warn.

- **Trust that only goes up.** If the failure path never calls `record(...,
  succeeded=False)`, every playbook drifts to trust 1.0 and nothing is ever
  retired. Wire the failure path first, before the success path.

- **Playbooks that outlive their environment.** The deploy procedure from before
  the CI migration, still at trust 0.9 because nobody has run it since. Age
  decay handles this; a trust score with no decay does not.

- **Too many playbooks.** Above roughly thirty, matching gets unreliable and the
  library becomes its own retrieval problem. If you are there, the granularity
  is too fine — merge related playbooks behind branching steps.

## Required tests

All offline. See `references/evaluation.md` for the universal set; these are
mandatory:

```python
def test_induction_refuses_below_threshold(inducer):
    assert inducer.induce([episode("deploy"), episode("deploy")]) is None
    assert "only 2 episodes" in inducer.skipped[-1][1]


def test_induction_refuses_unverified_successes(inducer):
    cluster = [episode("deploy", outcome=Outcome.CLAIMED_SUCCESS) for _ in range(4)]
    assert inducer.induce(cluster) is None


def test_no_common_procedure_is_honoured(inducer):
    inducer.client = FakeChat(["NO_COMMON_PROCEDURE"])
    assert inducer.induce(verified_cluster(4)) is None


def test_anti_precondition_blocks_the_match(matcher):
    matcher.client = FakeChat(['{"the repo is a monorepo": true}'])
    assert matcher.match("release the package", context={}) is None


def test_unknown_precondition_counts_as_failed(matcher):
    matcher.client = FakeChat(['{"working tree is clean": "unknown"}'])
    assert matcher.match("release the package", context={}) is None


def test_structured_check_short_circuits_without_a_model_call(matcher):
    matcher.checks = {"working tree is clean": lambda ctx: False}
    matcher.client = FakeChat([])            # any call would raise
    assert matcher.match("release the package", context={}) is None


def test_failure_lowers_trust_and_eventually_retires(store):
    procedure = active_procedure(uses=0, successes=0)
    store.put(procedure)
    for _ in range(6):
        record(store, procedure.id, succeeded=False)
    assert store.get(procedure.id).status is ProcStatus.RETIRED
    assert store.get(procedure.id) is not None        # retired, not deleted


def test_retired_playbooks_are_not_offered(matcher, store):
    store.put(retired_procedure())
    assert matcher.match("the exact goal of the retired playbook", context={}) is None


def test_trust_decays_with_age(store):
    fresh = procedure(uses=10, successes=9, last_used=time.time())
    stale = procedure(uses=10, successes=9, last_used=time.time() - 365 * 86400)
    assert stale.trust() < fresh.trust()


def test_strict_replay_refuses_unchecked_steps():
    procedure = Procedure(name="p", goal="g", steps=[Step(do="think about it", check=None)])
    with pytest.raises(ValueError, match="strict replay is unsafe"):
        StrictReplayer(noop, noop).replay(procedure)


def test_strict_replay_halts_at_the_first_failed_check():
    result = StrictReplayer(always_ok, fails_on(2)).replay(three_step_procedure())
    assert result["ok"] is False and result["failed_at"] == 2
    assert len(result["trace"]) == 2                   # did not continue


def test_low_trust_playbook_carries_a_caveat(matcher, store):
    store.put(procedure(uses=6, successes=3))
    match = matcher.match("the goal", context={})
    assert "3 of 6" in match.caveat
    assert "Verify each step" in render_advisory(match)
```

## Verify

Follow `references/evaluation.md`. For this skill specifically:

1. **Tier 0** — install, import, `--help`, `<command> list` on an empty store.
2. **Tier 1** — `pytest -q`, offline. Every test above.
3. **Tier 2** — run `induce --dry-run` over the user's real episode log and read
   both outputs. The skip list should be longer than the induced list. If it is
   not, the thresholds are not firing and you are about to build a library of
   one-offs.
4. **Tier 3** — take one induced playbook and one task it should *not* match,
   run `match` on both, and check the `why`. A playbook that matches everything
   is worse than no playbook: it replaces thinking with a script chosen at
   random.

Then tell the user what actually ran: how many clusters, how many induced, how
many refused and why, and the trust distribution across the library. A library
where every playbook is at trust 0.5 has never recorded an outcome — the
feedback path is not wired, and nothing will ever be retired.
