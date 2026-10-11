"""TG 下载器核心：连接、扫码登录、群组列表、下载任务（含频道评论区）。界面在 app.py。"""
import asyncio
import base64
import io
import json
import logging
import os
import re
import socket
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import qrcode
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.tl.types import MessageMediaWebPage

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
DATA = ROOT / "data"
SESSION = DATA / "tg"
DEFAULT_OUT = ROOT / "downloads"   # 下载根目录，可以在界面里改（存到 config.json 的 download_dir）

# 需要你自己的 api_id / api_hash：https://my.telegram.org → API development tools
# 第一次运行会生成 config.json 模板，填进去即可。
CONFIG_TEMPLATE = {"api_id": 0, "api_hash": "", "proxy": ""}
NEED_API = ("还没填 api_id / api_hash：去 https://my.telegram.org → API development tools 建一个应用，"
            "把两个值填到 config.json 里（已经给你建好模板了），再点重试")
COMMON_PROXY_PORTS = [(7890, "socks5"), (7897, "socks5"), (10808, "socks5"), (10809, "http"), (1080, "socks5")]

KINDS = {  # 类型 -> 子文件夹
    "photo": "图片",
    "video": "视频",
    "gif": "动图",
    "round": "视频消息",
    "audio": "音频",
    "voice": "语音",
    "file": "文件",
    "sticker": "贴纸",
}
DEFAULT_KINDS = [k for k in KINDS if k != "sticker"]
# 下载并行度。实测：一路 1.4 MB/s，4 路 7 MB/s，8 路 14 MB/s，16 路以上忽快忽慢、24 路反而变慢
CHUNK = 512 * 1024        # Telegram 一次最多给 512 KB
LANES_PER_FILE = 8        # 单个文件最多开几路
LANES_TOTAL = 16          # 全局同时最多几路，太多会被限流
STREAM_LANES = 8          # 在线播放最多预取几块（空闲时 8 路约 13 MB/s；下载占满时自动少拿几路，但至少 2 路保证不卡）
JOBS = 8                  # 每个任务同时处理几个文件（真正的限制是上面的总路数）

log = logging.getLogger("tg")



# ---------------- 界面语言 ----------------
LANG = "zh"  # 由界面通过 /api/lang 设置

EN = {
    "完成：新下载 {ok} 个，跳过已有 {skip} 个": "Done: {ok} new, {skip} already had",
    "（其中 {dup} 个是频道重复发的，没重复下）": " ({dup} were reposts, not downloaded again)",
    "，失败 {fail} 个": ", {fail} failed",
    "已停止。下次再下同一个会跳过已下好的文件": "Stopped. Re-running skips whatever is already downloaded",
    "出错：{e}": "Error: {e}",
    "下载全部消息": "Downloading all messages",
    "只下评论区": "Downloading comments only",
    "下载全部帖子 + 评论区": "Downloading all posts + comments",
    "（开了禁止保存，照样下）": " (save-restricted, downloading anyway)",
    "帖子 {id} 的评论读不到：{e}": "Can't read comments on post {id}: {e}",
    "被限流，等 {sec} 秒": "Rate limited, waiting {sec}s",
    "{name} 第 {n} 次失败：{e}": "{name} attempt {n} failed: {e}",
    " · 帖子{post}的评论 · {n} 条": " · comments on post {post} · {n} items",
    " · {n} 条": " · {n} items",
    " · 帖子{post}": " · post {post}",
    "不支持的代理类型: {s}（用 socks5:// 或 http://）": "Unsupported proxy type: {s} (use socks5:// or http://)",
    "连不上 Telegram。打开你的代理软件（Clash/v2rayN 等）再点重试；或在 config.json 里写 \"proxy\": \"socks5://127.0.0.1:端口\"":
        "Can't reach Telegram. Start your proxy (Clash / v2rayN …) and retry, or set \"proxy\": \"socks5://127.0.0.1:PORT\" in config.json",
    "打不开：找不到这个群/频道/用户（可能是私有的，或者你没加入）":
        "Can't open it: no such chat, channel or user (it may be private, or you haven't joined)",
    "已发送加入申请，等管理员同意后再点这个链接": "Join request sent — wait for an admin to approve, then open the link again",
    "邀请链接已失效": "That invite link has expired",
    "你加入的群/频道太多了，先退掉一些": "You're in too many groups/channels — leave some first",
    "加入失败：{e}": "Couldn't join: {e}",
    "已加入，刷新一下左边列表就能看到": "Joined — refresh the chat list to see it",
    "找不到消息 {id}": "Message {id} not found",
    "消息是空的": "Message is empty",
    "发不出去：{e}": "Couldn't send: {e}",
    "这里不能发言": "You can't post here",
    "发送中…": "Sending…",
    "读不到消息：{e}": "Can't read messages: {e}",
    "还没登录": "Not logged in yet",
    "没有要下载的": "Nothing selected to download",
    "消息找不到了，重新打开这个聊天再试": "Those messages are gone — reopen the chat and try again",
    "文件不见了，可能被删了": "File is missing — it may have been deleted",
    "还没填 api_id / api_hash：去 https://my.telegram.org → API development tools 建一个应用，把两个值填到 config.json 里（已经给你建好模板了），再点重试":
        "No api_id / api_hash yet: create an app at https://my.telegram.org → API development tools, put both values in config.json (a template has been created for you), then retry",
}


def tr(s, **kw):
    """s 用中文原文当 key；英文模式下查表，查不到就按原文。"""
    out = EN.get(s, s) if LANG == "en" else s
    return out.format(**kw) if kw else out


# ---------------- 工具函数 ----------------

def safe(name, maxlen=150):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    if len(name) > maxlen:
        stem, ext = os.path.splitext(name)
        name = stem[: maxlen - len(ext)] + ext
    return name or "_"


