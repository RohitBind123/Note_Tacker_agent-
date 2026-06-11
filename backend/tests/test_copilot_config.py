"""Unit tests for the Phase 2 copilot configuration (Batch 0).

Validates the env-driven settings and the trigger-CSV parser. Nothing is
hardcoded in the app; these tests pin the parsing contract.
"""
from app.config import Settings, settings


def test_default_copilot_triggers():
    assert settings.copilot_triggers == ["@centralagent"]


def test_copilot_defaults_are_sane():
    assert settings.copilot_enabled is False  # ships dark
    assert settings.copilot_bot_name == "CentralAgent"
    assert settings.vexa_ws_url.startswith("wss://")
    assert settings.gemini_embed_model == "gemini-embedding-001"
    assert settings.embed_dimensions == 768
    assert settings.copilot_context_top_k >= 1


def test_trigger_csv_is_split_normalised_and_deblanked():
    s = Settings(COPILOT_TRIGGERS="@CentralAgent, @Bot ,, @NoteTaker ")
    assert s.copilot_triggers == ["@centralagent", "@bot", "@notetaker"]


def test_single_trigger_no_commas():
    s = Settings(COPILOT_TRIGGERS="@centralagent")
    assert s.copilot_triggers == ["@centralagent"]


def test_empty_trigger_string_yields_empty_list():
    s = Settings(COPILOT_TRIGGERS="   ,  , ")
    assert s.copilot_triggers == []
