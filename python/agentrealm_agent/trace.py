"""The run's trace: one console line and one JSON line per call.

Every call the runner spends, and every note it makes between calls, is
written twice: a short line to the console (``out``) and a JSON record to
``<profile>.<character id>.trace.jsonl`` (``CharacterConfig.trace_path``),
which ``run_metrics`` and the acceptance reports read back. This module owns
how both look; the runner only says what happened.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .world import WorldModel


class Trace:
    """The console and the trace file of one run."""

    def __init__(self, path: Path, profile: str, out: Callable[[str], None] = print):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a", buffering=1)
        self.profile = profile
        self.out = out

    def log(self, w: WorldModel, call: str, detail: str, record: dict) -> None:
        """One line to the console and one record to the file, stamped with where and when."""
        pos = f"{w.map_id}:{w.pos[0]},{w.pos[1]}" if w.pos else "?"
        self.out(f"[{self.profile}] t={w.tick} @{pos} {call:<8} {detail}")
        self.file.write(json.dumps({"t": time.time(), "tick": w.tick, "pos": w.pos, "map": w.map_id, "call": call, **record}) + "\n")

    def close(self) -> None:
        self.file.close()


def tick_detail(intents: list[dict] | None, decided: dict | None, reason: str, events: list[dict], dropped: int = 0) -> str:
    """A tick's console line: what was sent (or ``decided`` when nothing was), why, and what came back."""
    detail = f"{fmt_submit(intents, decided)} ({reason})"
    if events:
        detail += " | " + ", ".join(fmt_event(e) for e in events)
    if dropped:
        detail += f" | dropped {dropped}"
    return detail


def oscillation_detail(event: dict) -> str:
    """The console line of an ``oscillation`` the dispatch guard raised (A15, A90)."""
    if "goal" in event:
        outcome = f"gave up {event['goal']} → {tuple(event['target'])}"
    else:
        outcome = f"nothing given up, moved by {', '.join(event.get('states') or []) or 'no state'}"
    return f"pacing {event['cells']}: {outcome}"


def fmt_submit(intents: list[dict] | None, decided: dict | None = None) -> str:
    """A submit in a few words: its one intent, or a walk queue's Steps and Waits."""
    if intents is None:
        return fmt_intent(decided)
    if len(intents) == 1:
        return fmt_intent(intents[0])
    steps = sum(1 for i in intents if i.get("verb") == "Step")
    waits = sum(1 for i in intents if i.get("verb") == "Wait")
    return f"queue {steps}×Step {waits}×Wait"


def fmt_intent(i: dict | None) -> str:
    if i is None:
        return "—"
    verb = i["verb"]
    if verb == "SetPosition":
        return f"SetPosition({i['x']},{i['y']})"
    if verb == "Step":
        return f"Step({i['direction']})"
    if verb == "Wait":
        return "Wait"
    if verb == "Use":
        t = i["target"]
        return f"Use({t.get('kind')}:{t.get('character_id', t.get('npc_id', ''))})"
    if verb == "Take":
        return f"Take({i['supply_id']})"
    if verb == "WithdrawFromChest":
        return f"WithdrawFromChest({i['chest_id']}:{','.join(map(str, i.get('supply_ids', ['all'])))})"
    if verb == "Drop":
        return f"Drop({i['supply_id']})"
    return verb


def fmt_event(e: dict) -> str:
    kind = e.get("kind", "?")
    if kind in ("SpokenTo", "BroadcastHeard"):
        return f'{kind} {e.get("speaker_id")}: "{e.get("text", "")}"'
    if kind == "Damaged":
        return f"Damaged {e.get('amount', 0)} by {e.get('source_kind', '?')}"
    if kind == "Died" and e.get("chest_id"):
        return f"Died, chest {e['chest_id']} at {e.get('map_id')}:{e.get('x')},{e.get('y')}"
    return kind
