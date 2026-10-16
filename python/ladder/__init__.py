"""Ladder Lorraine (``lorraine``, M14): the goal-tree agent (``docs/agents/lorraine.md``).

She is built on the agent ADK in ``agentrealm_adk`` (client, world model,
pathing, navigation, executor, supplies, knowledge base, the command line)
and imports nothing of Gary's (``agentrealm_agent``).

Her goal tree is ``goals`` (A139) and what it reads is ``readiness``; ``status``
prints the tree's readiness. ``run`` walks the tree and asks the planner one
question at its goal (``ask``, A140), then carries out the op in ``play``:
minimally, on Starter Stan's sync, flee and explore, until the executor and
reflexes (A141) land.

Run (from ``python/``, after ``export AGENTREALM_API_KEY=...``):

  python3 -m ladder create characters/starter.toml
  python3 -m ladder run characters/starter.toml --character-id ID
  python3 -m ladder status characters/starter.toml --character-id ID
"""