def kind_of(msg):
    if msg.media is None or isinstance(msg.media, MessageMediaWebPage):
        return None
    if msg.photo:
        return "photo"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "gif"
    if msg.video_note:
        return "round"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.document:
        return "file"
    return None


def file_name(msg, kind):
    f = msg.file
    base = f"{msg.date.astimezone():%Y%m%d}_{msg.id}"
    if f and f.name:
        return safe(f"{base}_{f.name}")
    ext = ".jpg" if kind == "photo" else ((f.ext if f else None) or "")
    return base + ext


def fmt_line(msg, kind, fname, fallback_sender):
    sender = utils.get_display_name(msg.sender) if msg.sender else (msg.post_author or fallback_sender)
    attach = f"  〔{KINDS[kind]}: {fname}〕" if kind else ""
    return f"[{msg.date.astimezone():%Y-%m-%d %H:%M:%S}] {sender}: {msg.message or ''}{attach}\n"


def parse_proxy(s):
    if not s:
        return None
    if "://" not in s:
        s = "socks5://" + s
    u = urlparse(s)
    scheme = {"socks5h": "socks5", "https": "http"}.get(u.scheme.lower(), u.scheme.lower())
    if scheme not in ("socks5", "socks4", "http"):
        raise ValueError(tr("不支持的代理类型: {s}（用 socks5:// 或 http://）", s=u.scheme))
    return {"proxy_type": scheme, "addr": u.hostname, "port": u.port, "rdns": True,
            "username": u.username, "password": u.password}


def parse_target(s):
    """返回 (聊天, 消息ID或None)。"""
    s = s.strip()
    m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/(.+)$", s)
    if m:
        parts = [p for p in m.group(1).split("?")[0].split("/") if p]
        if parts[0].startswith("+") or parts[0] == "joinchat":
            return s, None  # 邀请链接，交给 telethon 解析
        if parts[0] == "s":
            parts = parts[1:]
        if parts[0] == "c":
            chat, rest = int("-100" + parts[1]), parts[2:]
        else:
            chat, rest = parts[0], parts[1:]
        msg_id = int(rest[-1]) if rest and rest[-1].isdigit() else None
        return chat, msg_id
    if re.fullmatch(r"-?\d+", s):
        return int(s), None
    return s.lstrip("@"), None


# ---------------- 连接 & 登录 ----------------

def load_config():
    cfg = json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else {}
    if d := cfg.get("download_dir"):
        apply_download_dir(d)
    return cfg


def apply_download_dir(path):
    """换下载根目录。之前记的文件状态作废，重新打开聊天会按新目录重新判断。"""
    global DEFAULT_OUT
    new = Path(path).expanduser()
    if new == DEFAULT_OUT:
        return DEFAULT_OUT
    new.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUT = new
    FILES.clear()
    return DEFAULT_OUT


def save_download_dir(path):
    cfg = json.loads(CONFIG.read_text("utf-8")) if CONFIG.exists() else {}
    cfg["download_dir"] = str(apply_download_dir(path))
    save_config(cfg)


