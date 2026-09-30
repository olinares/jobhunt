"""Load the verified facts an application may use.

The facts file is a markdown checklist kept in gitignored `private/verified.md` (the format is
in `facts/verified.example.md`). Only ticked items are loaded; everything else is dropped, so
an unconfirmed claim can never reach an application. The parser leans towards dropping text
whenever a line is ambiguous.

Parsing rules:

- Items live under `## Section` headings. Text before the first `##` heading, and anything
  inside a ``` code fence, is ignored. A `#` heading closes the current section, so items after
  it are ignored until the next `##`.
- A section whose heading starts with "Conflicts" (any case) is skipped whole, ticked items
  included. So is a `###` (or deeper) sub-heading that starts with "Conflicts", up to the next
  heading at its level or above.
- An item is a list line (`-`, `*`, `+` or `1.`) that starts with a box. Only `[x]` / `[X]` is
  ticked; `[ ]`, `[-]`, `[✓]`, `[ x ]` and any other box are not. A list line without a box
  (`- ✓ Python`) is not a fact.
- A line with several boxes (`- [x] Python · [ ] JavaScript`) is split at each box, and each
  part is judged alone; the `·` separators are dropped. A `·` with no box after it is plain text.
- A continuation line (indented, not itself a checkbox) belongs to the item above it and is kept
  or dropped with that item. On a line with several boxes it belongs to the last one. An
  indented checkbox is its own item, and so is a box inside a continuation line, marker or
  not. A non-indented line that is not an item ends the item
  above, and continuation lines after it are dropped.
- `###` sub-headings stay inside their `##` section: the sub-heading text becomes a prefix of
  each item under it, `"Languages: Python"`. Deeper sub-headings join with " / ":
  `"Cloud / AWS: Lambda"`.
- Ids are the section slug plus four hex characters of a hash of the item text, e.g.
  `linkedin-3f9a`, so inserting or removing an item never renumbers the others. A duplicate id
  gets `-2`, `-3`, ... in file order.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

FACTS_ENV = "JOBHUNT_FACTS"  # path to a facts file
FACTS_TEXT_ENV = "VERIFIED_FACTS"  # the facts file's text itself, e.g. a secret on a remote host
DEFAULT_FACTS_PATH = Path("private/verified.md")
EXAMPLE_PATH = Path("facts/verified.example.md")
# Oz's unconfirmed working copy. It must never be loaded, even by explicit path.
DRAFT_NAME = "verified-draft.md"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_MARKER = r"(?:[-*+]|\d+[.)])"
# A checkbox: empty, an x (maybe padded), or one mark such as `-` or `✓`. `[1]`, `[ab]` and
# `[link](url)` are text.
_BOX = r"\[(\s*|\s*[xX]\s*|[^\w\s\[\]])\](?!\()"
_BOX_RE = re.compile(_BOX)
_CHECKBOX_LINE = re.compile(rf"^(\s*){_LIST_MARKER}\s+(?={_BOX})")
_SEPARATOR = "·"


class FactsNotFound(FileNotFoundError):
    """No verified facts file where one was expected."""


@dataclass(frozen=True)
class Fact:
    id: str
    text: str


@dataclass(frozen=True)
class FactSection:
    title: str
    items: tuple[Fact, ...]


@dataclass(frozen=True)
class Facts:
    """Ticked facts by section. repr hides the text so it can't leak into logs."""

    sections: tuple[FactSection, ...] = field(default=())

    @property
    def count(self) -> int:
        return sum(len(s.items) for s in self.sections)

    def __iter__(self) -> Iterator[Fact]:
        for section in self.sections:
            yield from section.items

    def __repr__(self) -> str:
        return f"Facts(<{self.count} facts in {len(self.sections)} sections>)"

    def to_markdown(self, *, level: int = 2) -> str:
        """Each section as a heading at `level`, each fact as "- `id` text"."""
        hashes = "#" * level
        blocks = []
        for section in self.sections:
            lines = [f"{hashes} {section.title}", ""]
            lines += [f"- `{fact.id}` {fact.text}" for fact in section.items]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


# --------------------------------------------------------------------------- loading


def facts_path(path: str | Path | None = None, *, root: str | Path = ".") -> Path:
    """Where the facts come from: `path`, else `$JOBHUNT_FACTS`, else `<root>/private/verified.md`."""
    if path is not None:
        return Path(path)
    env = os.environ.get(FACTS_ENV, "").strip()
    if env:
        return Path(env)
    return Path(root) / DEFAULT_FACTS_PATH


