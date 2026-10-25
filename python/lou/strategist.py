"""Lou's strategist: the judgments and tactics, from the game's documentation and what Lou knows (docs/v3/SPEC.md **Strategist**).

Its standing context, sent once and cached, is the Manual's world, game
rules and play guide (§3, §11, §16) and ``docs/GAME_NOTES.md``. Each ask
carries the needs graph with its status, Lou's memory (every clue, the
entrances, shops, levels, deaths), what was tried and why it failed. It
answers with judgments the needs graph reads (which entrance first, what
each level needs, where an item is sold, what opens a door) and tactics the
engines play. It never picks a step or a cell to walk.
"""

from __future__ import annotations

import json
import time
from typing import Any

from agentrealm_adk.llm import Backoff, CallBudget, LLMClient, ProviderConfig, connect, parse_reply, trace_messages
from agentrealm_adk.planner_reference import REFERENCE_PATH, game_notes_text, read_text, split_sections
from agentrealm_adk.world import chebyshev

from adk3.core.loop import Briefing
from adk3.realm.context import Ctx

MANUAL = ("/docs/manual / 3. how the world works", "/docs/manual / 11. game rules", "/docs/manual / 16. playing the world")
SHOPS_SHOWN = 30

CONTRACT = """\
You are the strategist of Layered Lou, an agent playing Agent Realm. The objective: beat the world by clearing every level.

How Lou works. She holds a needs graph built from the game's rules: beat the world needs every level cleared; a level needs Lou ready for it (the gear and potions you judge it needs, bought where they are sold, with gems from field work), through its entrance (opened first if it is locked or hidden), and its boss beaten inside on its clock. She works the first unmet need she can. Real-time engines play each moment without you: combat weighs every option as reward against risk in gems; opportunities (gems, food when hurt, signs, helpers, odd blocks, her death chest) are taken when worth their detour; travel replans every step. Her temperament is fixed by her levers (below); you work within it.

Your job is judgment: what the needs graph cannot know by itself. Read the clues: the world tells you how to progress through text (rumor helpers point at level doors, waystation helpers say what locked doors need). Use the Manual below for prices, where each gear tier is sold, where gems come from, how hostiles and bosses behave.

Reply with one JSON object and nothing else. Every field is optional; one you leave out keeps its last value.
{
  "why": "your reasoning, briefly",
  "order": ["<entrance key>", ...],            // entrances to clear, first first (keys as listed, "map:x,y")
  "needs": {"<entrance key>": {"items": ["<item code>", ...], "potions": 2, "potion": "small_potion", "why": "..."}},
  "doors": {"<entrance key>": {"opener": "<item code that opens it: a key's code, or bomb for a hidden one>", "why": "..."}},
  "where": {"<item code>": {"map": 76, "x": 0, "y": 0, "why": "where it is likely sold, to look there"}},
  "explore": {"toward": [x, y], "why": "where to look when nothing else can be worked"},
  "tactics": {"avoid": ["<npc type>", ...], "drink_below": 4, "boss_seconds": 90, "leave_with_seconds": 15},
  "notes": "what to remember for next time"
}

- items: gear Lou must carry, and arm or wear, before entering that level. Be concrete and affordable: name item codes from the Supplies reference.
- Name places only by keys and x, y the state lists. Name items by their code.
- A need that failed will fail the same way again unless something changed: judge differently (another entrance, other gear, another seller).
"""


class Strategist:
    def __init__(self, client: LLMClient, cfg: ProviderConfig, record=None):
        self.client, self.cfg, self.record = client, cfg, record
        self.budget, self.backoff = CallBudget(), Backoff()
        sections = [s.text for s in split_sections(read_text(REFERENCE_PATH)) if s.key.startswith(MANUAL)]
        self.system = (CONTRACT + "\nThe Manual (world, game rules, playing the world):\n" + "".join(sections)
                       + "\nGame notes (facts learned in play):\n" + game_notes_text())

    @classmethod
    def from_env(cls, record=None) -> Strategist:
        cfg = ProviderConfig.from_env()
        return cls(connect(cfg), cfg, record)

    def __call__(self, briefing: Briefing) -> dict[str, Any]:
        now = time.time()
        if self.backoff.waiting(now):
            raise RuntimeError("backing off after a failed call")
        messages = [{"role": "system", "content": self.system, "cache": True},
                    {"role": "user", "content": describe(briefing)}]
        self.budget.charge(now, messages)
        try:
            raw, usage = self.client.complete(messages)
        except Exception:
            self.backoff.fail(time.time())
            raise
        self.budget.settle(usage)
        self.backoff.ok()
        if self.record is not None:
            self.record({"t": now, "strategist": {"asked": briefing.reason, "messages": trace_messages(messages[1:]),
                                                  "reply": raw}})
        answer = parse_reply(raw, lambda o: isinstance(o, dict))
        if not isinstance(answer, dict):
            raise ValueError("the reply is not a JSON object")
        return answer


