"""登录状态加密存盘。DPAPI 只有 Windows 有：这里先用一个可逆的假加密测流程，真 DPAPI 的测试在 Windows 上跑。"""
import os

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

import core

MAGIC = b"FAKE"


def fake_crypt(data, encrypt):
    if encrypt:
        return MAGIC + bytes(b ^ 0x5A for b in data)
    if not data.startswith(MAGIC):
        raise OSError("bad data")
    return bytes(b ^ 0x5A for b in data[len(MAGIC):])


def make_plain_session(key):
    s = SQLiteSession(str(core.SESSION))
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(key)
    s.save()
    s.close()


@pytest.fixture
def crypt(monkeypatch):
    calls = []

    def wrapped(data, encrypt):
        calls.append(encrypt)
        return fake_crypt(data, encrypt)
    monkeypatch.setattr(core, "CRYPT", wrapped)
    return calls


def test_migrates_plaintext_session(crypt):
    key = os.urandom(256)
    make_plain_session(key)
    plain = core.DATA / "tg.session"
    assert plain.exists()
    s = core.open_session()
    assert isinstance(s, core.LockedSession)
    assert s.auth_key.key == key and s.dc_id == 2 and s.server_address == "149.154.167.51"
    assert not plain.exists()                                   # 明文删掉了
    assert key not in core.SESSION_ENC.read_bytes()             # 存盘的不是明文
    again = core.open_session()                                 # 下次启动读加密的
    assert again.auth_key.key == key


def test_save_only_writes_when_changed(crypt):
    s = core.open_session()
    s.set_dc(4, "149.154.167.91", 443)
    s.save()
    assert not core.SESSION_ENC.exists()            # 还没有授权密钥：不写
    s.auth_key = AuthKey(os.urandom(256))
    s.save()
    n = len(crypt)
    for _ in range(5):                              # Telethon 每分钟都会调一次 save()
        s.save()
    assert len(crypt) == n                          # 没变就不再加密写盘
    assert core.open_session().auth_key.key == s.auth_key.key


def test_undecryptable_file_is_kept_aside(crypt):
    core.SESSION_ENC.write_bytes(b"garbage from another PC")
    s = core.open_session()
    assert isinstance(s, core.LockedSession) and s.auth_key is None   # 只能重新扫码
    assert (core.DATA / "tg.session.dpapi.bad").read_bytes() == b"garbage from another PC"  # 不删，留着


def test_falls_back_to_plaintext_when_crypto_broken(monkeypatch):
    def broken(data, encrypt):
        raise OSError("no DPAPI")
    monkeypatch.setattr(core, "CRYPT", broken)
    make_plain_session(os.urandom(256))
    assert core.open_session() == str(core.SESSION)
    assert (core.DATA / "tg.session").exists()       # 加密失败绝不能把明文删了


def test_roundtrip_mismatch_keeps_plaintext(monkeypatch):
    monkeypatch.setattr(core, "CRYPT", lambda data, encrypt: data if encrypt else b"something else")
    make_plain_session(os.urandom(256))
    assert core.open_session() == str(core.SESSION)
    assert (core.DATA / "tg.session").exists() and not core.SESSION_ENC.exists()


def test_non_windows_uses_plain_path(monkeypatch):
    monkeypatch.setattr(core, "CRYPT", None)
    assert core.open_session() == str(core.SESSION)


def test_string_roundtrip_matches_telethon():
    s = StringSession()
    s.set_dc(2, "149.154.167.51", 443)
    s.auth_key = AuthKey(os.urandom(256))
    assert StringSession(s.save()).auth_key.key == s.auth_key.key


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 只有 Windows 有")
def test_real_dpapi_roundtrip():
    data = os.urandom(300)
    blob = core._dpapi(data, True)
    assert blob != data and data not in blob
    assert core._dpapi(blob, False) == data
    with pytest.raises(OSError):
        core._dpapi(blob[:-8] + b"\0" * 8, False)   # 被改过就解不开


@pytest.mark.skipif(os.name != "nt", reason="DPAPI 只有 Windows 有")
def test_real_dpapi_migration():
    assert core.CRYPT is core._dpapi
    key = os.urandom(256)
    make_plain_session(key)
    s = core.open_session()
    assert isinstance(s, core.LockedSession) and s.auth_key.key == key
    assert not (core.DATA / "tg.session").exists()
    assert core.open_session().auth_key.key == key
