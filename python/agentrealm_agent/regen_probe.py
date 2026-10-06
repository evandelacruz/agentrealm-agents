"""A60 regen probe: get hurt, then let Heal measure safe-zone regen.

The A16 gate's regen clause needs ``regen_known`` to answer, and a character
that is never hurt never gives Heal a window to measure. The probe plays the
normal agent on a profile that fights (``characters/regen_probe.toml``,
``on_hostile = "fight"``), so a hostile in reach lands a hit. Nothing here
steers: Fight swings, Retreat leaves at the ``should_retreat`` threshold, and
Heal walks to a known safe tile and samples regen (``note_regen_sample``),
saving a "yes" to the world knowledge base (``save_regen_yes``).

These metrics only watch, through the same hooks as the acceptance gates, and
stop the run on the first of:

- ``regen_known`` answers, "yes" (saved for every later run, so the M7 gate
  passes its regen clause) or "no" (this run only);
- the cap, ``target_seconds`` (default 30 minutes);
- a death: the first one ends the probe as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from .acceptance_survival import SurvivalAcceptanceMetrics
from .config import Policy
from .healing import hurt
from .knowledge_base import KnowledgeBase
from .memory import Memory
from .world import WorldModel

PROBE_SECONDS = 1800.0


@dataclass(kw_only=True)
class RegenProbeMetrics(SurvivalAcceptanceMetrics):
    """Watches for the regen verdict, a hit taken and deaths; stops on the first answer."""

    target_seconds: float | None = PROBE_SECONDS
    regen: str | None = None  # "yes" or "no" once regen_known answers
    hurt_seen: bool = False

    def before_tick(
        self,
        w: WorldModel,
        m: Memory,
        *,
        state: str,
        reason: str,
        intents: list[dict] | None,
        policy: Policy,
        params: dict[str, float | int],
        knowledge: KnowledgeBase | None,
        acted_op: dict | None = None,
        plan_op: dict | None = None,
    ) -> None:
        self.hurt_seen = self.hurt_seen or hurt(w)
        regen = self.note_survival_tick(
            w,
            m,
            state=state,
            reason=reason,
            intents=intents,
            policy=policy,
            params=params,
            knowledge=knowledge,
            track_regen=True,
        )
        self.regen = regen or self.regen
        if self.regen is not None and self.stop is not None:
            self.stop.set()

    def failures(self) -> list[str]:
        """Empty only when regen answered with no death."""
        out: list[str] = []
        if self.deaths and self.stop_on_death:
            out.append(f"{self.deaths} death(s): the probe ends on the first")
        if self.regen is None:
            why = "never hurt" if not self.hurt_seen else "hurt, but no verdict yet"
            out.append(f"safe-zone regen not answered before the probe stopped ({why})")
        return out

    def summary_lines(self) -> list[str]:
        return [
            f"safe-zone regen: {self.regen or 'not answered'}",
            f"hurt seen: {self.hurt_seen}",
            *self.survival_summary_lines(),
        ]
