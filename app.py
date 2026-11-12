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
from telethon import errors, events, utils

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
        self.qr = self.hint = self.error = self.me = self.notice = None
        self.client = None
        self.session = core.active_session()  # 当前用的是哪个号的登录文件
        self.clients = {}  # 登录文件 -> 已连上的客户端。切号不断开，别的号的下载照样跑
        self.adding = None  # 正在加新号时，记着原来那个号，点取消就切回去
        self.boot_task = None
        self.password = None
        self.chats = None
        self.jobs = []
        self.tasks = {}  # job.id -> asyncio.Task
        self.last_seen = time.time()
        self.closed_at = None  # 页面关闭时发来 /api/bye
        self.msgs = {}  # (chat, post, id) -> 浏览时拿到的消息，下载/预览直接用
        self.previews = {}  # 缩略图、头像缓存
        self.preview_sem = asyncio.Semaphore(12)  # 缩略图/头像同时拉几个
        self.outq = []  # 连续发送：排着队等慢速模式到点的消息
        self.outq_tasks = {}  # (号, 聊天, 帖子) -> 发送任务
        self.outq_ids = 0
        self.hooked = set()  # 已经挂上“回复我的”监听的客户端

    # ---- 登录 ----
    def start_boot(self):
        self.boot_task = asyncio.create_task(self.boot())

    async def boot(self):
        s = self.session
        self.stage, self.error, self.qr = "connecting", None, None
        client = self.clients.get(s)
        if client is not None and not client.is_connected():  # 断线了（比如电脑睡眠过）：重新连
            self.clients.pop(s, None)
            client = None
        try:
            if client is None:
                client = await core.connect(core.load_config(), s)
                self.clients[s] = client
            await core.login(client, self.show_qr, self.ask_password)
            me = await client.get_me()
            same = next((a for a in core.accounts() if a["id"] == me.id and a["s"] != s), None)
            if same:  # 扫的是已经登着的号：多出来的这份登录退掉，直接切过去
                await client.log_out()
                self.clients.pop(s, None)
                core.drop_session_files(s)
                self.session, self.adding, self.notice = same["s"], None, "这个号已经登录过了，直接切换过去了"
                return await self.boot()
            self.me = utils.get_display_name(me)
            core.save_account(s, me.id, self.me)
            core.set_active(s)
            self.client, self.adding = client, None
            self.stage = "ready"
            log.info("已登录：%s（%s）", self.me, s)
            asyncio.create_task(self.save_avatar(s, client))
            self.hook_replies(client, self.me)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("登录失败")
            self.stage, self.error = "error", str(e)
            if client:
                self.clients.pop(s, None)
                await client.disconnect()

    # ---- 别人回复我的消息：自动存到本地 ----
    def hook_replies(self, client, me_name):
        if id(client) in self.hooked:
            return
        self.hooked.add(id(client))

        async def on_msg(event):
            m = event.message
            if not m.is_reply or not core.load_config().get("save_replies", True):
                return
            if not m.is_private and not m.mentioned:  # 群里回复我的，Telegram 会标上 mentioned，省得每条都去查
                return
            try:
                mine = await m.get_reply_message()
                if mine and mine.out:
                    await core.save_reply(client, m, mine, me_name)
            except Exception as e:
                log.warning("存回复失败: %r", e)

        client.add_event_handler(on_msg, events.NewMessage(incoming=True))

    # ---- 连续发送：慢速模式的群里一次写好几条，到点自动一条条发 ----
    def queue_add(self, chat, post, text, reply_to, files):
        self.outq_ids += 1
        key = (self.session, chat, post)
        self.outq.append({"id": self.outq_ids, "key": key, "chat": chat, "post": post, "text": text,
                          "reply_to": reply_to, "files": files, "status": "queued", "eta": None, "error": None})
        if key not in self.outq_tasks or self.outq_tasks[key].done():
            self.outq_tasks[key] = asyncio.create_task(self.queue_run(key, self.client))

    async def queue_run(self, key, client):
        _, chat, post = key
        while it := next((x for x in self.outq if x["key"] == key and x["status"] in ("queued", "wait")), None):
            try:
                wait = await core.slowmode_wait(client, chat, post)
                if wait > 0:
                    it["status"], it["eta"] = "wait", time.time() + wait
                    await asyncio.sleep(wait + 1)
                    continue  # 等的时候可能被删了，重新挑一条
                it["status"], it["eta"] = "sending", None
                await core.send(client, chat, text=it["text"], reply_to=it["reply_to"],
                                comment_to=post or None, files=it["files"])
                it["status"], it["files"] = "sent", None
                if self.client is client:
                    self.chats = None
            except (errors.SlowModeWaitError, errors.FloodWaitError) as e:
                it["status"], it["eta"] = "wait", time.time() + e.seconds
                await asyncio.sleep(e.seconds + 1)
            except Exception as e:
                log.exception("排队发送失败")
                it["status"], it["error"], it["files"] = "fail", str(e), None
        # 发完的留 10 分钟给界面看，之后清掉
        cut = time.time() - 600
        self.outq = [x for x in self.outq if x["status"] not in ("sent", "fail") or x.setdefault("done_at", time.time()) > cut]

    async def save_avatar(self, s, client):
        """切号菜单里要显示每个号的头像，没连上的号也要能显示，所以存一份到本地。"""
        try:
            data = await client.download_profile_photo("me", file=bytes)
            f = core.DATA / f"{s}.jpg"
            f.write_bytes(data) if data else f.unlink(missing_ok=True)
        except Exception as e:
            log.info("存头像失败: %r", e)

    async def switch(self, s):
        """换到另一个号（或一个新的空登录文件 = 加号）。"""
        if self.boot_task and not self.boot_task.done():
            self.boot_task.cancel()
            await asyncio.gather(self.boot_task, return_exceptions=True)
        old = self.session
        if old != s and old not in {a["s"] for a in core.accounts()}:
            await self.drop(old)  # 刚才在加的号还没登上就切走了：那份半成品删掉
        self.session = s
        self.client = self.me = self.chats = self.qr = None
        self.msgs.clear()
        self.previews.clear()
        if self.password and not self.password.done():
            self.password.cancel()
        self.start_boot()

    async def drop(self, s):
        if c := self.clients.pop(s, None):
            await c.disconnect()
        core.drop_session_files(s)

    async def add_account(self):
        if self.adding is None:
            self.adding = self.session
        await self.switch(core.new_session())

    async def cancel_add(self):
        back, self.adding = self.adding, None
        if back:
            await self.switch(back)

    async def logout(self):
        """退出当前这个号：Telegram 那边的登录也一起删掉。"""
        s, client = self.session, self.client
        if client:
            for j in self.jobs:
                if j.client is client and not self.tasks[j.id].done():
                    self.tasks[j.id].cancel()
            try:
                done = await client.log_out()  # 成功的话它自己会断开、删登录文件
            except Exception as e:
                log.warning("退出登录出错: %r", e)
                done = False
            self.clients.pop(s, None)
            if not done:
                await client.disconnect()
        core.remove_account(s)
        rest = core.accounts()
        await self.switch(rest[0]["s"] if rest else core.new_session())

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


