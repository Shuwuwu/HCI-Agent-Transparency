"""
Purpose:
    Text analysis shared by the skill validators (validate_skills.py for per-scenario skills and
    validate_capability_skills.py for capability skills): parsing a skill file (frontmatter, sections, list items),
    finding tool names and specific values (addresses, dates, times, file names, names, quoted text) in a text,
    comparing texts for shared word runs, and the generic checks on tool accuracy (T1-T6) and safety (A1-A5)
    that do not depend on which scenario or skill a text belongs to.

Inputs:
    Texts and the tool tables passed in by the caller. Nothing is read from disk, except that injection_markers()
    uses the injection goals recorded in agentdojo_runs.py.

Outputs:
    Python values only (parsed pieces, sets of literals, dictionaries of findings per check id); no files.

Dependencies:
    agentdojo_runs.py (injection goals and attacker address). Standard library only.

Usage:
    Imported by the validators and by check_skill_map.py.
"""

import re

import agentdojo_runs as runs

# ---- constants

SEQUENCE_WORDS = 6  # a run of this many words shared with a scenario prompt counts as leakage
NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")
DESCRIPTION_MAX = 1024
HIDDEN_ARGUMENTS = ("calendar", "inbox", "cloud_drive")  # injected by AgentDojo, never seen by the agent
ENUM_NEUTRAL_WORDS = {"null", "true", "false", "none"}

# A snake_case word starting with one of these looks like a tool name (used to catch invented tools).
TOOL_VERBS = (
    "get", "search", "send", "create", "delete", "list", "append", "share", "cancel", "reschedule", "add", "update",
    "read", "fetch", "find", "forward", "reply", "move", "remove", "set", "schedule", "check", "open", "download",
    "upload", "write", "edit", "modify", "save", "show", "view", "retrieve", "query", "lookup", "archive", "mark",
    "label", "invite",
)
OVERRIDE_PHRASES = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier|other) (instructions|rules|messages)",
    r"disregard", r"before anything else", r"(do not|don't|never) tell the user", r"without telling the user",
    r"without (notifying|informing|alerting) the user", r"(do not|don't) mention (this|these|it) to the user",
    r"keep (this|it|these) (secret|hidden|confidential) from", r"\bsecretly\b", r"\bsilently\b",
    r"regardless of (what )?the user", r"system prompt", r"you must always",
]
BENCHMARK_PHRASES = [
    r"agentdojo", r"benchmark", r"hidden tests?", r"unit tests?", r"test cases?", r"ground[- ]truth",
    r"evaluation (script|harness|set)", r"\bgrader\b", r"\bverifier\b", r"injection tasks?", r"user tasks?",
    r"prompt injection",
]
SECRET_PATTERNS = [r"\bsk-[A-Za-z0-9_-]{16,}", r"\bghp_[A-Za-z0-9]{20,}", r"\bAKIA[0-9A-Z]{16}\b",
                   r"\bBearer\s+[A-Za-z0-9._-]{12,}"]
LONG_TOKEN = re.compile(r"[A-Za-z0-9+/_=-]{32,}")  # a key-like token is not a plain lowercase hyphenated slug
# Phrases from the injection goals that the quote-based extraction misses (the goal text nests quotes).
GOAL_MARKER_EXTRA = ("hi mark, it was really great to hear from you",)
EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME_PATTERN = re.compile(r"\b(\d{1,2}:\d{2})(?::\d{2})?\b")  # seconds are ignored
FILENAME_PATTERN = re.compile(r"\b[\w-]+\.(?:docx?|xlsx?|pdf|txt|csv|pptx?|md|json|png|jpe?g|zip)\b", re.I)
NUMBER_PATTERN = re.compile(r"\b\d{2,}\b")
QUOTED_PATTERN = re.compile(r"""(?<!\w)(?:'([^'\n]{4,}?)'|"([^"\n]{4,}?)")(?!\w)""")
NAME_FIELD_PATTERN = re.compile(r"^\s*-?\s*name: ([A-Za-z][A-Za-z .'-]{2,40})\s*$", re.M)
CAPITALISED_PAIR = re.compile(r"\b[A-Z][a-z]{2,} [A-Z][a-z]{2,}\b")


