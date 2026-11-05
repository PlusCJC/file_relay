import asyncio
from collections import deque
from contextlib import asynccontextmanager
import hmac
import json
import logging
from pathlib import Path
import time
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect, Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from . import __version__
from .auth import session_value, valid_login_token, valid_session
from .storage import StorageError, Store


LOG = logging.getLogger("file_relay")
WEB = Path(__file__).parent / "web"
COOKIE = "file_relay_session"


class Security:
    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        hosts = self.config.allowed_hosts
        scheme = "https" if self.config.tls_enabled else "http"
        origins = {f"{scheme}://{host}" for host in hosts}
        error = None
        status = 403
        if not request.client or not self.config.allows_peer(request.client.host):
            error = "客户端不在允许的主机 IP 白名单中"
        if self.config.tls_enabled and scope.get("scheme") != "https":
            error = "此服务仅接受 HTTPS"
        if request.headers.get("host", "").lower() not in hosts:
            error = "无效 Host：请使用配置的服务地址"
        if "origin" in request.headers and request.headers["origin"] not in origins:
            error = "禁止跨源访问"
        if request.headers.get("sec-fetch-site") == "cross-site":
            error = "禁止跨站访问"
        if request.method not in ("GET", "HEAD") and request.headers.get("x-file-relay") != "1":
            error = "缺少请求验证头"
        public = {"/", "/app.js", "/style.css", "/favicon.svg", "/api/login"}
        local_client = request.client and (request.client.host == self.config.listen_host or request.client.host.startswith("127."))
        if request.url.path == "/api/shutdown" and not local_client:
            error = "仅允许服务所在机器停止服务"
        if error is None and request.url.path not in public:
            bearer = request.headers.get("authorization", "")
            authenticated = local_client and bearer.startswith("Bearer ") and hmac.compare_digest(bearer[7:].encode(), self.config.token.encode())
            if not authenticated:
                authenticated = valid_session(self.config.token, request.cookies.get(COOKIE, ""), self.config.session_minutes * 60)
            if not authenticated:
                error, status = "请先登录", 401

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cache-control", b"no-store"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ])
            await send(message)

        if error:
            return await JSONResponse({"error": error}, status_code=status)(scope, receive, secured_send)
        await self.app(scope, receive, secured_send)