def save_config(cfg):
    CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def system_proxy():
    """读 Windows 系统代理（Clash 等开了“系统代理”时会写这里）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            if winreg.QueryValueEx(k, "ProxyEnable")[0]:
                server = winreg.QueryValueEx(k, "ProxyServer")[0]
                if "=" in server:  # 形如 http=1.2.3.4:80;https=...
                    server = dict(p.split("=", 1) for p in server.split(";") if "=" in p).get("http", "")
                return f"http://{server}" if server else None
    except OSError:
        pass
    return None


def proxy_candidates(cfg):
    cands = [cfg.get("proxy"), "", system_proxy()]  # 上次测通的排第一
    cands += [f"{t}://127.0.0.1:{p}" for p, t in COMMON_PROXY_PORTS if port_open(p)]
    return list(dict.fromkeys(c for c in cands if c is not None))


async def connect(cfg):
    api_id, api_hash = cfg.get("api_id"), cfg.get("api_hash")
    if not api_id or not api_hash:
        if not CONFIG.exists():
            save_config(CONFIG_TEMPLATE)
        raise ValueError(tr(NEED_API))
    DATA.mkdir(exist_ok=True)
    for proxy in proxy_candidates(cfg):
        log.info("连接 Telegram（%s）", proxy or "直连")
        client = TelegramClient(
            str(SESSION), api_id, api_hash, proxy=parse_proxy(proxy),
            flood_sleep_threshold=900, connection_retries=5, retry_delay=2, request_retries=10, timeout=10,
        )
        try:
            await asyncio.wait_for(client.connect(), 25)
            if cfg.get("proxy") != proxy:
                cfg["proxy"] = proxy
                save_config(cfg)
            return client
        except Exception as e:
            log.warning("连接失败: %r", e)
            await client.disconnect()
    raise ConnectionError(tr("连不上 Telegram。打开你的代理软件（Clash/v2rayN 等）再点重试；"
                             "或在 config.json 里写 \"proxy\": \"socks5://127.0.0.1:端口\""))


def qr_data_url(url):
    buf = io.BytesIO()
    qrcode.make(url, box_size=8, border=2).save(buf)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def login(client, show_qr, ask_password):
    """扫码登录。show_qr(url) 显示二维码；await ask_password(提示) 返回两步验证密码。"""
    if await client.is_user_authorized():
        return
    await _login(client, show_qr, ask_password)
    client.session.save()  # 立刻落盘，进程被强关也不会丢登录


async def _login(client, show_qr, ask_password):
    scanned = asyncio.Event()

    async def on_scanned(_):
        scanned.set()

    client.add_event_handler(on_scanned, events.Raw(types.UpdateLoginToken))
    export = functions.auth.ExportLoginTokenRequest(client.api_id, client.api_hash, [])
    try:
        while True:
            # 每次刷新都重新问一遍：扫过了会直接返回成功，就算漏了“已扫码”通知也不会卡住
            try:
                resp = await client(export)
                if isinstance(resp, types.auth.LoginTokenMigrateTo):  # 账号在别的数据中心
                    await client._switch_dc(resp.dc_id)
                    resp = await client(functions.auth.ImportLoginTokenRequest(resp.token))
            except errors.SessionPasswordNeededError:
                hint = "你开了两步验证，输入密码："
                while True:
                    try:
                        await client.sign_in(password=await ask_password(hint))
                        return
                    except errors.PasswordHashInvalidError:
                        hint = "密码不对，再输一次："
            if isinstance(resp, types.auth.LoginTokenSuccess):
                await client._on_login(resp.authorization.user)
                return
            show_qr("tg://login?token=" + base64.urlsafe_b64encode(resp.token).decode().rstrip("="))
            left = (resp.expires - datetime.now(timezone.utc)).total_seconds()
            scanned.clear()
            try:
                await asyncio.wait_for(scanned.wait(), max(left - 2, 3))
            except asyncio.TimeoutError:
                pass
    finally:
        client.remove_event_handler(on_scanned)


# ---------------- 聊天列表 & 浏览 ----------------

def chat_folder(entity):
    title = utils.get_display_name(entity) or str(entity.id)
    return DEFAULT_OUT / safe(f"{title}_{entity.id}")


def has_photo(e):
    p = getattr(e, "photo", None)
    return p is not None and not isinstance(p, (types.ChatPhotoEmpty, types.UserProfilePhotoEmpty))


def preview_parts(m):
    """返回 (类型标签, 正文)。标签交给界面按语言拼，省得切语言还要重新拉列表。"""
    if m is None:
        return None, ""
    if getattr(m, "action", None):
        return "系统消息", ""
    kind = kind_of(m)
    return (KINDS[kind] if kind else None), (m.message or "").strip()[:80]


async def list_chats(client):
    rows = []
    async for d in client.iter_dialogs():
        e, m = d.entity, d.message
        if d.is_user:
            kind = "收藏夹" if getattr(e, "is_self", False) else ("机器人" if getattr(e, "bot", False) else "私聊")
        else:
            kind = "频道" if d.is_channel and not d.is_group else "群组"
        prefix = ""
        if m and d.is_group and not m.out and m.sender:
            prefix = utils.get_display_name(m.sender) + ": "
        pkind, ptext = preview_parts(m)
        rows.append({
            "id": d.id,
            "name": "收藏夹" if kind == "收藏夹" else d.name,
            "kind": kind,
            "protected": bool(getattr(e, "noforwards", False)),
            "comments": bool(kind == "频道" and getattr(e, "has_link", False)),
            "photo": has_photo(e),
            "preview": prefix + ptext,
            "preview_kind": pkind,
            "date": m.date.astimezone().isoformat() if m else None,
            "unread": d.unread_count,
            "pinned": d.pinned,
            "can_send": can_send(e),
        })
    return rows


async def resolve(client, chat):
    try:
        return await client.get_entity(chat)
    except (ValueError, TypeError):
        await client.get_dialogs()  # 刷一遍缓存再试
        return await client.get_entity(chat)


def pick_thumb(msg, min_w=200):
    """挑一个宽度 >= min_w 的最小缩略图规格，返回规格类型字母；没有缩略图返回 None。"""
    if msg.photo:
        sizes = msg.photo.sizes
    elif msg.document:
        sizes = msg.document.thumbs or []
    else:
        return None
    real = sorted((s for s in sizes if isinstance(s, (types.PhotoSize, types.PhotoSizeProgressive))), key=lambda s: s.w)
    if real:
        return next((s for s in real if s.w >= min_w), real[-1]).type
    stripped = next((s for s in sizes if isinstance(s, types.PhotoStrippedSize)), None)
    return stripped.type if stripped else None


def save_path(out, msg, kind, post=None):
    """文件最终存放位置：帖子/消息按类型分文件夹；评论按帖子分文件夹。"""
    folder = out / "评论区" / f"帖子{post}" if post else out / KINDS[kind]
    return folder / file_name(msg, kind)


def msg_key(msg):
    return f"{msg.chat_id}:{msg.id}"


def file_id(msg):
    """Telegram 给每个文件的编号。同一个文件被转发/重发多次，编号都一样，拿来认重复最准。"""
    d = msg.document or msg.photo
    return str(d.id) if d else None


class FileIndex:
    """记这个聊天里已经下过哪些文件（按 Telegram 文件编号）。同一个文件重复出现就不再下第二遍。"""

    def __init__(self, out):
        self.path = out / ".files.json"
        try:
            self.map = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self.map = {}
        self.claimed = {}  # 本次已排队、还没下完的；扫描跑在下载前面，光看 map 认不出同批的重复
        self.dirty = False

    def hit(self, fid):
        """这个文件之前下过（或本次已经排上队）→ 返回那一份的相对路径。"""
        if not fid:
            return None
        if rel := self.claimed.get(fid):
            return rel
        rel = self.map.get(fid)
        if rel and (DEFAULT_OUT / rel).exists():
            return rel
        if rel:  # 文件被删了，索引作废
            del self.map[fid]
            self.dirty = True
        return None

    def claim(self, fid, rel):
        """排队时先占个位，后面再遇到同一个文件就知道是重复的。"""
        if fid:
            self.claimed[fid] = rel

    def drop(self, fid):
        """这一份没下成，把位子让出来，重复的那份下次还能下。"""
        if fid:
            self.claimed.pop(fid, None)

    def add(self, fid, rel):
        if fid and self.map.get(fid) != rel:
            self.map[fid] = rel
            self.dirty = True

    def save(self):
        if self.dirty:
            self.path.write_text(json.dumps(self.map), "utf-8")
            self.dirty = False


def forward_name(m):
    f = m.forward
    if not f:
        return None
    try:
        who = f.sender or f.chat
        return utils.get_display_name(who) if who else (f.from_name or "隐藏用户")
    except Exception:
        return f.from_name or "转发"


def forward_link(m):
    """“转发自”能点：频道转发跳到原帖，用户转发打开那个人。拿不到来源（隐藏了）返回 None。"""
    f = m.fwd_from if m else None
    p = getattr(f, "from_id", None)
    if isinstance(p, types.PeerChannel):
        return f"tg://privatepost?channel={p.channel_id}" + (f"&post={f.channel_post}" if f.channel_post else "")
    if isinstance(p, types.PeerUser):
        return f"tg://user?id={p.user_id}"
    return None


def entities_of(m):
    """文字里的格式和链接。offset/length 是 UTF-16 单位，和网页里 JS 字符串的下标一致，前端直接切。"""
    out = []
    for e in (m.entities if m else None) or []:
        d = {"t": type(e).__name__.replace("MessageEntity", "").lower(), "o": e.offset, "l": e.length}
        if isinstance(e, types.MessageEntityTextUrl):
            d["url"] = e.url
        elif isinstance(e, types.MessageEntityMentionName):
            d["uid"] = e.user_id
        out.append(d)
    return out


def buttons_of(m):
    """帖子下面的按钮。只有链接类按钮能点（回调按钮要给机器人发消息，这里不做）。"""
    rm = m.reply_markup if m else None
    if not isinstance(rm, types.ReplyInlineMarkup):
        return []
    rows = []
    for row in rm.rows:
        r = []
        for b in row.buttons:
            t = getattr(b, "type", b)  # 新版 layer 把 url 放在 type 里，旧版直接在按钮上
            url = getattr(t, "url", None) or getattr(b, "url", None)
            if not url and getattr(t, "user_id", None):
                url = f"tg://user?id={t.user_id}"
            r.append({"text": b.text, "url": url})
        rows.append(r)
    return rows


def webpage_of(m):
    """链接预览卡片。"""
    if m is None or not isinstance(m.media, MessageMediaWebPage) or not isinstance(m.media.webpage, types.WebPage):
        return None
    w = m.media.webpage
    return {"id": m.id, "url": w.url, "site": w.site_name, "title": w.title,
            "desc": (w.description or "")[:300], "thumb": bool(m.photo and pick_thumb(m))}


async def reply_quotes(client, msgs, in_thread):
    """回复引用：一页只多发一次请求。返回 {消息ID: 引用信息}。"""
    want = {}
    for m in msgs:
        r = m.reply_to
        if not r or not getattr(r, "reply_to_msg_id", None) or getattr(r, "reply_to_peer_id", None):
            continue
        if not r.reply_to_top_id and (in_thread or getattr(r, "forum_topic", False)):
            continue  # 评论区里直接回复原帖 / 话题群里回复话题本身，TG 也不显示引用
        want[m.id] = r.reply_to_msg_id
    if not want:
        return {}
    got = {}
    try:
        for g in await client.get_messages(msgs[0].peer_id, ids=list(set(want.values()))):
            if g:
                qk, qt = preview_parts(g)
                got[g.id] = {"id": g.id, "sender": utils.get_display_name(g.sender) if g.sender else "",
                             "kind": qk, "text": qt}
    except Exception as e:
        log.warning("读回复引用失败: %r", e)
    return {mid: got.get(rid, {"id": rid, "sender": "", "kind": None, "text": tr("消息已删除")})
            for mid, rid in want.items()}


async def browse(client, chat, post=None, offset_id=0, limit=40, asc=False, search=None):
    """一页消息，相册合并成一条。默认新的在前（往前翻），asc=True 时从旧到新。
    post 不为空时列的是这个帖子的评论；search 不为空时是聊天内搜索。返回 (条目, 原始消息, 下一页 offset)。"""
    entity = await resolve(client, chat)
    out = chat_folder(entity)
    msgs = await client.get_messages(entity, limit=limit, offset_id=offset_id, reply_to=post,
                                     reverse=asc, search=search or None)
    quotes = await reply_quotes(client, msgs, bool(post)) if msgs else {}
    groups = []
    for m in msgs:
        if groups and m.grouped_id and groups[-1][0].grouped_id == m.grouped_id:
            groups[-1].append(m)
        else:
            groups.append([m])
    items = []
    for g in groups:
        g.sort(key=lambda m: m.id)
        first = g[0]
        if getattr(first, "action", None):
            continue  # 系统消息（进群、改名之类）
        media = []
        for m in g:
            if not (kind := kind_of(m)):
                continue
            f, path = m.file, save_path(out, m, kind, post)
            media.append({
                "id": m.id, "key": msg_key(m), "fid": file_id(m), "kind": kind, "label": KINDS[kind],
                "name": f.name, "size": f.size or 0, "duration": f.duration, "mime": f.mime_type,
                "w": f.width, "h": f.height, "thumb": bool(pick_thumb(m)),
                "done": path.exists(), "rel": str(path.relative_to(DEFAULT_OUT)),
                "emoji": f.emoji if kind == "sticker" else None,
            })
        tm = next((m for m in g if m.message), None)  # 相册的文字在其中一条上
        web = next((w for m in g if (w := webpage_of(m))), None)
        if not tm and not media and not web:
            continue
        thread = next((m for m in g if m.replies and m.replies.comments), None)
        sender = first.sender
        items.append({
            "ids": [m.id for m in g],
            "gid": str(first.grouped_id) if first.grouped_id else None,  # 字符串：太大的整数 JS 存不准
            "date": first.date.astimezone().isoformat(),
            "sender": utils.get_display_name(sender) if sender else (utils.get_display_name(entity) if first.post else ""),
            "sender_id": first.sender_id,
            "sender_photo": has_photo(sender) if sender else False,
            "author": first.post_author,
            "out": bool(first.out),
            "post": bool(first.post),
            "views": first.views,
            "fwd": forward_name(first),
            "fwd_link": forward_link(first),
            "edited": bool(first.edit_date),
            "text": tm.message if tm else "",
            "ents": entities_of(tm),
            "web": web,
            "buttons": next((b for m in g if (b := buttons_of(m))), []),
            "reply": next((quotes[m.id] for m in g if m.id in quotes), None),
            "media": media,
            "replies": thread.replies.replies if thread else 0,
            "thread": thread.id if thread else None,  # 评论挂在相册里的哪条上
        })
    next_offset = msgs[-1].id if len(msgs) == limit else None
    return items, msgs, next_offset


# ---------------- 链接跳转 ----------------

TG_HOSTS = ("t.me", "telegram.me", "telegram.dog")
TG_RESERVED = {"addstickers", "addemoji", "addtheme", "share", "proxy", "socks", "login", "setlanguage", "iv",
               "bg", "invoice", "contact", "boost", "m", "confirmphone", "addlist", "giftcode", "nft", "$"}


def can_send(e):
    """能不能在这个聊天里发言。频道要有发帖权限，群要没被禁言。"""
    if isinstance(e, types.User):
        return True
    if getattr(e, "left", False) or getattr(e, "deactivated", False):
        return False
    if getattr(e, "creator", False) or getattr(e, "admin_rights", None):
        return True
    if isinstance(e, types.Channel) and e.broadcast:
        return False  # 频道：不是管理员就发不了，但评论区可以（单独判断）
    banned = getattr(e, "banned_rights", None) or getattr(e, "default_banned_rights", None)
    return not (banned and banned.send_messages)


def chat_info(e):
    peer_id = utils.get_peer_id(e)
    if isinstance(e, types.User):
        kind = "收藏夹" if e.is_self else ("机器人" if e.bot else "私聊")
    else:
        kind = "频道" if isinstance(e, types.Channel) and e.broadcast else "群组"
    return {"id": peer_id, "name": "收藏夹" if kind == "收藏夹" else (utils.get_display_name(e) or str(peer_id)),
            "kind": kind, "photo": has_photo(e), "protected": bool(getattr(e, "noforwards", False)),
            "comments": bool(kind == "频道" and getattr(e, "has_link", False)),
            "joined": not getattr(e, "left", False), "can_send": can_send(e)}


def parse_link(url):
    """把 t.me / tg:// / @用户名 链接拆成 (聊天, 消息号, 评论号, 邀请码)；不是 TG 链接返回 None。"""
    u = url.strip()
    if u.startswith("@"):
        u = "tg://resolve?domain=" + u[1:]
    p = urlparse(u if "://" in u else "https://" + u)
    q = parse_qs(p.query)

    def get(k):
        return (q.get(k) or [None])[0]

    def num(s):
        return int(s) if s and s.isdigit() else None

    if p.scheme == "tg":
        host = (p.netloc or p.path).strip("/").lower()
        if host == "resolve" and get("domain"):
            return get("domain"), num(get("post")), num(get("comment")), None
        if host == "privatepost" and num(get("channel")):
            return int("-100" + get("channel")), num(get("post")), num(get("comment")), None
        if host == "user" and num(get("id")):
            return num(get("id")), None, None, None
        if host == "join" and get("invite"):
            return None, None, None, get("invite")
        return None
    host = p.netloc.lower().split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    if host not in TG_HOSTS:
        return None
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if not parts or parts[0].lower() in TG_RESERVED:
        return None
    if parts[0].startswith("+"):
        if parts[0][1:].isdigit():
            return None  # t.me/+86xxxx 是手机号链接，不是邀请链接
        return None, None, None, parts[0][1:]
    if parts[0] == "joinchat" and len(parts) > 1:
        return None, None, None, parts[1]
    if parts[0] == "c" and len(parts) > 1 and parts[1].isdigit():
        chat, rest = int("-100" + parts[1]), parts[2:]
    else:
        chat, rest = parts[0], parts[1:]
    nums = [int(x) for x in rest if x.isdigit()]  # 话题群链接 /c/群/话题/消息：取最后一个
    return chat, (nums[-1] if nums else None), num(get("comment")), None


