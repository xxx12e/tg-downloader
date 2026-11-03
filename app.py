"""TG 下载器图形界面：本地小服务器 + Edge 应用窗口。双击 启动.bat 运行。"""
import asyncio
import base64
import logging
import mimetypes
import msvcrt
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from collections import OrderedDict
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

_bg = set()


def bg(coro):
    """后台跑一个任务。asyncio 只弱引用任务，不留引用的话可能跑到一半被回收。"""
    task = asyncio.create_task(coro)
    _bg.add(task)
    task.add_done_callback(_bg.discard)
    return task


class Lru(OrderedDict):
    """超过 cap 个就把最早放进来的扔掉。浏览得久了，消息和缩略图缓存不能无限涨。"""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self.cap:
            self.popitem(last=False)


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
        self.msgs = Lru(50_000)  # (chat, post, id) -> 浏览时拿到的消息，下载/预览直接用
        self.previews = Lru(3000)  # 缩略图、头像缓存（浏览器自己还有一层缓存）
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
        """缩略图/头像：并发限 12 个，结果缓存。"""
        if key in self.previews:
            return self.previews[key]
        async with self.preview_sem:
            if key in self.previews:
                return self.previews[key]
            try:
                data = await make()
            except Exception as e:
                log.warning("预览失败 %s: %r", key, e)
                data = None
            self.previews[key] = data
            return data


app = App()
routes = web.RouteTableDef()


