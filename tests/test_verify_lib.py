"""Offline coverage for `scripts/_verify_lib.sample_check`.

The verify scripts themselves can't be unit-tested (they need a real model), but the *decision
rule* they now gate on can be — and it's the one piece where a bug is invisible in the output:
a `sample_check` that quietly retried a must-not violation would turn the safety-shaped checks
green forever. Fake probes with scripted outcomes pin the tally and both early exits, with no
model calls.

Context: `verify_phase1` failed the gate on `add_goal wrote a row | 0 -> 0` when the same prompt
fired 3/3 on re-probe — single-sample variance, not a regression. `sample_check` is the fix.
"""

import pytest

from scripts._verify_lib import results, sample_check


@pytest.fixture(autouse=True)
def _clean_results():
    """`results` is module-level (shared by every check in a running script)."""
    results.clear()
    yield
    results.clear()


def scripted(outcomes):
    """A probe returning `outcomes` in order, plus a call counter — so the tests can assert
    that the early exits actually save model calls rather than just reaching the right verdict."""
    calls = {"n": 0}

    def probe():
        ok = outcomes[calls["n"]]
        calls["n"] += 1
        return ok, f"call{calls['n']}"

    return probe, calls


@pytest.mark.parametrize(
    "label,outcomes,samples,need,expect_pass,expect_calls",
    [
        # Capability semantics (need=1): retry, but never spend calls once decided.
        ("passes first try — the fast path", [True, True, True], 3, 1, True, 1),
        ("wobbles, passes on the 2nd", [False, True, True], 3, 1, True, 2),
        ("scrapes by on the 3rd", [False, False, True], 3, 1, True, 3),
        ("genuinely broken — all fail", [False, False, False], 3, 1, False, 3),
        # Must-not semantics (need=samples): every sample has to be clean.
        ("must-not, all clean", [True, True], 2, 2, True, 2),
        ("must-not, first sample violates", [False, True], 2, 2, False, 1),
        ("must-not, second sample violates", [True, False], 2, 2, False, 2),
        # Majority: stop as soon as `need` is unreachable.
        ("majority 2-of-3 becomes unreachable", [False, False, True], 3, 2, False, 2),
    ],
)
def test_sample_check_verdict_and_call_count(
    label, outcomes, samples, need, expect_pass, expect_calls
):
    probe, calls = scripted(outcomes)
    passed = sample_check(label, probe, samples=samples, need=need)
    assert passed is expect_pass
    assert calls["n"] == expect_calls, "early exit should stop as soon as the outcome is decided"


def test_a_retry_cannot_launder_a_must_not_violation():
    """The reason must-not checks set need=samples: a single violation fails outright, even
    though later samples are clean. Retrying past it is exactly the wrong behavior."""
    probe, _ = scripted([False, True, True])
    assert sample_check("must never happen", probe, samples=3, need=3) is False


def test_tally_is_always_reported_so_a_scrape_by_is_visible():
    """Retries reduce false alarms but could mask a slow decline — the printed hits/attempts is
    the mitigation, so a check limping in at 1/3 doesn't read as a clean green."""
    probe, _ = scripted([False, False, True])
    sample_check("limping", probe, samples=3, need=1)
    _name, ok, detail = results[-1]
    assert ok is True
    assert "1/3" in detail and "need 1" in detail


def test_healthy_check_reports_one_of_one():
    probe, _ = scripted([True])
    sample_check("healthy", probe, samples=3, need=1)
    assert "1/1" in results[-1][2]
