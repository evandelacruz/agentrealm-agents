"""Everything Lou can do, in one place. Adding a behavior is adding a class to one of these.

- Methods (``lou.publish.METHODS``, carried out by ``lou.skills``): what an approach can do.
- Goal tests (``lou.publish.GOAL_TESTS``): how the executive knows a goal is met.
- Reactions (``ENGINES``): real-time play, in fixed priority.
- Opportunities (``OPPORTUNITIES``): the reactive rules, each priced in gems.
- Milestones (``adk3.realm.milestones``): what counts as progress.
"""

from __future__ import annotations

from . import engines, opportunities

ENGINES = engines.ALL
OPPORTUNITIES = opportunities.ALL
