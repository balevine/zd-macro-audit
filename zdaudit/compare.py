"""Work out which words of a sent comment came from which macro.

Each macro is compiled into fixed text and gaps for its placeholders, then
aligned against the comment's words. A word is only ever credited to one macro.
Everything else follows from that alignment:

- A macro is intact when all of its fixed text appears, in order and unbroken.
- Its edit percentage is how many of its fixed words were removed or changed, or
  had words inserted among them, as a share of its fixed words.
- Text was added when words are left over that no macro claimed.

Differences in whitespace, formatting marks (bold, links, list bullets) and
curly versus straight quotes are ignored. A placeholder may only fill a limited
number of words, so a long filled-in value can count as an edit. That's on
purpose: it's better to flag one than to let added text pass as part of a
placeholder.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from html.parser import HTMLParser

# How many words each kind of placeholder may fill. The first match wins.
GAP_LIMITS = (
    (re.compile(r"comment|description"), 200),
    (re.compile(r"signature"), 40),
    (re.compile(r"url|link|title|subject"), 15),
    (re.compile(r"(?:^|\.)ticket_field_\d+$"), 15),   # custom fields are often free text
    (re.compile(r"email"), 8),
    (re.compile(r"phone"), 6),
    (re.compile(r"first_name|last_name"), 3),
    (re.compile(r"name"), 6),
    (re.compile(r"(?:^|[._])id$"), 3),
)
DEFAULT_GAP = 5
UNKNOWN_DC_GAP = 100   # dynamic content we couldn't read could be any length
LOOP_GAP = 30          # {% for %} repeats its contents an unknown number of times
MAX_DC_DEPTH = 3       # dynamic content can include other dynamic content
MAX_PLACEMENTS = 50    # places to consider for one macro in one comment, to bound the search

_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_TAG_NAME = re.compile(r"\{%-?\s*(\w+)")
_PLACEHOLDER_NAME = re.compile(r"\{\{-?\s*([\w.-]+)")
_SLOT = re.compile(r"\x00(\d+)\x00")
_HTML_TAG = re.compile(
    r"</?(?:p|div|br|ul|ol|li|span|strong|b|i|u|s|em|a|h[1-6]|blockquote|table|tr|td|th|img|hr|pre|code)\b[^>]*>",
    re.I,
)

# Liquid blocks whose contents may render, and those that never render.
_CHOICE_BLOCKS = {"if", "unless", "case"}
_BLOCKS = _CHOICE_BLOCKS | {"for", "capture", "comment"}

# Letters, digits and punctuation are separate tokens, so "Hi {{name}}," lines
# up with "Hi Sam,". Chinese, Japanese and Korean characters are one token each.
_TOKEN = re.compile(r"[぀-ヿ㐀-鿿가-힯]|\d+|[^\W\d_]+|\S")

_WORD_JOINERS = {"-", "'"}

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "\xa0": " ",
    "​": "", "‌": "", "‍": "", "⁠": "", "﻿": "", "\xad": "",
})
_MD_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+.!>-])")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_RULE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", re.M)
_MD_MARKS = re.compile(r"\*\*|__|~~|`")
_MD_STAR = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])")
_MD_UNDERSCORE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])")
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


def _normalize(text: str) -> str:
    text = html.unescape(text).translate(_CHAR_MAP)
    text = _MD_ESCAPE.sub(r"\1", text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_RULE.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = _MD_STAR.sub(r"\1", text)
    text = _MD_UNDERSCORE.sub(r"\1", text)
    text = _LINE_MARKS.sub("", text)
    return " ".join(text.split())


def _is_word(token: str) -> bool:
    return token[0].isalnum()


# ---- The comment -------------------------------------------------------------

class _Comment:
    def __init__(self, sent: str):
        self.text = _normalize(sent)
        found = list(_TOKEN.finditer(self.text))
        self.tokens = tuple(m.group(0) for m in found)
        self.spans = [m.span() for m in found]
        self.is_word = [_is_word(t) for t in self.tokens]
        # Mary-Jane and O'Neil are one word each when counting what a placeholder filled.
        self.starts_word = [self.is_word[i] and not self._continues_word(i) for i in range(len(self.tokens))]

    def _continues_word(self, i: int) -> bool:
        return (i >= 2 and self.tokens[i - 1] in _WORD_JOINERS and self.is_word[i - 2]
                and self.spans[i - 2][1] == self.spans[i - 1][0] and self.spans[i - 1][1] == self.spans[i][0])

    def without(self, claimed: list[tuple[int, int]]) -> list[str]:
        """The comment's words, leaving out the given ranges of tokens."""
        pieces, position = [], 0
        for start, end in sorted(claimed):
            pieces.append(self.text[position:self.spans[start][0]])
            position = self.spans[end - 1][1]
        pieces.append(self.text[position:])
        return " ".join(pieces).split()


