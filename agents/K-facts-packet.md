# Agent K — Verified facts + application packet

## Branch / PR
`phase4/k-facts-packet` → one PR into `main`. Runs in parallel with J.

## Agent model
`opus`: this is the truthfulness guard. A parsing slip here puts an unverified claim in front of
an employer.

## Owns
src/jobhunt/facts.py (new), src/jobhunt/packet.py (new), facts/verified.example.md (new),
tests/test_facts.py (new), tests/test_packet.py (new)

## Background
The repo is public. Oz's verified facts live in gitignored `private/verified.md`, a markdown
checklist: `## Section` headings and `- [ ]` / `- [x]` items, some items spanning several
indented lines, some lines holding several inline boxes (`- [x] Python · [ ] JavaScript`).
A draft file (`private/verified-draft.md`) also exists; **never read it**, in code or in tests.
Do not open anything under `private/`. Tests use synthetic facts only.

## Build
### facts.py
- `load_facts(path: str | Path | None = None, *, root: str | Path = ".") -> Facts`:
  explicit `path`, else `$JOBHUNT_FACTS`, else `<root>/private/verified.md`. Missing file →
  `FactsNotFound` with a message naming the paths tried and pointing at
  `facts/verified.example.md`. Never fall back to any other file.
- `Facts(sections: tuple[FactSection, ...])`, `FactSection(title: str, items: tuple[Fact, ...])`,
  `Fact(id: str, text: str)`; frozen dataclasses. `Facts.count`, `Facts.to_markdown()`.
- Parsing rules (each one gets a test):
  - Only `[x]` / `[X]` counts as ticked. `[ ]`, `✓`, `[-]` or anything else is unticked.
  - Any section whose heading starts with "Conflicts" (case-insensitive) is skipped whole, even
    ticked items. Resolved facts are moved into a real section by hand.
  - A continuation line (indented, not itself a checkbox) belongs to the item above it and is
    kept or dropped with that item.
  - An indented `- [ ]` / `- [x]` is its own item.
  - A line with several boxes is split on `·` and each part judged alone:
    `- [x] Python · [ ] JavaScript` yields only "Python".
  - Text before the first `##` heading, and anything inside ``` code fences, is ignored.
  - `###` sub-headings stay inside their `##` section (keep the sub-heading text as context in
    the item or as a prefix; pick one and document it).
  - Ids: section slug + short hash of the item text (e.g. `linkedin-3f9a`), so inserting an item
    doesn't renumber the others.
- `facts/verified.example.md`: the same format with obviously fake entries (fake company,
  fake metrics), including one Conflicts section, one multi-line item and one inline multi-box
  line. It doubles as a test fixture.

### packet.py
- `build_packet(job: Job, score: Score | None, resumes: Resumes, facts: Facts) -> Packet` (pure,
  no I/O, no LLM call).
- `Packet` (frozen dataclass): job title, company, url, locations, pay (if any), description as
  plain text via `scoring.html_to_text`, `variant` (from score; `"se"` when unscored), the resume
  text for that variant, the facts, and a fixed `RULES` text:
  use only the listed facts and the resume; no number, customer name or claim that does not
  appear verbatim in the facts; when a question needs something missing, say "not in verified
  facts" and leave it for Oz; Oz submits, never the assistant.
- `Packet.to_markdown()` — what the MCP tool returns: job header, rules, verified facts, resume,
  job description, in that order.
- `NoVerifiedFacts` if `facts.count == 0` (raised by `build_packet`).
- `Resumes` and `load_resumes` come from `jobhunt.scoring`. Do not change scoring.py or models.py.

## Tests
Synthetic facts text inline in tests plus `facts/verified.example.md`; a fake `Resumes`. Cover every
parsing rule above (especially: unticked continuation lines never appear; inline `[ ]` box never
appears; a ticked item in Conflicts never appears), `FactsNotFound`, env override, id stability
when an item is inserted, packet variant choice, `NoVerifiedFacts`, and that `to_markdown()` holds
no text from unticked items. No network.

## Done when
PR open with the template filled in, CI green.
