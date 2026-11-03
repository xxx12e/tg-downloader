"""只在 Windows 上有意义的部分：文件夹选择框的输出编码、“来自网络”标记。"""
import base64
import os
import subprocess

import pytest

import app
import core

pytestmark = pytest.mark.skipif(os.name != "nt", reason="只在 Windows 上跑")


def test_picker_output_survives_unicode_paths(tmp_path):
    """选择框脚本最后一行：路径经环境变量进、Base64 出。中文、日文、’ 这种 PowerShell 也当引号的字符都要原样回来。"""
    weird = str(tmp_path / "下载 ダウンロード O’Brien's [1] $x")
    last = app.PICK_PS.strip().splitlines()[-2].strip().replace("$d.SelectedPath", "$env:TGDL_CUR")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", last], capture_output=True, text=True,
                       timeout=60, env={**os.environ, "TGDL_CUR": weird})
    assert base64.b64decode(r.stdout.strip()).decode("utf-8") == weird, r.stderr


def test_mark_of_the_web(tmp_path):
    f = tmp_path / "setup.exe"
    f.write_bytes(b"MZ")
    core.mark_from_internet(f)
    with open(f"{f}:Zone.Identifier", "rb") as z:
        assert b"ZoneId=3" in z.read()
    assert f.read_bytes() == b"MZ"  # 文件本身不变