async def resolve_link(client, url):
    """点链接：TG 链接返回要跳去的聊天和消息；别的链接返回 external。"""
    parsed = parse_link(url)
    if parsed is None:
        return {"external": url}
    chat, msg, comment, invite = parsed
    try:
        if invite:
            r = await client(functions.messages.CheckChatInviteRequest(invite))
            if not isinstance(r, (types.ChatInviteAlready, types.ChatInvitePeek)):
                # 还没加入：把群的信息给前端，弹窗问要不要加入（和 TG 一样，点了“加入”才加）
                return {"invite": {"hash": invite, "title": r.title, "about": (r.about or "")[:300],
                                   "count": r.participants_count, "channel": bool(r.broadcast),
                                   "request": bool(r.request_needed)}}
            entity = r.chat
        else:
            entity = await resolve(client, chat)
    except (ValueError, TypeError, errors.RPCError) as e:
        log.info("链接解析失败 %s: %r", url, e)
        return {"error": tr("打不开：找不到这个群/频道/用户（可能是私有的，或者你没加入）")}
    return {"chat": chat_info(entity), "msg": msg, "comment": comment}


async def join_invite(client, invite):
    """用户在弹窗里点了“加入”。"""
    try:
        upd = await client(functions.messages.ImportChatInviteRequest(invite))
    except errors.UserAlreadyParticipantError:
        return {"chat": chat_info((await client(functions.messages.CheckChatInviteRequest(invite))).chat)}
    except errors.InviteRequestSentError:
        return {"error": tr("已发送加入申请，等管理员同意后再点这个链接")}
    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
        return {"error": tr("邀请链接已失效")}
    except errors.ChannelsTooMuchError:
        return {"error": tr("你加入的群/频道太多了，先退掉一些")}
    except errors.RPCError as e:
        return {"error": tr("加入失败：{e}", e=e)}
    chat = next(iter(getattr(upd, "chats", None) or []), None)
    return {"chat": chat_info(chat)} if chat else {"error": tr("已加入，刷新一下左边列表就能看到")}