def host_ok(request):
    """只认 127.0.0.1 / localhost 加我们自己的端口。
    防 DNS 重绑定：恶意网站把自己的域名改成解析到 127.0.0.1 以后，浏览器把它当“同源”，
    就能读到首页里的 token、进而调所有接口（读消息、发消息、下载并打开文件）。它发来的 Host 是它自己的域名。"""
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    port = sock[1] if sock else None
    return (request.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")


def token_ok(request):
    want = TOKEN.encode()
    return any(secrets.compare_digest(want, (x or "").encode())  # 定长比较，不给按耗时猜 token 的机会
               for x in (request.headers.get("X-Token"), request.query.get("t")))  # <img> 没法带请求头，走 ?t=


@web.middleware
async def guard(request, handler):
    if not host_ok(request):
        raise web.HTTPForbidden()
    if request.path != "/":  # 除了首页，全部要 token（白名单比“只管 /api/”更不容易漏）
        if not token_ok(request):
            raise web.HTTPForbidden()
        app.last_seen = time.time()
        app.closed_at = time.time() if request.path == "/api/bye" else None
    return await handler(request)


# 首页：只有带本次随机 nonce 的那个 <script> 能跑，onclick=/onerror= 这种内联脚本一律不执行
# （消息内容万一漏了转义，注入的代码也跑不起来）；样式、图片、请求都只许来自本机这个服务；不许被别的网页套在 iframe 里
PAGE_CSP = ("default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; frame-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


async def security_headers(_, resp):
    """每个响应（包括边下边播那种流式响应）发出去前都加上。"""
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")  # 地址里带着 ?t=token，任何时候都别外传
    resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")


def ok(**kw):
    return web.json_response({"ok": True, **kw})


def image(data):
    if not data:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


def under_downloads(path):
    try:
        return core.safe_path(path)
    except PermissionError:
        raise web.HTTPForbidden()


@routes.get("/")
async def index(_):
    nonce = secrets.token_urlsafe(18)  # 每次打开页面都换一个
    html = (WEB / "index.html").read_text("utf-8").replace("{{TOKEN}}", TOKEN).replace("{{NONCE}}", nonce)
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store", "Content-Security-Policy": PAGE_CSP.format(nonce=nonce)})


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
        bg(app.boot())
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
    # 再拒掉空白和控制字符：urlparse 会悄悄去掉 Tab/换行再判断，交给系统打开的却是原样的字符串
    if urlparse(url).scheme.lower() not in ("http", "https", "mailto") or re.search(r"[\x00-\x20\x7f]", url):
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


def stream_headers(ct):
    """边下边播/读本地文件时的类型和防护头。

    文件类型（mime）是发文件的人自己填的，可以是 text/html 或带脚本的 SVG。这些内容和界面同源，
    要是被当成网页打开（比如在大图上右键“在新标签页中打开图片”），里面的脚本就能拿到 token。
    所以：只原样给音视频/图片/PDF，网页之类的一律降成纯文本或二进制；再加 CSP sandbox，
    就算被直接打开，脚本也跑不起来、也不算同源。PDF 例外（加了 sandbox 浏览器就不肯显示 PDF）。"""
    ct = (ct or "").split(";")[0].strip().lower()
    ct = PLAY_MIME.get(ct, ct)
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", ct):
        ct = "application/octet-stream"
    elif not (ct == "application/pdf" or ct.startswith(("video/", "audio/", "image/"))):
        ct = "text/plain" if ct.startswith("text/") else "application/octet-stream"
    h = {"Content-Type": ct, "Content-Disposition": "inline", "Cache-Control": "no-store"}
    if ct != "application/pdf":
        h["Content-Security-Policy"] = "sandbox"
    return h


@routes.get("/api/stream")
async def stream(request):
    """在线预览/播放：边下边播，支持拖进度条（Range）。不落盘；已经下载过的直接读本地文件。"""
    q = request.query
    if q.get("path"):
        try:
            path = under_downloads(q["path"])
            if path.is_file():
                return web.FileResponse(path, headers=stream_headers(mimetypes.guess_type(path.name)[0]))
        except web.HTTPForbidden:
            pass
    key = (int(q["chat"]), int(q.get("post") or 0), int(q["id"]))
    msg = cached_msg(q)
    if not msg.document or not msg.file.size:
        raise web.HTTPNotFound()
    size = msg.file.size
    r = request.http_range
    start = r.start or 0
    stop = size if r.stop is None else min(r.stop, size)
    if start < 0:  # bytes=-500：最后 500 字节
        start, stop = max(0, size + start), size
    if start >= size:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    if start == 0 and size > 2 * core.CHUNK:
        bg(core.warm_media(app.client, msg.document, size))
    # 浏览器常发 bytes=0- 这种不带结尾的请求，它取一点就会掐断重发；
    # 一次最多给 16 MB，免得白白占着线路传后面用不上的数据
    if r.stop is None and stop - start > 16 * 1024 * 1024:
        stop = start + 16 * 1024 * 1024
    partial = "Range" in request.headers
    resp = web.StreamResponse(status=206 if partial else 200, headers={
        **stream_headers(msg.file.mime_type), "Accept-Ranges": "bytes", "Content-Length": str(stop - start)})
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


@routes.post("/api/warm")
async def warm(request):
    """点开视频/音频时先调这个，把开头和结尾并发抓进缓存，省掉播放器来回试探的等待。"""
    try:
        msg = cached_msg(await request.json())
    except web.HTTPNotFound:
        return ok(skipped=True)
    if msg.document and (msg.file.size or 0) > 2 * core.CHUNK:
        bg(core.warm_media(app.client, msg.document, msg.file.size))
    return ok()


@routes.get("/api/avatar")
async def avatar(request):
    peer = int(request.query["peer"])

    async def make():
        entity = await app.client.get_entity(peer)
        return await app.client.download_profile_photo(entity, file=bytes)

    return image(await app.preview(("avatar", peer), make))


# ---- 下载 ----
# 路径和提示文字走环境变量，不拼进脚本：拼接的话路径里的 ’ 之类（PowerShell 也当引号）会把脚本弄坏。
# 选中的路径按 UTF-8 转 Base64 输出：控制台默认用 OEM 代码页，中文/特殊字符的文件夹名会变成乱码
PICK_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = $env:TGDL_DESC
$d.ShowNewFolderButton = $true
if ($env:TGDL_CUR -and (Test-Path -LiteralPath $env:TGDL_CUR)) { $d.SelectedPath = $env:TGDL_CUR }
$top = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
if ($d.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($d.SelectedPath)))
}
"""


@routes.get("/api/outdir")
async def get_outdir(_):
    """用过的文件夹，第一个是上次用的。"""
    return web.json_response({"dirs": core.recent_dirs()})


@routes.post("/api/outdir")
async def set_outdir(request):
    """pick=1 弹系统的文件夹选择框，选完记下来；否则只是把列表里已有的路径挪到最前面。
    新文件夹只能经选择框（要人亲手点）加进来：列表就是“允许读写的文件夹”白名单，接口不能自己往里加。"""
    body = await request.json()
    path = body.get("dir")
    if body.get("pick"):
        env = {**os.environ, "TGDL_CUR": core.recent_dirs()[0],
               "TGDL_DESC": "选择下载文件夹" if core.LANG == "zh" else "Choose a download folder"}
        try:
            r = await asyncio.to_thread(subprocess.run, ["powershell", "-STA", "-NoProfile", "-Command", PICK_PS],
                                        capture_output=True, text=True, timeout=300, env=env)
            out = (r.stdout or "").strip()
            path = base64.b64decode(out).decode("utf-8") if out else ""
        except Exception as e:
            log.warning("选择文件夹失败: %r", e)
            return web.json_response({"error": str(e)}, status=400)
        if not path:
            return ok(cancelled=True, dirs=core.recent_dirs())
    elif not path or not core.is_known_dir(path):
        return web.json_response({"error": core.tr("只能下到列表里的文件夹；新文件夹请点“选择其他文件夹…”")}, status=400)
    try:
        Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        core.remember_dir(path)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return ok(dirs=core.recent_dirs())


async def read_upload(request, folder):
    """multipart 边收边写到 folder 里，文件名保持原样（发出去对方看到的就是这个名字）。
    不用 request.post()：它把整个文件读进内存，而且 aiohttp 默认超过 1 MB 直接拒收 —— 手机照片大多超过 1 MB。"""
    fields, path = {}, None
    reader = await request.multipart()
    async for part in reader:
        if part.name == "file" and part.filename:
            path = Path(folder) / core.safe(part.filename)
            with open(path, "wb") as f:
                while chunk := await part.read_chunk(1024 * 1024):
                    f.write(chunk)
        elif part.name:
            fields[part.name] = await part.text()
    return fields, path


@routes.post("/api/send")
async def send_msg(request):
    """发文字或文件。文件走 multipart，文字走 JSON。"""
    if app.stage != "ready":
        return web.json_response({"error": core.tr("还没登录")}, status=400)
    folder = None
    try:
        if request.content_type.startswith("multipart/"):
            folder = tempfile.mkdtemp(prefix="tgdl-up-")
            body, path = await read_upload(request, folder)
        else:
            body, path = await request.json(), None
        msg = await core.send(
            app.client, int(body["chat"]), text=body.get("text") or "",
            reply_to=int(body.get("reply_to") or 0) or None,
            comment_to=int(body.get("post") or 0) or None,
            file=path)
    except errors.FloodWaitError as e:
        return web.json_response({"error": core.tr("被限流，等 {sec} 秒", sec=e.seconds)}, status=429)
    except Exception as e:
        log.exception("发送失败")
        return web.json_response({"error": core.tr("发不出去：{e}", e=e)}, status=400)
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
    app.chats = None  # 列表里的“最后一条消息”变了
    return ok(id=msg.id)


@routes.post("/api/delete")
async def delete_msg(request):
    b = await request.json()
    try:
        await core.delete_msg(app.client, int(b["chat"]), int(b["id"]), bool(b.get("both", True)))
    except Exception as e:
        return web.json_response({"error": core.tr("删不掉：{e}", e=e)}, status=400)
    return ok()


@routes.post("/api/start")
async def start(request):
    if app.stage != "ready":
        return web.json_response({"error": core.tr("还没登录")}, status=400)
    body = await request.json()
    targets = [t for t in body.get("targets", []) if t not in ("", None)]
    if not targets:
        return web.json_response({"error": core.tr("没有要下载的")}, status=400)
    if d := body.get("out_dir"):
        if not core.is_known_dir(d):  # 只能下到选择框选过的文件夹里
            return web.json_response({"error": core.tr("只能下到列表里的文件夹；新文件夹请点“选择其他文件夹…”")}, status=400)
        try:
            Path(d).expanduser().mkdir(parents=True, exist_ok=True)
            core.remember_dir(d)
        except OSError as e:
            return web.json_response({"error": str(e)}, status=400)
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
    want = body.get("path") or ""
    path = under_downloads(want) if want else core.DEFAULT_OUT
    if not want:
        path.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return web.json_response({"error": core.tr("文件不见了，可能被删了")}, status=404)
    # 频道里发的 exe/bat/py 等，点一下“打开”就直接运行了。和 Telegram 一样不直接打开：
    # 只在文件夹里选中它，由你自己确认后再双击（文件带了“来自网络”标记，Windows 还会再提示一次）
    risky = path.is_file() and path.suffix.lower() in core.RISKY_EXT
    if body.get("reveal") or risky:
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        os.startfile(path)
    return ok(risky=path.suffix.lower() if risky else None)


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
        if "--no-window" not in sys.argv:
            open_window(f"http://127.0.0.1:{port}/")
        return

    webapp = web.Application(middlewares=[guard])
    webapp.on_response_prepare.append(security_headers)
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
    core.load_index()
    PORT_FILE.write_text(str(port))
    url = f"http://127.0.0.1:{port}/"
    log.info("界面地址 %s", url)

    bg(app.boot())
    if "--no-window" not in sys.argv:
        open_window(url)
    # Edge 会把窗口转交给别的进程，没法直接等它结束。改成：页面关闭时发 /api/bye，
    # 3 秒内没有新请求（不是刷新）就退出；万一 bye 没发出去，90 秒没动静也退出 ——
    # 但还有任务在下载时不按“没动静”退出（窗口最小化久了，浏览器会把页面的定时器压到一分钟一次甚至冻结）。
    while True:
        await asyncio.sleep(1)
        now = time.time()
        if (app.closed_at and now - app.closed_at > 3) or (now - app.last_seen > 90 and not app.running):
            break

    core.save_index()
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
