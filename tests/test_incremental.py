"""“下载全部”的增量扫描：只扫上次之后的新消息；选项变多/有失败/被停止时的处理。"""
import asyncio
import types
from datetime import datetime, timezone

import pytest

import core


def make_msg(i, photo=False):
    return types.SimpleNamespace(
        id=i, chat_id=100, peer_id=100, date=datetime(2026, 1, 1, tzinfo=timezone.utc), message=f"m{i}",
        media=object() if photo else None, photo=types.SimpleNamespace(id=1000 + i) if photo else None, document=None,
        sticker=None, gif=None, video_note=None, video=None, voice=None, audio=None,
        file=types.SimpleNamespace(name=None, ext=".jpg", size=10) if photo else None,
        grouped_id=None, replies=None, sender=None, post_author=None, sender_id=1, reply_to_msg_id=None)


class FakeClient:
    def __init__(self, n):
        self.msgs = [make_msg(i, photo=(i % 2 == 0)) for i in range(1, n + 1)]
        self.min_ids = []
        self.fail_ids = set()

    async def get_entity(self, chat):
        return types.SimpleNamespace(id=100)

    async def iter_messages(self, entity, reverse=False, min_id=0, reply_to=None):
        self.min_ids.append(min_id)
        for m in self.msgs:
            if m.id > min_id:
                await asyncio.sleep(0)
                yield m

    async def download_media(self, msg, file=None, progress_callback=None):
        if msg.id in self.fail_ids:
            raise RuntimeError("net")
        with open(file, "wb") as f:
            f.write(b"img")
        return file


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))  # 重试等待别真等


def run(client, **opts):
    async def main():
        core.LANES = core.LanePool(core.LANES_TOTAL)
        job = core.Job(client, 100, {"types": ["photo"], **opts})
        await job.run()
        return job
    return asyncio.run(main())


def state(job):
    return core.read_state(job.out)


def test_first_run_full_then_only_new():
    c = FakeClient(5)
    j1 = run(c)
    assert c.min_ids == [0] and j1.ok == 2 and state(j1)["scan"]["last"] == 5
    c.msgs += [make_msg(6, photo=True), make_msg(7)]
    j2 = run(c)
    assert c.min_ids[-1] == 5 and j2.scan == 2 and j2.ok == 1   # 只扫了 6、7
    assert state(j2)["scan"]["last"] == 7
    assert state(j2)["text_last_id"] == 7                       # 文字记录的进度没被覆盖


def test_full_rescan_option():
    c = FakeClient(4)
    run(c)
    run(c, incremental=False)
    assert c.min_ids == [0, 0]


def test_asking_for_more_forces_full_scan():
    c = FakeClient(4)
    run(c)                                   # 只要图片
    run(c, types=["photo", "video"])         # 这次多要视频：老消息里的视频上次没下，必须从头扫
    assert c.min_ids == [0, 0]
    run(c, types=["photo"])                  # 范围又缩回来：被上次（图片+视频）包含，可以增量
    assert c.min_ids[-1] == 4


def test_failure_rewinds_to_earliest_failed_post():
    c = FakeClient(10)
    c.fail_ids = {4, 8}
    j = run(c)
    assert len(j.fail) == 2 and state(j)["scan"]["last"] == 3   # 下次从 4 开始重扫
    c.fail_ids = set()
    j2 = run(c)
    assert c.min_ids[-1] == 3 and j2.ok == 2                    # 4、8 补上了，其余的已有直接跳过
    assert state(j2)["scan"]["last"] == 10


def test_stopped_job_does_not_advance():
    c = FakeClient(6)
    run(c)

    async def main():
        core.LANES = core.LanePool(core.LANES_TOTAL)
        c.msgs += [make_msg(i, photo=True) for i in range(7, 40)]
        job = core.Job(c, 100, {"types": ["photo"]})
        task = asyncio.create_task(job.run())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return job
    j = asyncio.run(main())
    assert state(j)["scan"]["last"] == 6   # 没跑完就不记进度，下次还从 6 往后扫
