# Ladder Lorraine

**Short name:** `lorraine`. **Package:** `python/ladder/` (M14). Run from `python/` with `python3 -m ladder create|run|status <profile>`, the same commands and profile files as Gary. Her command-line parser and `status` mirror Gary's until A149 moves them into the shared ADK. Until her goal tree drives play (A140–A141), `run` calls Stan's loop (`starter_agent.run`: sync, flee, explore). That tie is temporary and is her one import of Stan.

**Plays to:** levels cleared per hour. Everything else is a means to that.

**How it thinks.** One goal tree, held as data (`ladder/goals.py`, A139), walked from the top every turn: beat the world; beat the next level; under it, hold what it requires (the entrance's need: a key, sandals, a bomb) and ready for its risk (heals, weapon, armor against the level's danger); under both, hold the gems for what's missing; under that, gather; under that, stay alive. Each node has a readiness predicate computed from state and the op that works it. The walk goes into the first child that is not ready; a node whose children are all ready is the turn's goal. Risk is one function (`ladder/readiness.py` `risk_budget`): health plus the heals carried against the expected cost of the goal, Gary's win estimate reused, with a valuable goal (a level and its boss, a gem cache) allowed to cost three quarters and an ordinary one half. "Ready for its risk" is that function on the level, "stay alive" on the goal being worked, and her reflexes will read it too. A node the planner says it cannot work is skipped with its reason until the block lapses. Next, the planner is asked one question about the goal node: do it, or say why it cannot (A140); the state machine then carries out the op (A141). `ladder status` prints the tree's readiness.

**Temperament.** Fixed by the tree, not by knobs: she climbs as soon as she is ready and descends only as far as she must. The few knobs she has name a readiness bar (health fraction, gems in hand, weapon class) and nothing else.

**Will not.** Add a rule for a situation (A87, at zero from day one). Import from Gary's planner or states. Gather, shop or explore for its own sake.

**Lineage.** Built on Gary's plumbing (client, world model, pathing, executor, supplies) to test one claim: a goal tree with the situational calls delegated to the planner beats accreted rules. Becomes the reference agent if the metrics say so (A143).
