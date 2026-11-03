"""起一个真的本地服务，用原始 socket 发请求（不让 HTTP 客户端替我们改 Host 和路径）。"""
import asyncio
import json
import os
import re
import types

import pytest
from aiohttp import web

import app
import core


async def raw(port, line, host=None, headers="", body=b""):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    host = host or f"127.0.0.1:{port}"
    w.write(f"{line}\r\nHost: {host}\r\n{headers}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body)
    await w.drain()
    data = await r.read()
    w.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    hdrs = {k.lower(): v for k, v in (x.split(": ", 1) for x in lines[1:])}
    return int(lines[0].split()[1]), hdrs, rest


TOK = f"X-Token: {app.TOKEN}\r\n"
JSON = TOK + "Content-Type: application/json\r\n"


class FakeClient:
    def __init__(self):
        self.sent = None

    async def get_entity(self, chat):
        return chat

    async def send_file(self, entity, file, caption=None, force_document=False, **kw):
        self.sent = {"name": os.path.basename(file), "data": open(file, "rb").read(), "path": file, "caption": caption}
        return types.SimpleNamespace(id=7)

    async def iter_download(self, doc, offset=0, request_size=0, limit=None, file_size=None, stride=None):
        yield b"<svg><script>alert(1)</script></svg>"[:file_size]


def run_server(body):
    async def main():
        wa = web.Application(middlewares=[app.guard])
        wa.on_response_prepare.append(app.security_headers)
        wa.add_routes(app.routes)
        runner = web.AppRunner(wa)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        app.app.client, app.app.stage = FakeClient(), "ready"
        try:
            await body(site._server.sockets[0].getsockname()[1])
        finally:
            await runner.cleanup()
    asyncio.run(main())


def test_dns_rebinding_blocked():
    async def body(port):
        st, _, b = await raw(port, "GET / HTTP/1.1", host=f"evil.example:{port}")
        assert st == 403 and app.TOKEN.encode() not in b
        st, _, _ = await raw(port, "GET /api/state HTTP/1.1", host="evil.example", headers=TOK)
        assert st == 403  # 带着 token 也不行
        st, _, b = await raw(port, "GET / HTTP/1.1", host=f"localhost:{port}")
        assert st == 200 and app.TOKEN.encode() in b
    run_server(body)


def test_token_required_everywhere_but_index():
    async def body(port):
        for path in ("/api/state", "/favicon.ico", "/api/stream?chat=1&id=1"):
            st, _, _ = await raw(port, f"GET {path} HTTP/1.1")
            assert st == 403, path
        st, _, _ = await raw(port, "GET /api/state HTTP/1.1", headers="X-Token: " + "0" * 32 + "\r\n")
        assert st == 403
        st, _, _ = await raw(port, "GET /api/state HTTP/1.1", headers="X-Token: \u00e9\r\n")  # 非 ASCII 也不能崩
        assert st == 403
        st, _, _ = await raw(port, "GET /api/state HTTP/1.1", headers=TOK)
        assert st == 200
        st, _, _ = await raw(port, f"GET /api/state?t={app.TOKEN} HTTP/1.1")
        assert st == 200
    run_server(body)


def test_page_csp_uses_fresh_nonce():
    async def body(port):
        nonces = set()
        for _ in range(2):
            st, h, b = await raw(port, "GET / HTTP/1.1")
            csp = h["content-security-policy"]
            nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
            assert f'<script nonce="{nonce}">'.encode() in b
            script_src = re.search(r"script-src ([^;]+)", csp).group(1)
            assert "unsafe-inline" not in script_src and "frame-ancestors 'none'" in csp
            assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
            nonces.add(nonce)
        assert len(nonces) == 2
    run_server(body)


@pytest.mark.parametrize("mime, ctype", [("image/svg+xml", "image/svg+xml"), ("text/html", "text/plain")])
def test_stream_from_telegram_is_sandboxed(mime, ctype):
    async def body(port):
        doc = types.SimpleNamespace(id=hash(mime) & 0xFFFF)
        app.app.msgs[(1, 0, 5)] = types.SimpleNamespace(document=doc, photo=None,
                                                        file=types.SimpleNamespace(mime_type=mime, size=30))
        st, h, b = await raw(port, f"GET /api/stream?t={app.TOKEN}&chat=1&post=0&id=5 HTTP/1.1")
        assert st == 200 and h["content-type"] == ctype
        assert h["content-security-policy"] == "sandbox" and h["x-content-type-options"] == "nosniff"
    run_server(body)


def test_upload_bigger_than_1mb(tmp_path):
    async def body(port):
        payload = os.urandom(3 * 1024 * 1024)  # aiohttp 默认 1 MB 上限，以前这里必失败
        bd = "XyZ"
        part = (f"--{bd}\r\nContent-Disposition: form-data; name=\"chat\"\r\n\r\n1\r\n"
                f"--{bd}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi\r\n"
                f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"../../a\u202egpj.exe\"\r\n\r\n"
                ).encode() + payload + f"\r\n--{bd}--\r\n".encode()
        st, _, b = await raw(port, "POST /api/send HTTP/1.1",
                             headers=f"{TOK}Content-Type: multipart/form-data; boundary={bd}\r\n", body=part)
        sent = app.app.client.sent
        assert st == 200, b
        assert sent["data"] == payload and sent["caption"] == "hi"
        assert sent["name"] == "_.._agpj.exe"          # 穿不出临时文件夹，方向控制符也去掉了
        assert not os.path.exists(sent["path"])         # 临时文件发完就删
    run_server(body)


def test_download_folders_only_via_picker(tmp_path):
    async def body(port):
        evil = str(tmp_path / "anywhere")
        st, _, b = await raw(port, "POST /api/outdir HTTP/1.1", headers=JSON, body=json.dumps({"dir": evil}).encode())
        assert st == 400 and not os.path.exists(evil)
        st, _, _ = await raw(port, "POST /api/start HTTP/1.1", headers=JSON,
                             body=json.dumps({"targets": ["x"], "out_dir": evil}).encode())
        assert st == 400 and not os.path.exists(evil)
        assert evil not in core.recent_dirs()
        st, _, _ = await raw(port, "POST /api/outdir HTTP/1.1", headers=JSON,
                             body=json.dumps({"dir": str(core.DEFAULT_OUT)}).encode())
        assert st == 200  # 列表里已有的照样能用
    run_server(body)


def test_open_does_not_run_executables(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(app.subprocess, "Popen", lambda args, **kw: calls.append(("reveal", args[-1])))
    monkeypatch.setattr(app.os, "startfile", lambda p: calls.append(("start", str(p))), raising=False)
    folder = core.DEFAULT_OUT / "chat_1" / "文件"
    folder.mkdir(parents=True)
    for n in ("setup.exe", "run.py", "photo.jpg"):
        (folder / n).write_bytes(b"x")

    async def body(port):
        for n in ("setup.exe", "run.py", "photo.jpg"):
            st, _, b = await raw(port, "POST /api/open HTTP/1.1", headers=JSON,
                                 body=json.dumps({"path": str(folder / n)}).encode())
            assert st == 200
        st, _, _ = await raw(port, "POST /api/open HTTP/1.1", headers=JSON,
                             body=json.dumps({"path": str(tmp_path / "outside.jpg")}).encode())
        assert st == 403
    run_server(body)
    assert calls == [("reveal", str(folder / "setup.exe")), ("reveal", str(folder / "run.py")),
                     ("start", str(folder / "photo.jpg"))]


def test_openurl_filters(monkeypatch):
    opened = []
    monkeypatch.setattr(app.webbrowser, "open", opened.append)

    async def body(port):
        for url, want in (("https://example.com/a", 200), ("file:///C:/Windows/System32/calc.exe", 403),
                          ("javascript:alert(1)", 403), ("ht\ttps://x", 403), ("https://a b", 403)):
            st, _, _ = await raw(port, "POST /api/openurl HTTP/1.1", headers=JSON, body=json.dumps({"url": url}).encode())
            assert st == want, url
    run_server(body)
    assert opened == ["https://example.com/a"]