def load_facts(path: str | Path | None = None, *, root: str | Path = ".") -> Facts:
    """Read and parse the verified facts.

    Precedence: explicit `path`, then `$VERIFIED_FACTS` (the file's text), then `$JOBHUNT_FACTS`
    (a path), then `<root>/private/verified.md`. Raises FactsNotFound; never tries another source.
    """
    if path is None:
        text = os.environ.get(FACTS_TEXT_ENV, "")
        if text.strip():
            return parse_facts(text)
    target = facts_path(path, root=root)
    if target.name == DRAFT_NAME:
        raise FactsNotFound(
            f"Refusing to load {target}: the draft holds unconfirmed facts. "
            f"Tick confirmed items in {DEFAULT_FACTS_PATH} instead."
        )
    if not target.is_file():
        if path is not None:
            source = "the path given"
        elif os.environ.get(FACTS_ENV, "").strip():
            source = f"from ${FACTS_ENV}"
        else:
            source = "the default location"
        raise FactsNotFound(
            f"No verified facts file at {target} ({source}). Looked for: an explicit path, "
            f"then ${FACTS_TEXT_ENV} (text), then ${FACTS_ENV} (path), then "
            f"<root>/{DEFAULT_FACTS_PATH}. Copy the format from "
            f"{EXAMPLE_PATH} into {DEFAULT_FACTS_PATH} and tick the facts you would defend."
        )
    return parse_facts(target.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- parsing


@dataclass
class _Item:
    """One box on a line being collected, plus its continuation lines."""

    ticked: bool
    parts: list[str]


def _boxes(line: str) -> list[_Item]:
    """Split a checkbox line into one item per box; text before the first box is the marker."""
    matches = list(_BOX_RE.finditer(line))
    items = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        text = line[match.end() : end].strip().rstrip(_SEPARATOR).strip()
        items.append(_Item(ticked=match.group(1) in ("x", "X"), parts=[text]))
    return items


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40].rstrip("-") or "facts"


def _is_conflicts(title: str) -> bool:
    return title.strip().lower().startswith("conflicts")


def parse_facts(text: str) -> Facts:
    """Parse checklist markdown into ticked facts (see the module docstring for the rules)."""
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None  # ticked item texts of the open section, None = ignoring
    subheadings: dict[int, str] = {}  # level (3..6) -> sub-heading text
    skip_below: int | None = None  # inside a Conflicts sub-heading at this level
    pending: list[_Item] = []  # boxes on the last checkbox line; continuations go to the last
    in_fence = False

    def flush() -> None:
        if current is not None and skip_below is None:
            context = " / ".join(subheadings[k] for k in sorted(subheadings))
            for item in pending:
                body = " ".join(p for p in item.parts if p)
                if item.ticked and body:
                    current.append(f"{context}: {body}" if context else body)
        pending.clear()

    for raw in text.splitlines():
        line = raw.rstrip()
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            continue  # blank lines neither start nor end an item
        heading = _HEADING.match(line)
        if heading:
            flush()
            level, title = len(heading.group(1)), heading.group(2)
            if level == 1:
                current = None
            elif level == 2:
                subheadings.clear()
                skip_below = None
                if _is_conflicts(title):
                    current = None
                else:
                    current = []
                    sections.append((title, current))
            else:
                for deeper in [k for k in subheadings if k >= level]:
                    del subheadings[deeper]
                if skip_below is not None and level <= skip_below:
                    skip_below = None
                if skip_below is None and _is_conflicts(title):
                    skip_below = level
                subheadings[level] = title
            continue
        if _CHECKBOX_LINE.match(line):
            flush()
            pending.extend(_boxes(line))
            continue
        if line[0].isspace():
            # A box inside a continuation (`  [ ] more`, `  tail · [ ] JS`) is judged on its own;
            # only the text before it continues the item above.
            first_box = _BOX_RE.search(line)
            head = line[: first_box.start()] if first_box else line
            head = head.strip().rstrip(_SEPARATOR).strip()
            if pending and head:
                pending[-1].parts.append(head)
            if first_box and pending:
                pending.extend(_boxes(line))
            continue
        # A non-indented line that is not an item: it ends the item above, and anything
        # indented under it belongs to it, not to that item.
        flush()

    flush()
    return _with_ids(sections)


def _with_ids(sections: list[tuple[str, list[str]]]) -> Facts:
    seen: set[str] = set()
    built = []
    for title, texts in sections:
        if not texts:
            continue
        slug = _slug(title)
        items = []
        for text in texts:
            base = f"{slug}-{hashlib.sha1(text.encode('utf-8')).hexdigest()[:4]}"
            fact_id, n = base, 1
            while fact_id in seen:
                n += 1
                fact_id = f"{base}-{n}"
            seen.add(fact_id)
            items.append(Fact(id=fact_id, text=text))
        built.append(FactSection(title=title, items=tuple(items)))
    return Facts(sections=tuple(built))
