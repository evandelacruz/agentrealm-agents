"""Everything Lou can do, in one place. Adding a behavior is adding a class to one of these.

- Need kinds (``lou.needs``): what the objective requires; the graph grows from ``ROOT``.
- Skills (``lou.skills``): the procedures a need is worked by; each need kind names its own.
- Engines (``ENGINES``): real-time play, most urgent first.
- Opportunities (``OPPORTUNITIES``): the reactive rules, each priced in gems.
- Milestones (``adk3.realm.milestones``): what counts as progress.
"""

from __future__ import annotations

from . import engines, needs, opportunities

ROOT = needs.BeatTheWorld()
ENGINES = engines.ALL
OPPORTUNITIES = opportunities.ALL
