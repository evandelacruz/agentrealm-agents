# Modal Mona

**Short name:** `mona`. **Package:** `python/mona/`, on the v3 kit's game binding `python/adk3/realm/` (body, memory, router, state, actions) over the ADK's plumbing. Run from `python/` with `python3 -m mona create|run|status characters/mona.toml`. Design: [`docs/v4/SPEC.md`](../v4/SPEC.md).

**Plays to:** clearing every level, the lowest not yet cleared first.

**How it thinks.** A goal decider puts her in exactly one of four modes from the kit for level N: learn what level N needs, get money, get gear, beat level N. The mode is fixed until it is met, she dies, or the kit changes. One subgoal, get health, takes over at 3 health and hands back only at 10. One destination at a time, from the mode's fixed list, chosen only when the mode starts, the work there is done, or it fails. Under all of it a static list of reflexes plays every tick: step off fire, eat when hurt, fight what comes within 10 blocks, take gems, gear and chests, read signs and talk. An LLM strategist writes only the kit (and whether it is trusted) and picks from a mode's fixed list; it is never asked tick by tick.

**Temperament.** None to tune: the marks (3 and 10 health, 10 and 25 blocks) are fixed. Two characters on Mona differ only in what they have learned.

**Will not.** Price a choice in gems or roll dice for it. Guard against flip-flopping: every choice has a moment it is made and a moment it ends, so there is nothing to flip. Retreat to safe ground to heal (it heals nothing). Import Gary, Lorraine or Lou.

**Lineage.** v4, after Lou (v3), whose priced reactions grew guards against their own flip-flopping; Mona keeps Lou's body, memory and router and replaces everything above them.
