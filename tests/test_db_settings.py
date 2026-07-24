from waikiki import db


def test_settings_get_set(wiki):
    assert db.get_setting("missing") is None
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("k", "v")
    assert db.get_setting("k") == "v"
    db.set_setting("k", "v2")  # upsert
    assert db.get_setting("k") == "v2"


def test_default_settings_seeded(wiki):
    alls = db.all_settings()
    assert alls["theme"] == "default"
    assert alls["embedder_provider"] == "fastembed"


def test_ensure_vec_table_records_dim(wiki):
    if not db.VEC_AVAILABLE:
        return  # vector extension not present in this env; nothing to assert
    db.ensure_vec_table(8)
    assert db.get_setting("vec_dim") == "8"
