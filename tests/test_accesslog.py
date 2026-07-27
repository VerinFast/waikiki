import datetime
import re

from waikiki import accesslog, appconfig


def test_http_line_is_clf_shaped(wiki):
    accesslog.clear()
    accesslog.http("127.0.0.1", "GET", "/wiki/reef", "HTTP/1.1", 200, 512, 0.012,
                   agent="curl", referer="-")
    e = accesslog.read()[0]
    assert e["kind"] == "HTTP" and e["status"] == "200" and e["ms"] == 12
    assert e["request"] == "GET /wiki/reef HTTP/1.1"
    # core CLF prefix: host - - [time] "request" status size
    assert re.match(r'^127\.0\.0\.1 - - \[.+\] "GET /wiki/reef HTTP/1\.1" 200 512',
                    e["raw"])


def test_mcp_cli_error_kinds_and_filters(wiki):
    accesslog.clear()
    accesslog.mcp("get_page", True, 5.2, "slug=meru")
    accesslog.cli("agy:image", 0, 18.5)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        accesslog.error("POST /wiki/x/generate-image", exc)

    assert {e["kind"] for e in accesslog.read()} == {"MCP", "CLI", "ERROR"}
    assert accesslog.read(kind="MCP")[0]["ms"] == 5200
    err = accesslog.read(kind="ERROR")[0]
    assert "ValueError: boom" in err["detail"]
    # substring query
    assert len(accesslog.read(query="get_page")) == 1


def test_retention_sweep_removes_old_hour_files(wiki, monkeypatch):
    appconfig.set("log_retention_hours", 24)
    d = accesslog._logs_dir()
    # a fresh file (kept) and a 48h-old file (swept)
    now = datetime.datetime.now().astimezone()
    old = now - datetime.timedelta(hours=48)
    (d / f"access-{now:%Y-%m-%d-%H}.log").write_text('x - - [t] "GET / HTTP/1.1" 200 1 "-" "-" 1ms\n')
    (d / f"access-{old:%Y-%m-%d-%H}.log").write_text("old\n")
    removed = accesslog.sweep()
    assert removed == 1
    assert (d / f"access-{now:%Y-%m-%d-%H}.log").exists()
    assert not (d / f"access-{old:%Y-%m-%d-%H}.log").exists()


def test_retention_hours_setting(wiki):
    appconfig.set("log_retention_hours", 6)
    assert accesslog.retention_hours() == 6


def test_middleware_logs_requests_and_captures_errors(wiki):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from waikiki.api import AccessLogMiddleware
    accesslog.clear()
    app = FastAPI()

    @app.get("/ok")
    def ok():
        return {"ok": True}

    @app.get("/boom")
    def boom():
        raise ValueError("kaboom")

    app.add_middleware(AccessLogMiddleware)
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/ok").status_code == 200
    assert client.get("/boom").status_code == 500

    reqs = [e["request"] for e in accesslog.read()]
    assert "GET /ok HTTP/1.1" in reqs
    assert "GET /boom HTTP/1.1" in reqs           # 500 request line logged
    errs = accesslog.read(kind="ERROR")
    assert any("kaboom" in e["detail"] and e["kind"] == "ERROR" for e in errs)