# ---- What a macro compiles to --------------------------------------------------

@dataclass
class _Literal:
    tokens: tuple[str, ...]

    def ends(self, comment: _Comment, start: int) -> set[int]:
        end = start + len(self.tokens)
        return {end} if comment.tokens[start:end] == self.tokens else set()


@dataclass
class _Gap:
    max_words: int

    def ends(self, comment: _Comment, start: int) -> set[int]:
        found, words = {start}, 0
        for i in range(start, len(comment.tokens)):
            words += comment.starts_word[i]
            if words > self.max_words:
                break
            found.add(i + 1)
        return found


@dataclass
class _Choice:
    options: list[list]

    def ends(self, comment: _Comment, start: int) -> set[int]:
        found: set[int] = set()
        for option in self.options:
            found |= _sequence_ends(option, comment, start)
        return found


def _sequence_ends(parts: list, comment: _Comment, start: int) -> set[int]:
    """Every position in the comment where this run of parts could finish."""
    ends = {start}
    for part in parts:
        ends = {after for end in ends for after in part.ends(comment, end)}
    return ends


# ---- Reading a macro's text ----------------------------------------------------

@dataclass
class _Placeholder:
    source: str


@dataclass
class _Block:
    tag: str
    branches: list[list]
    has_else: bool = False


def _parse_tree(text: str) -> list:
    """Split text into strings, placeholders and (possibly nested) Liquid blocks."""
    root: list = []
    current = root
    stack: list[tuple[_Block, list]] = []
    position = 0
    for match in _TAG.finditer(text):
        current.append(text[position:match.start()])
        position = match.end()
        source = match.group(0)
        if source.startswith("{{"):
            current.append(_Placeholder(source))
            continue
        name_match = _TAG_NAME.match(source)
        name = name_match.group(1) if name_match else ""
        if name in _BLOCKS:
            block = _Block(name, [] if name == "case" else [[]])
            current.append(block)
            stack.append((block, current))
            current = block.branches[-1] if block.branches else []  # text before {% when %} never renders
        elif name in ("elsif", "else", "when") and stack:
            block = stack[-1][0]
            block.has_else = block.has_else or name == "else"
            block.branches.append([])
            current = block.branches[-1]
        elif name.startswith("end") and any(block.tag == name[3:] for block, _ in stack):
            while True:
                block, current = stack.pop()
                if block.tag == name[3:]:
                    break
    current.append(text[position:])
    return root


def _placeholder_name(source: str) -> str:
    match = _PLACEHOLDER_NAME.match(source)
    return match.group(1).lower() if match else ""


def _gap_limit(name: str) -> int:
    if name.startswith("dc."):
        return UNKNOWN_DC_GAP
    return next((limit for pattern, limit in GAP_LIMITS if pattern.search(name)), DEFAULT_GAP)


def _compile_placeholder(node: _Placeholder, dynamic: dict[str, list[str]], depth: int) -> tuple[list, str]:
    name = _placeholder_name(node.source)
    variants = dynamic.get(name) if name.startswith("dc.") and depth < MAX_DC_DEPTH else None
    if not variants:
        return [_Gap(_gap_limit(name))], "".join(node.source.split())
    # A variant may be stored as HTML, which needs the same treatment as a rich text macro.
    compiled = [
        _compile_nodes(_parse_tree(html_to_text(v) if _HTML_TAG.search(v) else v), dynamic, depth + 1)
        for v in variants
    ]
    return [_Choice([parts for parts, _ in compiled])], compiled[0][1]


def _compile_block(node: _Block, dynamic: dict[str, list[str]], depth: int) -> tuple[list, str]:
    label = f" {{%{node.tag}…%}} "   # Liquid output usually starts its own sentence
    if node.tag == "for":
        return [_Gap(LOOP_GAP)], label
    if node.tag in _CHOICE_BLOCKS:
        options = [_compile_nodes(branch, dynamic, depth)[0] for branch in node.branches]
        if not node.has_else:
            options.append([])
        return [_Choice(options)], label
    return [], ""  # {% capture %} and {% comment %} render nothing


