"""The article's own line about its past (issue #73).

Version history is this app's undo. It worked before this — four interactions,
a real diff, a restore that is itself versioned — and *nothing on an article
said it was there*. The audit (`docs/data-safety.md`, question 5) found the only
occurrence of the word "History" on a rendered article was the `aria-label` on
the browser back/forward buttons, which is worse than silence: it points the
person hunting for undo at the wrong control.

A safety net nobody can reach is, for the person who needs it, the same as no
safety net. So these tests are about *findability*, and they assert on what the
server actually serves — a template containing a string proves nothing about
whether the page a user gets carries it.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from waikiki import store
from waikiki.api import app


def _client() -> TestClient:
    c = TestClient(app, client=("127.0.0.1", 12345))
    c.cookies.set("waikiki_wiki", "main")
    return c


def _text(html: str) -> str:
    """The rendered page as running text: tags out, whitespace collapsed.

    Asserting on raw HTML would pass on markup a browser renders as three words
    jammed together, and fail on a harmless reflow of the template.
    """
    body = re.sub(r"(?s)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()


def _history_links(html: str) -> list[str]:
    return re.findall(r'href="([^"]*details\?show=history[^"]*)"', html)


def test_an_edited_article_says_when_it_changed_and_where_the_old_text_is(wiki):
    """The bar: someone who pasted over a paragraph, and doesn't know "version".

    They should not have to guess that a tab called *Details* holds their undo.
    """
    store.create_page("Packing List", "yesterday's careful list")
    store.update_page("packing-list", "Packing List", "oops, I pasted over everything")

    with _client() as client:
        article = client.get("/wiki/packing-list")
        assert article.status_code == 200
        words = _text(article.text)

        # It says the page changed, and roughly when — news, not a filing label.
        assert "Edited just now" in words, words[:400]
        # ...and what that buys you, in words nobody has to already know.
        assert "1 earlier version you can go back to" in words, words[:400]

        links = _history_links(article.text)
        assert links, "the article names a history but offers no way into it"


def test_the_line_lands_on_an_open_history_with_the_old_text_one_click_away(wiki):
    """A link that resolves, to a block that is already showing.

    A fragment never reaches the server, so a bare `#history` would land the
    reader on a collapsed `<details>` — a dead end for exactly the person who
    followed a promise of earlier versions.
    """
    store.create_page("Packing List", "yesterday's careful list")
    store.update_page("packing-list", "Packing List", "oops, I pasted over everything")

    with _client() as client:
        href = _history_links(client.get("/wiki/packing-list").text)[0]

        details = client.get(href)
        assert details.status_code == 200, f"{href} does not resolve"

        block = re.search(r'<details[^>]*id="history"[^>]*>', details.text)
        assert block, "the link's destination has no history block"
        assert " open" in block.group(0), \
            "arriving through the article's link leaves the history collapsed"

        old = store.page_versions("packing-list")[-1]
        version_href = f'/wiki/packing-list/history/{old["id"]}'
        assert version_href in details.text

        view = client.get(version_href)
        assert view.status_code == 200
        assert "yesterday's careful list" in view.text


def test_a_page_nobody_has_edited_offers_no_door_to_an_empty_room(wiki):
    """One revision means there is nothing earlier, so say that and stop.

    Same convention as the MCP `hint`: only when it is true. A link that always
    fires is one people learn to skip, and this one would land them on a list
    holding a single row marked "(current)".
    """
    store.create_page("Fresh Page", "written once and left alone")

    with _client() as client:
        article = client.get("/wiki/fresh-page")
        assert article.status_code == 200
        words = _text(article.text)

        assert "no earlier versions yet" in words, words[:400]
        assert "you can go back to" not in words
        assert not _history_links(article.text), \
            "a page with nothing earlier still points at the history"


@pytest.mark.parametrize("saves, expected", [
    (1, "1 earlier version you can go back to"),
    (2, "2 earlier versions you can go back to"),
    (4, "4 earlier versions you can go back to"),
])
def test_the_count_is_of_texts_you_can_actually_go_back_to(wiki, saves, expected):
    """Not the number of rows in the block — the newest of those is the page.

    Off by one here would promise a version that turns out to be what the reader
    is already looking at, which is precisely the disappointment this line exists
    to avoid.
    """
    store.create_page("Ledger", "draft 0")
    for n in range(saves):
        store.update_page("ledger", "Ledger", f"draft {n + 1}")

    with _client() as client:
        words = _text(client.get("/wiki/ledger").text)
    assert expected in words, words[:400]


def test_the_word_history_no_longer_only_names_the_back_button(wiki):
    """The audit's actual finding, pinned.

    "History" appeared once on a rendered article: as the accessible name of the
    browser back/forward group. Someone searching the page for their undo found
    it and was sent to the wrong control.
    """
    store.create_page("Packing List", "yesterday's careful list")
    store.update_page("packing-list", "Packing List", "oops")

    with _client() as client:
        html = client.get("/wiki/packing-list").text

    assert 'aria-label="History"' not in html, \
        "the browser's back/forward buttons still claim the bare name 'History'"
    assert 'aria-label="Browser history"' in html
    # And the page's own history is named, on the article, pointing at itself.
    assert re.search(r'title="Page history[^"]*"', html), \
        "nothing on the article names page history"


def test_the_affordance_is_not_shown_on_a_trashed_page(wiki):
    """A page in the Trash has one job on screen: Restore or Delete forever.

    Its version history is not the question being asked, and the tab row it sits
    under is hidden there too.
    """
    store.create_page("Doomed", "text")
    store.update_page("doomed", "Doomed", "more text")
    store.delete_page("doomed")

    with _client() as client:
        words = _text(client.get("/wiki/doomed").text)
    assert "you can go back to" not in words
    assert "no earlier versions yet" not in words
