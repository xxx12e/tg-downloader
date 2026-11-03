"""测试环境：所有数据目录都指到临时文件夹，不碰真的 data/ 和 downloads/。
不在 Windows 上时给 msvcrt 打个桩（app.py 只在“只开一个实例”的文件锁里用它），这样 Linux 上也能跑大部分测试。"""
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.modules.setdefault("msvcrt", types.ModuleType("msvcrt"))

import core  # noqa: E402


def point_at(base):
    base = Path(base)
    core.DATA = base / "data"
    core.DATA.mkdir(parents=True, exist_ok=True)
    core.CONFIG = base / "config.json"
    core.DEFAULT_OUT = base / "downloads"
    core.SESSION = core.DATA / "tg"
    core.SESSION_ENC = core.DATA / "tg.session.dpapi"
    core.INDEX_FILE = core.DATA / "downloaded.json"


point_at(tempfile.mkdtemp(prefix="tgdl-test-"))  # 导入 app.py 之前就要指好（它导入时会写 data/.token）


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """每个测试一套干净的目录和全局状态。"""
    for name in ("DATA", "CONFIG", "DEFAULT_OUT", "SESSION", "SESSION_ENC", "INDEX_FILE"):
        monkeypatch.setattr(core, name, getattr(core, name))  # 测试结束自动还原
    point_at(tmp_path)
    monkeypatch.setattr(core, "DOWNLOADED", {})
    monkeypatch.setattr(core, "CLAIMED", {})
    monkeypatch.setattr(core, "FILES", {})
    core.cache_clear()
    yield tmp_path
