"""边下边播的共享取块、多路下载、失败清理。"""
import asyncio
import types

import core


class SlowClient:
    def __init__(self, fail_lane=None):
        self.started = self.finished = 0
        self.fail_lane = fail_lane

    async def iter_download(self, doc, offset=0, request_size=0, limit=None, file_size=None, stride=None):
        self.started += 1
        if stride is not None and offset // core.CHUNK == self.fail_lane:
            await asyncio.sleep(0.05)
            raise RuntimeError("lane broke")
        pos = offset
        while pos < file_size:
            await asyncio.sleep(0.05)
            yield b"x" * min(core.CHUNK, file_size - pos)
            if limit or stride is None:
                break
            pos += stride
        self.finished += 1


def test_cancelling_one_waiter_keeps_shared_chunk():
    """拖进度条时旧请求被取消，不能连带把新请求也在等的同一块掐掉（以前会）。"""
    async def main():
        c, doc = SlowClient(), types.SimpleNamespace(id=1)
        a = asyncio.create_task(core.get_chunk(c, doc, 100, 0))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(core.get_chunk(c, doc, 100, 0))
        await asyncio.sleep(0.01)
        a.cancel()
        assert len(await b) == 100
        assert c.started == 1  # 只取了一遍
    asyncio.run(main())


def test_last_waiter_leaving_cancels_fetch():
    async def main():
        c, doc = SlowClient(), types.SimpleNamespace(id=2)
        t = asyncio.create_task(core.get_chunk(c, doc, 100, 0))
        await asyncio.sleep(0.01)
        t.cancel()
        await asyncio.sleep(0.2)
        assert c.finished == 0 and not core._fetching  # 没人要了就别白下
    asyncio.run(main())


def test_failed_lane_stops_the_others(tmp_path, monkeypatch):
    async def main():
        monkeypatch.setattr(core, "LANES", core.LanePool(core.LANES_TOTAL))
        # 每路 8 块、每块 0.05 秒：第 0 路 0.05 秒就坏了，别的路要是没被停掉，0.4 秒后会跑完
        msg = types.SimpleNamespace(document=types.SimpleNamespace(id=3),
                                    file=types.SimpleNamespace(size=64 * core.CHUNK))
        c = SlowClient(fail_lane=0)
        try:
            await core.download_file(c, msg, tmp_path / "f.part", lambda *a: None)
            raise AssertionError("should have failed")
        except RuntimeError:
            pass
        await asyncio.sleep(0.8)
        assert c.finished == 0                                  # 别的路没有接着往关掉的文件里写
        assert core.LANES.free == core.LANES.total              # 线路都还回去了
    asyncio.run(main())


def test_multi_lane_download_assembles_file(tmp_path, monkeypatch):
    class OrderedClient:
        async def iter_download(self, doc, offset=0, request_size=0, file_size=None, stride=None, **kw):
            pos = offset
            while pos < file_size:
                await asyncio.sleep(0)
                yield bytes([pos // core.CHUNK]) * min(core.CHUNK, file_size - pos)
                pos += stride

    async def main():
        monkeypatch.setattr(core, "LANES", core.LanePool(core.LANES_TOTAL))
        size = 5 * core.CHUNK + 123
        msg = types.SimpleNamespace(document=types.SimpleNamespace(id=4), file=types.SimpleNamespace(size=size))
        p = tmp_path / "f.bin"
        await core.download_file(OrderedClient(), msg, p, lambda *a: None)
        data = p.read_bytes()
        assert len(data) == size
        assert all(data[i * core.CHUNK] == i for i in range(6))
    asyncio.run(main())


def test_failed_download_removes_part_file(tmp_path, monkeypatch):
    async def boom(client, msg, path, progress):
        path.write_bytes(b"x" * 1000)  # 模拟预先占好的空间
        raise RuntimeError("net")

    real_sleep = asyncio.sleep
    monkeypatch.setattr(core, "download_file", boom)
    monkeypatch.setattr(asyncio, "sleep", lambda s: real_sleep(0))

    async def main():
        job = core.Job(None, 1, {})
        job.out, job.queue = tmp_path, asyncio.Queue()
        msg = types.SimpleNamespace(chat_id=1, id=9, document=types.SimpleNamespace(id=9), photo=None, peer_id=1)
        core.FILES["1:9"] = {"s": "queued", "got": 0, "size": 1000}
        await job.queue.put((msg, tmp_path / "a.bin", 9))
        await job.queue.put(None)
        await job.worker()
        assert not (tmp_path / "a.bin.part").exists()
        assert core.FILES["1:9"]["s"] == "fail" and job.fail_anchor == 9
    asyncio.run(main())
