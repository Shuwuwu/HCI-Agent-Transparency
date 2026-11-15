"""
Purpose:
    Read the text of AgentDojo tool results into Python values using only the standard library. AgentDojo turns a
    tool's return value (a record, a list of records, a date, a message) into text with PyYAML's block style; the
    stimulus pipeline needs to read a few fields back (a title, a recipient, a count of records). PyYAML is not in
    the project's standard-library-only toolset, so this module parses the small subset of YAML that PyYAML emits for
    such values, and refuses (ParseError) anything else, so a caller can fall back to a safe summary instead of
    guessing.

    Supported: block mappings and block sequences (sequences inside a mapping may sit at the mapping's own
    indentation, as PyYAML writes them), plain, single-quoted and double-quoted scalars that continue over several
    lines (PyYAML wraps long text), `[]`, `{}`, null and booleans. Plain scalars stay text (dates and numbers are not
    converted); quoted scalars are text. Not supported: block scalars (| and >), anchors, tags, flow collections
    other than the two empty ones.

Inputs:
    A string: the content of a tool message of an AgentDojo run.

Outputs:
    A dict, list, str, bool or None. Raises ParseError when the text is outside the supported subset.

Dependencies:
    None (standard library only).

Usage:
    import tool_result_parser as trp
    records = trp.parse("- title: Team Sync\\n  participants:\\n  - a@b.c\\n")   # [{"title": "Team Sync", ...}]
"""

import re

KEY = re.compile(r"""^(?P<key>"(?:[^"\\]|\\.)*"|'(?:[^']|'')*'|[^\s'"\[\]{}#&*!|>%@`,-][^:]*?|-[^\s:][^:]*?):(?:\s+(?P<rest>.*))?$""")


class ParseError(ValueError):
    """The text is not in the supported YAML subset."""


def parse(text: str):
    """Parse the text of a tool result. A text with no structure is returned as a (possibly multi-line) scalar."""
    lines = text.rstrip("\n").split("\n")
    if not any(line.strip() for line in lines):
        return None
    parser = _Parser(lines)
    value = parser.node(-1)
    parser.skip_blank()
    if parser.i < len(lines):
        raise ParseError(f"unparsed text from line {parser.i + 1}: {lines[parser.i][:60]!r}")
    return value


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


class _Parser:
    def __init__(self, lines: list[str]) -> None:
        self.lines, self.i = lines, 0

    def skip_blank(self) -> None:
        while self.i < len(self.lines) and not self.lines[self.i].strip():
            self.i += 1

    # A node starts at self.i and must be indented deeper than `parent` (-1 at the top level).
    def node(self, parent: int):
        self.skip_blank()
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        column = _indent(line)
        if column <= parent:
            return None
        body = line[column:]
        if body == "-" or body.startswith("- "):
            return self.sequence(column)
        if KEY.match(body) and not body.startswith(("'", '"')) or _quoted_key(body):
            return self.mapping(column)
        self.i += 1
        return self.scalar_from(body, parent)

    def sequence(self, column: int) -> list:
        items = []
        while True:
            self.skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            if _indent(line) != column or not (line[column:] == "-" or line[column:].startswith("- ")):
                break
            rest = line[column + 1:]
            if not rest.strip():  # "-" alone: the item starts on the next line
                self.i += 1
                items.append(self.node(column))
                continue
            # Treat the text after "- " as a line at column + 2, so a mapping item continues on the next lines.
            self.lines[self.i] = " " * (column + 2) + rest.lstrip(" ")
            items.append(self.node(column))
        return items

    def mapping(self, column: int) -> dict:
        result = {}
        while True:
            self.skip_blank()
            if self.i >= len(self.lines):
                break
            line = self.lines[self.i]
            if _indent(line) != column:
                break
            match = KEY.match(line[column:]) or _quoted_key(line[column:])
            if not match:
                break
            key = _unquote_key(match.group("key"))
            rest = (match.group("rest") or "").strip()
            self.i += 1
            if rest == "" or rest.startswith("#"):
                result[key] = self.nested(column)
            elif rest in ("|", ">") or rest[0] in "|>&*!":
                raise ParseError(f"unsupported value for {key!r}: {rest[:20]!r}")
            else:
                result[key] = self.scalar_from(rest, column)
        return result

    def nested(self, column: int):
        """The value of a key whose text after the colon is empty: a nested block, or null."""
        self.skip_blank()
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        indent = _indent(line)
        if indent == column and (line[indent:] == "-" or line[indent:].startswith("- ")):
            return self.sequence(column)  # PyYAML does not indent a sequence inside a mapping
        if indent > column:
            return self.node(column)
        return None

    def scalar_from(self, rest: str, column: int):
        """A scalar that starts after `key: ` and may continue on deeper-indented lines."""
        continuation = []
        while self.i < len(self.lines) and (not self.lines[self.i].strip() or _indent(self.lines[self.i]) > column):
            continuation.append(self.lines[self.i])
            self.i += 1
        while continuation and not continuation[-1].strip():
            continuation.pop()
        return _scalar(rest, continuation)


