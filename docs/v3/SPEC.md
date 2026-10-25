# v3 agent: spec (draft)

Status: draft for Evan's review. Nothing here is in the PLAN.md backlog yet.

## Why v3

Gary and Lorraine work, but they are slow to improve and hard to read. Each live-run failure added a rule ("one rule for X"), and the rules now live in hundreds of lines of prose and code across layers that call into each other. Strategy, task execution and twitch reactions are mixed. A newcomer cannot find where a decision is made, and neither can we.

Gary is v1: rules grown case by case. Lorraine is v2 and was meant to be a system. She drifted back into cases for four reasons:

1. **The LLM was demoted.** Her goal tree is a hand-written strategy. The model only answers "which op for this node", so every gap in the tree became a rule.
2. **She inherited Gary's opinions.** The ADK she plays on carries his rules (value tables, give-up memory, stuck ladders, the engagement estimate), and her reflexes are his priority list.
3. **The process stayed the same.** Each live-run failure became a backlog item, and each item added a clause (A168 to A172: blocks, waits, stall guards, give-ups). Her "how it thinks" is one paragraph of about 2,000 words over 3,400 lines.
4. **Nothing measured her.** With no progress number, added complexity was never weighed against what it bought.

v3 starts over on structure, keeps only plumbing with no opinions, and guards against each of those four: the strategist owns the plan, the ADK carries no strategy, size caps hold the code and this spec, and the measure below gates every change.

## The measure

**How annoyed Evan is, and how many cycles go by with no useful progress.** Made concrete:

- **In a run:** `minutes since last milestone` and `milestones per hour`. A milestone is a discrete, real step toward beating the game (list below). This is the headline number on every status line, trace and run report.
- **In development:** every v3 milestone ends with a live run whose milestones per hour beat the previous one. A slice that moves no number in a live run is not done, however clean the code.
- **No silent stalls:** a run with no milestone for `STALL_MINUTES` (default 10) stops itself and writes a one-page report: the goal, the plans tried, why each failed. Evan never watches a character do nothing.

Milestones (each counted once per character, kept across runs):

| Milestone | Example |
|---|---|
| Level cleared | `levels_cleared` grew |
| New map entered | first step onto a map id |
| Gear upgrade | a weapon or armor better than any held before |
| Capability gained | first item that opens, cuts, fords or breaks something new |
| New ground | each 16x16 chunk first seen |
| Gem high-water mark | gems held passed the next 25 |

## Decisions

| Question | Decision |
|---|---|
| ADK | Unopinionated: the loop, the interfaces, tracing and the Agent Realm plumbing. No strategy, no thresholds, no "one rule for X". |
| Plumbing | Reused from `agentrealm_adk` by an allowlist (below), not copied. Gary and Lorraine keep running unchanged. |
| Language | Python 3.11+, standard library only; `anthropic` only for the strategist. The plumbing is already Python, and people copying the agent expect Python. |
| Metric | The measure above. |

## Architecture

Three layers over one shared state. Each layer has one job and its own clock.

```
              ┌──────────────────────────────────────────────┐
  perceive ──►│ State: one snapshot per tick (the blackboard)│
              └──────────────────────────────────────────────┘
                    │                 │                 │
                    ▼                 ▼                 ▼
             1. Strategist      2. Executive      3. Controller
             LLM, on events     every tick        every tick
             goal + plan        current task      scores actions,
             (list of tasks)    done/failed?      sends the best
                    │                 │                 │
                    └──── plan ──────►└──── task ──────►└──► intent(s)
                    ◄──── report (done, failed + why, surprise) ───┘
```

**1. Strategist (slow, LLM).** Owns *what* and *why*. Given the state summary, the milestone ledger, the failures so far and the objective "beat every level", it returns a goal and a short plan: an ordered list of tasks from a fixed vocabulary, each with parameters and the reason it is there. It runs only on a trigger (below) and off the tick thread. Until its reply lands, the old plan keeps running.

**2. Executive (fast, deterministic).** Owns *which task now*. It holds the plan, checks the head task's `done` and `failed` against State each tick, and tracks the task's progress measure. It never improvises: a task that fails or stops progressing is reported to the strategist with why, and the next task runs meanwhile.

