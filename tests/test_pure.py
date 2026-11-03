"""不联网、不起服务的纯函数。"""
import os
from pathlib import Path

import pytest

import app
import core


@pytest.mark.parametrize("raw, want", [
    ("a/b\\c:d*e?f\"g<h>i|j", "a_b_c_d_e_f_g_h_i_j"),
    ("../../evil", "_.._evil"),                    # 斜杠没了就穿不出去
    ("x\u202egpj.exe", "xgpj.exe"),               # 方向控制符去掉：不能把 exe 伪装成 jpg
    ("a\x00b\x1fc\x7f", "a_b_c_"),
    ("  name. ", "name"),
    ("", "_"),
])
def test_safe(raw, want):
    assert core.safe(raw) == want


def test_safe_keeps_extension_when_truncating():
    out = core.safe("x" * 300 + ".mp4")
    assert len(out) == 150 and out.endswith(".mp4")


@pytest.mark.parametrize("url, want", [
    ("https://t.me/c/123/45", (-100123, 45, None, None)),
    ("t.me/somechan/12?comment=3", ("somechan", 12, 3, None)),
    ("https://www.t.me/s/somechan/9", ("somechan", 9, None, None)),
    ("https://t.me/+AbCdEf", (None, None, None, "AbCdEf")),
    ("https://t.me/joinchat/AbCdEf", (None, None, None, "AbCdEf")),
    ("https://t.me/+8613800000000", None),           # 手机号链接，不是邀请
    ("https://t.me/addstickers/x", None),
    ("tg://resolve?domain=chan&post=5", ("chan", 5, None, None)),
    ("tg://privatepost?channel=777&post=8&comment=2", (-100777, 8, 2, None)),
    ("@someone", ("someone", None, None, None)),
    ("https://example.com/t.me/x", None),
])
def test_parse_link(url, want):
    assert core.parse_link(url) == want


def test_parse_target():
    assert core.parse_target("https://t.me/c/123/45") == (-100123, 45)
    assert core.parse_target("-100123") == (-100123, None)
    assert core.parse_target("@chan") == ("chan", None)


def test_parse_proxy():
    assert core.parse_proxy("socks5h://u:p@127.0.0.1:7890")["proxy_type"] == "socks5"
    assert core.parse_proxy("https://127.0.0.1:7890")["proxy_type"] == "http"
    assert core.parse_proxy("127.0.0.1:1080")["port"] == 1080
    with pytest.raises(ValueError):
        core.parse_proxy("ftp://127.0.0.1:21")


