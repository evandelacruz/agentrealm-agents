# Ladder Lorraine

**Short name:** `lorraine`. **Package:** `python/ladder/` (M14). Run from `python/` with `python3 -m ladder create|run|status <profile>`, the same commands and profile files as Gary. Until her goal tree replaces it (A139–A141), `run` calls Stan's loop (`starter_agent.run`: sync, flee, explore). That tie is temporary and is her one import of Stan.

**Plays to:** levels cleared per hour. Everything else is a means to that.

**How it thinks.** One goal tree, held as data: beat the world, beat the next level, be geared for it, hold the gems for the gear, stand somewhere safe to gather. Each node has a readiness predicate computed from state and a how-to op. Every decision walks the tree from the root and takes the first node that is not ready whose children all are. The planner is asked one question about that node: do it, or say why it cannot. The state machine then carries out the op.

**Temperament.** Fixed by the tree, not by knobs: she climbs as soon as she is ready and descends only as far as she must. The few knobs she has name a readiness bar (health fraction, gems in hand, weapon class) and nothing else.

**Will not.** Add a rule for a situation (A87, at zero from day one). Import from Gary's planner or states. Gather, shop or explore for its own sake.

**Lineage.** Built on Gary's plumbing (client, world model, pathing, executor, supplies) to test one claim: a goal tree with the situational calls delegated to the planner beats accreted rules. Becomes the reference agent if the metrics say so (A143).