# ---- parsing, mentions and literals

def parse_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """Split a skill into (frontmatter fields, body, error).

    Accepts the simple YAML a skill needs: `key: value` lines (plain, 'single' or "double" quoted), indented
    continuation lines, and `>` / `|` block scalars. On failure the fields are None and the body is the whole text.
    """
    lines = text.lstrip("﻿").split("\n")
    if lines[0].strip() != "---":
        return None, text, "the file must start with a '---' line"
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None, text, "the frontmatter is not closed by a second '---' line"
    raw: dict[str, list[str]] = {}
    key = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match and not line[0].isspace():
            key = match.group(1)
            if key in raw:
                return None, text, f"duplicate frontmatter field {key!r}"
            raw[key] = [match.group(2).strip()]
        elif key and line[0].isspace():
            raw[key].append(line.strip())
        else:
            return None, text, f"cannot parse frontmatter line {line!r}"
    fields = {}
    for name, parts in raw.items():
        if parts[0] in ("", ">", ">-", ">+", "|", "|-", "|+"):
            value = ("\n" if parts[0].startswith("|") else " ").join(parts[1:])
        else:
            value = " ".join(parts)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1].replace('\\"', '"') if value[0] == '"' else value[1:-1].replace("''", "'")
        fields[name] = value.strip()
    return fields, "\n".join(lines[end + 1:]), None


def split_sections(body: str) -> tuple[list[str], dict[str, str]]:
    """Return (the level-2 headings in order, {heading: text under it}). Lines inside code fences are ignored."""
    headings, sections, current, in_fence = [], {}, None, False
    for line in body.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else re.match(r"^## (.+?)\s*$", line)
        if match:
            current = match.group(1)
            headings.append(current)
            sections.setdefault(current, "")
        elif current is not None:
            sections[current] += line + "\n"
    return headings, sections


def split_blocks(text: str) -> list[str]:
    """Split text into blocks: paragraphs, and one block per list item (with its continuation lines)."""
    blocks, current = [], []
    for line in text.split("\n"):
        starts_item = re.match(r"^\s*(\d+[.)]|[-*])\s", line)
        if not line.strip() or starts_item:
            if current:
                blocks.append("\n".join(current))
            current = [line] if line.strip() else []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def mentions(text: str, names) -> list[str]:
    """Return the given names that occur in text as whole identifiers, in order of first occurrence."""
    found = []
    for name in names:
        match = re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text)
        if match:
            found.append((match.start(), name))
    return [name for _, name in sorted(found)]


def backticked(text: str) -> list[str]:
    """Return the contents of all inline `code` spans."""
    return re.findall(r"`([^`\n]+)`", text)


# ---- specific values

def literals(text: str, with_quoted: bool = False) -> dict[str, set[str]]:
    """Extract the specific values in a text: emails, dates, times, file names, person names (and quoted strings).

    Person names come from email local parts ("jane.doe@..." -> "jane doe"), `name:` fields and, for quoted
    request text only, pairs of capitalised words. Everything is lower-cased.
    """
    found = {
        "email": {m.lower() for m in EMAIL_PATTERN.findall(text)},
        "date": set(DATE_PATTERN.findall(text)),
        "time": set(TIME_PATTERN.findall(text)),
        "filename": {m.lower() for m in FILENAME_PATTERN.findall(text)},
        "name": set(),
        "quoted": set(),
    }
    for local in (address.split("@")[0] for address in found["email"]):
        parts = re.split(r"[._-]", local)
        if len(parts) == 2 and all(p.isalpha() and len(p) >= 3 for p in parts):
            found["name"].add(" ".join(parts))
    found["name"] |= {m.strip().lower() for m in NAME_FIELD_PATTERN.findall(text) if " " in m.strip()}
    if with_quoted:
        found["name"] |= {m.lower() for m in CAPITALISED_PAIR.findall(text)}
        found["quoted"] = {(a or b).lower() for a, b in QUOTED_PATTERN.findall(text)}
    return found