def adopt(ctx: Ctx, answer: dict[str, Any]) -> list[str]:
    """Fold an answer into the judgments and tactics; returns what changed, for the console."""
    changed = []
    for kind in ("order", "needs", "doors", "where", "explore", "notes", "why"):
        if kind in answer and answer[kind] != ctx.judgments.get(kind):
            if isinstance(answer[kind], dict) and isinstance(ctx.judgments.get(kind), dict):
                ctx.judgments[kind] = {**ctx.judgments[kind], **answer[kind]}
            else:
                ctx.judgments[kind] = answer[kind]
            changed.append(kind)
    if isinstance(answer.get("tactics"), dict):
        ctx.tactics.update(answer["tactics"])
        changed.append("tactics")
    return changed


def describe(b: Briefing) -> str:
    ctx: Ctx = b.ctx
    s, mem = ctx.state, ctx.memory
    lines = [f"Asked because: {b.reason}"]
    if b.note:
        lines.append(f"Your last answer had a problem: {b.note}")
    lines += ["", "## Lou now",
              f"health {s.health}/{s.max_health}, lives {s.lives}, gems {s.gems}",
              "carried: " + (", ".join(f"{c.code} ({c.where})" for c in s.carried) or "nothing"),
              f"levels cleared: {list(s.levels_cleared)} of {s.level_count or '?'}",
              f"on map {s.map_id} ({f'inside level {s.level}' if s.level else 'outside every level'}) at {s.pos[0]},{s.pos[1]}",
              "levers: " + ", ".join(f"{k} {v:g}" for k, v in vars(ctx.levers).items())]
    if ctx.town:
        lines.append(f"town (safe, bronze shop, respawn): map {ctx.town[0]} near {ctx.town[1][0]},{ctx.town[1][1]}")

    lines += ["", "## Needs graph (what the objective requires; * is what Lou works now)"]
    for r in b.rows:
        mark = "*" if r.focus else " "
        extra = f" — blocked: {r.blocked}" if r.blocked else ""
        lines.append(f"{mark}{'  ' * r.depth}[{r.status.value}] {r.title} ({r.key}){': ' + r.why if r.why else ''}{extra}")

    lines += ["", "## Entrances (the minimap marks every one; unnumbered until entered)"]
    for key, e in sorted(mem.entrances.items(), key=lambda kv: chebyshev((kv[1].x, kv[1].y), s.pos)):
        facts = [f"block {e.block}" if e.block else "not seen up close", "locked" if e.locked else "",
                 "reached" if e.reached else "", f"refused us: {e.refused}" if e.refused else "",
                 f"opens level {e.level}" if e.level else "", f"{chebyshev((e.x, e.y), s.pos)} blocks away" if e.map_id == s.map_id else ""]
        lines.append(f"- {key}: " + ", ".join(f for f in facts if f))
    if mem.levels:
        lines += ["", "## Levels"]
        for lv in sorted(mem.levels.values(), key=lambda lv: lv.number):
            tries = "; ".join(f"{a.result} after {a.seconds:.0f}s, boss health {a.boss_health_left}" for a in lv.attempts[-3:])
            lines.append(f"- level {lv.number}: entrance {lv.entrance or '?'}, maps {lv.maps}, boss {lv.boss or '?'}"
                         f" (max health {lv.boss_max_health or '?'}){'; attempts: ' + tries if tries else ''}")

    lines += ["", "## Every clue so far"]
    lines += [f"- {c.source} {c.speaker} at {c.map_id}:{c.x},{c.y}: {c.text}" for c in mem.clues] or ["none yet"]

    lines += ["", "## Seen for sale"]
    shops = sorted(mem.shops.items(), key=lambda kv: (kv[1].map_id != s.map_id, chebyshev((kv[1].x, kv[1].y), s.pos)))
    lines += [f"- {v.code} for {v.price} gems at {v.map_id}:{v.x},{v.y}" for _, v in shops[:SHOPS_SHOWN]] or ["nothing yet"]

    breaks = sum(y.breaks for y in mem.yields.values())
    gems = sum(y.gems for y in mem.yields.values())
    lines += ["", f"## Income: our breaks dropped {gems} gems in {breaks} breaks"]
    if mem.deaths:
        lines += ["", "## Deaths (newest last)"]
        lines += [f"- killed by {d.killer or '?'} at {d.map_id}:{d.x},{d.y}; chest {'still out, holding ' + ', '.join(d.items) if d.chest_id else 'gone or empty'}"
                  for d in mem.deaths[-5:]]

    lines += ["", "## How it has gone",
              f"minutes since the last milestone: {b.minutes_since_milestone:.1f}",
              "recent milestones: " + (", ".join(b.milestones) or "none")]
    lines += [f"- {o.skill} for {o.need}: {o.result}" + (f" ({o.why})" if o.why else "") for o in b.outcomes[-10:]]
    lines += ["", "## Your standing judgments and tactics", json.dumps({**ctx.judgments, "tactics": ctx.tactics})]
    return "\n".join(lines)
