"""Compare the text an agent sent with the text a macro inserts.

A use counts as unedited only when the sent comment equals the macro text.
Differences in whitespace, formatting marks (bold, links, list bullets) and
curly versus straight quotes are ignored. Placeholders such as
{{ticket.requester.first_name}} match any text, because Zendesk fills them in
before it saves the comment. Dynamic content placeholders ({{dc.name}}) match
any of that item's variants when we know them.
"""
from __future__ import annotations

import html
import itertools
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser

# Liquid blocks ({% if %}...{% endif %}) come first so their contents are
# treated as part of the placeholder, since they may or may not appear.
PLACEHOLDER = re.compile(
    r"\{%-?\s*(?P<tag>if|unless|case|for|capture)\b.*?\{%-?\s*end(?P=tag)\s*-?%\}"
    r"|\{\{.*?\}\}"
    r"|\{%.*?%\}",
    re.S,
)
_DC = re.compile(r"\{\{\s*(dc\.[\w.-]+)\s*\}\}", re.I)
_SLOT = re.compile(r"\x00(\d+)\x00")

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    " ": " ", "​": "",
})
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKS = re.compile(r"\*\*|__|~~")
_LINE_MARKS = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+•][ \t]+|\d+[.)][ \t]+)", re.M)


class _TextExtractor(HTMLParser):
    BLOCKS = {
        "p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return "".join(parser.parts)


def normalize(text: str) -> str:
    text = html.unescape(text).translate(_CHAR_MAP)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_MARKS.sub("", text)
    text = _LINE_MARKS.sub("", text)
    return " ".join(text.split())


@dataclass
class Template:
    pattern: re.Pattern
    literal_words: list[str]   # macro words with placeholders left out, for scoring
    display_words: list[str]   # macro words with placeholders kept, for diffs


def _parse(text: str, dynamic: dict[str, list[str]], depth: int = 0) -> tuple[str, list[str], list[str]]:
    """Return (regex source, literal words, display words) for one macro text."""
    slots: list[str] = []

    def stash(match: re.Match) -> str:
        slots.append(match.group(0))
        return f"\x00{len(slots) - 1}\x00"

    pieces = _SLOT.split(normalize(PLACEHOLDER.sub(stash, text)))
    regex: list[str] = []
    literal: list[str] = []
    display: list[str] = []   # pieces of the display text, split into words at the end
    for i, piece in enumerate(pieces):
        if i % 2:  # a placeholder
            slot = slots[int(piece)]
            dc = _DC.fullmatch(slot.strip())
            variants = dynamic.get(dc.group(1).lower()) if dc and depth < 3 else None
            if variants:
                parsed = [_parse(v, dynamic, depth + 1) for v in variants]
                regex.append("(?:" + "|".join(p[0] for p in parsed) + ")")
                literal.extend(parsed[0][1])
                display.append(" ".join(parsed[0][2]))
            else:
                regex.append(".*?")
                display.append(_compact(slot))
            continue
        # Literal text. A space next to a placeholder is optional, because the
        # placeholder may render as nothing and leave a single space behind.
        core, lead, trail = piece, "", ""
        if i > 0 and core.startswith(" "):
            core, lead = core[1:], " ?"
        if i < len(pieces) - 1 and core.endswith(" "):
            core, trail = core[:-1], " ?"
        regex.append(lead + re.escape(core) + trail)
        literal.extend(core.split())
        display.append(piece)
    return "".join(regex), literal, "".join(display).split()


def _compact(slot: str) -> str:
    """A placeholder as one word, so it lines up with the word it turned into."""
    if slot.startswith("{%"):
        # Liquid output usually starts its own sentence, so keep it a separate word.
        tag = re.match(r"\{%-?\s*(\w+)", slot)
        return " {%" + (tag.group(1) if tag else "") + "…%} "
    return "".join(slot.split())


def is_placeholder(word: str) -> bool:
    return "{{" in word or "{%" in word


def build_template(text: str, dynamic: dict[str, list[str]] | None = None) -> Template:
    source, literal, display = _parse(text, dynamic or {})
    return Template(re.compile(source, re.S), literal, display)


@dataclass
class Comparison:
    matched: bool
    similarity: float  # 0 to 100, where 100 means the macro text was sent as is
    template: Template


def compare(macro_texts: list[str], sent: str, dynamic: dict[str, list[str]] | None = None) -> Comparison:
    """Compare sent text against one or more macro texts applied in the same update.

    When several macros were applied, their texts may appear in any order, so
    each order is tried (up to three macros).
    """
    sent_norm = normalize(sent)
    sent_words = sent_norm.split()
    orders = itertools.permutations(macro_texts) if len(macro_texts) <= 3 else [macro_texts]
    best: Comparison | None = None
    for order in orders:
        template = build_template("\n\n".join(order), dynamic)
        if template.pattern.fullmatch(sent_norm):
            return Comparison(True, 100.0, template)
        ratio = SequenceMatcher(None, template.literal_words, sent_words, autojunk=False).ratio()
        score = min(round(ratio * 100, 1), 99.9)
        if best is None or score > best.similarity:
            best = Comparison(False, score, template)
    assert best is not None
    return best


def word_diff(before: list[str], after: list[str]) -> list[tuple[str, list[str]]]:
    """Word-level diff as a list of ("equal" | "delete" | "insert", words)."""
    ops: list[tuple[str, list[str]]] = []
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            ops.append(("equal", before[i1:i2]))
            continue
        if i2 > i1:
            ops.append(("delete", before[i1:i2]))
        if j2 > j1:
            ops.append(("insert", after[j1:j2]))
    return ops