def numbers_in(text: str) -> set[str]:
    """Numbers of two or more digits outside emails, dates, times and list markers."""
    for pattern in (EMAIL_PATTERN, DATE_PATTERN, TIME_PATTERN):
        text = pattern.sub(" ", text)
    text = re.sub(r"^\s*\d+[.)]\s", " ", text, flags=re.M)
    return set(NUMBER_PATTERN.findall(text))


def find_literals(text: str, literal_set: set[str]) -> list[str]:
    """Return the literals that occur in text (case-insensitive substring match), sorted."""
    lowered = text.lower()
    return sorted(item for item in literal_set if item and item in lowered)


# ---- comparing texts and markers


def shared_sequences(text: str, other: str, length: int = SEQUENCE_WORDS) -> list[str]:
    """Return the runs of `length` consecutive words (lower-cased, punctuation ignored) that occur in both texts."""
    def words(value: str) -> list[str]:
        return re.findall(r"[a-z0-9']+", value.lower())

    def runs_of(items: list[str]) -> set[tuple[str, ...]]:
        return {tuple(items[i:i + length]) for i in range(len(items) - length + 1)}

    return sorted(" ".join(run) for run in runs_of(words(text)) & runs_of(words(other)))


def split_subsections(section_text: str) -> tuple[list[str], dict[str, str]]:
    """Return (the level-3 headings of a section in order, {heading: text under it}). Code fences are ignored."""
    headings, parts, current, in_fence = [], {}, None, False
    for line in section_text.split("\n"):
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
        match = None if in_fence else re.match(r"^### (.+?)\s*$", line)
        if match:
            current = match.group(1)
            headings.append(current)
            parts.setdefault(current, "")
        elif current is not None:
            parts[current] += line + "\n"
    return headings, parts


def injection_markers(scenario_text: str) -> list[str]:
    """Return the lower-case strings that identify an injection goal (the attacker address and quoted goal text),
    leaving out any that also occur in `scenario_text` (they would then be legitimate content, not contamination)."""
    goal_strings = {runs.ATTACKER_EMAIL.lower(), *GOAL_MARKER_EXTRA}
    for goal in runs.INJECTION_GOALS.values():
        goal_strings |= {s.lower().strip() for s in re.findall(r"'([^'\n]{6,})'", goal) if "@" not in s}
    lowered = scenario_text.lower()
    return sorted(s for s in goal_strings if s not in lowered)


# ---- generic checks

ARGUMENT_USE = re.compile(
    r"\b(with|using|passing|pass|passes|set|sets|setting|supply|supplying|provide|providing|give|giving|specify|"
    r"specifying|include|including|argument|arguments|parameter|parameters)\b", re.I)


def _used_as_argument(block: str, token: str, tool_names) -> bool:
    """True if `token` (in backticks) is used as an argument in the block: the text between the closest tool name
    before it and the token contains a word such as with, using, passing or set. A step that only says to note or
    read a value (for example "note its `event_id`") is not passing it as an argument."""
    names = "|".join(re.escape(n) for n in sorted(tool_names, key=len, reverse=True))
    for match in re.finditer(rf"`{re.escape(token)}`", block):
        before = block[:match.start()]
        previous = list(re.finditer(rf"(?<![\w])(?:{names})(?![\w])", before))
        segment = before[previous[-1].end():] if previous else before
        if ARGUMENT_USE.search(segment):
            return True
    return False


