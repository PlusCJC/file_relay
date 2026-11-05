from file_relay import cli
from file_relay.auth import valid_login_token


def test_cli_token_is_temporary_and_does_not_print_internal_key(config, capsys):
    cli.dispatch(cli.parser().parse_args(["--home", str(config.home), "token"]))
    ticket = capsys.readouterr().out.strip()
    assert valid_login_token(config.token, ticket, config.login_token_minutes * 60)
    assert config.token not in ticket


def test_download_all_reads_the_shared_list(config, monkeypatch, tmp_path):
    calls = []

    class FakeClient:
        def __init__(self, config):
            pass

        def request(self, method, path):
            calls.append((method, path))
            return [{"id": "a" * 32, "kind": "file"}, {"id": "b" * 32, "kind": "directory"}]

        def download(self, file_id, destination, extract):
            calls.append((file_id, destination, extract))
            return destination / file_id

    monkeypatch.setattr(cli, "Client", FakeClient)
    args = cli.parser().parse_args(["--home", str(config.home), "download", "--all", "--to", str(tmp_path), "--extract"])
    cli.dispatch(args)
    assert calls == [("GET", "/api/files"), ("a" * 32, tmp_path, False), ("b" * 32, tmp_path, True)]