def under_downloads(path):
    try:
        return core.safe_path(path)
    except PermissionError:
        raise web.HTTPForbidden()


@routes.get("/")
async def index(_):
    html = (WEB / "index.html").read_text("utf-8").replace("{{TOKEN}}", TOKEN)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


# ---- 登录 ----
@routes.get("/api/state")
async def state(_):
    notice, app.notice = app.notice, None
    return web.json_response({"stage": app.stage, "qr": app.qr, "hint": app.hint,
                              "error": app.error, "me": app.me, "notice": notice and core.tr(notice),
                              "session": app.session, "adding": bool(app.adding),
                              "save_replies": core.load_config().get("save_replies", True),
                              "replies_dir": str(core.DEFAULT_OUT / "回复我的"),
                              "accounts": [{**a, "photo": (core.DATA / f"{a['s']}.jpg").exists()}
                                           for a in core.accounts()]})


@routes.post("/api/account")
async def account(request):
    """切号 / 加号 / 取消加号 / 退出当前号。"""
    b = await request.json()
    act = b.get("action")
    if act == "switch":
        if b.get("s") not in {a["s"] for a in core.accounts()}:
            raise web.HTTPNotFound()
        await app.switch(b["s"])
    elif act == "add":
        await app.add_account()
    elif act == "cancel":
        await app.cancel_add()
    elif act == "logout":
        await app.logout()
    else:
        raise web.HTTPBadRequest()
    return ok()


