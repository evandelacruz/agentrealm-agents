"""The idle watchdog: the character never stands around doing nothing (A61).

Live runs showed the character standing still for minutes: Heal resting
with no health coming back, a plan ``wait`` hold, a walk whose steps were
accepted while the character never moved. Each state had a sensible reason
to wait, so no single state saw the problem. This module watches the result
instead, the same way whichever state is responsible.

**Productive** is any of:

- the character's cell changed (map or position);
- an intent that changes the world applied (``WORLD_VERBS``: Take, Use,
  Wear, Read, Say, a chest withdraw, and the like; ``Step``, ``Wait`` and
  ``Arm`` are not);
- health went up (regen or a heal actually worked).

The runner reports each round trip (``observe``, ``note_applied``), and
dispatch (``states/dispatch.py``) calls ``check`` once per decision, before
any state runs. When ``IDLE_REDIRECT_SECONDS`` of game time pass with
nothing productive, it redirects the agent:

- the navigation target being walked to is given up through stuck
  detection's step 5 (``stuck.give_up``, reason ``idle``), so it is backed
  off and not retried at once;
- a plan ``wait`` at the head of the goal stack is dropped, so the next
  plan goal runs;
- the state that held the idle decisions is backed off the same way as a
  stuck target (``stuck.back_off``): dispatch skips it until the backoff
  ends, so a lower state (Explore at the bottom) takes the round. Explore
  and Idle are the fallbacks the redirect lands on, and the survival
  states (Escape, Retreat, Flee, Fight, Boss) hold while danger lasts, so
  none of them is ever backed off (``NEVER_BACKED_OFF``); their idling is
  answered by giving up their target.

An ``idle_redirect`` event goes to the trace. The watchdog fires again
after another ``IDLE_REDIRECT_SECONDS`` if the redirect did not help.

**Exempt** time is where acting is impossible: dead and waiting to
respawn (Downed), asleep or not yet placed (Sync), or a server-forced
wait the runner reports with ``note_server_wait`` (paused, network down,
rate limited, not on the map). Exempt time restarts the clock; a server
wait restarts it at the first response after it, so ticks the server ran
meanwhile are not counted as idle. Heal resting with health not rising,
and plan or directive ``wait`` holds, are not exempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .executor.constants import DEFAULT_TICK_RATE_HZ
from .navigation import stuck as nav_stuck
from .world import Pos, WorldModel

if TYPE_CHECKING:
    from .memory import Memory
    from .plan import Plan

# Wall time with nothing productive before the watchdog redirects (A61).
# Converted to ticks with the world's tick rate (``tick_rate_hz`` from the
# world read; 10 ticks/s on Agent Realm today, so 600 ticks).
IDLE_REDIRECT_SECONDS = 60

# Applied verbs that change the world (the Manual's Intent reference). A
# Step shows up as a changed cell instead; Wait and Arm change nothing
# anyone else can see, so a loop of them is still idle.
WORLD_VERBS = frozenset(
    {
        "Take",
        "Use",
        "Wear",
        "Remove",
        "Drop",
        "Read",
        "Say",
        "Broadcast",
        "Compose",
        "WithdrawFromChest",
        "DepositToChest",
    }
)

# States a redirect never backs off: the fallbacks it lands on (Explore,
# Idle), and the survival states, which hold with no intent while the danger
# lasts (Retreat with no reachable safe tile, Flee with nowhere to go, Fight
# that cannot close). Backing those off would hand a hurt character to
# Explore; giving up their target is enough. Heal is not among them.
NEVER_BACKED_OFF = frozenset({"Explore", "Idle", "Escape", "Retreat", "Flee", "Fight", "Boss"})

# Cap on how many times a state's idle backoff doubles: 300 ticks at most
# 2**3 = 2400 ticks (4 minutes at 10 ticks/s), so a hurt character is never
# kept from Heal for hours. The doubling also starts over once the state does
# something productive (``_productive``).
BACKOFF_POWER_MAX = 3

GIVE_UP_REASON = "idle"
EVENTS_KEPT = 16  # newest kept until the runner writes them to the trace


@dataclass
class IdleWatch:
    """What the watchdog remembers between decisions."""

    productive_tick: int | None = None  # last tick something productive happened (or exempt time ended)
    cell: tuple[int | None, Pos | None] | None = None  # (map, position) last seen
    health: int | None = None  # health last seen
    server_wait: bool = False  # a server-forced wait is under way; ends at the next response (``observe(response=True)``)
    redirect_tick: int | None = None  # tick of the last redirect
    tick_hz: int = DEFAULT_TICK_RATE_HZ
    events: list[dict] = field(default_factory=list)  # idle_redirect events waiting for the trace


def redirect_ticks(tick_hz: int) -> int:
    """``IDLE_REDIRECT_SECONDS`` in ticks at ``tick_hz``."""
    return IDLE_REDIRECT_SECONDS * max(1, tick_hz)


def exempt(w: WorldModel) -> bool:
    """Acting is impossible: dead, asleep, or not placed on a map."""
    return not w.alive or w.asleep or w.pos is None


def observe(m: Memory, w: WorldModel, tick_hz: int | None = None, *, response: bool = False) -> None:
    """Note this round trip's cell and health; either changing (or exempt time) is productive.

    ``response`` is True when ``w`` was just updated from a server response.
    A server-forced wait ends there: its clock restarts at the response's
    tick, which jumped past the ticks the server ran while we waited.
    """
    idle = m.idle
    if tick_hz is not None:
        idle.tick_hz = tick_hz
    cell = (w.map_id, w.pos)
    healed = w.health is not None and idle.health is not None and w.health > idle.health
    if cell != idle.cell or healed:
        _productive(m, w.tick)
    elif idle.productive_tick is None or idle.server_wait or exempt(w):
        idle.productive_tick = w.tick
    if response:
        idle.server_wait = False
    idle.cell, idle.health = cell, w.health


def note_applied(m: Memory, verb: str | None, tick: int) -> None:
    """An intent applied; a world-changing one is productive."""
    if verb in WORLD_VERBS:
        _productive(m, tick)


def _productive(m: Memory, tick: int) -> None:
    """Something productive happened: restart the clock, and the doubling of
    the active state's idle backoff."""
    m.idle.productive_tick = tick
    if m.state:
        m.nav_stuck.backoff_power.pop(state_key(m.state), None)


