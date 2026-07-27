"""Phase 4A — the deterministic sensitivity router. Asserts the routing table + the
core invariant (sensitive data always stays local) without any network: we inspect the
endpoint of the ChatOpenAI the router builds, we never call it.
"""

from langchain_core.runnables import RunnableBinding

from app.agent import quarantine, router
from app.agent.router import Sensitivity
from app.config import settings

LOCAL = None  # filled per-test from settings


def _is_local(model) -> bool:
    return (
        str(model.openai_api_base) == settings.ollama_base_url
        and model.model_name == settings.local_model
    )


def _enable_hosted(monkeypatch):
    monkeypatch.setattr(settings, "hosted_enabled", True)
    monkeypatch.setattr(settings, "local_only", False)
    monkeypatch.setattr(settings, "hosted_base_url", "https://api.example.com/v1")
    monkeypatch.setattr(settings, "hosted_model", "big-open-model")
    monkeypatch.setattr(settings, "hosted_api_key", "sk-test")


def test_local_only_forces_local_for_both(monkeypatch):
    monkeypatch.setattr(settings, "local_only", True)
    _enable_hosted(monkeypatch)  # even fully configured...
    monkeypatch.setattr(settings, "local_only", True)  # ...LOCAL_ONLY wins
    assert _is_local(router.chat_model(Sensitivity.SENSITIVE))
    assert _is_local(router.chat_model(Sensitivity.NON_SENSITIVE))
    assert router.hosted_available() is False


def test_non_sensitive_routes_hosted_when_enabled(monkeypatch):
    _enable_hosted(monkeypatch)
    assert router.hosted_available() is True
    hosted = router.chat_model(Sensitivity.NON_SENSITIVE)
    assert "api.example.com" in str(hosted.openai_api_base)
    assert hosted.model_name == "big-open-model"
    # ...but SENSITIVE still local, even with hosting fully on.
    assert _is_local(router.chat_model(Sensitivity.SENSITIVE))


def test_hosted_disabled_stays_local(monkeypatch):
    _enable_hosted(monkeypatch)
    monkeypatch.setattr(settings, "hosted_enabled", False)  # configured but not opted in
    assert router.hosted_available() is False
    assert _is_local(router.chat_model(Sensitivity.NON_SENSITIVE))


def test_misconfigured_fails_closed(monkeypatch):
    _enable_hosted(monkeypatch)
    monkeypatch.setattr(settings, "hosted_model", None)  # a missing piece
    assert router.hosted_available() is False
    assert _is_local(router.chat_model(Sensitivity.NON_SENSITIVE))  # fails closed → local


def test_core_invariant_sensitive_and_reader_always_local(monkeypatch):
    _enable_hosted(monkeypatch)  # worst case: hosting fully enabled
    assert _is_local(router.chat_model(Sensitivity.SENSITIVE, temperature=0.4))
    # The quarantined reader is hard-wired local (email is sensitive) regardless of routing.
    assert str(quarantine.reader_llm.openai_api_base) == settings.ollama_base_url


def test_tools_are_bound_only_when_passed(monkeypatch):
    from app.agent.tools import ALL_TOOLS

    bound = router.chat_model(Sensitivity.SENSITIVE, tools=ALL_TOOLS)
    plain = router.chat_model(Sensitivity.SENSITIVE)
    assert isinstance(bound, RunnableBinding)
    assert not isinstance(plain, RunnableBinding)


# --- Model behavior profile (docs/14 #29 prep: per-model knobs live in config) ------------------


def test_profile_defaults_preserve_7b_behavior():
    # These defaults are what keep the change a no-op on the current 7B.
    assert settings.model_temperature == 0.4
    assert settings.model_max_output_tokens is None


def test_chat_model_inherits_profile_temperature_at_call_time(monkeypatch):
    # No explicit temp → the profile default, read when the model is built (so a config change
    # takes effect without editing code). This is what graph._base_llm relies on.
    assert router.chat_model(Sensitivity.SENSITIVE).temperature == settings.model_temperature
    monkeypatch.setattr(settings, "model_temperature", 0.15)
    assert router.chat_model(Sensitivity.SENSITIVE).temperature == 0.15


def test_explicit_temperature_overrides_profile(monkeypatch):
    monkeypatch.setattr(settings, "model_temperature", 0.15)
    assert router.chat_model(Sensitivity.SENSITIVE, temperature=0.9).temperature == 0.9


def test_max_tokens_none_is_omitted_not_null():
    # None must mean "no cap" — ChatOpenAI drops the field rather than sending "max_tokens": null,
    # which some OpenAI-compatible servers reject.
    m = router.chat_model(Sensitivity.SENSITIVE)
    assert m.max_tokens is None
    assert "max_tokens" not in m._default_params  # not in the request payload when unset


def test_max_tokens_flows_from_profile_and_explicit(monkeypatch):
    monkeypatch.setattr(settings, "model_max_output_tokens", 2048)
    assert router.chat_model(Sensitivity.SENSITIVE).max_tokens == 2048  # profile default
    assert router.chat_model(Sensitivity.SENSITIVE, max_tokens=8000).max_tokens == 8000  # explicit wins
