import waikiki
from waikiki import chat, config


def test_version_single_source():
    assert config.VERSION == waikiki.__version__


def test_build_prompt_includes_everything():
    p = chat.build_prompt(
        title="Meru",
        article_md="# Meru\nA mountain.",
        excerpts="[from 'Bram']\nrelated text",
        history=[{"role": "user", "content": "hi"},
                 {"role": "assistant", "content": "hello"}],
        question="How tall is it?",
        system="SYS-PROMPT")
    assert "SYS-PROMPT" in p
    assert "# Article: Meru" in p and "A mountain." in p
    assert "related text" in p
    assert "User: hi" in p and "Assistant: hello" in p
    assert "How tall is it?" in p


def test_build_prompt_omits_empty_sections():
    p = chat.build_prompt("T", "body", "", [], "q?", "S")
    assert "Other relevant excerpts" not in p
    assert "Conversation so far" not in p
    assert "q?" in p


def test_cli_args_claude_and_gemini():
    claude = chat._cli_args("claude", "/bin/claude", "claude-sonnet-4-5", "PROMPT")
    assert claude[0] == "/bin/claude" and "-p" in claude
    assert "--model" in claude and "claude-sonnet-4-5" in claude

    gemini = chat._cli_args("gemini", "/bin/gemini", "gemini-2.5-pro", "PROMPT")
    assert "-m" in gemini and "gemini-2.5-pro" in gemini

    # No model → no model flag
    bare = chat._cli_args("claude", "/bin/claude", "", "PROMPT")
    assert "--model" not in bare


def test_find_cli_missing_returns_none():
    assert chat.find_cli("definitely-not-a-real-cli-xyz") is None


def test_answer_reports_missing_cli(wiki):
    import pytest

    from waikiki import store
    if chat.find_cli("gemini"):
        pytest.skip("gemini CLI is installed here")
    store.create_page("Reef", "# Reef\nbody")
    out = chat.answer("reef", "what is this?", provider="gemini")
    # gemini isn't installed; should fail gracefully with guidance.
    assert out["ok"] is False
    assert "gemini" in out["error"].lower()