def note_server_wait(m: Memory) -> None:
    """The server made us wait (paused, network, rate limit, not on the map): exempt
    until the next response, whose tick the clock restarts from (``observe``)."""
    m.idle.server_wait = True


def idle_ticks(m: Memory, tick: int) -> int:
    """Ticks since the last productive one."""
    start = m.idle.productive_tick
    return 0 if start is None or m.idle.server_wait else max(0, tick - start)


def idle_seconds(m: Memory, tick: int) -> float:
    return idle_ticks(m, tick) / max(1, m.idle.tick_hz)


def state_key(state: str) -> str:
    """The backoff key a redirected state is held off under (``stuck.back_off``)."""
    return f"idle_state:{state}"


def held_off(m: Memory, state: str, tick: int) -> bool:
    """``state`` was redirected away from and its backoff has not ended."""
    return nav_stuck.is_backed_off(m.nav_stuck, state_key(state), tick)


def check(m: Memory, w: WorldModel, plan: Plan | None = None) -> dict | None:
    """Redirect the agent when nothing productive happened for ``IDLE_REDIRECT_SECONDS``.

    Returns the ``idle_redirect`` event (also queued on ``m.idle.events``
    for the trace), or None.
    """
    observe(m, w, plan.tick_hz if plan is not None else None)
    idle = m.idle
    limit = redirect_ticks(idle.tick_hz)
    ticks = idle_ticks(m, w.tick)
    if ticks < limit:
        return None
    if idle.redirect_tick is not None and w.tick - idle.redirect_tick < limit:
        return None  # one redirect per idle minute
    idle.redirect_tick = w.tick
    state = m.state
    event: dict = {
        "event": "idle_redirect",
        "reason": "nothing productive",
        "state": state or "none",
        "tick": w.tick,
        "map_id": w.map_id,
        "cell": list(w.pos) if w.pos else None,
        "ticks_idle": ticks,
    }
    att = nav_stuck.active(m, w)
    if att is not None:
        event.update(goal=att.goal, target=list(att.target))
        nav_stuck.give_up(m, w, att, GIVE_UP_REASON)
    op = plan.current() if plan is not None else None
    if plan is not None and op is not None and op["op"] == "wait":
        event["dropped_op"] = dict(op)
        plan.drop_current(GIVE_UP_REASON, m)
    if state and state not in NEVER_BACKED_OFF:
        event["held_off"] = state
        nav_stuck.back_off(m.nav_stuck, state_key(state), w.tick, max_power=BACKOFF_POWER_MAX)
    m.state = ""  # the active state no longer holds the round
    idle.events.append(event)
    del idle.events[:-EVENTS_KEPT]
    return event
