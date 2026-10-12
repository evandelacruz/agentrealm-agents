"""Ladder Lorraine (``lorraine``, M14): the goal-tree agent (``docs/agents/lorraine.md``).

She is built on Gary's plumbing in ``agentrealm_agent`` (client, world model,
pathing, navigation, executor, supplies, knowledge base) and imports nothing
from his planner or states: not ``strategist``, ``plan``, ``brain``,
``runner`` or any ``states`` module. ``tests/test_architecture.py`` holds
that line on the import closure of this package.

Her goal tree is ``goals`` (A139) and what it reads is ``readiness``; ``status``
prints the tree's readiness. She still plays as Starter Stan does (sync, flee,
explore) until the one planner question (A140) and the executor and reflexes
that carry out its op (A141) land.

Run (from ``python/``, after ``export AGENTREALM_API_KEY=...``):

  python3 -m ladder create characters/starter.toml
  python3 -m ladder run characters/starter.toml --character-id ID
  python3 -m ladder status characters/starter.toml --character-id ID
"""
