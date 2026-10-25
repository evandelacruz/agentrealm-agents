# Layered Lou

**Short name:** `lou`. **Package:** `python/lou/`, on the v3 kit `python/adk3/` (the game-free core in `adk3/core`, the Agent Realm binding in `adk3/realm`), over the ADK's plumbing. Run from `python/` with `python3 -m lou create|run|status characters/lou.toml`. Design: [`docs/v3/SPEC.md`](../v3/SPEC.md).

**Plays to:** clearing every level. Every milestone she counts (a level, a map, gear, a capability, an entrance reached, a clue, gems) is a step toward it.

**How it thinks.** She works backwards from the objective through a needs graph built from the game's rules: beat the world needs every level cleared; a level needs her ready for it, through its entrance, and its boss beaten; ready means carrying the gear judged right for it; gear means gems and a seller; gems mean field work. The first unmet need she can work is her focus, worked by a skill. An LLM strategist, with the Manual's rules and play guide as standing context and every clue she has heard, makes the judgments the graph cannot (which entrance first, what each level needs, where an item is sold, what opens a door) and writes the tactics her real-time engines play: combat weighs reward against risk in gems, with the chance of death from rolled play-outs; opportunities (gems, food when hurt, signs, helpers, odd blocks, her death chest) are taken when worth their detour; travel replans every step and cuts through bushes and trees when a weapon can. Everything she reads or hears is kept and handed to the strategist.

**Temperament.** Her levers in `characters/lou.toml`: risk 5, life worth 40 gems, time worth 6 gems a minute, curiosity 5, greed 5, heal below 40% health, patience 60 s. Another agent on the same kit is these numbers changed.

**Will not.** Add a branch for a situation a run hit: a fix is a need kind, a skill, an engine, an opportunity, a tactic or the strategist's input. Let the strategist act in the moment, or an engine pick a goal. Import Gary or Lorraine.

**Lineage.** v3, after Gary (v1, rules grown case by case) and Lorraine (v2, a fixed goal tree that drifted back into cases). Built to one plan in one delivery, then fixed from live runs.
