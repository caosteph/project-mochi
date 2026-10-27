"""verify_firing --floor: the pass/fail rule scripts/verify_smoke.sh gates on.

The smoke tier calls it with N=1 and a 33% floor so it fails only on a COLLAPSE (a tool firing on
none of its prompts), never on ordinary 7B variance. The rule is pure, so it's pinned here.
"""

from scripts.verify_firing import below_floor


def test_one_hit_out_of_three_clears_a_33_floor():
    results = {"add_reminder": {"a": 1, "b": 0, "c": 0}}
    assert below_floor(results, n=1, floor_pct=33) == []


def test_a_collapsed_tool_fails_and_is_named():
    results = {"add_reminder": {"a": 1, "b": 1, "c": 1}, "create_draft": {"a": 0, "b": 0, "c": 0}}
    assert below_floor(results, n=1, floor_pct=33) == ["create_draft 0/3"]


def test_rate_counts_every_sample():
    results = {"web_search": {"a": 1, "b": 1}}  # 2 of 2 prompts × 4 samples = 8
    assert below_floor(results, n=4, floor_pct=50) == ["web_search 2/8"]
    assert below_floor(results, n=1, floor_pct=50) == []


def test_a_tool_with_no_prompts_is_not_a_failure():
    assert below_floor({"ask_user": {}}, n=1, floor_pct=33) == []
