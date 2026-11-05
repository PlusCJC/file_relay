import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl

import pytest

from file_relay.auth import login_token
from file_relay.client import Client
from file_relay.config import Config
from file_relay.storage import Store
from file_relay.tls import configure_local, configure_vm, fingerprint, run_openssl, verify_certificate


VM_IP = "192.168.56.10"
HOST_IP = "192.168.56.1"


@pytest.fixture(scope="session")
def vm_certificate(tmp_path_factory):
    config = Config(tmp_path_factory.mktemp("vm-certificate") / "home")
    return configure_vm(config, VM_IP, [HOST_IP])


@pytest.fixture
def vm_config(config, vm_certificate):
    values = config.values | {"listen_host": VM_IP, "allowed_clients": [HOST_IP],
                              "tls_certfile": str(vm_certificate.cert_path),
                              "tls_keyfile": str(vm_certificate.key_path)}
    (config.home / "config.json").write_text(json.dumps(values))
    return Config(config.home)


@pytest.mark.parametrize("updates", [
    {"listen_host": "0.0.0.0"}, {"listen_host": "8.8.8.8"}, {"listen_host": "::"},
    {"listen_host": "vm.example.com"}, {"listen_host": VM_IP, "allowed_clients": []},
    {"listen_host": VM_IP, "allowed_clients": ["0.0.0.0/0"]},
    {"listen_host": VM_IP, "allowed_clients": ["192.168.56.0/24"]},
    {"listen_host": VM_IP, "allowed_clients": ["8.8.8.8"]},
    {"listen_host": VM_IP, "allowed_clients": HOST_IP},
    {"listen_host": VM_IP, "allowed_clients": [HOST_IP]},  # Missing TLS.
])
def test_reject_unsafe_network_configuration(config, updates):
    (config.home / "config.json").write_text(json.dumps(config.values | updates))
    with pytest.raises(ValueError):
        Config(config.home)


def test_configure_existing_certificate_and_restore_local(config, vm_certificate):
    internal_key = config.token
    configured = configure_vm(config, VM_IP, [HOST_IP], vm_certificate.cert_path, vm_certificate.key_path)
    assert configured.url == f"https://{VM_IP}:8765"
    assert configured.allows_peer(HOST_IP)
    assert configured.allows_peer(VM_IP)
    assert not configured.allows_peer("192.168.56.2")
    assert not configured.allows_peer("127.0.0.1")
    assert configured.token == internal_key
    assert len(fingerprint(configured.cert_path).split(":")) == 32
    restored = configure_local(configured)
    assert restored.url == "http://127.0.0.1:8765"
    assert restored.token == internal_key


def test_configuration_refuses_running_store(config, vm_certificate):
    before = (config.home / "config.json").read_bytes()
    store = Store(config)
    try:
        with pytest.raises(ValueError, match="先停止"):
            configure_vm(config, VM_IP, [HOST_IP], vm_certificate.cert_path, vm_certificate.key_path)
    finally:
        store.close()
    assert (config.home / "config.json").read_bytes() == before


def test_certificate_mismatch_does_not_replace_config(config, vm_certificate):
    before = (config.home / "config.json").read_bytes()
    with pytest.raises(ValueError, match="不匹配"):
        configure_vm(config, "192.168.56.11", [HOST_IP], vm_certificate.cert_path, vm_certificate.key_path)
    assert (config.home / "config.json").read_bytes() == before


def test_generated_certificate_and_private_key(vm_certificate):
    verify_certificate(vm_certificate)
    assert os.stat(vm_certificate.key_path).st_mode & 0o077 == 0
    output = run_openssl("x509", "-in", vm_certificate.cert_path, "-noout", "-text")
    assert f"IP Address:{VM_IP}" in output
    assert "CA:FALSE" in output


def test_allowlisted_browser_upload_download_session(vm_config, make_client):
    with make_client(vm_config, peer=HOST_IP) as client:
        client.headers["X-File-Relay"] = "1"
        assert client.get("/").status_code == 200
        assert client.get("/api/info").status_code == 401
        response = client.post("/api/login", json={"token": login_token(vm_config.token)}, headers={"Origin": vm_config.url})
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]
        meta = client.post("/api/files?name=vm.txt", content=b"from-host").json()
        assert client.get(f"/api/download/{meta['id']}").content == b"from-host"
        assert meta["sha256"] == hashlib.sha256(b"from-host").hexdigest()
        assert client.get("/api/info").json()["listen"] == VM_IP
        assert client.post("/api/shutdown").status_code == 403


