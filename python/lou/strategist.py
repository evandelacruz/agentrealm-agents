"""Lou's strategist: the LLM that owns what to do and why (docs/v3/SPEC.md **Strategist**).

It is shown the game reference, the objective, the task vocabulary, the
state and how the plan has gone, and answers with a goal and a plan. The
goal is always game progress (a level cleared, an item got), never
"explore": exploring is a task that serves a goal.
"""

from __future__ import annotations

import json
import time

from agentrealm_adk import supplies
from agentrealm_adk.llm import Backoff, CallBudget, LLMClient, ProviderConfig, connect, parse_reply, trace_messages
from agentrealm_adk.planner_reference import game_notes_text, reference_text
from agentrealm_adk.world import chebyshev

from adk3.core.interfaces import Report
from adk3.core.plan import Plan, PlanError, parse_plan
from adk3.realm.body import RealmBody
from adk3.realm.state import State, threat_of

from .tasks import DOCS, KINDS

NPCS_SHOWN = 12
SUPPLIES_SHOWN = 20

CONTRACT = """\
You are the strategist of Layered Lou, an agent playing Agent Realm. The objective: beat the game by clearing every level, in order.

You decide what to do and why; Lou carries it out. Under you, an executive runs your plan's tasks in order, and a controller picks each tick's action: it walks toward the task, and on its own flees, fights back, drinks potions and picks up gems as the danger and the task warrant. Do not plan those reflexes.

Reply with one JSON object, nothing else:
{"goal": "...", "why": "...", "plan": [{"task": "<kind>", ...args, "why": "..."}], "risk": 0.0-1.0}

- goal: one concrete step of game progress: clear level N, get an item, reach a place. Never "explore" alone: exploring is a task that serves a goal.
- plan: 1 to 6 tasks from the vocabulary, in order. You will be asked again when the plan is finished, when a task fails (with why), on a surprise (a death, a new map, an item got), and every couple of minutes, so plan the next few steps, not the whole game.
- risk: how much danger the controller may accept for progress. Low when hurt or with few lives, higher for a goal worth it.
- Name cells only by the x, y the state lists. A task that failed will fail the same way again unless something changed: try another way.

Task vocabulary:
"""


class LouStrategist:
    def __init__(self, client: LLMClient, cfg: ProviderConfig, body: RealmBody, trace=None):
        self.client, self.cfg, self.body, self.trace = client, cfg, body, trace
        self.budget, self.backoff = CallBudget(), Backoff()
        self.system = (CONTRACT + DOCS + "\nGame notes (facts learned in play):\n" + game_notes_text()
                       + "\nGame reference:\n" + reference_text("core"))

    @classmethod
    def from_env(cls, body: RealmBody, trace=None) -> LouStrategist:
        cfg = ProviderConfig.from_env()
        return cls(connect(cfg), cfg, body, trace)

    def plan(self, s: State, report: Report) -> Plan | None:
        messages = [{"role": "system", "content": self.system, "cache": True},
                    {"role": "user", "content": describe(s, self.body, report)}]
        if self.backoff.waiting(time.time()):
            raise RuntimeError("backing off after a failed call")
        self.budget.charge(time.time(), messages)
        try:
            raw, usage = self.client.complete(messages)
        except Exception:
            self.backoff.fail(time.time())
            raise
        self.budget.settle(usage)
        self.backoff.ok()
        if self.trace is not None:
            self.trace({"strategist": {"asked": report.reason, "messages": trace_messages(messages[1:]), "reply": raw}})
        try:
            return parse_plan(parse_reply(raw, lambda o: "plan" in o), set(KINDS))
        except (PlanError, ValueError) as e:
            raise RuntimeError(f"the reply was refused: {e}") from None


def describe(s: State, body: RealmBody, report: Report) -> str:
    """The state and the plan's history, as the strategist reads them."""
    lines = [f"Asked because: {report.reason}"]
    if report.note:
        lines.append(f"Your last reply had a problem: {report.note}")
    lines += ["", "## Self",
              f"health {s.health}/{s.max_health}, lives {s.lives}, gems {s.gems}",
              f"armed: {s.armed or 'nothing'}; worn: {', '.join(s.worn) or 'nothing'}",
              f"carried: {', '.join(f'{c} (id {i})' for i, c in s.held) or 'nothing'}",
              f"levels cleared: {list(s.levels_cleared)} of {s.level_count or '?'}",
              f"on map {s.map_id} ({f'inside level {s.level}' if s.level else 'the overworld'}) at {s.pos[0]},{s.pos[1]}",
              "", "## Known places"]
    if body.town:
        lines.append(f"town (safe, shops, respawn) on map {body.town[0]} at {body.town[1]},{body.town[2]}")
    for map_id, cells in sorted(body.entrances.items()):
        if cells:
            lines.append(f"level entrances on map {map_id}: " + "; ".join(f"{x},{y}" for x, y in cells))
    doors = s.doors()
    lines.append("doors seen on this map: " + ("; ".join(f"{x},{y} ({s.w.view.tiles[(x, y)]})" for x, y in doors[:20]) or "none"))
    lines.append("signs seen on this map: " + ("; ".join(f"{x},{y}" for x, y in s.signs()[:10]) or "none"))
    known = len(s.w.view.tiles)
    lines.append(f"ground known on this map: {known} cells; unseen edge cells: {len(s.w.frontier())}")
    lines += ["", "## In view"]
    npcs = sorted(s.npcs(), key=lambda e: chebyshev(e.pos, s.pos))[:NPCS_SHOWN]
    for e in npcs:
        kind = "boss" if e.is_boss else ("hostile" if threat_of(s, e) >= 1 else "not seen to fight")
        hp = f", health {e.health}/{e.max_health}" if e.health is not None else ""
        lines.append(f"npc {e.code} id {e.id} at {e.pos[0]},{e.pos[1]} ({kind}{hp})")
    for e in sorted(s.supplies(), key=lambda e: chebyshev(e.pos, s.pos))[:SUPPLIES_SHOWN]:
        price = f", costs {e.gem_price} gems" if e.gem_price else ""
        lines.append(f"supply {e.code} at {e.pos[0]},{e.pos[1]}{price}: {what(e.code)}")
    if not npcs and not s.supplies():
        lines.append("nothing")
    if body.notes:
        lines += ["", "## What signs and helpers said"] + body.notes[-15:]
    lines += ["", "## Progress",
              f"minutes since the last milestone: {report.minutes_since_milestone:.1f}",
              "recent milestones: " + (", ".join(report.milestones) or "none")]
    if report.plan is not None:
        lines.append(f"current goal: {report.plan.goal}; plan: {' > '.join(str(st) for st in report.plan.steps)}")
        lines.append(f"task under way: {report.current or 'none'}")
    if report.outcomes:
        lines.append("tasks over, oldest first:")
        lines += [f"- {o.result}: {o.step}" + (f" ({o.why})" if o.why else "") for o in report.outcomes]
    return "\n".join(lines)


def what(code: str) -> str:
    """A supply's reference row, short."""
    r = supplies.row(code)
    if r is None:
        return "not in the reference"
    return json.dumps({k: v for k, v in vars(r).items() if v not in (None, "", (), frozenset(), 0)}, default=list)
