from waikiki import debuglog


def test_record_tail_clear(wiki):
    debuglog.clear()
    assert debuglog.tail() == []
    debuglog.record("agy:image", ["agy", "--print", "hello"], "out", "err", 0, 1.2)
    debuglog.record("claude:chat", ["claude", "-p", "hi"], "answer", "", 0, 0.4)
    entries = debuglog.tail()
    assert len(entries) == 2
    assert entries[0]["label"] == "claude:chat"      # newest first
    assert entries[1]["stdout"] == "out" and entries[1]["returncode"] == 0
    debuglog.clear()
    assert debuglog.tail() == []


def test_record_truncates_huge_output(wiki):
    debuglog.clear()
    debuglog.record("agy:image", ["agy"], "x" * 50000, "", 0, 1.0)
    e = debuglog.tail()[0]
    assert len(e["stdout"]) < 20000 and "truncated" in e["stdout"]
