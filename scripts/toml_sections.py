#!/usr/bin/env python3
"""toml_sections — shared section-scoped TOML *text* utilities (stdlib only).

The house installers edit harness.toml as TEXT, never via a tomllib→writer
re-serialize, so sibling sections and their comments survive byte-for-byte
(ADR-0003's non-destructive-write rule). This module is the single owner of the
section-splitting + header-matching logic those writers share:

  * skills/setup-merge-gate/scripts/install_local.py   (merge_harness_toml)
  * skills/harness-doctor/scripts/record_profile.py    (merge_harness_section)

Header matching NORMALISES TOML-equivalent spellings — `[ harness ]`
(whitespace) and `["harness"]` / `['harness']` (quoted bare keys, per dotted
segment) all declare the SAME table — so an equivalent block is replaced, never
duplicated (a duplicate table is a TOMLDecodeError; codex:finding-0,
scaffold-doctor #03).

DELIBERATE LIMITATIONS — this is a LINE-ORIENTED text splitter, not a TOML
parser (the stdlib has no comment-preserving *writer*, which is the whole reason
for the text approach). Two known cases it does NOT handle, both because the fix
would require a real tokenizer the house pattern intentionally avoids:
  1. A *quoted key containing a dot* (`["a.b"]`) mis-normalises its segments.
  2. A multi-line / triple-quoted string value (`x = '''` … `'''`) whose body
     contains a line that LOOKS like a header (`[x]` or `[[x]]`) is mis-split —
     header() reads each line in isolation, with no string-context tracking.
Both are SAFE in practice because no house harness.toml uses either construct
(configs are flat single-line `key = value` tables); a partial triple-quote-
toggle tokenizer would mishandle TOML's escape/quote rules and trade a
documented limitation for a subtly-wrong one. If a harness.toml ever needs
multi-line values, replace this module with a real round-tripping TOML library,
not a half-parser. (Pre-existing: install_local / record_profile were
line-oriented before this module consolidated them.)
"""

__all__ = ["header", "split_sections", "section_name", "section_is"]


def header(line):
    """The raw "[…]" or "[[…]]" table header opening `line`, or None when the
    line is not a header.

    BOTH a normal table (`[x]`) and an array-of-tables (`[[x]]`) START A BLOCK.
    Returning None for `[[x]]` (the old behaviour) let an array-of-tables be
    ABSORBED into the preceding block — and record_profile then dropped it along
    with a preceding `[harness]` block on re-record (silent data loss; the
    Codex toml_sections:24 finding, reproduced). An array-of-tables is still
    never the SAME table as a single-bracket query — section_is/section_name
    reject `[[…]]` — so install_local never edit/deletes one; it only stops it
    from being swallowed by a deletable neighbour."""
    s = line.strip()
    if s.startswith("[[") and "]]" in s:
        return s[:s.index("]]") + 2]
    if s.startswith("[") and "]" in s:
        return s[:s.index("]") + 1]
    return None


def split_sections(text):
    """Split TOML text into [(header_or_None, raw_lines)] blocks. The first
    block's header is None (the preamble before any table). Lines are kept
    verbatim (keepends) so blocks re-join byte-for-byte, comments included."""
    blocks, cur_header, cur = [], None, []
    for line in text.splitlines(keepends=True):
        h = header(line)
        if h is not None:
            blocks.append((cur_header, cur))
            cur_header, cur = h, [line]
        else:
            cur.append(line)
    blocks.append((cur_header, cur))
    return blocks


def _normalize(name):
    """A dotted table name with each segment stripped of surrounding whitespace
    and one level of matching quotes — the TOML-equivalence classes a text
    comparison must fold."""
    parts = []
    for seg in name.split("."):
        seg = seg.strip()
        if len(seg) >= 2 and seg[0] in "\"'" and seg[-1] == seg[0]:
            seg = seg[1:-1]
        parts.append(seg)
    return ".".join(parts)


def section_name(hdr):
    """The normalized bare dotted table name a SINGLE-bracket header declares
    (e.g. '[ merge-gate . "local" ]' → 'merge-gate.local'), or None for a None
    header OR an array-of-tables (`[[…]]`) header. An array-of-tables is a
    distinct construct from the table that edit/delete logic targets, so it
    never resolves to a normal table name."""
    if hdr is None or hdr.startswith("[["):
        return None
    return _normalize(hdr[hdr.index("[") + 1:hdr.rindex("]")])


def section_is(hdr, name):
    """True iff `hdr` (a "[…]" header from split_sections, or None) declares
    the SINGLE-bracket table `name` (a bare dotted name like "merge-gate.local"),
    tolerating TOML-equivalent header spellings. An array-of-tables header
    (`[[name]]`) is never a match — it is a different construct, so callers that
    edit or drop a table by name never touch one."""
    if hdr is None or hdr.startswith("[["):
        return False
    return section_name(hdr) == _normalize(name)