**3. Controller (fast, deterministic).** Owns *which action this tick*. Every tick it lists candidate actions (step toward the task's target, attack, flee, drink, pick up, open a block, wait), scores each one, and sends the best. Scores come from small considerations multiplied together: danger, health, battle odds, value, and how much the action advances the current task. Survival wins because its score spikes when it matters, not because an `if` comes first.

This is the standard three-layer pattern (deliberator, sequencer, controller), with an HTN-style plan in the middle and utility scoring at the bottom.

### State

One immutable snapshot built each tick from the world model: where we are, what's around (terrain, hostiles, supplies, NPCs), health, lives, gear, gems, known maps, levels cleared, the milestone ledger. Every derived fact (`threat_here`, `odds_vs(npc)`, `path_cost(to)`) is a pure function of State. Layers read State; only perception writes it.

### Replan triggers

The strategist runs when one of these fires, at most once per `MIN_REPLAN_SECONDS` (default 10):

- the plan is finished or empty
- a task failed or stalled (no progress for `TASK_STALL_SECONDS`)
- a surprise: death, new map, level cleared, new capability, a new kind of hostile, health lost faster than predicted
- `MAX_PLAN_AGE_SECONDS` (default 120) since the last plan

### Tasks (the strategist's vocabulary)

Each task kind is one small class: `target(state)`, `done(state)`, `failed(state)`, `progress(state)` (a number that should go up), and `wants(action, state)` (how much an action advances it, 0 to 1).

First vocabulary: `explore(area?)`, `goto(map, x, y)`, `gather(gems)`, `buy(item)`, `equip(item)`, `fight(npc)`, `heal`, `break(x, y)`, `use(x, y, item)`, `enter_level(n)`, `clear_level(n)`, `talk(npc)`, `read(x, y)`.

A new behavior is a new task kind or a new consideration. Never a branch inside the loop.

### Plan format

```json
{
  "goal": "clear level 2",
  "why": "level 1 cleared; level 2 entrance needs a key the east shop sells",
  "plan": [
    {"task": "gather", "gems": 30, "why": "key costs 30"},
    {"task": "buy", "item": "key", "why": "entrance is locked"},
    {"task": "enter_level", "level": 2},
    {"task": "clear_level", "level": 2}
  ],
  "risk": 0.5
}
```

`risk` (0 to 1) is the one knob the strategist sets on the controller: how much danger it may accept for task progress.

## Interfaces (ADK)

```python
class Perception(Protocol):
    def sense(self) -> State: ...                    # reads within the call budget

class Strategist(Protocol):
    def plan(self, state: State, report: Report) -> Plan | None: ...   # async, may return None

class Task(Protocol):
    def done(self, s: State) -> bool: ...
    def failed(self, s: State) -> str | None: ...    # reason, or None
    def progress(self, s: State) -> float: ...
    def wants(self, a: Action, s: State) -> float: ...

class Consideration(Protocol):
    def score(self, a: Action, s: State, task: Task, risk: float) -> float: ...   # 0..1

class Actuator(Protocol):
    def act(self, a: Action) -> None: ...            # intents, within the call budget

def run(perception, strategist, tasks, considerations, actuator, trace): ...
```

The loop, in full:

```python
while alive:
    s = perception.sense()
    ledger.update(s)                                 # milestones
    if triggers.fire(s, executive, ledger):
        strategist.request(s, executive.report())    # off-thread
    executive.adopt(strategist.latest())             # a new plan, if one landed
    task = executive.step(s)                         # done/failed/stall checks
    action = controller.best(candidates(s), s, task, plan.risk)
    actuator.act(action)
    trace.write(s, plan, task, top_scores, action, ledger)
```

## Plumbing reused from `agentrealm_adk`

Allowlist: `client`, `pacer`, `world`, `pathing`, `navigation`, `executor` (queue building, pacing), `knowledge_base`, `reference`, `llm`, `supplies`, `cli`. Anything else stays out until a v3 task needs it, and then it comes in only if it has no thresholds or strategy of its own. An architecture test holds the line.

## Packages

| Package | Holds | Imports |
|---|---|---|
| `python/adk3/core/` | loop, protocols, triggers, ledger, trace | standard library |
| `python/adk3/realm/` | Agent Realm `Perception`, `Actuator`, `State`, candidate actions | `core`, the allowlist |
| `python/<agent>/` | strategist prompt, task kinds, considerations | `adk3` only |

Size rules: no module over 300 lines; this spec stays under 300 lines. If the spec needs more, the design is wrong.

## Seeing it work

- **Status line every second:** `goal | task (progress) | action (score) | hp | gems | last milestone 3m ago | 4.2/h`.
- **Trace:** one JSON line per tick with State digest, plan, task, the top 3 scored actions with their factors, and the action sent. Replayable.
- **Run report:** milestones gained, time per milestone, plans tried, failures with why. Written at exit and on a stall stop.

## Milestones

Each ends with a live run and its numbers in `docs/observations/`.

| # | Done when |
|---|---|
| V0 | Loop, State, trace, ledger and status line run live with a trivial strategist (fixed plan `explore`) and two considerations (danger, task). New-ground milestones tick up; no deaths from standing still. |
| V1 | LLM strategist with replan triggers and the stall stop. Plans and failures show in the trace. |
| V2 | Full task vocabulary for one level: gather, buy, equip, enter, clear. Clears level 1 from a fresh character. |
| V3 | Combat considerations (odds, health, flee). Milestones per hour beat Lorraine's on the same world. |
| V4 | Clears levels in order across sessions, with the run report as the only thing Evan needs to read. |

## Will not

- Add a branch for a situation a run hit. It is a task, a consideration, a weight, or the strategist's call.
- Let a lower layer pick a goal, or the strategist pick a step.
- Run silently with no progress.
- Import Gary or Lorraine.

## Open

- The agent's name (convention: alliterative, e.g. Layered Lou).
- Whether V0's trivial strategist should be Lorraine's goal tree instead, for a stronger baseline.