def _compile_nodes(nodes: list, dynamic: dict[str, list[str]], depth: int) -> tuple[list, str]:
    """Return (parts, display text). Text is normalized as a whole, with slots for the rest."""
    slots: list = []
    joined: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            joined.append(node)
        else:
            joined.append(f"\x00{len(slots)}\x00")
            slots.append(node)

    parts: list = []
    display: list[str] = []
    for i, piece in enumerate(_SLOT.split(_normalize("".join(joined)))):
        if i % 2 == 0:
            tokens = tuple(_TOKEN.findall(piece))
            if tokens:
                parts.append(_Literal(tokens))
            display.append(piece)
            continue
        node = slots[int(piece)]
        compile_slot = _compile_placeholder if isinstance(node, _Placeholder) else _compile_block
        slot_parts, slot_display = compile_slot(node, dynamic, depth)
        parts.extend(slot_parts)
        display.append(slot_display)
    return parts, "".join(display)


def _required_words(parts: list) -> int:
    """How many fixed words must appear, whichever options are taken."""
    total = 0
    for part in parts:
        if isinstance(part, _Literal):
            total += sum(_is_word(t) for t in part.tokens)
        elif isinstance(part, _Choice):
            total += min(_required_words(option) for option in part.options)
    return total


@dataclass
class Macro:
    core: list                 # parts from the first to the last that isn't a placeholder gap
    lead_words: int            # words the placeholders before the core can fill
    trail_words: int           # words the placeholders after the core can fill
    display_words: list[str]   # macro words with placeholders kept, for diffs
    comparable: bool           # False when nothing but placeholders is certain, so any comment could match


def compile_macro(text: str, dynamic: dict[str, list[str]] | None = None) -> Macro:
    parts, display = _compile_nodes(_parse_tree(text), dynamic or {}, 0)
    first = next((i for i, part in enumerate(parts) if not isinstance(part, _Gap)), len(parts))
    last = next((i for i in range(len(parts), first, -1) if not isinstance(parts[i - 1], _Gap)), first)
    return Macro(
        parts[first:last],
        sum(part.max_words for part in parts[:first]),
        sum(part.max_words for part in parts[last:]),
        display.split(),
        _required_words(parts) > 0,
    )


# ---- Comparing ---------------------------------------------------------------

@dataclass
class MacroResult:
    intact: bool
    pct_of_macro: float  # 0 to 100, how much of the macro was edited, where 0 means intact
    diff: list[tuple[str, list[str]]] | None = None


@dataclass
class Comparison:
    results: list[MacroResult]  # one per macro, in the order given
    added_text: bool            # words are left over that no macro accounts for


def _outline(parts: list, available: set[str]) -> list:
    """The macro as fixed words and gap sizes, taking the option closest to what was sent."""
    outline: list = []
    for part in parts:
        if isinstance(part, _Literal):
            outline.extend(t for t in part.tokens if _is_word(t))
        elif isinstance(part, _Gap):
            outline.append(part.max_words)
        else:
            options = [_outline(option, available) for option in part.options]
            outline.extend(max(options, key=lambda o: (
                sum(item in available for item in o if isinstance(item, str)),
                -sum(isinstance(item, str) for item in o),
            )))
    return outline


def _pct_of_macro(outline: list, sent_words: list[str], starts_word: list[bool]) -> tuple[float, list[tuple[int, int]]]:
    """How much of a macro was edited, and which runs of sent words matched it (start, length).

    A replaced stretch counts as its longer side. Words inserted where a
    placeholder sits only count beyond what the placeholder can fill. Words
    before the macro's first match or after its last are text added around it,
    so they don't count.
    """
    words: list[str] = []
    free: dict[int, int] = {}   # position in words -> how many words a gap there can fill
    for item in outline:
        if isinstance(item, str):
            words.append(item)
        else:
            free[len(words)] = free.get(len(words), 0) + item
    matcher = SequenceMatcher(None, words, sent_words, autojunk=False)
    changed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 == 0 or i2 == len(words):
            changed += i2 - i1
        else:
            allowed = sum(free.get(i, 0) for i in range(i1, i2 + 1))
            changed += max(i2 - i1, sum(starts_word[j1:j2]) - allowed)
    # An edited macro never reads 0%, even if only its punctuation changed.
    pct = min(max(round(100 * changed / len(words), 1), 0.1), 100.0)
    return pct, [(block.b, block.size) for block in matcher.get_matching_blocks() if block.size]