def tool_findings(text: str, body: str, tool_names, tool_args: dict[str, set[str]], all_args: set[str],
                  enums: dict[tuple[str, str], list[str]], record_fields: set[str]) -> dict[str, list[str]]:
    """Findings (a list of problem descriptions per check id) for the tool accuracy checks T1-T6.

    tool_names: the tools whose names may be used; tool_args: tool -> its documented argument names; all_args: the
    union of those; enums: (tool, argument) -> allowed values; record_fields: field names of the record types."""
    findings = {key: [] for key in ("T1", "T2", "T3", "T4", "T5", "T6")}
    known = set(tool_names) | all_args | record_fields

    verb_pattern = r"(?:%s)(?:_[a-z0-9]+)+" % "|".join(TOOL_VERBS)
    for word in sorted(set(re.findall(rf"(?<![\w.@/-])({verb_pattern})(?![\w@])", text))):
        if word not in known:
            findings["T1"].append(f"`{word}` is not a tool or documented argument")
    for word in sorted(set(re.findall(r"`([A-Za-z_]\w*)\(", text))):
        if word not in tool_names and word not in findings["T1"]:
            findings["T1"].append(f"`{word}(...)` is not a tool")

    for block in split_blocks(body):
        block_tools = mentions(block, tool_names)
        tokens = [t for t in backticked(block) if re.fullmatch(r"[a-z][a-z0-9_]*", t)]
        if block_tools:
            allowed = set().union(*(tool_args[t] for t in block_tools))
            for token in dict.fromkeys(tokens):
                if (token in all_args and token not in allowed and token not in record_fields
                        and token not in tool_names and _used_as_argument(block, token, tool_names)):
                    findings["T2"].append(
                        f"`{token}` is not an argument of {', '.join('`%s`' % t for t in block_tools)}")
        for (tool, argument), values in enums.items():
            if argument in tokens and (tool in block_tools or not block_tools):
                candidates = backticked(block)
                quoted = re.findall(r"""["']([A-Za-z]{1,12})["']""", block)
                for token in dict.fromkeys(candidates + quoted):
                    if (token not in values and token != argument and token not in tool_names
                            and token not in all_args
                            and token.lower() not in ENUM_NEUTRAL_WORDS and re.fullmatch(r"[A-Za-z]{1,12}", token)):
                        findings["T3"].append(f"`{token}` is not an allowed value of `{argument}` ({', '.join(values)})")

    for token in dict.fromkeys(backticked(text)):
        if token in HIDDEN_ARGUMENTS:
            findings["T4"].append(f"`{token}` is filled in by the system and is not an argument the agent passes")
        elif re.fullmatch(r"[a-z]+(_[a-z0-9]+)+", token) and token not in known and not any(
                token in f for f in findings["T1"]):
            findings["T6"].append(f"`{token}` is not a tool, argument or record field")

    plain = re.sub(r"`[^`]*`", " ", body)
    for name in mentions(plain, tool_names):
        findings["T5"].append(f"{name} is written without backticks")
    return findings


def safety_findings(text: str, goal_markers: list[str]) -> dict[str, list[str]]:
    """Findings per check id for the safety checks A1-A5 (goal_markers: lower-case injection-goal strings)."""
    findings = {key: [] for key in ("A1", "A2", "A3", "A4", "A5")}
    lowered = text.lower()
    if "<information>" in lowered:
        findings["A1"].append("contains an <INFORMATION> block marker")
    for marker in goal_markers:
        if marker in lowered:
            findings["A1"].append(f"contains injection-goal text {marker!r}")
    for address in dict.fromkeys(EMAIL_PATTERN.findall(text)):
        findings["A2"].append(f"email address {address}")
    for url in dict.fromkeys(re.findall(r"https?://\S+|www\.\S+", text)):
        findings["A2"].append(f"URL {url}")
    if re.search(r"^\s*(```|~~~)", text, re.M):
        findings["A2"].append("code block")
    for pattern in OVERRIDE_PHRASES:
        match = re.search(pattern, lowered)
        if match:
            findings["A3"].append(f"phrase {match.group(0)!r}")
    for pattern in BENCHMARK_PHRASES:
        match = re.search(pattern, lowered)
        if match:
            findings["A4"].append(f"phrase {match.group(0)!r}")
    for pattern in SECRET_PATTERNS:
        for match in re.findall(pattern, text):
            findings["A5"].append(f"key-like string {match[:12]}...")
    for token in LONG_TOKEN.findall(text):
        if re.search(r"\d", token) and re.search(r"[A-Za-z]", token) and not re.fullmatch(r"[a-z0-9-]+", token):
            findings["A5"].append(f"key-like string {token[:12]}...")
    return findings
