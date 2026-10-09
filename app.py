"""TG 下载器图形界面：本地小服务器 + Edge 应用窗口。双击 启动.bat 运行。"""
import asyncio
import logging
import mimetypes
import msvcrt
import os
import secrets
import subprocess
import sys
import time
import webbrowser
from contextlib import aclosing
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from telethon import errors, utils

import core

PORT = 17863
WEB = core.ROOT / "web"
PORT_FILE = core.DATA / ".port"


def load_token():
    """防止别的网页偷偷调本地接口。存到文件里，后台重启后已开着的窗口刷新一下还能用。"""
    f = core.DATA / ".token"
    if not f.exists():
        core.DATA.mkdir(exist_ok=True)
        f.write_text(secrets.token_hex(16))
    return f.read_text().strip()


TOKEN = load_token()

log = logging.getLogger("tg")


class App:
    def __init__(self):
        self.stage = "connecting"  # connecting / qr / password / ready / error
        self.qr = self.hint = self.error = self.me = None
        self.client = None
        self.password = None
        self.chats = None
        self.jobs = []
        self.tasks = {}  # job.id -> asyncio.Task
        self.last_seen = time.time()
        self.closed_at = None  # 页面关闭时发来 /api/bye
        self.msgs = {}  # (chat, post, id) -> 浏览时拿到的消息，下载/预览直接用
        self.previews = {}  # 缩略图、头像缓存
        self.preview_sem = asyncio.Semaphore(12)  # 缩略图/头像同时拉几个

    # ---- 登录 ----
    async def boot(self):
        self.stage, self.error = "connecting", None
        try:
            if self.client is None:
                self.client = await core.connect(core.load_config())
            await core.login(self.client, self.show_qr, self.ask_password)
            self.me = utils.get_display_name(await self.client.get_me())
            self.stage = "ready"
            log.info("已登录：%s", self.me)
        except Exception as e:
            log.exception("登录失败")
            self.stage, self.error = "error", str(e)
            if self.client:
                await self.client.disconnect()
                self.client = None

    def show_qr(self, url):
        self.stage, self.qr = "qr", core.qr_data_url(url)

    async def ask_password(self, hint):
        self.stage, self.hint = "password", hint
        self.password = asyncio.get_running_loop().create_future()
        try:
            return await self.password
        finally:
            self.stage = "connecting"

    # ---- 下载（各任务同时跑，共用 core.LANES 限制总并行路数）----
    @property
    def running(self):
        return any(not t.done() for t in self.tasks.values())

    def start(self, targets, opts):
        new = []
        for t in targets:
            if isinstance(t, dict) and "ids" in t:  # 手动点的消息
                chat, post = t["chat"], t.get("post") or 0
                msgs = [m for i in t["ids"] if (m := self.msgs.get((chat, post, i)))]
                if msgs:
                    new.append(core.Job(self.client, chat, opts, picked={"post": post, "msgs": msgs}))
            else:
                new.append(core.Job(self.client, t, opts))
        for job in new:
            self.jobs.append(job)
            self.tasks[job.id] = asyncio.create_task(job.run())
        return new

    def cancel(self, job_id=None):
        for jid, task in self.tasks.items():
            if job_id in (None, jid) and not task.done():
                task.cancel()

    async def preview(self, key, make):
        """缩略图/头像：并发限 6 个，结果缓存。"""
        if key not in self.previews:
            async with self.preview_sem:
                if key not in self.previews:
                    try:
                        self.previews[key] = await make()
                    except Exception as e:
                        log.warning("预览失败 %s: %r", key, e)
                        self.previews[key] = None
        return self.previews[key]


app = App()
routes = web.RouteTableDef()


@web.middleware
async def guard(request, handler):
    if request.path.startswith("/api/"):
        if TOKEN not in (request.headers.get("X-Token"), request.query.get("t")):  # <img> 没法带请求头，走 ?t=
            raise web.HTTPForbidden()
        app.last_seen = time.time()
        app.closed_at = time.time() if request.path == "/api/bye" else None
    return await handler(request)


def ok(**kw):
    return web.json_response({"ok": True, **kw})


def image(data):
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


def under_downloads(rel):
    path = (core.DEFAULT_OUT / rel).resolve()
    if core.DEFAULT_OUT.resolve() not in path.parents:  # 只允许动下载目录里的
        raise web.HTTPForbidden()
    return path


@routes.get("/")
async def index(_):
    html = (WEB / "index.html").read_text("utf-8").replace("{{TOKEN}}", TOKEN)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


# ---- 登录 ----
@routes.get("/api/state")
async def state(_):
    return web.json_response({"stage": app.stage, "qr": app.qr, "hint": app.hint,
                              "error": app.error, "me": app.me})


@routes.post("/api/password")
async def password(request):
    pw = (await request.json()).get("password", "")
    if app.password and not app.password.done():
        app.password.set_result(pw)
    return ok()


@routes.post("/api/retry")
async def retry(_):
    if app.stage == "error":
        asyncio.create_task(app.boot())
    return ok()