def test_host_cannot_use_local_cli_key_for_browser_auth(vm_config, make_client):
    with make_client(vm_config, peer=HOST_IP) as client:
        assert client.get("/api/info", headers={"Authorization": f"Bearer {vm_config.token}"}).status_code == 401
    with make_client(vm_config, peer=VM_IP) as client:
        assert client.get("/api/info", headers={"Authorization": f"Bearer {vm_config.token}"}).status_code == 200


@pytest.mark.parametrize("peer", ["192.168.56.2", "10.0.0.5", "8.8.8.8"])
def test_unauthorized_peers_rejected_before_login(vm_config, make_client, peer):
    with make_client(vm_config, peer=peer) as client:
        assert client.get("/").status_code == 403
        response = client.post("/api/login", json={"token": login_token(vm_config.token)},
                               headers={"X-File-Relay": "1", "X-Forwarded-For": HOST_IP})
        assert response.status_code == 403


def test_https_origin_and_host_checks_remain_strict(vm_config, make_client):
    with make_client(vm_config, peer=HOST_IP) as client:
        assert client.get("/", headers={"Host": "evil.test:8765"}).status_code == 403
        assert client.get("/", headers={"Origin": f"http://{VM_IP}:8765"}).status_code == 403
        assert client.get("/", headers={"Origin": vm_config.url}).status_code == 200


def test_cli_uses_verified_https(vm_config):
    connection = Client(vm_config).connection()
    assert isinstance(connection, http.client.HTTPSConnection)
    assert connection.host == VM_IP
    assert connection._context.check_hostname
    assert connection._context.verify_mode == ssl.CERT_REQUIRED


def handshake(client_context, server_context, hostname):
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(client_in, client_out, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for index, side in enumerate((client, server)):
            if index not in completed:
                try:
                    side.do_handshake()
                    completed.add(index)
                except ssl.SSLWantReadError:
                    pass
        for source, destination in ((client_out, server_in), (server_out, client_in)):
            if data := source.read():
                destination.write(data)
        if len(completed) == 2:
            client.write(b"encrypted-transfer")
            server_in.write(client_out.read())
            assert server.read(1024) == b"encrypted-transfer"
            return
    raise AssertionError("TLS handshake did not finish")


def test_real_tls_crypto_without_network_sockets(vm_config):
    context = Client(vm_config).connection()._context
    handshake(context, vm_config.server_ssl_context(), VM_IP)
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(context, vm_config.server_ssl_context(), "192.168.56.11")
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(ssl.create_default_context(), vm_config.server_ssl_context(), VM_IP)


def test_cli_can_restore_local_mode_after_tls_files_go_missing(config):
    from file_relay.cli import dispatch, parser
    invalid = config.values | {"listen_host": VM_IP, "allowed_clients": [HOST_IP],
                               "tls_certfile": "missing.pem", "tls_keyfile": "missing.key"}
    (config.home / "config.json").write_text(json.dumps(invalid))
    with pytest.raises(ValueError):
        Config(config.home)
    dispatch(parser().parse_args(["--home", str(config.home), "configure-local"]))
    assert Config(config.home).url == "http://127.0.0.1:8765"


def test_stop_waits_until_service_releases_listener(config, monkeypatch, capsys):
    from file_relay import cli
    calls = []

    class FakeClient:
        def __init__(self, _config):
            self.polls = 0

        def request(self, method, path, timeout=10):
            calls.append((method, path))
            if path == "/api/info":
                self.polls += 1
                if self.polls >= 2:
                    raise ConnectionRefusedError()
            return {"ok": True}

    monkeypatch.setattr(cli, "Client", FakeClient)
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    cli.dispatch(cli.parser().parse_args(["--home", str(config.home), "stop"]))
    assert calls == [("POST", "/api/shutdown"), ("GET", "/api/info"), ("GET", "/api/info")]
    assert "已停止" in capsys.readouterr().out
