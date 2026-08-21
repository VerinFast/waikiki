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


# --- MCP access (#30) ---------------------------------------------------------

def test_claude_gets_an_mcp_config_and_read_only_tools():
    args = chat._cli_args("claude", "claude", "", "PROMPT")
    assert "--mcp-config" in args
    assert "--strict-mcp-config" in args, "the user's own MCP setup is not ours to use"
    tools = [a for a in args if a.startswith("mcp__waikiki__")]
    assert tools, "no tools granted — the agent would be blind again"
    assert "mcp__waikiki__switch_wiki" in tools, (
        "a fresh MCP session has no active wiki, so without switch_wiki every "
        "content tool refuses and chat cannot read anything")


def test_no_write_tools_are_granted():
    """A chat session looks things up; it does not edit the wiki."""
    args = chat._cli_args("claude", "claude", "", "PROMPT")
    granted = {a.removeprefix("mcp__waikiki__") for a in args
               if a.startswith("mcp__waikiki__")}
    forbidden = {"create_page", "edit_page", "replace_page", "append_to_page",
                 "delete_page", "set_metadata", "set_property", "upload_asset",
                 "create_wiki", "generate_image", "set_setting"}
    assert not (granted & forbidden), f"write tools granted: {granted & forbidden}"


def test_gemini_is_left_alone():
    """Gemini's CLI takes a different MCP shape; don't hand it Claude's flags."""
    args = chat._cli_args("gemini", "gemini", "", "PROMPT")
    assert "--mcp-config" not in args and not any(
        a.startswith("mcp__") for a in args)


def test_the_prompt_tells_the_agent_which_wiki_to_switch_to():
    p = chat.build_prompt("Meru", "body", "", [], "q?", "SYS", wiki="beaconlight")
    assert "switch_wiki" in p and "beaconlight" in p


def test_the_prompt_no_longer_carries_pre_retrieved_excerpts(wiki, monkeypatch):
    """Retrieval was a stand-in for the agent being unable to search. It can now."""
    from waikiki import store

    store.create_page("Meru", "body")
    captured = {}
    # Pretend the CLI is installed. Without this the test asserts nothing on a
    # machine that lacks `claude`: answer() returns the "not found" error before
    # it ever builds a prompt, and the assertion below dies on an empty capture
    # rather than on the thing it means to check.
    monkeypatch.setattr(chat, "find_cli", lambda name: f"/usr/local/bin/{name}")

    def fake_run(tag, argv, timeout):
        captured["prompt"] = argv[argv.index("-p") + 1]

        class R:
            returncode, stdout, stderr = 0, "answer", ""
        return R()

    import waikiki.clirun as clirun
    real, clirun.run = clirun.run, fake_run
    try:
        chat.answer("meru", "anything", provider="claude")
    finally:
        clirun.run = real
    assert "Other relevant excerpts" not in captured["prompt"]