async def send(client, chat, text=None, reply_to=None, comment_to=None, file=None, name=None):
    """发消息。comment_to 不为空 = 发到某条帖子的评论区。"""
    entity = await resolve(client, chat)
    kw = dict(reply_to=reply_to or None, comment_to=comment_to or None)
    if file is not None:
        f = io.BytesIO(file)
        f.name = name or "file"
        msg = await client.send_file(entity, f, caption=(text or None), force_document=False, **kw)
    else:
        if not (text or "").strip():
            raise ValueError(tr("消息是空的"))
        msg = await client.send_message(entity, text, link_preview=True, **kw)
    return msg if not isinstance(msg, list) else msg[0]


async def delete_msg(client, chat, msg_id, both=True):
    entity = await resolve(client, chat)
    await client.delete_messages(entity, [msg_id], revoke=both)


async def thumb_bytes(client, msg, min_w=200):
    t = pick_thumb(msg, min_w)
    return await client.download_media(msg, file=bytes, thumb=t) if t else None


async def messages_for_link(client, entity, msg_id):
    msg = await client.get_messages(entity, ids=msg_id)
    if msg is None:
        raise ValueError(tr("找不到消息 {id}", id=msg_id))
    if not msg.grouped_id:  # 不是相册
        return [msg]
    near = await client.get_messages(entity, ids=list(range(msg_id - 10, msg_id + 11)))
    return [m for m in near if m and m.grouped_id == msg.grouped_id]


