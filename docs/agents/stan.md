# Starter Stan

**Short name:** `stan`. **File:** `python/starter_agent.py`, on the agent ADK's client, world model and pathing (`python/agentrealm_adk/`).

**Plays to:** staying alive while wandering. He clears nothing and buys nothing.

**How it thinks.** One file, about 400 lines: sync, flee, explore. One tile at a time with `SetPosition`, waiting out the cooldown.

**Temperament.** None to speak of. He walks away from anything hostile and toward anything unexplored.

**Will not.** Fight, trade, or read. Use the planner.

**Lineage.** A52. The file people copy and grow before they touch Gary or Lorraine. Stays small on purpose: if a change makes him harder to read, it does not go in. Until Lorraine's goal tree drives play (A140–A141), her `run` calls his `starter_agent.run`, so a change to his loop changes how she plays too; that tie is temporary.
