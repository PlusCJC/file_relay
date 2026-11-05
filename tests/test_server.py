import hashlib
import time

import pytest

from file_relay.auth import login_token, session_value, valid_session
from conftest import upload


def test_upload_download_metadata_delete(client):
    meta = upload(client, content="中转测试".encode())
    assert meta["sha256"] == hashlib.sha256("中转测试".encode()).hexdigest()
    assert "direction" not in meta
    assert client.get("/api/files").json() == [meta]
    response = client.get(f"/api/download/{meta['id']}")
    assert response.content.decode() == "中转测试"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-sha256"] == meta["sha256"]
    assert client.delete(f"/api/files/{meta['id']}").status_code == 200
    assert client.get(f"/api/download/{meta['id']}").status_code == 404


def test_empty_file(client):
    meta = upload(client, content=b"")
    assert client.get(f"/api/download/{meta['id']}").content == b""


@pytest.mark.parametrize("name", ["../bad", "a\\b", "CON.txt", "bad\nname", "a:", ".", "a."])
def test_unsafe_upload_names(client, name):
    assert client.post("/api/files", params={"name": name}, content=b"x").status_code == 400
    assert client.get("/api/files").json() == []


def test_authentication_and_session(client, config):
    client.headers.pop("Authorization")
    assert client.get("/").status_code == 200
    assert client.get("/api/info").status_code == 401
    assert client.post("/api/login", json={"token": "错误"}).status_code == 401
    ticket = login_token(config.token)
    response = client.post("/api/login", json={"token": ticket})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert config.token not in response.headers["set-cookie"]
    assert ticket not in response.headers["set-cookie"]
    assert "Max-Age=7200" in response.headers["set-cookie"]
    assert client.get("/api/info").status_code == 200
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/info").status_code == 401


def test_expired_login_and_internal_key_cannot_log_in(client, config):
    expired = login_token(config.token, expiry=int(time.time()) - 1)
    for token in (expired, config.token, session_value(config.token)):
        response = client.post("/api/login", json={"token": token})
        assert response.status_code == 401
        assert "过期" in response.json()["error"]


def test_cookie_restores_login_after_reload_and_token_expiry(client, config, monkeypatch):
    client.headers.pop("Authorization")
    started = time.time()
    ticket = login_token(config.token)
    assert client.post("/api/login", json={"token": ticket}).status_code == 200
    cookie = client.cookies.get("file_relay_session")
    # The original login ticket has expired, but the independently signed cookie
    # still authenticates a fresh page and API request without a new token.
    monkeypatch.setattr("file_relay.auth.time.time", lambda: started + 11 * 60)
    client.cookies.clear()
    headers = {"Cookie": f"file_relay_session={cookie}"}
    assert client.get("/").status_code == 200
    assert client.get("/api/info", headers=headers).status_code == 200
    assert client.post("/api/login", json={"token": ticket}).status_code == 401
    monkeypatch.setattr("file_relay.auth.time.time", lambda: started + 121 * 60)
    assert client.get("/api/info", headers=headers).status_code == 401


def test_legacy_direction_parameters_do_not_split_shared_list(client):
    first = upload(client, name="one", direction="to-host")
    second = upload(client, name="two", direction="to-wsl")
    expected = {first["id"], second["id"]}
    for query in ("", "?direction=to-host", "?direction=to-wsl"):
        assert {item["id"] for item in client.get("/api/files" + query).json()} == expected


def test_minimal_page_has_no_direction_controls(client):
    html = client.get("/").text
    for removed in ('id="direction"', 'class="filters"', 'class="cli-note"', "主机 →", "发往", "HOST ↔"):
        assert removed not in html
    assert 'id="login-panel" class="login-card" hidden' in html
    assert 'id="drop-zone"' in html


def test_login_rate_limit(client):
    for _ in range(10):
        assert client.post("/api/login", json={"token": "bad"}).status_code == 401
    assert client.post("/api/login", json={"token": "bad"}).status_code == 429


def test_expired_or_forged_session(config):
    assert valid_session(config.token, session_value(config.token))
    assert not valid_session(config.token, session_value(config.token, int(time.time()) - 1))
    assert not valid_session(config.token, session_value(config.token) + "tampered")
    assert not valid_session(config.token, "garbage")


@pytest.mark.parametrize("headers", [
    {"Host": "attacker.test:8765"}, {"Origin": "https://attacker.test"},
    {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"},
])
def test_rebinding_and_cross_origin_rejected(client, headers):
    response = client.get("/api/info", headers=headers)
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_csrf_header_required(client):
    client.headers.pop("X-File-Relay")
    assert client.post("/api/cleanup").status_code == 403
    assert client.post("/api/login", json={"token": "x"}).status_code == 403


def test_non_loopback_peer_rejected(config, make_client):
    with make_client(config, peer="192.168.1.20") as client:
        assert client.get("/").status_code == 403


def test_proxy_headers_do_not_grant_remote_access(client):
    assert client.get("/api/info", headers={"X-Forwarded-Host": "evil", "X-Forwarded-For": "8.8.8.8"}).status_code == 200


def test_security_headers_and_assets(client):
    for path in ("/", "/app.js", "/style.css", "/favicon.svg"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/config.json").status_code == 404
    assert client.get("/api/files/not-a-valid-id").status_code == 404


def test_cleanup_defaults_to_preview(client):
    meta = upload(client)
    store = client.app.state.store
    with store.db() as db:
        db.execute("UPDATE files SET created=0 WHERE id=?", (meta["id"],))
    assert client.post("/api/cleanup").json()["count"] == 1
    assert len(client.get("/api/files").json()) == 1
    assert client.post("/api/cleanup?dry_run=0").json()["count"] == 1
    assert client.get("/api/files").json() == []


def test_stream_without_content_length_rejected(client):
    response = client.post("/api/files?name=bad", content=iter([b"one", b"two"]))
    assert response.status_code == 411


def test_bounded_login_json(client):
    assert client.post("/api/login", content=b"x" * 3000).status_code == 413
    assert client.post("/api/login", content=b"not-json").status_code == 400
    assert client.post("/api/login", json=[]).status_code == 400