# ---------------- 下载任务 ----------------

class TextLog:
    """追加写 聊天记录.txt + messages.jsonl，记住写到哪条，续跑不重复。"""

    def __init__(self, out, chat_title):
        self.state_path = out / ".state.json"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {}
        self.last = self.state.get("text_last_id", 0)
        self.chat_title = chat_title
        self.txt = open(out / "聊天记录.txt", "a", encoding="utf-8")
        self.jsonl = open(out / "messages.jsonl", "a", encoding="utf-8")

    def add(self, msg, kind, fname):
        if msg.id <= self.last:
            return
        self.last = msg.id
        if not msg.message and not kind:
            return
        self.txt.write(fmt_line(msg, kind, fname, self.chat_title))
        self.jsonl.write(json.dumps({
            "id": msg.id, "date": msg.date.astimezone().isoformat(), "sender_id": msg.sender_id,
            "text": msg.message or "", "reply_to": msg.reply_to_msg_id, "media": kind, "file": fname,
        }, ensure_ascii=False) + "\n")

    def close(self):
        self.txt.close()
        self.jsonl.close()
        self.state["text_last_id"] = self.last
        self.state_path.write_text(json.dumps(self.state), "utf-8")


FILES = {}  # "chat:msg" -> {"s": queued/loading/done/fail, "got", "size", "rel"}，给界面显示每个文件的状态


class LanePool:
    """全局并行路数预算。要么一次拿够、要么等着，不会几个任务各占一半互相等死。"""

    def __init__(self, total):
        self.total = total
        self.free = total
        self.cond = asyncio.Condition()

    @asynccontextmanager
    async def take(self, want):
        """下载用：拿不够就排队等。"""
        want = max(1, min(want, self.total))
        async with self.cond:
            await self.cond.wait_for(lambda: self.free >= want)
            self.free -= want
        try:
            yield want
        finally:
            async with self.cond:
                self.free += want
                self.cond.notify_all()

    @asynccontextmanager
    async def take_free(self, want, floor=2):
        """在线播放用：有多少空的就拿多少，一路都不等。
        最少也给 floor 路（宁可挤一下下载，也不能让视频卡住）；此时 free 会变成负数，下载那边自然就让出来了。"""
        async with self.cond:
            got = max(floor, min(want, self.free))
            self.free -= got
        try:
            yield got
        finally:
            async with self.cond:
                self.free += got
                self.cond.notify_all()


LANES = LanePool(LANES_TOTAL)