def _candidates(macro: Macro, comment: _Comment) -> list[tuple[int, int]]:
    """Each (start, end) range of tokens where this macro's core could sit, longest kept first."""
    found = []
    for start in range(len(comment.tokens)):
        found.extend((start, end) for end in _sequence_ends(macro.core, comment, start) if end > start)
    found.sort(key=lambda r: (r[0] - r[1], r[0]))
    return sorted(found[:MAX_PLACEMENTS])


def _extend(comment: _Comment, claimed: list[bool], start: int, end: int, macro: Macro) -> tuple[int, int]:
    """Widen a placed macro over free text its outer placeholders could have filled."""
    if macro.lead_words:
        words = 0
        while start > 0 and not claimed[start - 1] and words + comment.starts_word[start - 1] <= macro.lead_words:
            start -= 1
            words += comment.starts_word[start]
    if macro.trail_words:
        words = 0
        while end < len(claimed) and not claimed[end] and words + comment.starts_word[end] <= macro.trail_words:
            words += comment.starts_word[end]
            end += 1
    return start, end


def _place(candidates: list[list[tuple[int, int]]]) -> dict[int, tuple[int, int]]:
    """Give each macro its own range of the comment, with no word shared.

    Tries every arrangement. It keeps the most macros intact, then accounts for
    the most text, then takes the earliest text, so the result doesn't depend on
    the order macros were applied. Only macros with identical text can still tie.
    """
    memo: dict[tuple[int, int], tuple[int, int, tuple]] = {}

    def rank(option: tuple[int, int, tuple]) -> tuple:
        placed, covered, spots = option
        return placed, covered, tuple((-start, -end) for _, start, end in spots)

    def best(position: int, used: int) -> tuple[int, int, tuple]:
        key = (position, used)
        if key not in memo:
            result = (0, 0, ())
            for index, ranges in enumerate(candidates):
                if used >> index & 1:
                    continue
                for start, end in ranges:
                    if start >= position:
                        placed, covered, rest = best(end, used | 1 << index)
                        option = (placed + 1, covered + end - start, ((index, start, end), *rest))
                        if rank(option) > rank(result):
                            result = option
            memo[key] = result
        return memo[key]

    return {index: (start, end) for index, start, end in best(0, 0)[2]}


def _word_diff(before: list[str], after: list[str]) -> list[tuple[str, list[str]]]:
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


def compare(macros: list[Macro], sent: str) -> Comparison:
    """Compare the sent text against every comparable macro applied in the same update."""
    comment = _Comment(sent)
    placed = _place([_candidates(macro, comment) for macro in macros])
    claimed = [False] * len(comment.tokens)
    for start, end in placed.values():
        claimed[start:end] = [True] * (end - start)
    # Outer placeholders only get text that no macro's fixed text needs.
    for index, (start, end) in placed.items():
        placed[index] = _extend(comment, claimed, start, end, macros[index])
        start, end = placed[index]
        claimed[start:end] = [True] * (end - start)
    remaining = comment.without(list(placed.values()))

    results: list[MacroResult | None] = [
        MacroResult(True, 0.0) if index in placed else None for index in range(len(macros))
    ]
    # Measure edited macros largest first, so a word only counts toward one of them.
    edited = [index for index, result in enumerate(results) if result is None]
    available = {comment.tokens[i] for i in range(len(claimed)) if not claimed[i]}
    outlines = {index: _outline(macros[index].core, available) for index in edited}
    for index in sorted(edited, key=lambda i: -sum(isinstance(item, str) for item in outlines[i])):
        open_words = [i for i in range(len(claimed)) if comment.is_word[i] and not claimed[i]]
        pct, matches = _pct_of_macro(outlines[index], [comment.tokens[i] for i in open_words],
                                     [comment.starts_word[i] for i in open_words])
        for start, size in matches:
            for i in open_words[start:start + size]:
                claimed[i] = True
        results[index] = MacroResult(False, pct, _word_diff(macros[index].display_words, remaining))

    added = any(comment.is_word[i] and not claimed[i] for i in range(len(claimed)))
    return Comparison(results, added)


def is_placeholder(word: str) -> bool:
    return "{{" in word or "{%" in word
