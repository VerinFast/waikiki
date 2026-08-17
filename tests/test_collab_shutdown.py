"""A pending CRDT edit must survive shutdown.

The updater quits the app on purpose (SIGTERM) so the lifespan shutdown runs,
and docs/updates.md says that is what flushes collab.py's pending snapshots.
But the flusher only persists text that has been *stable* for _FLUSH_IDLE, on a
1s loop — so an edit made just before the quit has never been written, and
cancelling the task simply stops the loop. See issue #19.
"""
import anyio

from waikiki import collab, store


def _room_with_unsaved_text(slug: str, text: str) -> str:
    """Put `text` in a room and mark it as never-persisted, as mid-edit state."""
    key = f"main::{slug}"
    collab._seeded.add(key)
    collab._last_text[key] = text
    collab._last_saved[key] = "stale"      # differs -> a flush is owed
    return key


def test_pending_edit_is_flushed_on_shutdown(wiki):
    """The edit is owed a flush when the app quits; it must not be dropped."""
    page = store.create_page("Meru", "original body")
    slug = page["slug"]

    async def scenario():
        async with collab.server:
            room = await collab.server.get_room(f"main::{slug}")
            collab._ytext(room)[:] = "edited but not yet flushed"
            _room_with_unsaved_text(slug, "edited but not yet flushed")
            # What the lifespan does on the way out.
            await collab.flush_all()

    anyio.run(scenario)

    assert store.get_page(slug)["markdown"] == "edited but not yet flushed", (
        "a pending edit was lost when the app shut down")


def test_flush_all_is_safe_with_nothing_pending(wiki):
    """Shutdown must not fail or rewrite pages when there is nothing owed."""
    page = store.create_page("Quiet", "untouched")
    key = f"main::{page['slug']}"
    collab._seeded.add(key)
    collab._last_saved[key] = "untouched"
    collab._last_text[key] = "untouched"
    anyio.run(collab.flush_all)
    assert store.get_page(page["slug"])["markdown"] == "untouched"
