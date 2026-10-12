# Grinding Gary

**Short name:** `gary`. **Package:** `python/agentrealm_agent/` (kept under its historical name; see Lineage).

**Plays to:** staying alive and growing: gems, gear, levels, in that order of frequency.

**How it thinks.** AI plans, state machine executes (PLAN.md **Architecture**). A planner holds a stack of ops and re-plans every few seconds from a wide state report and a four-stage arc (survive and learn, build up, beat levels, beat the world). Twenty-odd states carry out the top op tick by tick, with survival reflexes above them.

**Temperament.** Set in the directives file: `fight_margin` 1.5 (fights it expects to win by half again), `retreat_hits` 2 (leaves when two hits would floor it), `risk` 0.5, `curiosity` 0.2 (reads a sign or talks to an NPC now and then), `potion_reserve` 2, `lives_floor` 3.

**Will not.** Fight at a loss by its own estimate. Leave safe ground hurt. Carry fewer than two potions into a level by choice.

**Lineage.** Grown run by run from the first playable agent (M6, M7) through 23 free-play runs. Every rule in it was earned against the live game, which is its strength and its weight. Kept as the full-featured baseline that Ladder Lorraine is measured against.
