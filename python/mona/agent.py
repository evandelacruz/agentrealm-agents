"""Mona's loop (docs/v4/SPEC.md **Layers**).

Each decision: take what the strategist answered; on a death, a met mode or a
changed kit, run the goal decider; then the first reflex that applies acts,
else get health when it is on, else the mode's flow. The strategist runs off
this thread and is asked only at decision points.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from agentrealm_adk.navigation.rejection import navigation_avoid_costly

from adk3.core.asker import Asker
from adk3.core.ledger import Ledger
from adk3.realm import actions, milestones, travel
from adk3.realm.body import Body
from adk3.realm.context import Ctx

from . import reflexes
from .decider import Mode, decide, next_level
from .health import GetHealth
from .kit import Kit, KitStore
from .modes import Beat, Flow, Gear, Learn, Money
from .strategist import Ask, Strategist, kit_of, pick_of

RETRY_SECONDS = 20  # a failed strategist call is asked again this long after
PARK_SECONDS = 90  # asked to stop, how long to spend getting to safe ground first
SAVE_SECONDS = 30  # memory is written this often (and at a death), so a run cut short loses little


class Mona:
    def __init__(self, body: Body, strategist: Strategist | None, kits: KitStore, ledger: Ledger, trace: Path,
                 out: Callable[[str], None] = print, stall_minutes: float = 15.0):
        self.body, self.kits, self.ledger, self.out = body, kits, ledger, out
        self.kit: Kit | None = kits.load()
        self.health = GetHealth()
        self.mode: Mode | None = None
        self.flow: Flow | None = None
        self.asker = Asker(strategist) if strategist is not None else None
        self.asking: Ask | None = None
        self.pending: Ask | None = None
        self.retry_at = 0.0
        self.trace = open(trace, "a", buffering=1)
        self.stall_minutes = stall_minutes
        self._status_at = 0.0
        self._saved_at = time.time()
        self.stopped_why = ""

    # The loop.

    def run(self, stop: Callable[[], bool]) -> str:
        try:
            while not stop():
                ctx = self.sense()
                if ctx is not None:
                    if not ctx.state.world.alive and ctx.state.lives == 0:
                        self.stopped_why = "the character has ended: no lives left"
                        return self.stopped_why  # nothing to park
                    self.decide(ctx, time.time())
                if self.ledger.minutes_since_last() >= self.stall_minutes:
                    self.stopped_why = f"no milestone for {self.stall_minutes:.0f} minutes"
                    break
            else:
                self.stopped_why = "stopped"
            self.park()
        finally:
            self.ledger.save()
            self.out(f"run over: {self.stopped_why or 'stopped'}; milestones: {', '.join(self.ledger.recent(30)) or 'none'}")
        return self.stopped_why

    def sense(self) -> Ctx | None:
        state = self.body.sense()
        if state is None:
            return None
        return Ctx(state, self.body.memory, self.body.kb, self.body.nav, self.body.town)

    def decide(self, ctx: Ctx, now: float) -> None:
        s = ctx.state
        if not s.world.inventory_read:  # her first full read is not in (health, gems and gear read as none): an empty
            self.body.act(None)  # tick brings it
            return
        for key, label in milestones.shown(ctx):
            if self.ledger.record(key, label, now):
                self.out(f"milestone: {label}")
        self.take_answer(ctx)
        died = "we died" in self.body.take_surprises()
        if died or now - self._saved_at >= SAVE_SECONDS:
            self._saved_at = now
            self.body.memory.save()
        if died:
            if self.kit is not None and self.kit.set_off:  # a death sends her back through the gate
                self.kit.set_off = False
                self.kits.save(self.kit)
            self.health.clear()
            self.flow = None
            self.ask_kit(ctx, "she died; revise the kit if it was too thin")
        if self.flow is None or self.met(ctx):
            self.start_mode(ctx)
        self.serve_flow(ctx)
        change = self.health.update(s)
        if change:
            self.out(change)
        found = reflexes.first(ctx, turned=self.health.active)  # get health turns her from a fight
        if found is not None:
            who, action = found
        elif self.health.active:
            who, action = "get health", self.health.act(ctx)
        else:
            who, action = self.mode.value if self.mode else "-", self.flow.act(ctx) if self.flow else None
        readied = reflexes.ready(ctx, action)  # the standing rule: her weapon in hand whenever nothing else needs it
        if readied is not action:
            who, action = "armed", readied
        self.body.act(action)
        self.record({"t": now, "mode": self.mode.value if self.mode else None, "health": self.health.active,
                     "who": who, "action": str(action) if action else None, "pos": ctx.state.pos,
                     "path": list(getattr(action, "path", ()))[:12]})
        if now - self._status_at >= 1.0:
            self._status_at = now
            self.out(f"{self.mode.value if self.mode else '-'} ({self.flow.status() if self.flow else ''})"
                     f"{' | GET HEALTH' if self.health.active else ''} | {who}: {action or 'nothing'} | {self.status(ctx)}"
                     f" | last milestone {self.ledger.minutes_since_last(now):.1f}m ago")

    # The goal decider, run only when the mode is met, after a death, or when the kit changes.

    def met(self, ctx: Ctx) -> bool:
        s, k = ctx.state, self.kit
        if self.mode == Mode.LEARN:
            return k is not None and k.trusted and k.level == next_level(s)
        if self.mode == Mode.MONEY:
            return k is not None and s.gems >= k.cost(s)
        if self.mode == Mode.GEAR:
            return k is not None and not k.missing(s)
        if self.mode == Mode.BEAT:
            return k is not None and k.level in s.levels_cleared
        return True

    def kit_changed(self, ctx: Ctx) -> None:
        """The decider runs; a flow goes on only when the mode it serves is still the one decided."""
        mode = decide(ctx.state, self.kit)
        if mode == self.mode and isinstance(self.flow, Learn):
            self.flow.kit = self.kit
            if self.flow.reported:  # the step it was waiting on has been answered: the next one
                self.flow.learned, self.flow.reported = "", False
            return
        self.flow = None

    def start_mode(self, ctx: Ctx) -> None:
        s = ctx.state
        self.mode = decide(s, self.kit)
        n = next_level(s)
        if self.mode == Mode.LEARN:
            done = self.kit.learned if self.kit is not None and self.kit.level == n else []
            self.flow = Learn(self.kit, done)
            if self.kit is None or self.kit.level != n:
                self.ask_kit(ctx, f"no kit yet for level {n}")
        elif self.mode == Mode.MONEY:
            self.flow = Money(self.kit)
        elif self.mode == Mode.GEAR:
            self.flow = Gear(self.kit)
        else:
            self.flow = Beat(self.kit)
            if not self.kit.set_off:  # the kit was met: she sets off, and it is not checked again on the way
                self.kit.set_off = True
                self.kits.save(self.kit)
        self.out(f"mode: {self.mode.value} (level {n})")

    def serve_flow(self, ctx: Ctx) -> None:
        f = self.flow
        if f is None:
            return
        if f.learned and isinstance(f, Learn) and not f.reported:
            f.reported = True  # once: the flow waits until the revised kit lands, then the decider runs
            n = next_level(ctx.state)
            self.kit = self.kit if self.kit is not None and self.kit.level == n else Kit(level=n)
            self.kit.learned = list(f.done)
            self.kits.save(self.kit)
            self.ask_kit(ctx, f"learn step done: {f.learned}")
        if f.blocked:
            if "no way on" in f.blocked:  # the map as she sees it, to replay offline
                s, (avoid, costly) = ctx.state, navigation_avoid_costly(ctx.nav.nav, ctx.kb, ctx.state.map_id, ctx.state.tick)
                self.record({"t": time.time(), "no_way_on": {
                    "map": s.map_id, "pos": s.pos, "tiles": {f"{x},{y}": b for (x, y), b in s.world.view.tiles.items()},
                    "entities": [[e.kind, e.id, e.pos, e.code] for e in s.world.entities], "avoid": sorted(avoid),
                    "costly": sorted(costly)}})
            self.ask_kit(ctx, f"{self.mode.value} is blocked: {f.blocked}")
            f.blocked = ""
        if f.question is not None and not self._asked_for(f):
            self.ask(Ask("pick", ctx, self.kit, next_level(ctx.state), f.question.about, f.question.options, f))

    # The strategist.

    def ask_kit(self, ctx: Ctx, reason: str) -> None:
        self.ask(Ask("kit", ctx, self.kit, next_level(ctx.state), reason))

    def ask(self, ask: Ask) -> None:
        if self.asker is None:
            return
        if not ask.ctx.state.world.inventory_read:  # until then gems and gear read as none: brief it once they are known
            self.pending = ask
            return
        if self.asking is None and time.time() >= self.retry_at and self.asker.ask(ask):
            self.asking = ask
            self.out(f"asking the strategist: {ask.reason}")
        else:
            self.pending = ask  # the newest waits; an older one it replaces is out of date

    def _asked_for(self, flow: Flow) -> bool:
        return any(a is not None and a.flow is flow for a in (self.asking, self.pending))

    def take_answer(self, ctx: Ctx) -> None:
        if self.asker is None:
            return
        if self.asking is not None and not self.asker.busy():
            answer, error = self.asker.take()
            ask, self.asking = self.asking, None
            if error or not isinstance(answer, dict):
                self.out(f"strategist: {error or 'no answer'}")
                self.retry_at = time.time() + RETRY_SECONDS
                self.pending = self.pending or ask
            elif ask.kind == "kit":
                kit = kit_of(answer, ask)
                kit.set_off = kit.same_as(self.kit) and self.kit.set_off  # the same kit keeps her on her way
                self.kit = kit
                self.kits.save(self.kit)
                self.out(f"kit for level {self.kit.level}: {self.kit.items} + {self.kit.potions} potions, entrance "
                         f"{self.kit.entrance}, {'trusted' if self.kit.trusted else 'not trusted'} — {self.kit.why}")
                self.kit_changed(ctx)
            else:
                flow = ask.flow
                if flow is self.flow and flow is not None:
                    i = pick_of(answer, ask)
                    flow.picked(i, ctx)
                    self.out(f"strategist picks: {ask.options[i]} — {answer.get('why', '')}")
        if self.asking is None and self.pending is not None and time.time() >= self.retry_at:
            ask, self.pending = self.pending, None
            self.ask(ask)

    # Stopping somewhere safe, the status line, the trace.

    def park(self) -> None:
        until = time.time() + PARK_SECONDS
        while time.time() < until:
            ctx = self.sense()
            if ctx is None:
                continue
            s = ctx.state
            if s.on_safe_ground:  # no blow lands here
                self.out("parked somewhere safe")
                return
            self.health.update(s)
            found = reflexes.first(ctx, turned=self.health.active, only=reflexes.PARKING)
            if found is not None:  # safe ground is the destination: fire and a fight still come first
                self.body.act(reflexes.ready(ctx, found[1]))
                continue
            path = travel.to_safe(ctx)
            self.body.act(reflexes.ready(ctx, actions.Walk(tuple(path), "to safe ground before stopping")) if path
                          else actions.Stop("no safe ground known"))
        self.out(f"could not reach somewhere safe in {PARK_SECONDS}s")

    def status(self, ctx: Ctx) -> str:
        s = ctx.state
        gear = ",".join(c.code for c in s.carried if c.where != "held") or "-"
        where = f"level {s.level}" if s.level else "overworld"
        return f"hp {s.health}/{s.max_health} lives {s.lives} gems {s.gems} [{gear}] {where} {s.map_id}:{s.pos[0]},{s.pos[1]}"

    def record(self, entry: dict) -> None:
        self.trace.write(json.dumps(entry, default=str) + "\n")

    def close(self) -> None:
        self.body.save()
        self.trace.close()
