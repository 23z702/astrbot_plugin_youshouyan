"""有兽焉追番插件。

指令：/看 有兽焉第12话
数据来源：站点媒体接口 /api/media.php（list 检索 + video 取临时直链）
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from typing import AsyncGenerator, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api import AstrBotConfig
except Exception:  # noqa: BLE001 - 兼容不同版本的 AstrBot
    AstrBotConfig = dict  # type: ignore

try:
    from astrbot.api.message_components import Plain, Video
except Exception:  # noqa: BLE001
    Plain = Video = None  # type: ignore

try:  # 正常包方式加载
    from . import settings
    from .api import ApiError, EpisodeResult, MediaAPI, human_size
    from .parser import ParseError, Query, parse_query, strip_command
except ImportError:  # 某些加载器以单文件方式导入
    import settings  # type: ignore
    from api import ApiError, EpisodeResult, MediaAPI, human_size  # type: ignore
    from parser import (  # type: ignore
        ParseError,
        Query,
        parse_query,
        strip_command,
    )

PLUGIN_NAME = "astrbot_plugin_youshouyan"
VERSION = "1.8.0"

# QQ 群视频走 highway 上传，实测超过约 50MB 会在上传中途失败（retcode 1200 / 102902），
# 所以默认阈值取 50MB；真发失败时还会自动收紧并记住。
_DEFAULT_MAX_SEND_MB = 50

_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]')

HELP_TEXT = (
    "🐾 有兽焉追番\n"
    "用法：/看 有兽焉第12话\n"
    "也支持：/看 有兽焉 第12集、/看 有兽焉 12、/看 有兽焉第1-3话\n"
    "换成别的片子也行：/看 斗破苍穹第5话、/看 三体 08\n"
    "找文件：/找 关键词（不带话数就列出匹配的文件）\n"
    "自检：/有兽焉自检\n"
    "改配置：/有兽焉设置（仅管理员）"
)

UNCONFIGURED_TEXT = (
    "⚙️ 有兽焉插件还没配置好\n"
    "请管理员在 AstrBot 管理面板 → 插件 → 有兽焉追番 → 配置 中填写：\n"
    "· 媒体接口地址（https://pan.xn--jhqq4y88zxqsfgb.icu/api/media.php）\n"
    "· API 密钥（pk_ 开头，用户中心 → 视频 API 中创建）"
)

# 站点提示：没配好 / 没内容时都带上，方便自己注册、上传、申请密钥
SITE_TEXT_TEMPLATE = (
    "🌐 网盘站点：{site}\n"
    "   注册/登录后可在网页上传文件；API 密钥在「用户中心 → 视频 API」里创建"
)

_DIGIT_HINT = re.compile(r"\d|[零〇一二两三四五六七八九十百千]")

# 改配置是管理操作：新版本用 ADMIN 过滤器，老版本没有就退化成不限制
_ADMIN_PERMISSION = getattr(getattr(filter, "PermissionType", None), "ADMIN", None)
_PERMISSION_DECORATOR = getattr(filter, "permission_type", None)


def admin_only(func):
    if _ADMIN_PERMISSION is None or _PERMISSION_DECORATOR is None:
        return func
    try:
        return _PERMISSION_DECORATOR(_ADMIN_PERMISSION)(func)
    except Exception:  # noqa: BLE001 - 装饰失败也不该让插件起不来
        return func


# 一次能改/重置的配置项别名（中文口语 → 配置键）在 settings.ALIASES 里维护


def _duration(seconds: int) -> str:
    if seconds >= 3600:
        hours = seconds / 3600
        return f"{hours:.0f} 小时" if hours >= 1.5 else "1 小时"
    return f"{max(1, seconds // 60)} 分钟"


@register(PLUGIN_NAME, "WorkBuddy", "发送 /看 有兽焉第X话 获取剧集视频", VERSION)
class YoushouyanPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self.api = MediaAPI(self.config)
        # 运行期学到的「实际能直发」的上限（MB），None 表示还没失败过
        self._upload_limit_mb: Optional[float] = None
        logger.info(
            f"[有兽焉] 插件已加载（v{VERSION}），接口"
            f"{'已' if self.api.configured else '未'}配置"
        )

    # ------------------------------------------------------------ 配置读取

    def _int_conf(self, key: str, default: int) -> int:
        try:
            value = self.config.get(key, default)
            return default if value is None else int(value)
        except Exception:  # noqa: BLE001
            return default

    def _str_conf(self, key: str, default: str) -> str:
        try:
            value = self.config.get(key, default)
            return default if value is None else str(value).strip()
        except Exception:  # noqa: BLE001
            return default

    def _bool_conf(self, key: str, default: bool) -> bool:
        try:
            value = self.config.get(key, default)
            return default if value is None else bool(value)
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        text = getattr(event, "message_str", "") or ""
        if not text:
            getter = getattr(event, "get_message_str", None)
            if callable(getter):
                try:
                    text = getter() or ""
                except Exception:  # noqa: BLE001
                    text = ""
        return text

    # ------------------------------------------------------------ 消息构造

    @staticmethod
    def _video_chain(result: EpisodeResult) -> Optional[list]:
        if Plain is None or Video is None:
            return None
        component = None
        try:
            component = Video.fromURL(result.content)
        except Exception:  # noqa: BLE001 - 不同版本 API 差异
            try:
                component = Video(url=result.content)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[有兽焉] 构造视频消息失败：{exc}")
                return None
        if component is None:
            return None
        return [Plain(result.headline()), component]

    # ------------------------------------------------------------ 发送

    def _upload_budget_mb(self, max_mb: int) -> float:
        """当前允许直发视频的大小上限（MB）。"""
        limit = float(max_mb) if max_mb > 0 else float("inf")
        if self._upload_limit_mb is not None:
            limit = min(limit, self._upload_limit_mb)
        return limit

    async def _try_send_video(self, event: AstrMessageEvent, result: EpisodeResult) -> bool:
        """尝试直接发送视频。成功返回 True；失败返回 False 由调用方降级发链接。

        注意必须在这里自己 await event.send()，异常才抓得到——
        用 yield 交给框架发的话，上传失败只会在日志里留一条 ERRO，
        用户那边什么都不收到。
        """
        chain = self._video_chain(result)
        if not chain:
            return False
        sender = getattr(event, "send", None)
        if not callable(sender):  # 太老的版本没有 send，退回原行为
            return False
        try:
            await sender(event.chain_result(chain))
        except Exception as exc:  # noqa: BLE001
            size_mb = result.size_bytes / 1024 / 1024 if result.size_bytes else 0
            logger.warning(
                f"[有兽焉] 视频上传失败（{size_mb:.1f} MB）：{exc}"
            )
            # 记住一个更保守的上限，下次同类文件直接走链接，别再白等一次上传
            learned = max(10.0, size_mb * 0.6)
            if self._upload_limit_mb is None or learned < self._upload_limit_mb:
                self._upload_limit_mb = learned
                logger.info(
                    f"[有兽焉] 已把直发上限收紧到 {learned:.0f} MB，"
                    "更大的文件将改为发送直链"
                )
            return False
        return True

    async def _try_group_file(
        self, event: AstrMessageEvent, result: EpisodeResult
    ) -> bool:
        """把视频传到群文件（QQ 群视频消息卡 ~50MB，群文件上限大得多）。

        流程：直链 → 下载到本地临时文件 → upload_group_file → 删掉临时文件。
        任何一步失败都返回 False，让调用方继续降级。
        """
        bot = getattr(event, "bot", None)
        getter = getattr(event, "get_group_id", None)
        if bot is None or not callable(getter):
            return False
        try:
            group_id = getter()
        except Exception:  # noqa: BLE001
            group_id = None
        if not group_id:
            return False  # 私聊没有群文件

        name = _UNSAFE_NAME.sub("_", result.title or f"第{result.episode}话")[:80]
        if not os.path.splitext(name)[1]:
            name += ".mp4"

        workdir = tempfile.mkdtemp(prefix="ysy_upload_")
        path = os.path.join(workdir, name)
        try:
            timeout = self._int_conf("download_timeout", 180)
            await self.api.download(result.content, path, timeout=timeout)
            size_mb = os.path.getsize(path) / 1024 / 1024
            logger.info(f"[有兽焉] 已下载 {size_mb:.1f} MB，上传到群文件…")
            await bot.call_action(
                "upload_group_file",
                group_id=str(group_id),
                file=path,
                name=name,
            )
            logger.info(f"[有兽焉] 群文件上传成功：{name}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[有兽焉] 群文件上传失败：{exc}")
            return False
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    @staticmethod
    def _text_reply(result: EpisodeResult, note: str = "") -> str:
        lines = [result.headline()]
        if result.title:
            lines.append(f"📁 {result.title}")
        if note:
            lines.append(note)
        lines.append(f"🔗 {result.content}")
        if result.expires_in:
            lines.append(
                f"⏳ 直链 {_duration(result.expires_in)}后失效，过期后再发一次指令即可"
            )
        return "\n".join(lines)

    def _site_text(self) -> str:
        return SITE_TEXT_TEMPLATE.format(site=self.api.site_url)

    def _unconfigured_text(self) -> str:
        return f"{UNCONFIGURED_TEXT}\n\n{self._site_text()}"

    def _error_reply(self, episode: int, exc: ApiError) -> str:
        text = f"❌ 第{episode}话获取失败：{exc}"
        message = str(exc)
        if exc.code == 401 and ("登录" in message or "游客" in message):
            return (
                f"{text}\n"
                "💡 站点关掉了公共网盘的游客访问：在站点后台开启「允许游客访问」，"
                "或把文件传到自己的网盘并把来源改成「我的网盘（own）」\n"
                f"{self._site_text()}"
            )
        advice = {
            401: "密钥可能被撤销或过期了，去站点重新创建一个",
            403: "密钥缺少 media 作用域，去站点重新创建",
            429: "接口限流了，等十几分钟再试",
            503: "站点还没执行 install/migrate.php",
        }.get(exc.code)
        if advice:
            text = f"{text}\n💡 {advice}\n{self._site_text()}"
        return text

    # ------------------------------------------------------------ 指令入口

    @filter.command("看", alias={"看有兽焉", "youshouyan", "kan"})
    async def watch(self, event: AstrMessageEvent) -> AsyncGenerator:
        """/看 有兽焉第X话"""
        raw = self._raw_text(event)
        strict = bool(re.match(r"^\s*[/／!！。.]", raw))

        try:
            query: Query = parse_query(raw, self._int_conf("max_range", 3))
        except ParseError as exc:
            body = strip_command(raw)
            if not body:
                yield event.plain_result(HELP_TEXT)
            elif strict or _DIGIT_HINT.search(body):
                yield event.plain_result(f"❌ {exc}\n\n{HELP_TEXT}")
            # 闲聊里顺口带个「看」字，不打扰
            return

        # 指令里没写片名就用配置里的默认关键词；写了就以指令为准
        keyword = query.keyword or self.api.keyword
        episodes = query.episodes

        # /看 关键词：没给话数时当作搜索，但只认 / 开头的，避免闲聊误触
        if not episodes:
            if strict and query.keyword:
                async for item in self._search_reply(event, query.keyword):
                    yield item
                return
            yield event.plain_result(HELP_TEXT)
            return

        async for item in self._play(event, episodes, keyword):
            yield item

    @filter.command("找", alias={"搜索", "搜", "search", "find", "找文件"})
    async def find_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        """/找 关键词 —— 不写话数就列出匹配到的文件。"""
        raw = self._raw_text(event)
        try:
            query: Query = parse_query(raw, self._int_conf("max_range", 3))
        except ParseError:
            yield event.plain_result(HELP_TEXT)
            return

        keyword = query.keyword or self.api.keyword
        if not keyword:
            yield event.plain_result("🔍 想找什么？例如：/找 有兽焉")
            return

        if query.episodes:  # /找 有兽焉 12 等同于 /看 有兽焉 12
            async for item in self._play(event, query.episodes, keyword):
                yield item
            return

        async for item in self._search_reply(event, keyword):
            yield item

    # ------------------------------------------------------------ 配置管理

    @filter.command(
        "有兽焉设置",
        alias={"有兽焉配置", "有兽焉设置项", "ysy设置", "ysy配置"},
    )
    @admin_only
    async def config_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        """/有兽焉设置 —— 在聊天里查看/修改插件配置（仅管理员）。"""
        body = settings.strip_command(self._raw_text(event))
        if not body:
            yield event.plain_result(settings.render_all(self.config))
            return

        parts = body.split(None, 1)
        head = parts[0].strip()
        value_text = parts[1].strip() if len(parts) > 1 else ""

        if head in ("全部重置", "重置全部", "resetall"):
            if value_text not in ("确认", "确定", "confirm", "yes"):
                yield event.plain_result(
                    "⚠️ 全部重置会连接口地址、密钥、目录密码一起恢复默认。\n"
                    "确认真要这么做，发：/有兽焉设置 全部重置 确认"
                )
                return
            async for item in self._reset_all(event):
                yield item
            return

        key = settings.resolve(head)
        if key is None:
            yield event.plain_result(
                f"❌ 没有「{head}」这个配置项\n"
                "发 /有兽焉设置 查看全部配置项；常用名字：\n"
                "接口 / 密钥 / 片名 / 来源 / 密码 / 上限 / 发送方式 / 站点"
            )
            return

        if not value_text:
            yield event.plain_result(settings.describe(key, self.config))
            return

        try:
            value, notes = settings.parse_value(key, value_text)
        except settings.ConfigError as exc:
            yield event.plain_result(
                f"❌ {exc}\n\n{settings.describe(key, self.config)}"
            )
            return

        old = settings.mask(key, settings.value_of(self.config, key))
        self.config[key] = value
        # 改了配置，列表缓存/直链缓存都要失效，否则还会拿旧密钥、旧来源的结果
        self.api.clear_cache()
        if key == "max_send_size_mb":
            self._upload_limit_mb = None  # 上限改了，忘掉之前学到的收紧值

        lines = [f"✅ 已修改 {key}", f"· {old} → {settings.mask(key, value)}"]
        for note in notes:
            lines.append(f"· {note}")
        lines.append(settings.save_config(self.config))

        if key == "max_send_size_mb":
            lines.append("· 顺手清掉了「上传失败后自动收紧」的上限")

        # 改了接口/密钥就顺手验一次，省得等到有人追番才发现填错
        if key in ("api_key", "api_base_url"):
            try:
                data = await self.api.check_key()
                info = data.get("key") if isinstance(data.get("key"), dict) else {}
                lines.append(
                    f"🔎 连通性：正常（密钥「{info.get('name') or '-'}」，"
                    f"作用域 {info.get('scopes') or '-'}）"
                )
            except ApiError as exc:
                lines.append(f"🔎 连通性：失败（{exc}）")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"🔎 连通性：失败（{exc}）")

        yield event.plain_result("\n".join(lines))

    async def _reset_all(self, event: AstrMessageEvent) -> AsyncGenerator:
        """把所有配置恢复默认值。"""
        changed: List[str] = []
        for key in settings.keys():
            default = settings.default_of(key)
            if settings.value_of(self.config, key) != default:
                self.config[key] = default
                changed.append(key)
        self.api.clear_cache()
        self._upload_limit_mb = None

        if not changed:
            yield event.plain_result("✅ 所有配置本来就是默认值，没动它")
            return

        shown = "、".join(changed[:8]) + ("…" if len(changed) > 8 else "")
        yield event.plain_result(
            f"♻️ 已把 {len(changed)} 项恢复默认：{shown}\n"
            f"{settings.save_config(self.config)}\n"
            "⚠️ 接口地址/密钥也回到默认了，如果之前手填过，记得重新设置"
        )

    # ------------------------------------------------------------ 取片与搜索

    async def _play(
        self, event: AstrMessageEvent, episodes: List[int], keyword: str
    ) -> AsyncGenerator:
        """按话数取片并发送。"""
        if not self.api.configured:
            yield event.plain_result(self._unconfigured_text())
            return

        max_episode = self._int_conf("max_episode", 0)
        if max_episode > 0 and keyword in ("", self.api.keyword):
            invalid = [ep for ep in episodes if ep > max_episode]
            if invalid:
                yield event.plain_result(
                    f"❌ 目前共 {max_episode} 话，第{invalid[0]}话还没有哦"
                )
                return

        for index, episode in enumerate(episodes):
            if index:
                await asyncio.sleep(0.6)  # 范围查询稍作间隔，避免触发限流

            try:
                result = await self.api.find_episode(episode, keyword)
            except ApiError as exc:
                logger.warning(f"[有兽焉] 第{episode}话获取失败：{exc}（code={exc.code}）")
                yield event.plain_result(self._error_reply(episode, exc))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[有兽焉] 第{episode}话未预期异常")
                yield event.plain_result(f"❌ 第{episode}话出错了：{exc}")
                continue

            async for item in self._deliver(event, result, episode):
                yield item

    async def _deliver(
        self, event: AstrMessageEvent, result: EpisodeResult, episode: int = 0
    ) -> AsyncGenerator:
        """把一集送到群里：视频消息 → 群文件 → 直链。"""
        link_only = self._bool_conf("link_only", False)
        max_mb = max(0, self._int_conf("max_send_size_mb", _DEFAULT_MAX_SEND_MB))
        mode = self._str_conf("upload_mode", "auto").lower()
        allow_file = mode in ("auto", "group_file")

        budget = self._upload_budget_mb(max_mb)
        too_big = result.size_bytes > 0 and result.size_bytes > budget * 1024 * 1024

        # 1) 先按老办法直接发视频消息（小文件最省事）
        if (
            mode in ("auto", "video")
            and not link_only
            and not too_big
            and Video is not None
        ):
            if await self._try_send_video(event, result):
                return

        # 2) 视频消息发不出去（或文件太大）→ 传到群文件，QQ 内可直接播放
        if allow_file and not link_only:
            if await self._try_group_file(event, result):
                yield event.plain_result(
                    f"📁 第{episode}话已上传到群文件：{result.title or ''}\n"
                    f"{result.headline()}\n"
                    "在群文件里点开就能看，不受视频消息 50MB 的限制"
                )
                return

        # 3) 兜底：发直链
        note = ""
        if too_big:
            note = f"📦 文件超过 {budget:.0f} MB，已改为发送直链，点开即可播放"
        elif not link_only:
            note = "📦 视频没能直接发出（多半是平台上传限制），已改为发送直链"

        yield event.plain_result(self._text_reply(result, note))

    async def _search_reply(self, event: AstrMessageEvent, keyword: str) -> AsyncGenerator:
        """关键词搜索，列出匹配到的文件；只命中一个就直接送上去。"""
        if not self.api.configured:
            yield event.plain_result(self._unconfigured_text())
            return

        limit = self._int_conf("search_limit", 10)
        try:
            hits, total = await self.api.search(keyword, limit)
        except ApiError as exc:
            yield event.plain_result(f"❌ 搜索失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[有兽焉] 搜索未预期异常")
            yield event.plain_result(f"❌ 搜索出错了：{exc}")
            return

        if not hits:
            yield event.plain_result(
                f"🔍 「{keyword}」没搜到文件"
                f"（{self.api.source_label}共 {total} 个视频）"
                f"{self.api.empty_hint()}"
            )
            return

        if len(hits) == 1:
            try:
                result = await self.api.get_play_url(hits[0])
            except ApiError as exc:
                yield event.plain_result(f"❌ 取直链失败：{exc}")
                return
            async for item in self._deliver(event, result):
                yield item
            return

        lines = [f"🔍 「{keyword}」搜到 {len(hits)} 个文件："]
        for item in hits:
            name = str(item.get("filename") or "-")
            try:
                size = int(item.get("size") or item.get("file_size") or 0)
            except (TypeError, ValueError):
                size = 0
            lines.append(f"· {name}" + (f"（{human_size(size)}）" if size else ""))
        if len(hits) == limit:
            lines.append(f"…只显示前 {limit} 个，把关键词写全一点可以缩小范围")
        lines.append(f"直接播放：/看 {keyword}第N话")
        yield event.plain_result("\n".join(lines))

    @filter.command("有兽焉帮助")
    async def help_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        yield event.plain_result(HELP_TEXT)

    @filter.command("有兽焉自检", alias={"有兽焉状态"})
    async def status_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        """调用 action=info 检查密钥与接口连通性。"""
        if not self.api.configured:
            yield event.plain_result(self._unconfigured_text())
            return
        try:
            data = await self.api.check_key()
        except ApiError as exc:
            yield event.plain_result(
                f"❌ 接口自检失败：{exc}\n"
                "💡 确认接口地址填对、密钥没被删除；网络抖动可以稍等再发一次\n"
                f"{self._site_text()}"
            )
            return

        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}

        expire = key.get("expire_time") or 0
        if not expire:
            expire_text = "永久"
        else:
            left = int(expire) - int(time.time())
            expire_text = f"还剩 {_duration(left)}" if left > 0 else "已过期"

        site = self.api.site_url
        passwords = self.api.folder_passwords

        lines = [
            "🔍 有兽焉接口自检",
            f"· 密钥：{key.get('name') or '-'}（{key.get('prefix') or '-'}）",
            f"· 作用域：{key.get('scopes') or '-'}",
            f"· 有效期：{expire_text}",
            f"· 调用次数：{key.get('call_count', '-')}",
            f"· 账号：{user.get('nickname') or user.get('id') or '-'}",
            f"· 站点：{site}",
        ]

        # 两个网盘各查一次，方便判断内容到底在哪边
        counts: dict = {}
        failed_labels: List[str] = []
        failed_reasons: dict = {}
        for source, label in (("own", "我的网盘"), ("public", "公共网盘")):
            try:
                count = await self.api.count_videos(source)
            except ApiError as exc:
                failed_labels.append(label)
                failed_reasons[label] = str(exc)
                lines.append(f"· {label}：查询失败（{exc}）")
                continue
            counts[source] = count
            mark = " ← 当前" if source == self.api.source else ""
            lines.append(f"· {label}：{count} 个视频{mark}")

        # 公共网盘扫描的细枝末节：锁着的目录、读失败的目录
        report = self.api.scan_report or {}
        locked = int(report.get("locked_count") or 0)
        failed_folders = int(report.get("failed_count") or 0)
        if failed_folders:
            reason = str((report.get("failed") or ["接口异常"])[0])
            lines.append(f"   └ ⚠️ {failed_folders} 个目录读取失败：{reason}")
        if locked:
            names = "、".join(str(n) for n in (report.get("locked") or [])[:3])
            state = "密码可能不对" if passwords else "还没配密码"
            lines.append(f"   └ 🔒 {locked} 个带密码的目录（{names}）打不开，{state}")

        empty_labels = [
            label
            for source, label in (("own", "我的网盘"), ("public", "公共网盘"))
            if source in counts and counts[source] == 0
        ]
        empty = bool(empty_labels) or bool(failed_labels)

        lines.append(
            f"· 目录密码：{f'已配置 {len(passwords)} 个' if passwords else '未配置（带锁的目录看不到）'}"
        )

        # 有网盘是空的 / 有查询失败 → 直接把站点甩给用户，让他自己去注册和上传
        if empty or failed_folders:
            if empty_labels:
                lines.append(f"   └ {'、'.join(empty_labels)}里还没有视频（或者这次没读到）")
            need_login = any(
                "登录" in reason or "游客" in reason
                for reason in failed_reasons.values()
            )
            if need_login:
                lines.append(
                    "   └ 公共网盘现在只对登录用户开放；"
                    "在站点后台开启「允许游客访问」，或把来源改成「我的网盘（own）」"
                )
            lines.append(
                f"   └ 到 {site} 注册/登录后即可上传；API 密钥在「用户中心 → 视频 API」创建"
            )
            if self.api.source == "public":
                lines.append("   └ 带密码的目录，把密码填到配置「公共目录密码」里才能看到")
            if failed_labels or failed_folders:
                lines.append("   └ 有查询没成功，隔一会儿再发一次自检会更准")

        budget = self._upload_budget_mb(
            self._int_conf("max_send_size_mb", _DEFAULT_MAX_SEND_MB)
        )
        budget_text = "不限" if budget == float("inf") else f"{budget:.0f} MB"
        if self._upload_limit_mb is not None:
            budget_text += "（上传失败后已自动收紧）"
        lines.append(f"· 直发上限：{budget_text}")

        lines.append(f"· 默认片名：{self.api.keyword or '（空 = 检索全部）'}")

        mode = self._str_conf("upload_mode", "auto")
        mode_text = {
            "auto": "自动（先视频消息，不行再传群文件，最后发直链）",
            "video": "只发视频消息",
            "group_file": "只传群文件",
            "link": "只发直链",
        }.get(mode, mode)
        lines.append(f"· 发送方式：{mode_text}")

        folders = await self.api.public_folders()
        if folders:
            shown = []
            for folder in folders[:5]:
                name = str(folder.get("name") or folder.get("id") or "-")
                shown.append(name + ("🔒" if folder.get("has_password") else ""))
            lines.append(f"· 公共网盘目录：{'、'.join(shown)}")
        elif empty:
            lines.append("· 公共网盘目录：还没有（或接口暂时读不到）")

        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        self.api.clear_cache()
        logger.info("[有兽焉] 插件已卸载")