def _quoted_key(body: str):
    return KEY.match(body) if body[:1] in ("'", '"') else None


def _unquote_key(key: str) -> str:
    if key[:1] == "'" and key[-1:] == "'":
        return key[1:-1].replace("''", "'")
    if key[:1] == '"' and key[-1:] == '"':
        return _double_quoted(key[1:-1])
    return key


def _fold(first: str, continuation: list[str]) -> str:
    """Fold the lines of a flow scalar: a line break becomes a space, and a blank line becomes a newline."""
    parts = [first.rstrip()]
    pending_newlines = 0
    for raw in continuation:
        text = raw.strip()
        if not text:
            pending_newlines += 1
            continue
        parts.append("\n" * pending_newlines if pending_newlines else " ")
        parts.append(text)
        pending_newlines = 0
    return "".join(parts)


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"', "\\": "\\", "/": "/", " ": " ", "a": "\a",
            "b": "\b", "e": "\x1b", "f": "\f", "v": "\v", "N": "\x85", "_": "\xa0", "L": " ", "P": " "}


def _double_quoted(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        i += 1
        if i >= len(body):
            raise ParseError("dangling backslash")
        code = body[i]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 1
        elif code in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[code]
            digits = body[i + 1:i + 1 + width]
            if len(digits) != width or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                raise ParseError(f"bad escape \\{code}{digits}")
            out.append(chr(int(digits, 16)))
            i += 1 + width
        else:
            raise ParseError(f"unknown escape \\{code}")
    return "".join(out)


def _scalar(first: str, continuation: list[str]):
    """Interpret a scalar given its first line (after `key: `) and the continuation lines."""
    if first[:1] == "'":
        text = _fold(first, continuation)
        if not (len(text) >= 2 and text.endswith("'")) or _has_unescaped_quote(text[1:-1]):
            raise ParseError("single-quoted scalar is not closed")
        return text[1:-1].replace("''", "'")
    if first[:1] == '"':
        # A backslash at the end of a line joins it to the next one without a space.
        joined = first.rstrip()
        for raw in continuation:
            text = raw.strip()
            if (len(joined) - len(joined.rstrip("\\"))) % 2 == 1:  # an odd number of trailing backslashes
                joined = joined[:-1] + text
            elif not text:
                joined += "\n"
            else:
                joined += ("" if joined.endswith("\n") else " ") + text
        if not (len(joined) >= 2 and joined.endswith('"')):
            raise ParseError("double-quoted scalar is not closed")
        return _double_quoted(joined[1:-1])
    text = _fold(first, continuation)
    if text in ("null", "~", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text[:1] in "[{":
        raise ParseError(f"flow collection not supported: {text[:30]!r}")
    return text


def _has_unescaped_quote(inner: str) -> bool:
    return re.search(r"(?<!')'(?!')", inner.replace("''", "")) is not None
