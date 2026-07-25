from waikiki import ai, config, db, help_content, store, wikis


def _in_help(fn):
    token = db.current_wiki.set(config.HELP_WIKI)
    try:
        return fn()
    finally:
        db.current_wiki.reset(token)


def test_help_seed_creates_pages(wiki):
    help_content.seed()
    assert wikis.exists("help")
    assert _in_help(lambda: store.get_page("getting-started")) is not None
    about = _in_help(lambda: store.get_page("about"))
    assert about is not None and config.VERSION in about["markdown"]
    assert _in_help(lambda: store.get_page("generation-system-prompt")) is not None
    assert _in_help(lambda: store.get_page("chat-system-prompt")) is not None


def test_seed_is_idempotent(wiki):
    help_content.seed()
    help_content.seed()  # must not raise or duplicate
    pages = _in_help(lambda: [p["slug"] for p in store.list_pages()])
    assert pages.count("getting-started") == 1


def test_generation_prompt_reads_help_wiki(wiki):
    help_content.seed()
    # With the help wiki seeded, the editable copy is used.
    assert ai.generation_system_prompt() == ai.DEFAULT_GEN_SYSTEM
    # A user edit to the prompt page is honored.
    _in_help(lambda: store.update_page(
        "generation-system-prompt", "Generation System Prompt",
        "# Generation System Prompt\n\nBe terse.", author="human"))
    assert ai.generation_system_prompt() == "Be terse."
