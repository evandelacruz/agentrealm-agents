#!/usr/bin/env python3
"""Refresh the AI planner's game reference (A62).

Downloads https://agentrealm.gg/docs and https://agentrealm.gg/guides and
every page they link under ``/docs`` and ``/guides``, turns each page's
article into compact Markdown, and writes
``python/agentrealm_agent/reference/agentrealm_reference.md``. The planner
reads that file at start, so play never fetches the website.

    python3 scripts/refresh_planner_reference.py

Standard library only. Navigation, headers, footers and scripts are dropped;
headings, paragraphs, lists, tables and code are kept as they are.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

SITE = "https://agentrealm.gg"
ROOTS = ("/docs", "/guides")
OUT = Path(__file__).resolve().parent.parent / "python" / "agentrealm_agent" / "reference" / "agentrealm_reference.md"
USER_AGENT = "agentrealm-agents-reference-refresh/1"

SKIP_TAGS = {"script", "style", "nav", "header", "footer", "svg", "button", "form", "noscript", "head"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "blockquote", "dl", "dt", "dd", "figure", "details", "summary"}


class PageToMarkdown(HTMLParser):
    """One page's main content as Markdown, plus the same-site links it holds."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.links: set[str] = set()
        self.title = ""
        self._skip = 0  # depth inside a dropped element
        self._in_title = False
        self._in_main = 0  # depth inside <main>; content outside it is boilerplate
        self._pre = 0
        self._lists: list[list[int | str]] = []  # per open list: [kind, counter]
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._table: list[list[str]] | None = None
        self._header_rows = 0
        self._href: str | None = None

    # -- output helpers
    def _emit(self, text: str) -> None:
        if self._cell is not None:
            self._cell.append(text)
        else:
            self.out.append(text)

    def _block(self) -> None:
        if self._cell is None:
            self.out.append("\n\n")

    # -- parser hooks
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "a" and a.get("href"):
            href = a["href"] or ""
            path = urllib.parse.urlsplit(urllib.parse.urljoin(SITE + "/", href)).path.rstrip("/")
            if href.startswith(("/", SITE)) and path.startswith(ROOTS):
                self.links.add(path)
        if self._skip or tag in SKIP_TAGS:
            if tag in SKIP_TAGS:
                self._skip += 1
            return
        if tag == "main":
            self._in_main += 1
            return
        if not self._in_main:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._block()
            self._emit("#" * int(tag[1]) + " ")
        elif tag in BLOCK_TAGS:
            self._block()
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._block()
        elif tag in ("ul", "ol"):
            self._lists.append([tag, 0])
            if len(self._lists) == 1:
                self._block()
        elif tag == "li":
            depth = len(self._lists) - 1
            marker = "-"
            if self._lists and self._lists[-1][0] == "ol":
                self._lists[-1][1] = int(self._lists[-1][1]) + 1
                marker = f"{self._lists[-1][1]}."
            self._emit("\n" + "  " * max(depth, 0) + marker + " ")
        elif tag == "pre":
            self._block()
            self._emit("```\n")
            self._pre += 1
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "table":
            self._block()
            self._table, self._header_rows = [], 0
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            if tag == "th" and self._table is not None and not self._table:
                self._header_rows = 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if self._skip:
            if tag in SKIP_TAGS:
                self._skip -= 1
            return
        if tag == "main":
            self._in_main = max(0, self._in_main - 1)
            return
        if not self._in_main:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") or tag in BLOCK_TAGS:
            self._block()
        elif tag in ("ul", "ol"):
            if self._lists:
                self._lists.pop()
            if not self._lists:
                self._block()
        elif tag == "pre":
            self._pre = max(0, self._pre - 1)
            self._emit("\n```")
            self._block()
        elif tag == "code" and not self._pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag in ("td", "th") and self._cell is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip().replace("|", "\\|")
            self._cell = None
            if self._row is not None:
                self._row.append(text)
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self._emit(table_markdown(self._table, self._header_rows))
            self._table = None
            self._block()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        if self._skip or not self._in_main:
            return
        if self._pre:
            self._emit(data)
        else:
            self._emit(re.sub(r"\s+", " ", data))

    def markdown(self) -> str:
        parts = "".join(self.out).split("```")
        for i in range(0, len(parts), 2):  # even parts are outside code fences
            text = re.sub(r"[ \t]+\n", "\n", parts[i])
            text = re.sub(r"\n[ \t]+(?=[^\s\-0-9])", "\n", text)  # stray indent; list nesting starts with - or N.
            parts[i] = re.sub(r"\n{3,}", "\n\n", text)
        return "```".join(parts).strip() + "\n"


def table_markdown(rows: list[list[str]], header_rows: int) -> str:
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, body = (rows[0], rows[1:]) if header_rows else ([""] * width, rows)
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def page_markdown(html: str) -> tuple[str, str, set[str]]:
    """(title, markdown, linked /docs and /guides paths) for one HTML page."""
    parser = PageToMarkdown()
    parser.feed(html)
    parser.close()
    title = re.sub(r"\s*\|\s*Agent Realm\s*$", "", parser.title.strip())
    return title, parser.markdown(), parser.links


def fetch(path: str) -> str:
    req = urllib.request.Request(SITE + path, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def crawl() -> list[tuple[str, str, str]]:
    """Every page under the roots, breadth first: (path, title, markdown)."""
    seen: set[str] = set()
    todo = list(ROOTS)
    pages = []
    while todo:
        path = todo.pop(0)
        if path in seen:
            continue
        seen.add(path)
        title, md, links = page_markdown(fetch(path))
        pages.append((path, title, md))
        todo += sorted(links - seen)
    return pages


def demote(md: str) -> str:
    """Push every heading down one level, so each page sits under one ``#`` title."""
    parts = md.split("```")
    for i in range(0, len(parts), 2):  # leave code fences alone
        parts[i] = re.sub(r"^(#{1,5}) ", r"#\1 ", parts[i], flags=re.MULTILINE)
    return "```".join(parts)


def render(pages: list[tuple[str, str, str]], fetched: dt.date) -> str:
    head = [
        "# Agent Realm reference (for the AI planner)",
        "",
        f"Source: {SITE}/docs and {SITE}/guides, every page they link. Fetched {fetched.isoformat()}.",
        "Regenerate with `python3 scripts/refresh_planner_reference.py`; do not edit by hand.",
        "Each page below starts with a `# page:` line; the planner splits on them (`reference_sections`).",
        "",
    ]
    body = [f"# page: {path} — {title}\n\n{demote(md)}" for path, title, md in pages]
    return "\n".join(head) + "\n" + "\n".join(body)


def check_core(text: str) -> None:
    """Stop before writing when a core section the planner always sends is gone (a renamed heading)."""
    sys.path.insert(0, str(OUT.parents[2]))
    from agentrealm_agent.planner_reference import CORE_SECTIONS, missing_core, split_sections

    missing = missing_core(split_sections(text))
    if missing:
        raise SystemExit(
            "refresh: core section(s) not found: " + "; ".join(missing)
            + f". Update CORE_SECTIONS in planner_reference.py ({len(CORE_SECTIONS)} entries) to the new headings."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    pages = crawl()
    text = render(pages, dt.date.today())
    check_core(text)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(pages)} pages, {len(text)} chars, ~{len(text) // 4} tokens)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
