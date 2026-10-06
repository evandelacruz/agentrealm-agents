"""The game knowledge in the planner's system prompt (A62).

Two local files, read once, never the website during play:

- ``reference/agentrealm_reference.md``: the site's docs and guides as
  Markdown, written by ``scripts/refresh_planner_reference.py``.
- ``docs/GAME_NOTES.md``: the facts we measured in play. Where it and the
  reference disagree, the planner is told to trust the measured fact.

The reference is split into sections, one per page heading (``### ``).
Core sections (rules, intents, combat, survival, items, maps) always go in;
the rest are added in file order while the total stays under
:data:`REFERENCE_TOKEN_BUDGET`, except :data:`LAST_SECTIONS` (history and
setup), which go in only by name or with ``all``. ``reference_sections`` picks differently:

- ``""`` (default): core, then the rest under the budget, minus the last sections.
- ``"all"``: every section, whatever the size.
- ``"core"``: core sections only.
- ``"a,b"``: core plus each section whose key contains ``a`` or ``b``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REFERENCE_PATH = Path(__file__).resolve().parent / "reference" / "agentrealm_reference.md"
GAME_NOTES_PATH = Path(__file__).resolve().parents[2] / "docs" / "GAME_NOTES.md"
CHARS_PER_TOKEN = 4
REFERENCE_TOKEN_BUDGET = 60_000

# Section keys (lower case, "page path / heading") that always go in.
CORE_SECTIONS = (
    "/docs/manual / 1. contract",
    "/docs/manual / 3. how the world works",
    "/docs/manual / 6. intent reference",
    "/docs/manual / 8. events",
    "/docs/manual / 9. perception",
    "/docs/manual / 11. game rules",
    "/docs/manual / 14. what is served today",
    "/docs/manual / 15. glossary",
    "/docs/manual / 16. playing the world",
    "/docs/api / principles",
    "/docs/api / tick loop",
    "/docs/api / intents",
    "/docs/api / events",
)
# History and setup, not play: left out unless named or ``all``.
LAST_SECTIONS = ("/docs/changelog", "accounts", "running the stack locally", "quick start")


@dataclass(frozen=True)
class Section:
    key: str  # "page path / heading", lower case
    text: str

    @property
    def tokens(self) -> int:
        return len(self.text) // CHARS_PER_TOKEN + 1


def split_sections(reference: str) -> list[Section]:
    """The reference cut at each page (``# page:``) and page heading (``### ``)."""
    sections: list[Section] = []
    page, heading, lines = "", "", []

    def flush() -> None:
        if "".join(lines).strip():
            sections.append(Section(f"{page} / {heading}".lower(), "".join(lines)))

    in_code = False
    for line in reference.splitlines(keepends=True):
        if line.startswith("```"):
            in_code = not in_code
        if not in_code and (line.startswith("# page: ") or line.startswith("### ")):
            flush()
            lines = []
            if line.startswith("# page: "):
                page, heading = line[len("# page: ") :].split(" ", 1)[0], ""
            else:
                heading = line[4:].strip()
        lines.append(line)
    flush()
    return sections


def missing_core(sections: list[Section]) -> list[str]:
    """The :data:`CORE_SECTIONS` entries no section matches (a renamed site heading)."""
    return [c for c in CORE_SECTIONS if not any(s.key.startswith(c) for s in sections)]


def is_core(section: Section) -> bool:
    return any(section.key.startswith(c) for c in CORE_SECTIONS)


def select_sections(
    sections: list[Section], choice: str = "", budget: int = REFERENCE_TOKEN_BUDGET
) -> list[Section]:
    """The sections ``choice`` asks for, in file order (see the module docstring)."""
    choice = choice.strip().lower()
    if choice == "all":
        return list(sections)
    keep = {s.key for s in sections if is_core(s)}
    if choice == "core":
        pass
    elif choice:
        wanted = [w.strip() for w in choice.split(",") if w.strip()]
        keep |= {s.key for s in sections if any(w in s.key for w in wanted)}
    else:
        used = sum(s.tokens for s in sections if s.key in keep)
        rest = [s for s in sections if s.key not in keep and not any(last in s.key for last in LAST_SECTIONS)]
        for s in rest:
            if used + s.tokens <= budget:
                keep.add(s.key)
                used += s.tokens
    return [s for s in sections if s.key in keep]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=8)
def reference_text(choice: str = "") -> str:
    """The game reference as it goes in the prompt; "" when the file is missing."""
    reference = read_text(REFERENCE_PATH)
    if not reference:
        return ""
    sections = split_sections(reference)
    picked = select_sections(sections, choice)
    head = sections[0].text if sections and not sections[0].key.startswith("/") else ""
    left_out: list[str] = []
    for s in sections:
        if s in picked or not s.key.startswith("/"):
            continue
        page = s.key.split(" / ", 1)[0]
        whole_page = all(t not in picked for t in sections if t.key.startswith(page + " / "))
        name = page if whole_page else s.key
        if name not in left_out:
            left_out.append(name)
    note = f"\n(Sections left out for size: {'; '.join(left_out)}.)\n" if left_out else ""
    return head + note + "".join(s.text for s in picked if s.text != head)


@lru_cache(maxsize=1)
def game_notes_text() -> str:
    """docs/GAME_NOTES.md; "" when this is not a repo checkout."""
    return read_text(GAME_NOTES_PATH)