@routes.get("/api/acc_avatar")
async def acc_avatar(request):
    s = request.query.get("s")
    if s not in {a["s"] for a in core.accounts()}:  # 只认登录过的号，防止拿来读别的文件
        raise web.HTTPNotFound()
    f = core.DATA / f"{s}.jpg"
    return image(f.read_bytes() if f.exists() else None)


@routes.post("/api/password")
async def password(request):
    pw = (await request.json()).get("password", "")
    if app.password and not app.password.done():
        app.password.set_result(pw)
    return ok()


@routes.post("/api/retry")
async def retry(_):
    if app.stage == "error":
        app.start_boot()
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
    if q.get("path"):
        try:
            path = under_downloads(q["path"])
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
    if start == 0 and size > 2 * core.CHUNK:
        asyncio.create_task(core.warm_media(app.client, msg.document, size))
    # 浏览器常发 bytes=0- 这种不带结尾的请求，它取一点就会掐断重发；
    # 一次最多给 16 MB，免得白白占着线路传后面用不上的数据
    if r.stop is None and stop - start > 16 * 1024 * 1024:
        stop = start + 16 * 1024 * 1024
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


@routes.post("/api/warm")
async def warm(request):
    """点开视频/音频时先调这个，把开头和结尾并发抓进缓存，省掉播放器来回试探的等待。"""
    try:
        msg = cached_msg(await request.json())
    except web.HTTPNotFound:
        return ok(skipped=True)
    if msg.document and (msg.file.size or 0) > 2 * core.CHUNK:
        asyncio.create_task(core.warm_media(app.client, msg.document, msg.file.size))
    return ok()


@routes.get("/api/avatar")
async def avatar(request):
    peer = int(request.query["peer"])

    async def make():
        entity = await app.client.get_entity(peer)
        return await app.client.download_profile_photo(entity, file=bytes)

    return image(await app.preview(("avatar", peer), make))


# ---- 下载 ----
PICK_PS = """
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'DESC'
$d.ShowNewFolderButton = $true
if (Test-Path 'CUR') { $d.SelectedPath = 'CUR' }
$top = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
if ($d.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
"""


@routes.get("/api/outdir")
async def get_outdir(_):
    """用过的文件夹，第一个是上次用的。"""
    return web.json_response({"dirs": core.recent_dirs()})


@routes.post("/api/outdir")
async def set_outdir(request):
    """pick=1 弹系统的文件夹选择框，选完记下来；否则只是把传来的路径挪到最前面。"""
    body = await request.json()
    path = body.get("dir")
    if body.get("pick"):
        desc = "选择下载文件夹" if core.LANG == "zh" else "Choose a download folder"
        script = PICK_PS.replace("DESC", desc).replace("CUR", str(core.DEFAULT_OUT).replace("'", "''"))
        try:
            r = await asyncio.to_thread(subprocess.run, ["powershell", "-STA", "-NoProfile", "-Command", script],
                                        capture_output=True, text=True, timeout=300)
            path = (r.stdout or "").strip()
        except Exception as e:
            log.warning("选择文件夹失败: %r", e)
            return web.json_response({"error": str(e)}, status=400)
        if not path:
            return ok(cancelled=True, dirs=core.recent_dirs())
    if not path:
        return web.json_response({"error": "没给路径"}, status=400)
    try:
        Path(path).expanduser().mkdir(parents=True, exist_ok=True)
        core.remember_dir(path)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=400)
    return ok(dirs=core.recent_dirs())