@routes.post("/api/lang")
async def set_lang(request):
    core.LANG = "en" if (await request.json()).get("lang") == "en" else "zh"
    return ok()


@routes.post("/api/bye")
async def bye(_):
    return ok()


# ---- 浏览 ----
@routes.get("/api/chats")
async def chats(request):
    if app.stage != "ready":
        return web.json_response([])
    if app.chats is None or request.query.get("refresh"):
        app.chats = await core.list_chats(app.client)
    return web.json_response(app.chats)


@routes.get("/api/messages")
async def messages(request):
    q = request.query
    chat, post = int(q["chat"]), int(q.get("post") or 0)
    try:
        items, msgs, nxt = await core.browse(
            app.client, chat, post or None, int(q.get("offset") or 0), limit=min(int(q.get("limit") or 40), 100),
            asc=bool(q.get("asc")), search=q.get("search"))
    except Exception as e:
        log.exception("读取消息失败")
        return web.json_response({"error": core.tr("读不到消息：{e}", e=e)}, status=400)
    for m in msgs:
        app.msgs[(chat, post, m.id)] = m
    return web.json_response({"items": items, "next": nxt})


@routes.post("/api/resolve")
async def resolve_link(request):
    """点了 TG 链接：告诉前端跳去哪个聊天、哪条消息。"""
    return web.json_response(await core.resolve_link(app.client, (await request.json()).get("url", "")))


@routes.post("/api/join")
async def join(request):
    """邀请链接弹窗里点了“加入”。"""
    r = await core.join_invite(app.client, (await request.json()).get("hash", ""))
    if "chat" in r:
        app.chats = None  # 左边列表下次重新读
    return web.json_response(r)


@routes.post("/api/openurl")
async def open_url(request):
    """普通网址用系统默认浏览器打开。只放行 http/https/mailto，防止消息里的怪链接调起别的程序。"""
    url = (await request.json()).get("url", "").strip()
    if urlparse(url).scheme.lower() not in ("http", "https", "mailto"):
        raise web.HTTPForbidden()
    webbrowser.open(url)
    return ok()


def cached_msg(q):
    msg = app.msgs.get((int(q["chat"]), int(q.get("post") or 0), int(q["id"])))
    if msg is None:
        raise web.HTTPNotFound()
    return msg


@routes.get("/api/thumb")
async def thumb(request):
    msg, w = cached_msg(request.query), int(request.query.get("w") or 200)
    key = ("thumb", msg.chat_id, msg.id, w)
    return image(await app.preview(key, lambda: core.thumb_bytes(app.client, msg, w)))


@routes.get("/api/photo")
async def photo(request):
    """看大图：整张原图（只限图片）。"""
    msg = cached_msg(request.query)
    if not msg.photo:
        raise web.HTTPNotFound()
    return image(await app.client.download_media(msg, file=bytes))


# 浏览器认得的类型：mov 基本是 h264，按 mp4 放；mkv 按 webm 试试
PLAY_MIME = {"video/quicktime": "video/mp4", "video/x-matroska": "video/webm", "audio/x-m4a": "audio/mp4"}


@routes.get("/api/stream")
async def stream(request):
    """在线预览/播放：边下边播，支持拖进度条（Range）。不落盘；已经下载过的直接读本地文件。"""
    q = request.query
    if q.get("rel"):
        try:
            path = under_downloads(q["rel"])
            if path.is_file():
                headers = {"Content-Disposition": "inline", "Cache-Control": "no-store"}
                ct = mimetypes.guess_type(path.name)[0]
                if ct:
                    headers["Content-Type"] = PLAY_MIME.get(ct, ct)
                return web.FileResponse(path, headers=headers)
        except web.HTTPForbidden:
            pass
    key = (int(q["chat"]), int(q.get("post") or 0), int(q["id"]))
    msg = cached_msg(q)
    if not msg.document or not msg.file.size:
        raise web.HTTPNotFound()
    size = msg.file.size
    mime = PLAY_MIME.get(msg.file.mime_type, msg.file.mime_type) or "application/octet-stream"
    r = request.http_range
    start = r.start or 0
    stop = size if r.stop is None else min(r.stop, size)
    if start < 0:  # bytes=-500：最后 500 字节
        start, stop = max(0, size + start), size
    if start >= size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    partial = "Range" in request.headers
    resp = web.StreamResponse(status=206 if partial else 200, headers={
        "Content-Type": mime, "Accept-Ranges": "bytes", "Content-Length": str(stop - start),
        "Content-Disposition": "inline", "Cache-Control": "no-store"})
    if partial:
        resp.headers["Content-Range"] = f"bytes {start}-{stop - 1}/{size}"
    await resp.prepare(request)
    pos = start
    want = int(q.get("lanes") or core.STREAM_LANES)  # 调试时可以用 ?lanes=N 试不同并行度
    for _ in range(3):
        try:
            # aclosing：播放器拖进度条/关窗口会中途断开，这里保证生成器被关掉、占用的路数立刻还回池子
            async with aclosing(core.stream_chunks(app.client, msg.document, size, pos, stop, lanes=want)) as it:
                async for chunk in it:
                    await resp.write(chunk)
                    pos += len(chunk)
            break
        except errors.FileReferenceExpiredError:  # 文件引用过期（隔了几个小时）：重新拿一下消息接着传
            fresh = await app.client.get_messages(msg.peer_id, ids=msg.id)
            if not fresh:
                break
            msg = app.msgs[key] = fresh
        except ConnectionError:  # 播放器拖进度条/关掉了，会断开旧请求，正常（Windows 上报的是 Connection lost）
            break
    return resp


