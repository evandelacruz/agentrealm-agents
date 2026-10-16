"""Ladder Lorraine (``lorraine``, M14): the goal-tree agent (``docs/agents/lorraine.md``).

She is built on the agent ADK in ``agentrealm_adk`` (client, world model,
pathing, navigation, executor, supplies, knowledge base, the command line)
and imports nothing of Gary's (``agentrealm_agent``).

Her goal tree is ``goals`` (A139) and what it reads is ``readiness``; ``status``
prints the tree's readiness. ``run`` plays on her own loop (``play``, A141):
her reflexes (``reflexes``) first, then the tree's goal and the one question
the planner is asked about it (``ask``, A140), and the op carried out over the
ADK's executor and pathing (``act``).

Run (from ``python/``, after ``export AGENTREALM_API_KEY=...``):

  python3 -m ladder create characters/starter.toml
  python3 -m ladder run characters/starter.toml --character-id ID
  python3 -m ladder status characters/starter.toml --character-id ID
"""