def lanes_for(msg, size):
    """这个文件开几路。小文件开太多路反而慢（光建连接就花光时间）。"""
    if not msg.document or size < 2 * CHUNK:
        return 1
    return max(1, min(LANES_PER_FILE, -(-size // CHUNK)))


async def download_file(client, msg, path, on_progress):
    """下载一个文件到 path。大文件分多路同时拉不同段，比一路顺着下快十倍左右。"""
    size = (msg.file.size if msg.file else 0) or 0
    lanes = lanes_for(msg, size)
    if lanes == 1:
        async with LANES.take(1):
            return await client.download_media(msg, file=str(path), progress_callback=on_progress)
    async with LANES.take(lanes):
        with open(path, "wb") as f:  # 先占好位置，各路按自己的偏移往里写
            f.truncate(size)
        got = 0
        with open(path, "r+b") as f:
            async def lane(i):
                nonlocal got
                pos = i * CHUNK
                if pos >= size:
                    return
                async for chunk in client.iter_download(msg.document, offset=pos, stride=lanes * CHUNK,
                                                        request_size=CHUNK, file_size=size):
                    f.seek(pos)  # seek 和 write 中间没有 await，不会被别的路插进来改位置
                    f.write(chunk)
                    pos += lanes * CHUNK
                    got += len(chunk)
                    on_progress(got, size)

            await asyncio.gather(*(lane(i) for i in range(lanes)))
        if got != size:
            raise RuntimeError(f"只拿到 {got}/{size} 字节")
        return str(path)


async def stream_chunks(client, doc, size, start, stop, lanes=STREAM_LANES):
    """按顺序产出 [start, stop) 这段数据，但同时预取后面几块，起播和拖进度条都快很多。"""
    offsets = list(range(start - start % CHUNK, stop, CHUNK))

    async def fetch(o):
        buf = b""
        async for c in client.iter_download(doc, offset=o, request_size=CHUNK, limit=1, file_size=size):
            buf += c
        return buf

    async with LANES.take_free(lanes) as got:
        flying = deque()  # [(这块的起点, 任务)]
        nxt = 0
        try:
            while nxt < len(offsets) and len(flying) < got:
                flying.append((offsets[nxt], asyncio.create_task(fetch(offsets[nxt]))))
                nxt += 1
            while flying:
                pos, task = flying.popleft()
                data = await task
                if nxt < len(offsets):  # 吐一块就补一块，始终有 got 块在路上
                    flying.append((offsets[nxt], asyncio.create_task(fetch(offsets[nxt]))))
                    nxt += 1
                lo, hi = max(0, start - pos), min(len(data), stop - pos)
                if hi > lo:
                    yield data[lo:hi]
        finally:
            for _, t in flying:
                t.cancel()


class Job:
    """下载一个群/频道/链接。scope: posts=只要帖子, comments=只要评论区, both=都要（评论区只对带评论的频道有效）。
    picked={"post": 帖子ID或0, "msgs": [消息]} 时只下这些手动点的消息（post 不为 0 = 点的是这个帖子下的评论）。
    手动点的不受“文件类型 / 大小上限”限制。"""

    _ids = 0

    def __init__(self, client, target, opts, picked=None):
        Job._ids += 1
        self.id = Job._ids
        self.client = client
        self.target = target
        self.picked = picked
        self.title = str(target)
        self.out = None
        self.status = "排队中"
        self.kinds = set(opts.get("types") or DEFAULT_KINDS)
        mb = opts.get("max_size")
        self.max_bytes = float(mb) * 1024 * 1024 if mb else None
        self.scope = opts.get("scope") or "both"
        self.text = opts.get("text", True)
        self.scan = self.total = self.done = self.ok = self.bytes = self.skip = self.dup = 0
        self.comment_posts = self.comments = 0
        self.index = None  # 查重索引，拿到聊天文件夹后才建
        self.fail = []
        self.active = {}
        self.keys = set()
        self.logs = deque(maxlen=100)
        self.queue = None

    def say(self, s):
        self.logs.append(f"{datetime.now():%H:%M:%S}  {s}")
        log.info("[%s] %s", self.title, s)

    def snapshot(self):
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "out": str(self.out) if self.out else None, "picked": bool(self.picked),
            "scan": self.scan, "total": self.total, "done": self.done, "ok": self.ok, "skip": self.skip,
            "fail": len(self.fail), "dup": self.dup, "bytes": self.bytes + sum(a["got"] for a in self.active.values()),
            "comment_posts": self.comment_posts, "comments": self.comments,
            "active": list(self.active.values()), "logs": list(self.logs)[-30:],
        }

    async def run(self):
        try:
            await self._run()
            self.status = "完成"
            self.say(tr("完成：新下载 {ok} 个，跳过已有 {skip} 个", ok=self.ok, skip=self.skip)
                     + (tr("（其中 {dup} 个是频道重复发的，没重复下）", dup=self.dup) if self.dup else "")
                     + (tr("，失败 {fail} 个", fail=len(self.fail)) if self.fail else ""))
        except asyncio.CancelledError:
            self.status = "已停止"
            self.say(tr("已停止。下次再下同一个会跳过已下好的文件"))
            raise
        except Exception as e:
            self.status = "出错"
            self.say(tr("出错：{e}", e=e))
            log.exception("任务出错")
        finally:
            if self.index:
                self.index.save()
            for k in self.keys:  # 没下完的恢复成“未下载”
                if FILES.get(k, {}).get("s") in ("queued", "loading"):
                    FILES.pop(k, None)
            if self.fail and self.out:
                (self.out / "失败列表.txt").write_text("\n".join(self.fail), "utf-8")

    async def _run(self):
        if isinstance(self.target, str):
            chat, msg_id = parse_target(self.target)
        elif isinstance(self.target, dict):  # {"chat", "msg"}：某条帖子（和它的评论）
            chat, msg_id = self.target["chat"], self.target["msg"]
        else:
            chat, msg_id = self.target, None
        entity = await resolve(self.client, chat)
        self.title = utils.get_display_name(entity) or str(entity.id)
        if msg_id and not self.picked:
            self.title += tr(" · 帖子{post}", post=msg_id)
        self.out = chat_folder(entity)
        self.out.mkdir(parents=True, exist_ok=True)
        self.index = FileIndex(self.out)

        has_comments = isinstance(entity, types.Channel) and entity.broadcast and getattr(entity, "has_link", False)
        scope = self.scope if has_comments else "posts"
        want_posts, want_comments = scope in ("posts", "both"), scope in ("comments", "both")
        post_id = self.picked["post"] if self.picked else 0
        if self.picked:
            n = len({m.grouped_id or m.id for m in self.picked["msgs"]})
            self.title += (tr(" · 帖子{post}的评论 · {n} 条", post=post_id, n=n) if post_id else tr(" · {n} 条", n=n))
        self.status = "下载中"
        if not self.picked:
            self.say(tr({"posts": "下载全部消息", "comments": "只下评论区", "both": "下载全部帖子 + 评论区"}[scope])
                     + (tr("（开了禁止保存，照样下）") if getattr(entity, "noforwards", False) else ""))

        self.queue = asyncio.Queue(maxsize=JOBS * 4)
        workers = [asyncio.create_task(self.worker()) for _ in range(JOBS)]
        try:
            if self.picked:
                seen = set()
                for m in self.picked["msgs"]:
                    self.scan += 1
                    if kind := kind_of(m):
                        await self.enqueue(m, kind, save_path(self.out, m, kind, post_id), force=True)
                    if not post_id and want_comments and m.replies and m.replies.comments and m.replies.replies:
                        if (key := m.grouped_id or m.id) not in seen:
                            seen.add(key)
                            await self.download_comments(entity, m)
            elif msg_id:
                msgs = await messages_for_link(self.client, entity, msg_id)
                for m in msgs:
                    self.scan += 1
                    if want_posts and (kind := kind_of(m)):
                        await self.enqueue(m, kind, save_path(self.out, m, kind))
                if want_comments:
                    post = next((m for m in msgs if m.replies and m.replies.comments), msgs[0])
                    await self.download_comments(entity, post)
            else:
                textlog = TextLog(self.out, self.title) if self.text and want_posts else None
                seen = set()
                try:
                    async for msg in self.client.iter_messages(entity, reverse=True):
                        self.scan += 1
                        kind = kind_of(msg)
                        if want_posts:
                            if textlog:
                                textlog.add(msg, kind, file_name(msg, kind) if kind else None)
                            if kind:
                                await self.enqueue(msg, kind, save_path(self.out, msg, kind))
                        if want_comments and msg.replies and msg.replies.comments and msg.replies.replies:
                            key = msg.grouped_id or msg.id  # 相册只取一次评论
                            if key not in seen:
                                seen.add(key)
                                await self.download_comments(entity, msg)
                finally:
                    if textlog:
                        textlog.close()
            for _ in workers:
                await self.queue.put(None)
            await asyncio.gather(*workers)
        finally:
            for w in workers:
                w.cancel()

    async def download_comments(self, entity, post):
        lines = []
        try:
            async for c in self.client.iter_messages(entity, reply_to=post.id, reverse=True):
                self.scan += 1
                self.comments += 1
                kind = kind_of(c)
                lines.append(fmt_line(c, kind, file_name(c, kind) if kind else None, "匿名"))
                if kind:
                    await self.enqueue(c, kind, save_path(self.out, c, kind, post.id))
        except errors.RPCError as e:
            self.say(tr("帖子 {id} 的评论读不到：{e}", id=post.id, e=e))
            return
        self.comment_posts += 1
        if self.text and lines:
            folder = self.out / "评论区" / f"帖子{post.id}"
            folder.mkdir(parents=True, exist_ok=True)
            head = (f"帖子 {post.id}（{post.date.astimezone():%Y-%m-%d %H:%M}）\n"
                    f"{(post.message or '').strip()[:500]}\n{'-' * 40}\n")
            (folder / "评论.txt").write_text(head + "".join(lines), "utf-8")

    async def enqueue(self, msg, kind, path, force=False):
        if not force:
            if kind not in self.kinds:
                return
            if self.max_bytes and msg.file and (msg.file.size or 0) > self.max_bytes:
                return
        key = msg_key(msg)
        rel = str(path.relative_to(DEFAULT_OUT))
        if path.exists():
            self.skip += 1
            self.index.add(file_id(msg), rel)
            FILES[key] = {"s": "done", "rel": rel}
            return
        if hit := self.index.hit(file_id(msg)):  # 同一个文件之前下过（频道重复发了），指到那一份就行
            self.skip += 1
            self.dup += 1
            FILES[key] = {"s": "done", "rel": hit, "dup": True}
            return
        if FILES.get(key, {}).get("s") in ("queued", "loading"):
            return  # 别的任务正在下这个
        path.parent.mkdir(parents=True, exist_ok=True)
        self.index.claim(file_id(msg), rel)
        self.total += 1
        self.keys.add(key)
        FILES[key] = {"s": "queued", "got": 0, "size": (msg.file.size if msg.file else 0) or 0, "rel": rel}
        await self.queue.put((msg, path))

    async def worker(self):
        while (item := await self.queue.get()) is not None:
            msg, path = item
            key = msg_key(msg)
            state = FILES[key]
            state["s"] = "loading"
            self.active[key] = {"name": path.name, "got": 0, "size": state["size"]}

            def progress(got, size, key=key, state=state):
                if a := self.active.get(key):
                    a["got"], a["size"] = got, size
                    state["got"], state["size"] = got, size

            tmp = path.with_name(path.name + ".part")
            ok = False
            for attempt in range(1, 6):
                try:
                    if await download_file(self.client, msg, tmp, progress) is None:
                        raise RuntimeError("没有拿到文件")
                    os.replace(tmp, path)
                    ok = True
                    break
                except errors.FileReferenceExpiredError:
                    fresh = await self.client.get_messages(msg.peer_id, ids=msg.id)
                    if fresh is None:
                        break
                    msg = fresh
                except errors.FloodWaitError as e:
                    self.say(tr("被限流，等 {sec} 秒", sec=e.seconds))
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    self.say(tr("{name} 第 {n} 次失败：{e}", name=path.name, n=attempt, e=repr(e)))
                    await asyncio.sleep(3 * attempt)
            self.active.pop(key, None)
            self.done += 1
            if ok:
                self.ok += 1
                self.bytes += path.stat().st_size
                state["s"] = "done"
                self.index.add(file_id(msg), state["rel"])
                if self.done % 20 == 0:  # 中途也存一下，万一中断了索引不至于全丢
                    self.index.save()
            else:
                self.index.drop(file_id(msg))
                self.fail.append(str(path.relative_to(self.out)))
                state["s"] = "fail"
