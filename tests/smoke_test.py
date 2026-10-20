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
    # 别的片子：验证关键词能跟着指令走，而不是写死「有兽焉」
    {"id": 201, "filename": "斗破苍穹 第3话.mp4", "size": 314572800,
     "size_text": "300 MB", "mime": "video/mp4", "create_time": 1760000090},
    {"id": 202, "filename": "斗破苍穹 第4话.mp4", "size": 314572800,
     "size_text": "300 MB", "mime": "video/mp4", "create_time": 1760000095},
]

# 受密码保护的公共目录：不带对密码时，list 里看不到这些文件
PROTECTED_PASSWORDS = {"dir-pass", "second-pass"}
PROTECTED_VIDEOS = [
    {"id": 77, "filename": "有兽焉 第13话 密码目录.mp4", "size": 209715200,
     "size_text": "200 MB", "mime": "video/mp4", "create_time": 1760000300},
    {"id": 78, "filename": "有兽焉 第14话 另一个密码目录.mp4", "size": 157286400,
     "size_text": "150 MB", "mime": "video/mp4", "create_time": 1760000310},
]

# 公共网盘（api/public.php）：根目录下只有一个带密码的目录
PUBLIC_FOLDERS = [
    {"id": 1, "name": "有兽焉", "parent_id": 0, "has_password": True},
]
PUBLIC_VIDEOS = [
    {"id": 43, "filename": "有兽焉 第13话 貔貅驾到.mp4", "file_size": 81419204,
     "file_size_text": "77.65 MB", "file_type": "mp4", "folder_id": 1,
     "create_time": 1791903341, "preview_sig": "s" * 64,
     "preview_exp": 9999999999},
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
    seen: list = []  # 记录每次请求的 query，便于断言 source 等参数
    guest_blocked: bool = False  # True 时模拟「公共网盘只允许登录用户」

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
        _Handler.seen.append(
            {**{k: v[0] for k, v in query.items()}, "_path": parsed.path}
        )

        if action == "preview":  # 直链本体：给一段可下载的字节流
            body = b"\x00" * 65536
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/public.php":
            self._public(action, query)
            return

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
            pwd = (query.get("folder_password") or [""])[0]
            pool = list(VIDEOS)
            # 受密码保护的目录：只有密码对才看得见里面的文件
            if pwd in PROTECTED_PASSWORDS:
                pool += PROTECTED_VIDEOS
            items = [v for v in pool if keyword in v["filename"]]
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
            pwd = (query.get("folder_password") or [""])[0]
            pool = list(VIDEOS)
            if pwd in PROTECTED_PASSWORDS:
                pool += PROTECTED_VIDEOS
            match = next((v for v in pool if v["id"] == video_id), None)
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

    def _public(self, action: str, query: dict) -> None:
        """模拟站点前端的公共网盘接口：目录带密码，密码不对返回 code 2。"""
        if _Handler.guest_blocked:
            self._json(200, {"code": 401, "msg": "请先登录", "data": None})
            return

        folder_id = int((query.get("folder_id") or ["0"])[0])
        pwd = (query.get("folder_password") or [""])[0]

        if action == "list":
            if folder_id == 0:
                self._json(
                    200,
                    {"code": 0, "msg": "ok", "data": {
                        "folders": PUBLIC_FOLDERS, "list": [], "total": 0,
                        "page": 1, "page_size": 100}},
                )
                return
            if pwd not in PROTECTED_PASSWORDS:
                self._json(200, {"code": 2, "msg": "需要目录密码",
                                 "data": {"folder_id": folder_id}})
                return
            files = [v for v in PUBLIC_VIDEOS if v["folder_id"] == folder_id]
            self._json(
                200,
                {"code": 0, "msg": "ok", "data": {
                    "folders": [], "list": files, "total": len(files),
                    "page": 1, "page_size": 100}},
            )
            return

        self._json(400, {"code": 400, "msg": "未知 action"})

    def log_message(self, *args):
        pass


def _serve() -> tuple:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


# --------------------------------------------------------------- 假事件
class FakeBot:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if not self.ok:
            raise RuntimeError("协议端不支持 upload_group_file")
        return {"file_id": "x"}


class FakeEvent:
    """fail_upload=True 时模拟 QQ highway 上传中途失败。"""

    def __init__(self, text: str, fail_upload: bool = False, bot=None, group_id="10086"):
        self.message_str = text
        self.sent: list = []
        self.fail_upload = fail_upload
        self.bot = bot
        self._group_id = group_id

    def get_group_id(self):
        return self._group_id

    def plain_result(self, text):
        item = ("text", text)
        self.sent.append(item)
        return item

    def chain_result(self, components):
        # 只构造不登记，是否真的发出去由 send() 决定
        return ("chain", components)

    async def send(self, message):
        if self.fail_upload:
            raise RuntimeError(
                "[Highway] httpUpload Error uploading block at offset 54525952: "
                "HTTP Upload failed with code 102902"
            )
        self.sent.append(message)
        return message


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
        "max_send_size_mb": 200,  # 让大部分文件走直发分支
    }
    plugin = plugin_main.YoushouyanPlugin(context=None, config=config)

    print("\n== 片名 + 话数解析 ==")
    from parser import parse_query

    qcases = [
        ("/看 有兽焉第12话", [12], "有兽焉"),
        ("/看 斗破苍穹第5话", [5], "斗破苍穹"),
        ("/看 三体 08", [8], "三体"),
        ("/看 有兽焉第1-3话", [1, 2, 3], "有兽焉"),
        ("/看 第12话", [12], ""),
        ("/找 有兽焉", [], "有兽焉"),
        ("/看 有兽焉 12", [12], "有兽焉"),
        ("有兽焉第6话", [6], "有兽焉"),
    ]
    for text, want_eps, want_kw in qcases:
        try:
            q = parse_query(text, 3)
            got = (q.episodes, q.keyword)
        except Exception as exc:  # noqa: BLE001
            got = (f"异常 {exc}", "")
        ok = got == (want_eps, want_kw)
        if not ok:
            failed += 1
        print(f"  {'✓' if ok else '✗'} {text} → {got}（期望 {want_eps}, {want_kw!r}）")

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
    status_text = _show(event)
    print(status_text)
    if "客厅电视" not in status_text or "永久" not in status_text:
        failed += 1
    if "扫描目录：" in status_text and "上限" in status_text:
        print("  ✓ 自检显示扫描目录数 / max_folders 上限")
    else:
        print(f"  ✗ 自检没显示扫描目录数：{status_text[-160:]}")
        failed += 1
    if "站点：" in status_text and "注册/登录后即可上传" in status_text:
        print("  ✓ 自检为 0 时给出站点地址与注册指引")
    else:
        print(f"  ✗ 自检没提示站点：{status_text[-120:]}")
        failed += 1

    # 站点关掉「游客访问」时，自检要说清原因，而不是干巴巴 0 个视频
    _Handler.guest_blocked = True
    try:
        blocked = plugin_main.YoushouyanPlugin(
            context=None,
            config={**config, "source": "public", "folder_password": "dir-pass"},
        )
        event = FakeEvent("/有兽焉自检")
        async for _ in blocked.status_command(event):
            pass
        blocked_text = _show(event)
        print(f"\n[游客被禁] {blocked_text}")

        event = FakeEvent("/看 有兽焉第13话")
        async for _ in blocked.watch(event):
            pass
        show_blocked = _show(event)
    finally:
        _Handler.guest_blocked = False

    if "只对登录用户开放" in blocked_text and "允许游客访问" in show_blocked:
        print("  ✓ 公共网盘要求登录时，自检与播放都说明原因和解决办法")
    else:
        print(f"  ✗ 登录限制没说明：{show_blocked[-120:]}")
        failed += 1

    print("\n== 公共网盘（api/public.php）==")
    _Handler.seen.clear()
    pub = plugin_main.YoushouyanPlugin(
        context=None,
        config={**config, "source": "public", "folder_password": "dir-pass\nsecond-pass"},
    )
    event = FakeEvent("/看 有兽焉第13话")  # 只存在于公共网盘的受密码目录
    async for _ in pub.watch(event):
        pass
    print(f"  public 输出：{_show(event)[:70]}")

    pub_list = [
        q
        for q in _Handler.seen
        if q.get("_path") == "/api/public.php" and q.get("action") == "list"
    ]
    media_req = [q for q in _Handler.seen if q.get("_path") == "/api/media.php"]
    root_q = [q for q in pub_list if str(q.get("folder_id")) == "0"]
    folder_q = [q for q in pub_list if str(q.get("folder_id")) == "1"]
    out = _show(event)
    if (
        root_q
        and folder_q
        and any(q.get("folder_password") in PROTECTED_PASSWORDS for q in folder_q)
        and not media_req
        and "action=preview" in out
        and "id=43" in out
    ):
        print(
            f"  ✓ 走 public.php：先读根目录，再用配置的密码进入受保护目录，直链带签名"
        )
    else:
        print(
            f"  ✗ 公共网盘链路不对：root={root_q} folder={folder_q} "
            f"media={len(media_req)} out={out[:80]}"
        )
        failed += 1

    # 不给密码时，受保护目录里的文件不应该被看到
    _Handler.seen.clear()
    nopwd = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "source": "public", "folder_password": ""}
    )
    event2 = FakeEvent("/看 有兽焉第13话")
    async for _ in nopwd.watch(event2):
        pass
    if "没有第13话" in _show(event2):
        print("  ✓ 没配密码时看不到受保护目录里的文件")
    else:
        print(f"  ✗ 没密码却也拿到了文件：{_show(event2)[:70]}")
        failed += 1

    # 没有密码时，受保护目录里的文件应该完全看不到
    _Handler.seen.clear()
    nopwd = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "source": "public", "folder_password": ""}
    )
    event = FakeEvent("/看 有兽焉第13话")
    async for _ in nopwd.watch(event):
        pass
    text = _show(event)
    if "没有第13话" in text and "文件夹密码" not in text:
        print("  ✓ 未配置密码时看不到受保护目录的文件")
    elif "没有第13话" in text:
        print("  ✓ 未配置密码时看不到受保护目录的文件（并提示了如何填密码）")
    else:
        print(f"  ✗ 未配置密码却拿到了文件：{text[:80]}")
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

    print("\n== 搜别的片子（关键词跟着指令走）==")
    searcher = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "link_only": True, "max_send_size_mb": 0}
    )

    event = FakeEvent("/找 斗破苍穹")
    async for _ in searcher.find_command(event):
        pass
    out = _show(event)
    if "搜到 2 个" in out and "斗破苍穹 第3话" in out:
        print(f"  ✓ 关键词搜索列出结果：{out[:60]}")
    else:
        print(f"  ✗ 搜索结果不对：{out[:100]}")
        failed += 1

    event = FakeEvent("/看 斗破苍穹第4话")
    async for _ in searcher.watch(event):
        pass
    out = _show(event)
    if "第4话" in out and "🔗" in out and "有兽焉" not in out:
        print(f"  ✓ 换片名也能直接播放：{out[:60]}")
    else:
        print(f"  ✗ 换片名失败：{out[:100]}")
        failed += 1

    event = FakeEvent("/找 斗破苍穹第3话")
    async for _ in searcher.find_command(event):
        pass
    out = _show(event)
    if "🔗" in out and "搜到" not in out:
        print("  ✓ /找 带话数时等同于 /看，直接给直链")
    else:
        print(f"  ✗ /找 带话数没直接播放：{out[:80]}")
        failed += 1

    event = FakeEvent("/找 不存在的片子")
    async for _ in searcher.find_command(event):
        pass
    miss_text = _show(event)
    if "没搜到" in miss_text and "注册/登录后就能上传" in miss_text:
        print("  ✓ 搜不到时给明确提示，并带上站点注册地址")
    elif "没搜到" in miss_text:
        print("  ✓ 搜不到时给明确提示")
    else:
        print(f"  ✗ 搜不到却没提示：{miss_text[:80]}")
        failed += 1

    single = plugin_main.YoushouyanPlugin(
        context=None,
        config={**config, "link_only": True, "max_send_size_mb": 0, "search_limit": 2},
    )
    event = FakeEvent("/找 有兽焉")  # 有 6 个，只显示前 2 个
    async for _ in single.find_command(event):
        pass
    out = _show(event)
    if "只显示前" in out:
        print("  ✓ 命中超过上限时提示缩小范围")
    else:
        print(f"  ✗ 没有截断提示：{out[:80]}")
        failed += 1

    print("\n== 上传失败自动降级 ==")
    uploader = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "max_send_size_mb": 0}
    )
    event = FakeEvent("/看 有兽焉第1话", fail_upload=True)  # 150MB，允许直发但上传失败
    async for _ in uploader.watch(event):
        pass
    out = _show(event)
    chain_sent = any(kind == "chain" for kind, _ in event.sent)
    if "没能直接发出" in out and "🔗" in out and not chain_sent:
        print(f"  ✓ 上传失败后补发直链：{out[:60]}")
    else:
        print(f"  ✗ 上传失败后没有降级：{out[:80]} chain={chain_sent}")
        failed += 1

    learned = uploader._upload_limit_mb or 0
    if 0 < learned < 150:
        print(f"  ✓ 已记住更保守的直发上限：{learned:.0f} MB")
    else:
        print(f"  ✗ 没有收紧直发上限：{learned}")
        failed += 1

    event = FakeEvent("/看 有兽焉第1话", fail_upload=True)  # 同一文件，仍失败
    async for _ in uploader.watch(event):
        pass
    if not any(kind == "chain" for kind, _ in event.sent):
        print("  ✓ 第二次不再尝试直发，直接给链接（不再白等一次上传）")
    else:
        print("  ✗ 记住上限后仍在尝试直发")
        failed += 1

    small = plugin_main.YoushouyanPlugin(
        context=None, config={**config, "max_send_size_mb": 0}
    )
    small._upload_limit_mb = 200.0  # 模拟已学到 200MB 上限
    event = FakeEvent("/看 有兽焉第2话")  # 100MB，在上限内
    async for _ in small.watch(event):
        pass
    if any(kind == "chain" for kind, _ in event.sent):
        print("  ✓ 上限内的小文件照旧直发，不受影响")
    else:
        print(f"  ✗ 小文件也被误伤：{_show(event)[:60]}")
        failed += 1

    print("\n== 群文件上传（超过 50MB 也能真的传上去）==")
    bot = FakeBot(ok=True)
    uploader2 = plugin_main.YoushouyanPlugin(
        context=None,
        config={**config, "source": "public", "folder_password": "dir-pass",
                "max_send_size_mb": 50},
    )
    event = FakeEvent("/看 有兽焉第13话", fail_upload=True, bot=bot)  # 77.65 MB
    async for _ in uploader2.watch(event):
        pass
    out = _show(event)
    if "群文件" in out and any(a == "upload_group_file" for a, _ in bot.calls):
        print(f"  ✓ 视频消息失败后改传群文件：{out[:60]}")
    else:
        print(f"  ✗ 没有走群文件：{out[:80]} calls={bot.calls}")
        failed += 1

    bad_bot = FakeBot(ok=False)
    event = FakeEvent("/看 有兽焉第13话", fail_upload=True, bot=bad_bot)
    async for _ in uploader2.watch(event):
        pass
    out = _show(event)
    if "🔗" in out and "群文件" not in out:
        print("  ✓ 协议端不支持群文件时，退回发直链")
    else:
        print(f"  ✗ 群文件失败后没降级：{out[:80]}")
        failed += 1

    only_link = plugin_main.YoushouyanPlugin(
        context=None,
        config={**config, "source": "public", "folder_password": "dir-pass",
                "upload_mode": "link"},
    )
    event = FakeEvent("/看 有兽焉第13话", bot=FakeBot(ok=True))
    async for _ in only_link.watch(event):
        pass
    if "群文件" not in _show(event) and "🔗" in _show(event):
        print("  ✓ upload_mode=link 时不尝试上传，只发直链")
    else:
        print(f"  ✗ upload_mode=link 仍在上传：{_show(event)[:80]}")
        failed += 1

    print("\n== 命令改配置 ==")

    class SaveableConfig(dict):
        """模拟 AstrBotConfig：dict + save_config() 落盘。"""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.saves = 0

        def save_config(self, *args, **kwargs):
            self.saves += 1

    sconf = SaveableConfig(
        {
            "api_base_url": f"http://127.0.0.1:{port}/api/media.php",
            "api_key": VALID_KEY,
            "keyword": "有兽焉",
            "source": "own",
            "max_send_size_mb": 200,
        }
    )
    setter = plugin_main.YoushouyanPlugin(context=None, config=sconf)

    event = FakeEvent("/有兽焉设置")
    async for _ in setter.config_command(event):
        pass
    out = _show(event)
    if "api_key" in out and "••••" in out and VALID_KEY not in out:
        print("  ✓ /有兽焉设置 列出全部配置，密钥打码")
    else:
        print(f"  ✗ 列配置不对：{out[:120]}")
        failed += 1

    event = FakeEvent("/有兽焉设置 上限 80")
    async for _ in setter.config_command(event):
        pass
    out = _show(event)
    if sconf.get("max_send_size_mb") == 80 and "已修改" in out and sconf.saves == 1:
        print(f"  ✓ 中文别名改数值：max_send_size_mb=80，已落盘")
    else:
        print(f"  ✗ 改数值失败：{sconf.get('max_send_size_mb')} saves={sconf.saves} {out[:80]}")
        failed += 1

    event = FakeEvent("/有兽焉设置 来源 公共")
    async for _ in setter.config_command(event):
        pass
    if sconf.get("source") == "public" and setter.api.source == "public":
        print("  ✓ 枚举认中文：「来源 公共」→ source=public")
    else:
        print(f"  ✗ 枚举没转换：{sconf.get('source')}")
        failed += 1

    event = FakeEvent("/有兽焉设置 上限 abc")
    async for _ in setter.config_command(event):
        pass
    out = _show(event)
    if sconf.get("max_send_size_mb") == 80 and "整数" in out:
        print("  ✓ 非法值被拒且不写坏配置")
    else:
        print(f"  ✗ 非法值没拦住：{sconf.get('max_send_size_mb')} {out[:80]}")
        failed += 1

    event = FakeEvent("/有兽焉设置 上限 默认")
    async for _ in setter.config_command(event):
        pass
    if sconf.get("max_send_size_mb") == 50:
        print("  ✓ 「默认」还原成 schema 默认值 50")
    else:
        print(f"  ✗ 没还原：{sconf.get('max_send_size_mb')}")
        failed += 1

    event = FakeEvent("/有兽焉设置 附加请求头 {\"Referer\": \"https://x.test\"}")
    async for _ in setter.config_command(event):
        pass
    if isinstance(sconf.get("extra_headers"), dict) and sconf["extra_headers"].get("Referer"):
        print("  ✓ JSON 字段（额外请求头）能改")
    else:
        print(f"  ✗ JSON 字段没写入：{sconf.get('extra_headers')}")
        failed += 1

    event = FakeEvent("/有兽焉设置 乱七八糟 xyz")
    async for _ in setter.config_command(event):
        pass
    if "没有" in _show(event):
        print("  ✓ 未知配置项给出提示")
    else:
        print(f"  ✗ 未知项没提示：{_show(event)[:80]}")
        failed += 1

    event = FakeEvent("/有兽焉设置 密钥 " + VALID_KEY)
    async for _ in setter.config_command(event):
        pass
    out = _show(event)
    if "连通性：正常" in out:
        print("  ✓ 改密钥后自动验一次接口连通性")
    else:
        print(f"  ✗ 改密钥没验连通性：{out[:120]}")
        failed += 1

    event = FakeEvent("/有兽焉设置 全部重置")
    async for _ in setter.config_command(event):
        pass
    if sconf.get("api_key") == VALID_KEY and "确认" in _show(event):
        print("  ✓ 全部重置要先确认，没确认不动配置")
    else:
        print(f"  ✗ 没确认就重置了：{sconf.get('api_key')}")
        failed += 1

    event = FakeEvent("/有兽焉设置 全部重置 确认")
    async for _ in setter.config_command(event):
        pass
    if sconf.get("api_key") == "" and sconf.get("max_send_size_mb") == 50:
        print("  ✓ 确认后全部恢复默认")
    else:
        print(f"  ✗ 全部重置没生效：{sconf.get('api_key')!r}")
        failed += 1

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
    if "站点：" not in _show(event) or "注册" not in _show(event):
        print("  ✗ 401 时没告诉用户去站点重新取密钥")
        failed += 1

    bare = plugin_main.YoushouyanPlugin(context=None, config={})
    event = FakeEvent("/看 有兽焉第5话")
    async for _ in bare.watch(event):
        pass
    print(f"未配置：{_show(event)}")
    if "还没配置好" not in _show(event):
        failed += 1
    if "站点：" not in _show(event):
        print("  ✗ 未配置时没给出站点地址")
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
