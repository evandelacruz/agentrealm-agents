# Grinding Gary

**Short name:** `gary`. **Package:** `python/agentrealm_agent/` (kept under its historical name; see Lineage).

**Plays to:** staying alive and growing: gems, gear, levels, in that order of frequency.

**How it thinks.** AI plans, state machine executes (PLAN.md **Architecture**). A planner holds a stack of ops and re-plans every few seconds from a wide state report and a four-stage arc (survive and learn, build up, beat levels, beat the world). Twenty-odd states carry out the top op tick by tick, with survival reflexes above them.

**Temperament.** Set per profile in its directives file; the defaults live in `agentrealm_agent/directives.py` (`PARAM_DEFAULTS`) and the free-play profile `characters/olympuff_m8.directives.toml` runs them as shipped: `fight_margin` 1.5 (fights it expects to win by half again), `retreat_hits` 2 (leaves when two hits would floor it), `risk` 0.5, `curiosity` 0.2 (reads a sign or talks to an NPC now and then), `potion_reserve` 2, `low_health` 0.5 (below half health a carried potion is spent, A89), `lives_floor` 3.

**Will not.** Fight an engagement its win estimate says it loses (A94, `engagement.py`: one decision per engagement, read by Fight, Flee and Retreat alike). Add a branch for the situation a live run hit (A87).

**Lineage.** Grown run by run from the first playable agent (M6, M7) through the A67 free-play runs (`docs/observations/A67_free_play_runs.md`). Every rule in it was earned against the live game, which is its strength and its weight. Kept as the full-featured baseline that Ladder Lorraine is measured against.