def test_hide_secret():
    assert core.hide_secret("socks5://user:pw@127.0.0.1:7890") == "socks5://***@127.0.0.1:7890"
    assert core.hide_secret("user:pw@127.0.0.1:7890") == "***@127.0.0.1:7890"
    assert core.hide_secret("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert core.hide_secret("") == ""


@pytest.mark.parametrize("mime, ctype, sandbox", [
    ("video/mp4", "video/mp4", True),
    ("video/quicktime", "video/mp4", True),          # mov 按 mp4 放
    ("image/svg+xml", "image/svg+xml", True),        # 能显示，但被直接打开时脚本跑不起来
    ("application/pdf", "application/pdf", False),   # 加了 sandbox 浏览器就不显示 PDF
    ("text/html", "text/plain", True),
    ("application/xhtml+xml", "application/octet-stream", True),
    ("x\"y", "application/octet-stream", True),
    (None, "application/octet-stream", True),
])
def test_stream_headers(mime, ctype, sandbox):
    h = app.stream_headers(mime)
    assert h["Content-Type"] == ctype
    assert (h.get("Content-Security-Policy") == "sandbox") is sandbox


def test_recent_dirs_always_keep_default():
    core.save_config({"recent_dirs": [f"/tmp/tgdl-d{i}" for i in range(8)]})
    dirs = core.recent_dirs()
    assert len(dirs) == 8 and str(core.DEFAULT_OUT) in dirs  # 用满 8 个以后默认文件夹也不能被挤掉


def test_allowed_dir(tmp_path):
    picked = tmp_path / "picked"
    core.save_config({"recent_dirs": [str(picked)]})
    assert core.allowed_dir(picked / "chat_1" / "a.jpg")
    assert core.allowed_dir(core.DEFAULT_OUT / "x")
    assert not core.allowed_dir(tmp_path / "elsewhere" / "secret.txt")
    assert not core.allowed_dir(picked.parent / (picked.name + "2") / "a")  # 名字前缀相同不算
    # 文件夹被挤出列表以后，自己下过的文件还要能打开
    old = tmp_path / "dropped" / "chat_1" / "图片" / "a.jpg"
    core.DOWNLOADED["f1"] = str(old)
    assert core.allowed_dir(old)
    assert core.allowed_dir(old.parent)
    assert not core.allowed_dir(old.parent / "other.jpg")


def test_is_known_dir(tmp_path):
    core.save_config({"recent_dirs": [str(tmp_path / "a")]})
    assert core.is_known_dir(tmp_path / "a")
    assert core.is_known_dir(core.DEFAULT_OUT)
    assert not core.is_known_dir(tmp_path / "a" / "sub")
    assert not core.is_known_dir(tmp_path / "b")


def test_write_atomic_leaves_no_temp(tmp_path):
    d = tmp_path / "w"
    d.mkdir()
    p = d / "x.json"
    core.write_atomic(p, "1")
    core.write_atomic(p, b"2")
    assert p.read_bytes() == b"2" and os.listdir(d) == ["x.json"]


def test_state_keys_do_not_clobber_each_other(tmp_path):
    core.update_state(tmp_path, text_last_id=5)
    core.update_state(tmp_path, scan={"last": 9})
    assert core.read_state(tmp_path) == {"text_last_id": 5, "scan": {"last": 9}}
    (tmp_path / ".state.json").write_text("{broken")
    assert core.read_state(tmp_path) == {}


BASE = {"kinds": ["photo", "video"], "max": None, "posts": True, "comments": True, "text": True}


@pytest.mark.parametrize("cur, covered", [
    (BASE, True),
    ({**BASE, "kinds": ["photo"]}, True),                 # 要的更少：可以增量
    ({**BASE, "kinds": ["photo", "file"]}, False),        # 多了一种类型：老消息里的文件上次没下
    ({**BASE, "max": 100.0}, True),
    ({**BASE, "comments": False}, True),
])
def test_scan_covers(cur, covered):
    assert core.scan_covers(BASE, cur) is covered


def test_scan_covers_size_limit():
    prev = {**BASE, "max": 100.0}
    assert core.scan_covers(prev, {**BASE, "max": 50.0})
    assert not core.scan_covers(prev, {**BASE, "max": 200.0})
    assert not core.scan_covers(prev, BASE)               # 这次不限大小，上次限了
    assert not core.scan_covers({**BASE, "text": False}, BASE)
    assert not core.scan_covers(None, BASE)


def test_risky_ext_includes_python():
    # 用这个程序就一定装了 Python，.py 双击就会运行
    assert {".exe", ".bat", ".py", ".pyw", ".lnk", ".hta", ".docm"} <= core.RISKY_EXT
    assert not {".jpg", ".mp4", ".pdf", ".txt"} & core.RISKY_EXT


def test_launcher_is_valid_windows_batch():
    """启动.bat 要 CRLF（cmd 在纯 LF 的批处理里找 goto 标签会出错），只能往 NUL 重定向（写成 /dev/null 在 Windows 上
    会“找不到路径”，命令直接不执行 —— 真出过：依赖整个没装）。"""
    data = (Path(core.ROOT) / "启动.bat").read_bytes()
    assert b"/dev/null" not in data
    assert data.count(b"\n") == data.count(b"\r\n")
    assert b"--require-hashes -r requirements.lock" in data and b"--only-binary=:all:" in data


def test_page_has_no_inline_handlers():
    """CSP 不放行内联脚本：页面里不能再有 onclick= 这种，否则按钮会失效。"""
    import re
    html = (Path(core.ROOT) / "web" / "index.html").read_text("utf-8")
    assert not re.findall(r"\son[a-z]+\s*=\s*[\"'`]", html)
    assert html.count("<script") == 1 and '<script nonce="{{NONCE}}">' in html
