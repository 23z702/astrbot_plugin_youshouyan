"""有兽焉追番插件。

指令：/看 有兽焉第12话
数据来源：站点媒体接口 /api/media.php（list 检索 + video 取临时直链）
"""

from __future__ import annotations

import asyncio
import re
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
    from .api import ApiError, EpisodeResult, MediaAPI
    from .parser import ParseError, parse_request, strip_command
except ImportError:  # 某些加载器以单文件方式导入
    from api import ApiError, EpisodeResult, MediaAPI  # type: ignore
    from parser import ParseError, parse_request, strip_command  # type: ignore

PLUGIN_NAME = "astrbot_plugin_youshouyan"
VERSION = "1.1.0"

HELP_TEXT = (
    "🐾 有兽焉追番\n"
    "用法：/看 有兽焉第12话\n"
    "也支持：/看 有兽焉 第12集、/看 有兽焉 12、/看 有兽焉第1-3话\n"
    "自检：/有兽焉自检"
)

UNCONFIGURED_TEXT = (
    "⚙️ 有兽焉插件还没配置好\n"
    "请管理员在 AstrBot 管理面板 → 插件 → 有兽焉追番 → 配置 中填写：\n"
    "· 媒体接口地址（https://你的域名/api/media.php）\n"
    "· API 密钥（pk_ 开头，用户中心 → 视频 API 中创建）"
)

_DIGIT_HINT = re.compile(r"\d|[零〇一二两三四五六七八九十百千]")

_DEFAULT_ENDPOINT = "https://你的域名/api/media.php"


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

    @staticmethod
    def _error_reply(episode: int, exc: ApiError) -> str:
        advice = {
            401: "请在插件配置里更新 API 密钥（可能被撤销或过期了）",
            403: "该密钥缺少 media 作用域，请在用户中心重新创建",
            429: "接口限流了，等十几分钟再试",
            503: "站点还没执行 install/migrate.php",
        }.get(exc.code)
        text = f"❌ 第{episode}话获取失败：{exc}"
        return f"{text}\n💡 {advice}" if advice else text

    # ------------------------------------------------------------ 指令入口

    @filter.command("看", alias={"看有兽焉", "youshouyan", "kan"})
    async def watch(self, event: AstrMessageEvent) -> AsyncGenerator:
        """/看 有兽焉第X话"""
        raw = self._raw_text(event)
        strict = bool(re.match(r"^\s*[/／!！。.]", raw))

        try:
            episodes: List[int] = parse_request(raw, self._int_conf("max_range", 3))
        except ParseError as exc:
            body = strip_command(raw)
            if not body:
                yield event.plain_result(HELP_TEXT)
            elif strict or _DIGIT_HINT.search(body):
                yield event.plain_result(f"❌ {exc}\n\n{HELP_TEXT}")
            # 闲聊里顺口带个「看」字，不打扰
            return

        max_episode = self._int_conf("max_episode", 0)
        if max_episode > 0:
            invalid = [ep for ep in episodes if ep > max_episode]
            if invalid:
                yield event.plain_result(
                    f"❌ 目前共 {max_episode} 话，第{invalid[0]}话还没有哦"
                )
                return

        if not self.api.configured:
            yield event.plain_result(UNCONFIGURED_TEXT)
            return

        link_only = self._bool_conf("link_only", False)
        max_mb = self._int_conf("max_send_size_mb", 100)

        for index, episode in enumerate(episodes):
            if index:
                await asyncio.sleep(0.6)  # 范围查询稍作间隔，避免触发限流

            try:
                result = await self.api.find_episode(episode)
            except ApiError as exc:
                logger.warning(f"[有兽焉] 第{episode}话获取失败：{exc}（code={exc.code}）")
                yield event.plain_result(self._error_reply(episode, exc))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"[有兽焉] 第{episode}话未预期异常")
                yield event.plain_result(f"❌ 第{episode}话出错了：{exc}")
                continue

            too_big = max_mb > 0 and result.size_bytes > max_mb * 1024 * 1024
            if not link_only and not too_big:
                chain = self._video_chain(result)
                if chain:
                    try:
                        yield event.chain_result(chain)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"[有兽焉] 视频消息发送失败，降级为链接：{exc}")

            note = f"📦 文件超过 {max_mb} MB，已改为发送直链，点开即可播放" if too_big else ""
            yield event.plain_result(self._text_reply(result, note))

    @filter.command("有兽焉帮助")
    async def help_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        yield event.plain_result(HELP_TEXT)

    @filter.command("有兽焉自检", alias={"有兽焉状态"})
    async def status_command(self, event: AstrMessageEvent) -> AsyncGenerator:
        """调用 action=info 检查密钥与接口连通性。"""
        if not self.api.configured:
            yield event.plain_result(UNCONFIGURED_TEXT)
            return
        try:
            data = await self.api.check_key()
        except ApiError as exc:
            yield event.plain_result(f"❌ 接口自检失败：{exc}")
            return

        key = data.get("key") if isinstance(data.get("key"), dict) else {}
        user = data.get("user") if isinstance(data.get("user"), dict) else {}

        expire = key.get("expire_time") or 0
        if not expire:
            expire_text = "永久"
        else:
            left = int(expire) - int(time.time())
            expire_text = f"还剩 {_duration(left)}" if left > 0 else "已过期"

        lines = [
            "🔍 有兽焉接口自检",
            f"· 密钥：{key.get('name') or '-'}（{key.get('prefix') or '-'}）",
            f"· 作用域：{key.get('scopes') or '-'}",
            f"· 有效期：{expire_text}",
            f"· 调用次数：{key.get('call_count', '-')}",
            f"· 账号：{user.get('nickname') or user.get('id') or '-'}",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self) -> None:
        self.api.clear_cache()
        logger.info("[有兽焉] 插件已卸载")