def create_app(config):
    failures = deque()
    slots = asyncio.Semaphore(4)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(config)
        app.state.store = store

        async def cleanup_loop():
            while True:
                try:
                    result = await asyncio.to_thread(store.cleanup)
                    if result["count"]:
                        LOG.info("自动清理 %d 个文件 / %d 字节", result["count"], result["bytes"])
                except Exception:
                    LOG.exception("自动清理失败，将在下个周期重试")
                await asyncio.sleep(config.cleanup_interval_seconds)

        task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            store.close()

    async def small_json(request):
        # Read a bounded stream, including when Content-Length is absent or false.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2048:
                raise StorageError("请求体过大", 413)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise StorageError("请求体必须是 JSON") from None
        if not isinstance(data, dict):
            raise StorageError("请求体必须是 JSON 对象")
        return data

    async def login(request):
        now = time.monotonic()
        while failures and failures[0] < now - 60:
            failures.popleft()
        if len(failures) >= 10:
            return JSONResponse({"error": "登录失败次数过多，请 60 秒后重试"}, status_code=429, headers={"Retry-After": "60"})
        data = await small_json(request)
        token = data.get("token")
        if not valid_login_token(config.token, token, config.login_token_minutes * 60):
            failures.append(now)
            return JSONResponse({"error": "令牌无效或已过期"}, status_code=401)
        failures.clear()
        response = JSONResponse({"ok": True})
        seconds = config.session_minutes * 60
        response.set_cookie(COOKIE, session_value(config.token, seconds=seconds), max_age=seconds,
                            httponly=True, samesite="strict", secure=config.tls_enabled, path="/api/")
        return response

    async def logout(request):
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/api/", httponly=True, samesite="strict", secure=config.tls_enabled)
        return response

    async def info(request):
        files = await asyncio.to_thread(request.app.state.store.list)
        return JSONResponse({
            "version": __version__, "listen": config.listen_host, "port": config.port,
            "network_mode": "vm" if config.remote else "local", "https": config.tls_enabled,
            "retention_hours": config.retention_hours,
            "cleanup_interval_seconds": config.cleanup_interval_seconds,
            "max_file_bytes": config.max_file_bytes,
            "total_quota_bytes": config.total_quota_bytes,
            "used_bytes": sum(item["size"] for item in files), "file_count": len(files),
        })

    async def files(request):
        store = request.app.state.store
        if request.method == "GET":
            return JSONResponse(await asyncio.to_thread(store.list))
        raw_size = request.headers.get("content-length")
        if raw_size is None:
            raise StorageError("上传必须包含 Content-Length", 411)
        try:
            size = int(raw_size)
        except ValueError:
            raise StorageError("Content-Length 无效") from None
        if slots.locked():
            raise StorageError("已有 4 个并发上传，请稍后重试", 429)
        async with slots:
            upload = store.begin(
                request.query_params.get("name", ""), size, request.query_params.get("kind", "file"))
            try:
                # A stalled transfer cannot occupy a worker indefinitely.
                async with asyncio.timeout(3600):
                    async for chunk in request.stream():
                        upload.write(chunk)
                    result = upload.commit()
                LOG.info("上传完成 id=%s size=%d", result["id"], result["size"])
                return JSONResponse(result, status_code=201)
            except TimeoutError:
                raise StorageError("上传超过 1 小时时限", 408) from None
            except ClientDisconnect:
                raise StorageError("上传连接中断") from None
            finally:
                # No await: cancellation must never strand a quota reservation.
                upload.abort()

    async def download(request):
        store = request.app.state.store
        lease = store.download(request.path_params["file_id"])
        meta, handle = lease.__enter__()
        closed = False

        def release():
            nonlocal closed
            with store.lock:
                if not closed:
                    closed = True
                    lease.__exit__(None, None, None)

        def stream():
            try:
                while chunk := handle.read(1024 * 1024):
                    yield chunk
            finally:
                release()

        headers = {
            "Content-Length": str(meta["size"]),
            "Content-Disposition": f"attachment; filename=download; filename*=UTF-8''{quote(meta['name'], safe='')}",
            "X-Content-SHA256": meta["sha256"],
        }
        class DownloadResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    release()

        return DownloadResponse(stream(), media_type="application/octet-stream", headers=headers,
                                background=BackgroundTask(release))

    async def metadata(request):
        store = request.app.state.store
        if request.method == "DELETE":
            await asyncio.to_thread(store.delete, request.path_params["file_id"])
            return JSONResponse({"ok": True})
        return JSONResponse(await asyncio.to_thread(store.get, request.path_params["file_id"]))

    async def cleanup(request):
        return JSONResponse(await asyncio.to_thread(request.app.state.store.cleanup,
                            request.query_params.get("dry_run", "1") != "0"))

    async def shutdown(request):
        callback = getattr(request.app.state, "shutdown", None)
        if callback is None:
            raise StorageError("此运行方式不支持远程停止", 409)
        callback()
        return JSONResponse({"ok": True})

    async def asset(request):
        name = request.path_params.get("name", "index.html")
        if name not in {"index.html", "app.js", "style.css", "favicon.svg"}:
            raise StorageError("页面不存在", 404)
        return FileResponse(WEB / name)

    async def storage_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=error.status)

    async def unexpected_error(request, error):
        LOG.error("请求处理失败", exc_info=(type(error), error, error.__traceback__))
        return JSONResponse({"error": "服务内部错误，请检查应用日志"}, status_code=500)

    app = Starlette(routes=[
        Route("/", asset),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/info", info),
        Route("/api/files", files, methods=["GET", "POST"]),
        Route("/api/files/{file_id}", metadata, methods=["GET", "DELETE"]),
        Route("/api/download/{file_id}", download),
        Route("/api/cleanup", cleanup, methods=["POST"]),
        Route("/api/shutdown", shutdown, methods=["POST"]),
        Route("/{name}", asset),
    ], lifespan=lifespan, exception_handlers={StorageError: storage_error, Exception: unexpected_error})
    app.add_middleware(Security, config=config)
    return app
