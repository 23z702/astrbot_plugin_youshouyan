"""离线冒烟测试：不依赖 AstrBot，用假 astrbot 模块 + 本地 /api/media.php 跑通指令流程。

用法（项目根目录）：
    python tests/smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

VALID_KEY = "pk_" + "a" * 48

# 模拟站点里的文件（mime / 文件名 / 大小 / 上传时间）
VIDEOS = [
    {"id": 12, "filename": "有兽焉 第12话 1080p.mp4", "size": 734003200,
     "size_text": "700 MB", "mime": "video/mp4", "create_time": 1760000100},
    {"id": 13, "filename": "有兽焉 第12话 预告.mp4", "size": 52428800,
     "size_text": "50 MB", "mime": "video/mp4", "create_time": 1760000200},
    {"id": 1, "filename": "有兽焉 第1话 720p.mp4", "size": 157286400,
     "size_text": "150 MB", "mime": "video/mp4", "create_time": 1760000010},
    {"id": 3, "filename": "有兽焉 第3话 720p.mp4", "size": 209715200,
     "size_text": "200 MB", "mime": "video/mp4", "create_time": 1760000050},
    {"id": 5, "filename": "有兽焉.S01E05.1080p.mkv", "size": 1073741824,
     "size_text": "1.0 GB", "mime": "video/x-matroska", "create_time": 1760000060},
    {"id": 2, "filename": "有兽焉 1080p 第2话.mp4", "size": 104857600,
     "size_text": "100 MB", "mime": "video/mp4", "create_time": 1760000070},
    {"id": 99, "filename": "有兽焉 主题曲.mp3", "size": 10485760,
     "size_text": "10 MB", "mime": "audio/mpeg", "create_time": 1760000080},
]


# --------------------------------------------------------------- 伪造 astrbot
def _install_fake_astrbot() -> None:
    class _Logger:
        def info(self, msg, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def warning(self, msg, *a, **k):
            print(f"    [warn] {msg}")

        def exception(self, msg, *a, **k):
            print(f"    [exc] {msg}")

    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()
    api.AstrBotConfig = dict
    pkg.api = api

    event_mod = types.ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class _Filter:
        @staticmethod
        def command(name, alias=None):
            def deco(func):
                return func

            return deco

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = _Filter()
    api.event = event_mod

    star_mod = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    def register(*args, **kwargs):
        def deco(cls):
            return cls

        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api.star = star_mod

    comp = types.ModuleType("astrbot.api.message_components")

    class Plain:
        def __init__(self, text):
            self.text = text

        def __repr__(self):
            return f"Plain({self.text!r})"

    class Video:
        def __init__(self, url="", **kwargs):
            self.url = url

        @classmethod
        def fromURL(cls, url):
            return cls(url)

        def __repr__(self):
            return f"Video({self.url})"

    comp.Plain, comp.Video = Plain, Video
    api.message_components = comp

    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.star"] = star_mod
    sys.modules["astrbot.api.message_components"] = comp


# --------------------------------------------------------------- 假 media.php
class _Handler(BaseHTTPRequestHandler):
    hits: dict = {}

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        action = (query.get("action") or [""])[0]
        _Handler.hits[action] = _Handler.hits.get(action, 0) + 1

        if parsed.path != "/api/media.php":
            self._json(404, {"code": 404, "msg": "not found"})
            return

        if self.headers.get("X-API-Key") != VALID_KEY:
            self._json(401, {"code": 401, "msg": "缺少或无效的密钥"})
            return

        if action == "info":
            self._json(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "key": {
                            "name": "客厅电视",
                            "prefix": "pk_1a2b3c4d••••",
                            "scopes": "media",
                            "expire_time": 0,
                            "call_count": 12,
                            "last_used": 1760000000,
                        },
                        "user": {"id": 1, "nickname": "站长"},
                        "server_time": 1760000000,
                    },
                },
            )
        elif action == "list":
            keyword = (query.get("q") or [""])[0]
            page = int((query.get("page") or ["1"])[0])
            page_size = int((query.get("page_size") or ["20"])[0])
            items = [v for v in VIDEOS if keyword in v["filename"]]
            start = (page - 1) * page_size
            self._json(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "total": len(items),
                        "page": page,
                        "page_size": page_size,
                        "list": items[start : start + page_size],
                    },
                },
            )
        elif action == "video":
            video_id = int((query.get("id") or ["0"])[0])
            ttl = int((query.get("ttl") or ["3600"])[0])
            match = next((v for v in VIDEOS if v["id"] == video_id), None)
            if not match:
                self._json(404, {"code": 404, "msg": "文件不存在"})
                return
            if not str(match["mime"]).startswith("video/"):
                self._json(415, {"code": 415, "msg": "该文件不是可播放视频"})
                return
            self._json(
                200,
                {
                    "code": 0,
                    "msg": "ok",
                    "data": {
                        "id": video_id,
                        "filename": match["filename"],
                        "size": match["size"],
                        "size_text": match["size_text"],
                        "mime": match["mime"],
                        "url": (
                            f"http://cdn.local/api/files.php?action=preview"
                            f"&id={video_id}&exp=1760003600&sig=deadbeef"
                        ),
                        "expires_in": ttl,
                        "expires_at": 1760003600,
                        "direct_url": "",
                        "range_support": True,
                    },
                },
            )
        else:
            self._json(400, {"code": 400, "msg": "未知 action"})

    def log_message(self, *args):
        pass


def _serve() -> tuple:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --------------------------------------------------------------- 假事件
class FakeEvent:
    def __init__(self, text: str):
        self.message_str = text
        self.sent: list = []

    def plain_result(self, text):
        item = ("text", text)
        self.sent.append(item)
        return item

    def chain_result(self, components):
        item = ("chain", components)
        self.sent.append(item)
        return item


def _show(event: FakeEvent) -> str:
    out = []
    for kind, payload in event.sent:
        if kind == "text":
            out.append(f"[文本] {payload}")
        else:
            out.append(f"[消息链] {payload}")
    return " / ".join(out) or "（无回复）"


async def main() -> int:
    _install_fake_astrbot()
    server, port = _serve()
    import main as plugin_main
    from api import match_episode

    print("== 文件名匹配 ==")
    expect = [
        ("有兽焉 第12话 1080p.mp4", 12, 100),
        ("有兽焉 第12话 预告.mp4", 12, 80),  # 预告扣分，让位给正片
        ("有兽焉.S01E05.1080p.mkv", 5, 90),
        ("有兽焉 1080p 第2话.mp4", 2, 100),
        ("有兽焉 主题曲.mp3", 3, 0),
        ("有兽焉 1080p.mp4", 1080, 0),  # 分辨率不能被当成话数
        ("有兽焉 第120话.mp4", 12, 0),  # 不能被前缀骗到
    ]
    failed = 0
    for name, ep, want in expect:
        got = match_episode(name, ep)
        flag = "✓" if got == want else "✗"
        if got != want:
            failed += 1
        print(f"  {flag} {name} → 第{ep}话 得分 {got}（期望 {want}）")

    config = {
        "api_base_url": f"http://127.0.0.1:{port}/api/media.php",
        "api_key": VALID_KEY,
        "keyword": "有兽焉",
        "play_ttl": 7200,
        "cache_ttl": 60,
        "max_range": 3,
        "timeout": 5,
    }
    plugin = plugin_main.YoushouyanPlugin(context=None, config=config)

    print("\n== 指令流程 ==")
    cases = [
        "/看 有兽焉第12话",
        "/看 有兽焉 第5集",
        "/看 有兽焉第2话",
        "/看 有兽焉第999话",
        "/看 有兽焉 第1-2话",
        "/看 有兽焉",
        "看这个",
    ]
    for text in cases:
        event = FakeEvent(text)
        async for _ in plugin.watch(event):
            pass
        print(f"输入：{text}\n  输出：{_show(event)}\n")

    print("== 自检 ==")
    event = FakeEvent("/有兽焉自检")
    async for _ in plugin.status_command(event):
        pass
    print(_show(event))
    if "客厅电视" not in _show(event) or "永久" not in _show(event):
        failed += 1

    print("\n== 缓存 ==")
    before = dict(_Handler.hits)
    event = FakeEvent("/看 有兽焉第12话")
    async for _ in plugin.watch(event):
        pass
    if _Handler.hits == before:
        print("  ✓ 列表与直链都命中缓存，没有重复请求接口")
    else:
        failed += 1
        print(f"  ✗ 仍然请求了接口：{before} -> {_Handler.hits}")

    print("\n== 异常分支 ==")
    bad_key = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "api_key": "pk_" + "b" * 48}
    )
    event = FakeEvent("/看 有兽焉第12话")
    async for _ in bad_key.watch(event):
        pass
    print(f"错误密钥：{_show(event)}")
    if "401" not in _show(event) and "密钥" not in _show(event):
        failed += 1

    bare = plugin_main.YoushouyanPlugin(context=None, config={})
    event = FakeEvent("/看 有兽焉第5话")
    async for _ in bare.watch(event):
        pass
    print(f"未配置：{_show(event)}")
    if "还没配置好" not in _show(event):
        failed += 1

    limited = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "max_episode": 12}
    )
    event = FakeEvent("/看 有兽焉第90话")
    async for _ in limited.watch(event):
        pass
    print(f"超范围：{_show(event)}")
    if "还没有哦" not in _show(event):
        failed += 1

    server.shutdown()
    print("\n结果：", "全部通过" if failed == 0 else f"{failed} 项失败")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