@routes.get("/api/avatar")
async def avatar(request):
    peer = int(request.query["peer"])

    async def make():
        entity = await app.client.get_entity(peer)
        return await app.client.download_profile_photo(entity, file=bytes)

    return image(await app.preview(("avatar", peer), make))


# ---- 下载 ----
@routes.post("/api/start")
async def start(request):
    if app.stage != "ready":
        return web.json_response({"error": core.tr("还没登录")}, status=400)
    body = await request.json()
    targets = [t for t in body.get("targets", []) if t not in ("", None)]
    if not targets:
        return web.json_response({"error": core.tr("没有要下载的")}, status=400)
    new = app.start(targets, body)
    if not new:
        return web.json_response({"error": core.tr("消息找不到了，重新打开这个聊天再试")}, status=400)
    return ok(ids=[j.id for j in new])


@routes.post("/api/cancel")
async def cancel(request):
    app.cancel((await request.json()).get("id"))
    return ok()


@routes.post("/api/clear")
async def clear(_):
    """清掉已结束的任务记录。"""
    app.jobs = [j for j in app.jobs if not app.tasks[j.id].done()]
    app.tasks = {j.id: app.tasks[j.id] for j in app.jobs}
    return ok()


@routes.get("/api/lanes")
async def lanes_left(_):
    return web.json_response({"free": core.LANES.free, "total": core.LANES.total})


@routes.get("/api/progress")
async def progress(_):
    return web.json_response({"running": app.running, "jobs": [j.snapshot() for j in reversed(app.jobs)]})


@routes.post("/api/files")
async def files(request):
    keys = (await request.json()).get("keys", [])
    return web.json_response({k: core.FILES[k] for k in keys if k in core.FILES})


@routes.post("/api/open")
async def open_path(request):
    """打开下载目录里的文件夹或文件；reveal=1 时在资源管理器里选中它。"""
    body = await request.json()
    rel = body.get("rel") or ""
    path = under_downloads(rel) if rel else core.DEFAULT_OUT
    if body.get("path"):  # 任务里给的是绝对路径
        path = Path(body["path"]).resolve()
        if core.DEFAULT_OUT.resolve() not in (path, *path.parents):
            raise web.HTTPForbidden()
    if not rel:
        path.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return web.json_response({"error": core.tr("文件不见了，可能被删了")}, status=404)
    if body.get("reveal"):
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        os.startfile(path)
    return ok()


# ---------------- 启动 ----------------

def find_browser():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            if base and (p := Path(base) / rel).exists():
                return p
    return None


def open_window(url):
    """用 Edge/Chrome 的“应用模式”开一个独立窗口。"""
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return
    subprocess.Popen([str(browser), f"--app={url}", f"--user-data-dir={core.DATA / 'ui'}",
                      "--window-size=1280,860", "--no-first-run", "--no-default-browser-check"])


def single_instance():
    f = open(core.DATA / ".lock", "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return None
    return f


async def main():
    core.DATA.mkdir(exist_ok=True)
    lock = single_instance()
    if lock is None:  # 已经开着了，再弹一个窗口就行
        port = PORT_FILE.read_text().strip() if PORT_FILE.exists() else PORT
        open_window(f"http://127.0.0.1:{port}/")
        return

    webapp = web.Application(middlewares=[guard])
    webapp.add_routes(routes)
    runner = web.AppRunner(webapp, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", PORT)
        await site.start()
    except OSError:  # 端口被占，随便换一个
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
    port = site._server.sockets[0].getsockname()[1]
    PORT_FILE.write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    log.info("界面地址 %s", url)

    asyncio.create_task(app.boot())
    if "--no-window" not in sys.argv:
        open_window(url)
    # Edge 会把窗口转交给别的进程，没法直接等它结束。改成：页面关闭时发 /api/bye，
    # 3 秒内没有新请求（不是刷新）就退出；万一 bye 没发出去，90 秒没动静也退出。
    while True:
        await asyncio.sleep(1)
        now = time.time()
        if (app.closed_at and now - app.closed_at > 3) or now - app.last_seen > 90:
            break

    log.info("窗口已关闭，退出")
    app.cancel()
    await asyncio.sleep(0.5)
    if app.client:
        await app.client.disconnect()
    await runner.cleanup()


if __name__ == "__main__":
    core.DATA.mkdir(exist_ok=True)
    handler = RotatingFileHandler(core.DATA / "运行日志.txt", maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("telethon").setLevel(logging.WARNING)
    try:
        asyncio.run(main())
    except Exception:
        log.exception("崩溃")
        sys.exit(1)
