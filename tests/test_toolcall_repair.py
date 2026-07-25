"""app/agent/toolcall_repair.py — parsing a tool call the model wrote as text instead of calling.

Pure functions, no model or DB, so these run in CI. Cases are drawn from the real leak forms in the
2026-07-24 transcript (fenced ```json build_web_app blocks) plus the template variants (parameters key,
stringified arguments, braces inside strings).
"""

from app.agent import toolcall_repair as tr


def test_finds_fenced_json_tool_call_and_strips_to_prose():
    content = (
        "Sure, building that now.\n\n"
        '```json\n{"name": "build_web_app", "arguments": {"description": "a bakery page"}}\n```'
    )
    hit = tr.find_text_toolcall(content)
    assert hit is not None
    assert hit.name == "build_web_app"
    assert hit.args == {"description": "a bakery page"}
    # stripping removes the whole fence, leaving only the prose
    assert tr.strip_toolcall(content) == "Sure, building that now."


def test_finds_bare_json_object():
    content = 'Here you go {"name": "add_reminder", "arguments": {"text": "call mom", "when": "6pm"}} done'
    hit = tr.find_text_toolcall(content)
    assert hit is not None and hit.name == "add_reminder"
    assert hit.args["text"] == "call mom"
    stripped = tr.strip_toolcall(content)
    assert "name" not in stripped and "call mom" not in stripped
    assert stripped.startswith("Here you go") and stripped.endswith("done")


def test_parameters_key_variant():
    content = '{"name": "web_search", "parameters": {"query": "weather"}}'
    hit = tr.find_text_toolcall(content)
    assert hit is not None and hit.name == "web_search" and hit.args == {"query": "weather"}
    assert tr.strip_toolcall(content) == ""  # nothing but the call


def test_stringified_arguments_are_parsed():
    content = '{"name": "add_reminder", "arguments": "{\\"text\\": \\"stretch\\"}"}'
    hit = tr.find_text_toolcall(content)
    assert hit is not None and hit.args == {"text": "stretch"}


def test_braces_inside_a_string_value_do_not_break_span():
    content = '{"name": "make_document", "arguments": {"description": "use {curly} braces literally"}}'
    hit = tr.find_text_toolcall(content)
    assert hit is not None
    assert hit.args["description"] == "use {curly} braces literally"
    assert tr.strip_toolcall(content) == ""


def test_plain_prose_is_left_alone():
    content = "Your Greece trip is on August 15th, 2026."
    assert tr.find_text_toolcall(content) is None
    assert tr.strip_toolcall(content) == content


def test_json_that_is_not_a_tool_call_is_ignored():
    # A JSON-looking object with no name key is not a tool call.
    content = 'Config: {"temperature": 0.4, "top_p": 0.9} is what I use.'
    assert tr.find_text_toolcall(content) is None
    assert tr.strip_toolcall(content) == content


def test_legit_json_with_a_name_but_no_arguments_is_not_a_tool_call():
    # Guard against eating JSON the user actually asked for: {"name":...} without an arguments key is
    # data, not a call, so it must survive untouched.
    content = 'Here is the record you wanted: {"name": "Alice", "age": 30, "city": "NYC"}'
    assert tr.find_text_toolcall(content) is None
    assert tr.strip_toolcall(content) == content


def test_hint_regex_gates_cheaply():
    assert tr.TOOLCALL_HINT.search('```json') is not None
    assert tr.TOOLCALL_HINT.search('{"name": "x"}') is not None
    assert tr.TOOLCALL_HINT.search("just a normal sentence") is None
