from waikiki import db, mcp_server


def test_get_and_set_setting_allowlist(wiki, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE", "main")

    # allowed key writes through
    out = mcp_server.set_setting("image_style_prompt", "clockwork punk pixel art")
    assert out.get("value") == "clockwork punk pixel art"
    assert db.get_setting("image_style_prompt", "") == "clockwork punk pixel art"

    # get_settings echoes it back
    got = mcp_server.get_settings()
    assert got["wiki"] == "main"
    assert got["settings"]["image_style_prompt"] == "clockwork punk pixel art"

    # disallowed key is rejected and NOT written
    bad = mcp_server.set_setting("allow_html", "1")
    assert "error" in bad and db.get_setting("allow_html", "0") == "0"

    # enum validation
    assert "error" in mcp_server.set_setting("gen_provider", "bogus")
    assert mcp_server.set_setting("gen_provider", "ollama").get("value") == "ollama"

    # int coercion / clamping
    assert mcp_server.set_setting("retention_versions", "-3").get("value") == "0"
