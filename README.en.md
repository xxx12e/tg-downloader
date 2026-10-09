# TG Downloader · Telegram Downloader

English · [简体中文](README.md)

A Telegram client that looks and feels like Telegram Desktop — except every photo, video and file has a download button.
**Comments, restricted ("content protected") channels, all downloadable. Up to 10× faster downloads.**

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Windows-0078D4?logo=windows&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## Why this one

Most Telegram download tools are command-line only: you can't see what you're getting, so you bulk-download everything and sift through it afterwards. And almost all of them miss the comment section — which is exactly where a lot of channels put the real content.

This one works the other way around: **browse like you normally do in Telegram, preview anything with one click, then download just what you want.** Or grab an entire channel in one button press.

## Feels like the real Telegram

Chat list on the left, message bubbles on the right — much like the app you use every day:

- Chat list with avatars, last message, unread badges; filter by DM / group / channel / has-comments / restricted
- Message bubbles, album grids, reply quotes, forward headers, link preview cards, inline buttons under posts
- **Links actually work**: `t.me` links jump straight to that channel, message or comment; `@usernames`, `#hashtags` and reply quotes are all clickable
- "💬 N comments" under a channel post opens the comment thread, just like on mobile
- In-chat search; Esc or the mouse back button to go back
- **Bilingual UI** — switch between English and Chinese from the top left (job logs and statuses follow too)

The only difference: a download button in the corner of every file.

## Preview without downloading

Click to preview — no need to fill your disk with things you only wanted to look at:

- **Photos** → full-size viewer, ← → for previous/next
- **Videos, GIFs** → play right away, seekable, streamed while downloading
- **Music, voice messages** → player bar at the top; keeps playing while you browse other chats
- **PDF, txt** → rendered in-window (txt auto-detects UTF-8 / GBK)
- Already-downloaded files play straight from disk, instantly

## What it can download

| | Supported |
|---|---|
| Media in posts | photos, videos, GIFs, documents, audio, voice, video messages, stickers |
| **Channel comments** | a single comment, every comment on one post, or every comment in a channel |
| Albums | merged into one message, downloaded as a group |
| Text | exports `聊天记录.txt` plus `messages.jsonl` for further processing |
| **Restricted / protected channels** | uses the official MTProto API, so the client-side save restriction doesn't apply |
| Entire group / channel | filter by file type and size limit |

From "just this one photo" to "this whole channel plus every comment" — whatever you need.

## Speed: up to 10× faster

A normal download fetches one chunk at a time, so most of the time is spent waiting on network round trips. This splits a large file into 8 parts fetched in parallel:

| Parallel lanes | Speed |
|---|---|
| 1 (the usual approach) | 1.4 MB/s |
| 4 | 7 MB/s |
| **8 (default here)** | **15 MB/s** |
| 24 | 12 MB/s (too many, gets slower) |

- **Streaming is parallel too** — 8 chunks prefetched, first byte in 0.2s, so videos start instantly and seeking is immediate
- **Downloads and playback share one budget**, so watching a video while downloading doesn't stall either one
- **Automatic deduplication**: channels often repost the same files; identical files are detected by Telegram's file ID and stored only once
- Already-downloaded files are skipped, interrupted jobs resume, rate limits are waited out automatically

## Setup

1. Install [Python 3.10+](https://www.python.org/downloads/) (tick *Add Python to PATH*)
2. Go to <https://my.telegram.org> → **API development tools**, create an app, note the `api_id` and `api_hash`
3. Copy `config.example.json` to `config.json` and fill it in:

```json
{
  "api_id": 1234567,
  "api_hash": "your api_hash",
  "proxy": ""
}
```

Leave `proxy` empty for auto-detection (direct connection, system proxy, and the usual Clash / v2rayN ports are all tried).
You can also set it explicitly: `"socks5://127.0.0.1:7890"` or `"http://127.0.0.1:7890"`.

## Running

Double-click **启动.bat** (installs dependencies on first run), or:

```bash
pip install -r requirements.txt
python app.py
```

A QR code appears on first launch — scan it with your phone: **Settings → Devices → Link Desktop Device**. You only log in once. Closing the window exits the program.

## How to download

| What | How |
|---|---|
| A single file | click the **⬇** in its corner |
| Several messages | **选择 (Select)** in the top right → click messages → **下载选中的 (Download selected)** |
| A whole group / channel | **下载全部 (Download all)**, with type and size filters |
| Every comment on a post | open the comment thread → **下载全部评论** |

The **⬇** in the top left is the download manager (progress, speed, per-task stop); **📁** opens the download folder.

## Where files go

```
downloads/<chat name>_<id>/
  图片/ 视频/ 文件/ ...     photos / videos / documents — named 日期_消息ID_原文件名
  评论区/帖子123/           files from that post's comments + 评论.txt
  聊天记录.txt              text log
  messages.jsonl           structured log for further processing
```

## How it works

- [Telethon](https://github.com/LonamiWebs/Telethon) (MTProto) + an aiohttp local server + an Edge / Chrome app-mode window
- Downloads use `iter_download` with `stride` to run several lanes, each writing directly into the same file at its own offset
- Streaming is ordered output with out-of-order prefetch: 8 chunks always in flight, delivered in order, with HTTP Range support for seeking
- Parallelism is managed by a global budget pool — all-or-nothing acquisition, so tasks never deadlock; playback can briefly over-borrow so video never stalls

## Notes

- `data/` holds your login session. **Never share it or commit it** (`.gitignore` already excludes it).
  To revoke: delete `data/tg.session`, then remove the session under **Settings → Devices** in Telegram.
- This only saves content your own account can already see. **Keep what you download to yourself.**
- Check `data/运行日志.txt` if something goes wrong.
- Only tested on Windows.

## License

MIT

---

<sub>Keywords: telegram downloader, telegram media downloader, download restricted telegram content,
telegram protected content saver, save restricted media, telegram no forwards, telegram comments downloader,
telegram channel downloader, telegram bulk download, telegram backup tool, telegram chat export,
telethon, mtproto, telegram gui client, telegram scraper, 电报下载器, TG 下载器</sub>
