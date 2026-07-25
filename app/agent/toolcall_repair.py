"""Recover from a local-model failure mode: emitting a tool call as *text* in the reply instead of a
real `tool_calls` entry.

On a small local model (qwen2.5:7b), when the model wants a tool it can't call — because that tool
isn't in this turn's bound subset, or it just mis-formats — it writes the call into `content`, e.g.

    ```json
    {"name": "build_web_app", "arguments": {"description": "..."}}
    ```

`tools_condition` only sees `AIMessage.tool_calls`, so this leaks straight to the user as raw JSON
(the "looks broken" symptom from the 2026-07-24 transcript). The standard fix for local models is to
parse the call out of `content` (the ToolCallingLLM pattern) rather than to constrain decoding —
constrained decoding fixes JSON *syntax*, not *which* tool the model reaches for.

This module is pure (no model, no I/O) so it's fully unit-testable. The graph decides what to DO with a
found call (execute if the tool was bound this turn, strip if not — see app/agent/graph.py); the stream
uses the detector to keep a nascent block from flashing on screen.
"""

import json
import re
from dataclasses import dataclass

# Cheap "this text might contain a tool call" probe for the streaming path (partial buffers). Matches
# the start of a ```json fence or a JSON object whose first key is name/tool/function.
TOOLCALL_HINT = re.compile(r'```\s*json|\{\s*"(?:name|tool|tool_name|function)"\s*:', re.I)

# Keys different chat templates use for the tool name and its arguments.
_NAME_KEYS = ("name", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "args", "parameters", "params", "input")


@dataclass
class ToolCallHit:
    name: str
    args: dict
    start: int  # span of the whole block to strip (includes an enclosing ```json fence if present)
    end: int


def _iter_json_spans(text: str):
    """Yield (start, end) spans of balanced {...} regions, respecting string literals so a brace
    inside a quoted value doesn't throw off the depth count."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield i, j + 1
                    break
            j += 1
        i = j + 1


def _as_toolcall(obj) -> tuple[str, dict] | None:
    """If a parsed object looks like a tool call, return (name, args); else None.

    Requires BOTH a name key AND an arguments key to be present — the specific `{"name":…,
    "arguments":…}` shape a chat template emits. This is deliberately strict so legitimate JSON the
    user actually asked for (e.g. `{"name": "Alice", "age": 30}`) is NOT mistaken for a tool call and
    stripped from her reply. The graph strips the tool call only if `name` is a real tool anyway."""
    if not isinstance(obj, dict):
        return None
    name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), None)
    if not name:
        return None
    arg_key = next((k for k in _ARG_KEYS if k in obj), None)
    if arg_key is None:  # a name with no arguments key is prose/data, not a tool call
        return None
    raw_args = obj[arg_key]
    if isinstance(raw_args, str):  # some templates JSON-encode the arguments as a string
        try:
            raw_args = json.loads(raw_args)
        except (ValueError, TypeError):
            raw_args = {}
    if not isinstance(raw_args, dict):
        raw_args = {}
    return name, raw_args


def find_text_toolcall(content: str) -> ToolCallHit | None:
    """Return the first text tool-call in `content`, or None. The span covers an enclosing ```json
    fence when there is one, so strip_toolcall removes the whole block, not just the braces."""
    if not content or not TOOLCALL_HINT.search(content):
        return None
    for start, end in _iter_json_spans(content):
        candidate = content[start:end]
        try:
            obj = json.loads(candidate)
        except ValueError:
            continue
        hit = _as_toolcall(obj)
        if hit is None:
            continue
        name, args = hit
        # Widen the span to swallow an enclosing ```json ... ``` fence, so nothing orphaned is left.
        fs, fe = _enclosing_fence(content, start, end)
        return ToolCallHit(name=name, args=args, start=fs, end=fe)
    return None


def _enclosing_fence(content: str, start: int, end: int) -> tuple[int, int]:
    """If the JSON at [start,end) sits inside a ```...``` fence, return the fence's span; else the
    JSON's own span."""
    open_fence = content.rfind("```", 0, start)
    if open_fence != -1 and not content[open_fence:start].strip("` \njson").strip():
        close_fence = content.find("```", end)
        if close_fence != -1:
            return open_fence, close_fence + 3
    return start, end


def strip_toolcall(content: str) -> str:
    """Remove a text tool-call block from `content`, leaving the surrounding prose (trimmed). Returns
    '' if the content was nothing but the tool call."""
    hit = find_text_toolcall(content)
    if hit is None:
        return content.strip()
    return (content[: hit.start] + content[hit.end :]).strip()