@routes.post("/api/send")
async def send_msg(request):
    """发文字或文件。文件走 multipart，文字走 JSON。"""
    if app.stage != "ready":
        return web.json_response({"error": core.tr("还没登录")}, status=400)
    try:
        if request.content_type.startswith("multipart/"):
            data = await request.post()
            up = data.get("file")
            blob = up.file.read() if up is not None else None
            body = {k: data.get(k) for k in ("chat", "post", "text", "reply_to")}
            name = getattr(up, "filename", None)
        else:
            body, blob, name = await request.json(), None, None
        wait = await core.slowmode_wait(app.client, int(body["chat"]), int(body.get("post") or 0))
        if wait > 0:  # 不提前问的话 Telethon 会一声不吭地卡着等，界面看着像死了
            return web.json_response({"error": core.tr("慢速模式：还要等 {t} 才能发。打开 ⏱ 连续发送，到点会自动发",
                                                       t=core.mmss(wait)), "slow": wait}, status=429)
        msg = await core.send(
            app.client, int(body["chat"]), text=body.get("text") or "",
            reply_to=int(body.get("reply_to") or 0) or None,
            comment_to=int(body.get("post") or 0) or None,
            file=blob, name=name)
    except errors.FloodWaitError as e:
        return web.json_response({"error": core.tr("被限流，等 {sec} 秒", sec=e.seconds)}, status=429)
    except Exception as e:
        log.exception("发送失败")
        return web.json_response({"error": core.tr("发不出去：{e}", e=e)}, status=400)
    app.chats = None  # 列表里的“最后一条消息”变了
    return ok(id=msg.id)


@routes.post("/api/queue")
async def queue_add(request):
    """连续发送：把一条（文字 + 可选的几个文件）排进队列。"""
    if app.stage != "ready":
        return web.json_response({"error": core.tr("还没登录")}, status=400)
    if request.content_type.startswith("multipart/"):
        data = await request.post()
        files = [(f.filename, f.file.read()) for f in data.getall("file", []) if hasattr(f, "file")]
        body = {k: data.get(k) for k in ("chat", "post", "text", "reply_to")}
    else:
        body, files = await request.json(), []
    text = (body.get("text") or "").strip()
    if not text and not files:
        return web.json_response({"error": core.tr("消息是空的")}, status=400)
    app.queue_add(int(body["chat"]), int(body.get("post") or 0), text, int(body.get("reply_to") or 0) or None, files)
    return ok()


@routes.get("/api/queue")
async def queue_list(request):
    key = (app.session, int(request.query["chat"]), int(request.query.get("post") or 0))
    now = time.time()
    return web.json_response([{
        "id": x["id"], "text": x["text"], "files": [n for n, _ in (x["files"] or [])], "status": x["status"],
        "left": max(0, x["eta"] - now) if x["eta"] else None, "error": x["error"],
    } for x in app.outq if x["key"] == key])


@routes.post("/api/queue/del")
async def queue_del(request):
    i = (await request.json()).get("id")
    app.outq = [x for x in app.outq if not (x["id"] == i and x["status"] != "sending")]
    return ok()


@routes.post("/api/settings")
async def settings(request):
    b = await request.json()
    cfg = core.load_config()
    if "save_replies" in b:
        cfg["save_replies"] = bool(b["save_replies"])
    core.save_config(cfg)
    return ok()


@routes.post("/api/delete")
async def delete_msg(request):
    b = await request.json()
    try:
        await core.delete_msg(app.client, int(b["chat"]), int(b["id"]), bool(b.get("both", True)))
    except Exception as e:
        return web.json_response({"error": core.tr("发不出去：{e}", e=e)}, status=400)
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

    webapp = web.Application(middlewares=[guard], client_max_size=2 * 1024 ** 3)  # 默认只收 1MB，发文件会被拒
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

    app.start_boot()
    if "--no-window" not in sys.argv:
        open_window(url)
    # Edge 会把窗口转交给别的进程，没法直接等它结束。改成：页面关闭时发 /api/bye，
    # 3 秒内没有新请求（不是刷新）就退出；万一 bye 没发出去，90 秒没动静也退出。
    while True:
        await asyncio.sleep(1)
        now = time.time()
        if (app.closed_at and now - app.closed_at > 3) or now - app.last_seen > 90:
            break

    core.save_index()
    log.info("窗口已关闭，退出")
    app.cancel()
    await asyncio.sleep(0.5)
    try:  # 有连接卡着（窗口卡死、播放到一半）也别一直等，5 秒还没收完就直接退
        await asyncio.wait_for(asyncio.gather(*(c.disconnect() for c in app.clients.values()), runner.cleanup()), 5)
    except Exception as e:
        log.warning("退出时没收拾干净: %r", e)
    os._exit(0)


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
