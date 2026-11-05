import asyncio
from contextlib import contextmanager
import anyio.to_thread
import httpx
import pytest

from file_relay.config import Config
from file_relay.server import create_app


@pytest.fixture
def config(tmp_path):
    return Config(tmp_path / "state")


class InProcessTransport(httpx.BaseTransport):
    """Exercise ASGI without sockets. Thread dispatch is inline in this fixture only."""
    def __init__(self, app, runner, peer):
        self.runner = runner
        self.transport = httpx.ASGITransport(app=app, client=(peer, 50000))

    def handle_request(self, request):
        original_length = request.headers.get("content-length")
        incoming = httpx.Request(request.method, request.url, headers=request.headers, content=request.read())
        if original_length is None:
            incoming.headers.pop("content-length", None)

        async def exchange():
            response = await self.transport.handle_async_request(incoming)
            body = await response.aread()
            await response.aclose()
            return httpx.Response(response.status_code, headers=response.headers, content=body, request=request)

        return self.runner.run(exchange())


@pytest.fixture
def make_client(monkeypatch):
    async def inline(function, *args, **kwargs):
        for key in ("abandon_on_cancel", "cancellable", "limiter"):
            kwargs.pop(key, None)
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)
    monkeypatch.setattr(anyio.to_thread, "run_sync", inline)

    @contextmanager
    def factory(config, peer="127.0.0.1"):
        app = create_app(config)
        with asyncio.Runner() as runner:
            lifespan = app.router.lifespan_context(app)
            runner.run(lifespan.__aenter__())
            try:
                with httpx.Client(transport=InProcessTransport(app, runner, peer), base_url=config.url) as value:
                    value.app = app
                    yield value
            finally:
                runner.run(lifespan.__aexit__(None, None, None))
    return factory


@pytest.fixture
def client(config, make_client):
    with make_client(config) as value:
        value.headers.update({"Authorization": f"Bearer {config.token}", "X-File-Relay": "1"})
        yield value


def upload(client, name="测试.txt", content=b"hello", **params):
    response = client.post("/api/files", params={"name": name, **params}, content=content)
    assert response.status_code == 201, response.text
    return response.json()
