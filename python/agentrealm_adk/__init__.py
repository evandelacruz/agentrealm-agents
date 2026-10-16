"""The agent ADK (A149): the plumbing every agent in this repo plays on.

The API client, the world model, the knowledge base, pathing and navigation,
the executor, travel, the reflex helpers, the reference data, and the command
line both agents share (``cli``). Grinding Gary (``agentrealm_agent``) and
Ladder Lorraine (``ladder``) import from here; nothing here imports either
agent, and ``tests/test_architecture.py`` ``ImportLineTest`` holds that line.
"""
