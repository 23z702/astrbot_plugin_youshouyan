"""有兽焉媒体接口封装（对接 /api/media.php）。

接口约定：
    GET /api/media.php?action=list&page=1&page_size=100&q=有兽焉
    GET /api/media.php?action=video&id=123&ttl=3600
鉴权：X-API-Key: pk_xxx（推荐，不依赖 nginx 透传 Authorization）

插件做的事：按关键词拉视频列表 → 用文件名匹配「第N话」→ 换取临时播放直链。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp

from astrbot.api import logger

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 接口错误码 → 中文提示
ERROR_HINTS = {
    400: "参数错误",
    401: "密钥无效、已被撤销或已过期",
    403: "密钥作用域不足（需要 media 或 media:public）",
    404: "文件不存在或不属于该密钥的账号",
    415: "该文件不是可播放的视频",
    429: "调用太频繁，被接口限流了（15 分钟内 30 次）",
    500: "服务端异常",
    503: "服务端还没跑迁移（缺少 api_keys 表）",
}

# 文件名里的话数标记会撞上这些噪声，匹配前先剔除
_NOISE_RE = re.compile(
    r"(?i)\b(?:2160p|1080p|1080i|720p|480p|360p|4k|8k|uhd|hdr10?|"
    r"h\.?26[45]|x26[45]|hevc|avc|aac|ac3|flac|opus|8bit|10bit|"
    r"web-?dl|webrip|bluray|blu-ray|remux|hdtv|xvid|dvdrip)\b"
)
# S01E05 → E05，避免季号被当成话数
_SEASON_RE = re.compile(r"(?i)s\s*0*\d{1,2}\s*[._-]?\s*e")
# 预告/花絮类，同等匹配时让正片优先
_BONUS_RE = re.compile(r"(?i)(预告|花絮|特典|彩蛋|片头|片尾|\bpv\b|\bop\d?\b|\bed\d?\b|\bcm\b)")

# 公共网盘按扩展名区分文件类型，这些算视频
_PUBLIC_VIDEO_TYPES = {
    "mp4", "mkv", "mov", "webm", "avi", "m4v", "flv", "ogv", "ts", "wmv", "mpg", "mpeg",
}


def normalize_url(url: str) -> str:
    """把中文域名（IDN）转成 punycode，避免 aiohttp 拒收非 ASCII 主机名。

    中文域名 → 对应的 xn-- 形式
    """
    raw = (url or "").strip()
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
    except ValueError:
        return raw
    if not host or host.isascii():
        return raw

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return raw

    netloc = ascii_host
    if parts.port:
        netloc = f"{ascii_host}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class ApiError(Exception):
    """接口调用失败，code 与接口返回的 code 一致（0 表示本地错误）。"""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class EpisodeResult:
    episode: int
    type: str
    content: str
    title: str = ""
    file_id: int = 0
    size_bytes: int = 0
    size_text: str = ""
    expires_at: int = 0
    expires_in: int = 0

    @property
    def display_name(self) -> str:
        return os.path.splitext(os.path.basename(self.title))[0].strip()

    def headline(self, show_filename: bool = False) -> str:
        head = f"🐾 有兽焉 第{self.episode}话"
        extras: List[str] = []
        if self.size_text:
            extras.append(self.size_text)
        if show_filename and self.display_name:
            extras.append(self.display_name)
        if extras:
            head += " · " + " · ".join(extras)
        return head


def match_episode(filename: str, episode: int) -> int:
    """判断文件名属于第几话，返回匹配得分，0 表示不匹配。

    可信度：裸数字 50 < 括号数字 80 < E12/EP12 90 < 第12话 100。
    """
    stem = os.path.splitext(os.path.basename(filename or ""))[0]
    if not stem:
        return 0
    text = _SEASON_RE.sub("E", _NOISE_RE.sub(" ", stem))
    best = 0

    if re.search(rf"第\s*0*{episode}(?!\d)\s*[话話集期回]", text):
        best = max(best, 100)
    if re.search(rf"(?i)(?<![a-z])e(?:p)?s?\s*[._-]?\s*0*{episode}(?!\d)", text):
        best = max(best, 90)
    if re.search(rf"[\[\(【]\s*0*{episode}\s*[\]\)】]", text):
        best = max(best, 80)
    if re.search(rf"(?<![\d.])0*{episode}(?![\d.])", text):
        best = max(best, 50)

    if best and _BONUS_RE.search(stem):
        best = max(10, best - 20)  # 预告/花絮让位给正片
    return best


class MediaAPI:
    def __init__(self, config: Any = None) -> None:
        self._config = config if config is not None else {}
        self._list_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
        self._url_cache: Dict[Tuple[str, int], Tuple[int, EpisodeResult]] = {}
        self._video_pwd: Dict[Tuple[str, int], str] = {}
        self._source_override: Optional[str] = None

    # ---------------------------------------------------------------- 配置

    def _get(self, key: str, default: Any = None) -> Any:
        try:
            value = self._config.get(key, default)
        except Exception:  # noqa: BLE001
            return default
        return default if value is None else value

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._get(key, default))
        except (TypeError, ValueError):
            return default

    @property
    def base_url(self) -> str:
        return normalize_url(str(self._get("api_base_url", "") or "").strip())

    @property
    def api_key(self) -> str:
        return str(self._get("api_key", "") or "").strip()

    @property
    def keyword(self) -> str:
        return str(self._get("keyword", "有兽焉") or "").strip()

    @property
    def source(self) -> str:
        """own = 自己的网盘；public = 公共网盘。"""
        if self._source_override:
            return self._source_override
        value = str(self._get("source", "own") or "own").strip().lower()
        return value if value in ("own", "public") else "own"

    @property
    def folder_password(self) -> str:
        return str(self._get("folder_password", "") or "").strip()

    @property
    def folder_passwords(self) -> List[str]:
        """多个受保护目录可填多个密码，用换行 / 逗号 / 竖线分隔。"""
        raw = str(self._get("folder_password", "") or "")
        parts = re.split(r"[\r\n,;|]+", raw)
        seen: List[str] = []
        for item in parts:
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
        return seen

    @property
    def source_label(self) -> str:
        return "公共网盘" if self.source == "public" else "我的网盘"

    def _source_params(self, password: Optional[str] = None) -> Dict[str, Any]:
        """source + 目录密码，按需附加到 list 与 video。

        password 传 None 时用配置里的第一个；传 "" 表示这一次不带密码。
        """
        params: Dict[str, Any] = {"source": self.source}
        pwd = self.folder_password if password is None else password
        if pwd:
            params["folder_password"] = pwd
        return params

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def play_ttl(self) -> int:
        return min(86400, max(60, self._int("play_ttl", 7200)))

    # ---------------------------------------------------------------- 请求

    def _headers(self) -> Dict[str, str]:
        header_name = str(self._get("api_key_header", "X-API-Key") or "X-API-Key")
        prefix = str(self._get("api_key_prefix", "") or "")
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            header_name: f"{prefix}{self.api_key}",
        }
        extra = self._get("extra_headers", {}) or {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                headers[str(key)] = str(value)
        return headers

    def _build_url(self, action: str, **params: Any) -> str:
        query = {"action": action}
        query.update({k: v for k, v in params.items() if v is not None and v != ""})
        sep = "&" if "?" in self.base_url else "?"
        return f"{self.base_url}{sep}{urlencode(query)}"

    async def _request(
        self, action: str, not_found_hint: str = "", **params: Any
    ) -> Any:
        if not self.configured:
            raise ApiError("还没配置接口地址或 API 密钥")

        url = self._build_url(action, **params)
        timeout = max(1, self._int("timeout", 15))

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(url, headers=self._headers()) as response:
                    status = response.status
                    content_type = response.headers.get("Content-Type", "")
                    raw = await response.text(errors="ignore")
        except asyncio.TimeoutError as exc:
            raise ApiError("接口请求超时了，稍后再试试") from exc
        except aiohttp.ClientError as exc:
            raise ApiError(f"接口请求失败：{exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ApiError(f"接口请求异常：{exc}") from exc

        body = (raw or "").strip()
        if not body:
            raise ApiError(f"接口返回了空内容（HTTP {status}）")

        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            # 站点把 404/403 交给前端页面兜底时，返回的是 HTML，这里给出可定位的提示
            if status == 404:
                if not_found_hint:
                    raise ApiError(not_found_hint, 404) from None
                raise ApiError(
                    f"接口地址 404：确认路径是 /api/media.php，且服务器已部署该文件"
                    f"（请求 {url.split('?')[0]}）",
                    404,
                ) from None
            if status in (401, 403):
                raise ApiError(
                    ERROR_HINTS.get(status, f"HTTP {status}"), status
                ) from None
            if "html" in content_type.lower():
                raise ApiError(
                    f"接口返回了网页而不是 JSON（HTTP {status}），地址可能填错了", status
                ) from None
            raise ApiError(f"接口返回了非 JSON 内容（HTTP {status}）") from None

        if not isinstance(payload, dict):
            raise ApiError("接口返回格式无法识别")

        code = payload.get("code", 0 if status == 200 else status)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = status

        if code != 0:
            message = str(
                payload.get("msg") or ERROR_HINTS.get(code, f"接口返回错误 {code}")
            )
            raise ApiError(message, code)

        data = payload.get("data")
        return data if data is not None else {}

    # ---------------------------------------------------------------- 列表

    async def fetch_videos(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """拉取关键词匹配的视频列表（带缓存、自动翻页）。"""
        # 公共网盘走 public.php：media.php 的 source=public 看不到受密码保护的目录
        if self.source == "public":
            return await self.fetch_public_videos(refresh)

        # 缓存键带上来源与密码集合，切换 own/public 或改密码都不会串用旧结果
        cache_key = f"{self.source}:{self.folder_password}:{self.keyword}"
        cached = self._list_cache.get(cache_key)
        ttl = max(0, self._int("cache_ttl", 300))
        if not refresh and cached and time.time() - cached[0] <= ttl:
            return cached[1]

        # 先不带密码扫一遍公开部分，再逐个带密码扫受保护目录，结果合并去重
        passes: List[str] = [""]
        for pwd in self.folder_passwords:
            if pwd not in passes:
                passes.append(pwd)

        videos: List[Dict[str, Any]] = []
        seen: set = set()
        for pwd in passes:
            for item in await self._fetch_pages(pwd):
                fingerprint = (item.get("id"), item.get("filename"))
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                videos.append(item)
                try:
                    video_id = int(item.get("id") or 0)
                except (TypeError, ValueError):
                    video_id = 0
                if video_id:
                    # 记住这个文件是靠哪个密码才看得见的，取直链时要用同一个
                    self._video_pwd[(self.source, video_id)] = pwd

        videos = [v for v in videos if str(v.get("mime", "")).startswith("video/")]
        self._list_cache[cache_key] = (time.time(), videos)
        logger.info(
            f"[有兽焉] {self.source_label} 关键词「{self.keyword}」"
            f"匹配到 {len(videos)} 个视频"
            f"（密码 {len(passes) - 1} 个）"
        )
        return videos

    async def _fetch_pages(self, password: str = "") -> List[Dict[str, Any]]:
        """按指定密码（空串 = 不带密码）翻页拉取原始列表。"""
        page_size = min(100, max(1, self._int("page_size", 100)))
        max_pages = max(1, self._int("max_pages", 5))
        items: List[Dict[str, Any]] = []

        for page in range(1, max_pages + 1):
            data = await self._request(
                "list",
                page=page,
                page_size=page_size,
                q=self.keyword or None,
                **self._source_params(password),
            )
            batch = data.get("list") or []
            if not isinstance(batch, list):
                break
            items.extend([item for item in batch if isinstance(item, dict)])

            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if len(batch) < page_size or (total and len(items) >= total):
                break

        return items

    # ---------------------------------------------------------------- 直链

    async def get_play_url(self, video: Dict[str, Any]) -> EpisodeResult:
        """取临时播放直链，缓存期内（且未临近过期）直接复用。"""
        if self.source == "public":
            return await self.public_play_url(video)

        try:
            video_id = int(video.get("id") or 0)
        except (TypeError, ValueError):
            video_id = 0
        if not video_id:
            raise ApiError("视频缺少 id，无法换取直链")

        # 公共网盘与自己的网盘 id 是两套体系，缓存键必须带来源
        cache_key = (self.source, video_id)
        cached = self._url_cache.get(cache_key)
        if cached and cached[0] - time.time() > 60:
            return cached[1]

        data = await self._request(
            "video",
            not_found_hint="这个文件已经不在网盘里了（可能被删除或移走），列一次新目录再试",
            id=video_id,
            ttl=self.play_ttl,
            **self._source_params(self._video_pwd.get((self.source, video_id), "")),
        )
        url = str(data.get("url") or "").strip()
        if not url:
            raise ApiError("接口没有返回可用的播放地址")

        try:
            size_bytes = int(data.get("size") or video.get("size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0

        # 用本地时钟 + expires_in 推算过期点，避免本机与服务器时钟偏差影响缓存判断
        try:
            expires_in = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            expires_in = self.play_ttl
        now = int(time.time())

        result = EpisodeResult(
            episode=0,
            type="video",
            content=url,
            title=str(data.get("filename") or video.get("filename") or ""),
            file_id=video_id,
            size_bytes=size_bytes,
            size_text=str(data.get("size_text") or video.get("size_text") or ""),
            expires_at=now + expires_in,
            expires_in=expires_in,
        )
        if not result.size_text and size_bytes:
            result.size_text = human_size(size_bytes)

        self._url_cache[cache_key] = (result.expires_at, result)
        return result

    # ---------------------------------------------------------------- 业务

    async def check_key(self) -> Dict[str, Any]:
        """调用 action=info 自检密钥。"""
        return await self._request("info")

    @property
    def public_endpoint(self) -> str:
        """公共网盘页面用的接口（不需密钥，只能看目录结构）。"""
        base = self.base_url
        if not base:
            return ""
        if "media.php" in base:
            return base.replace("media.php", "public.php")
        head = base.rpartition("/")[0]
        return f"{head}/public.php" if head else base

    async def public_folders(self, folder_id: int = 0) -> List[Dict[str, Any]]:
        """列出公共网盘某层的子目录。

        受密码保护的目录会带 has_password=True —— 这类目录里的文件不会
        出现在 media.php 的 list 里，必须先配置正确的 folder_password。
        """
        endpoint = self.public_endpoint
        if not endpoint:
            return []
        url = f"{endpoint}?{urlencode({'action': 'list', 'folder_id': folder_id})}"
        timeout = max(1, self._int("timeout", 15))
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(
                    url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
                ) as response:
                    payload = await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - 目录信息只用于提示，失败不影响主流程
            logger.debug(f"[有兽焉] 读取公共网盘目录失败：{exc}")
            return []

        data = payload.get("data") if isinstance(payload, dict) else None
        folders = data.get("folders") if isinstance(data, dict) else None
        if not isinstance(folders, list):
            return []
        return [item for item in folders if isinstance(item, dict)]

    async def _public_get(self, action: str, **params: Any) -> Dict[str, Any]:
        """调用公共网盘接口（api/public.php，无需密钥）。

        code=2 表示该目录需要密码：此时抛出 ApiError(码 2)，由调用方决定
        换一个密码再试，还是当作「锁着的目录」跳过。
        """
        endpoint = self.public_endpoint
        if not endpoint:
            raise ApiError("还没配置接口地址")
        query = {"action": action}
        query.update({k: v for k, v in params.items() if v is not None and v != ""})
        url = f"{endpoint}?{urlencode(query)}"
        timeout = max(1, self._int("timeout", 15))

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                ) as response:
                    status = response.status
                    raw = await response.text(errors="ignore")
        except asyncio.TimeoutError as exc:
            raise ApiError("公共网盘接口超时了，稍后再试") from exc
        except aiohttp.ClientError as exc:
            raise ApiError(f"公共网盘接口请求失败：{exc}") from exc

        try:
            payload = json.loads((raw or "").strip())
        except (json.JSONDecodeError, TypeError):
            raise ApiError(
                f"公共网盘接口返回了网页而不是 JSON（HTTP {status}）", status
            ) from None
        if not isinstance(payload, dict):
            raise ApiError("公共网盘接口返回格式无法识别")

        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = 0
        if code == 2:
            raise ApiError("需要目录密码", 2)
        if code != 0:
            raise ApiError(str(payload.get("msg") or f"公共网盘接口错误 {code}"), code)
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    async def _public_list_folder(
        self, folder_id: int, inherited: str = ""
    ) -> Tuple[Dict[str, Any], str]:
        """列一个目录，自动试密码。返回 (数据, 生效的密码)；打不开返回 ({}, "")。"""
        candidates: List[str] = []
        for pwd in [inherited, *self.folder_passwords, ""]:
            if pwd not in candidates:
                candidates.append(pwd)

        last_error: Optional[ApiError] = None
        for pwd in candidates:
            try:
                data = await self._public_list_folder_page(folder_id, pwd, 1)
            except ApiError as exc:
                last_error = exc
                if exc.code == 2:  # 换下一个密码再试
                    continue
                raise
            return data, pwd

        if last_error is not None:
            logger.debug(f"[有兽焉] 公共目录 {folder_id} 打不开：{last_error}")
        return {}, ""

    async def _public_list_folder_page(
        self, folder_id: int, password: str, page: int
    ) -> Dict[str, Any]:
        return await self._public_get(
            "list",
            folder_id=folder_id,
            page=page,
            page_size=min(100, max(1, self._int("page_size", 100))),
            folder_password=password,
        )

    async def fetch_public_videos(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """遍历公共网盘目录，收集视频文件。

        media.php 的 source=public 只列公开部分，受密码保护的目录完全不返回，
        所以公共网盘必须走 public.php 自己走目录树。
        """
        cache_key = f"public:{self.folder_password}:{self.keyword}"
        cached = self._list_cache.get(cache_key)
        ttl = max(0, self._int("cache_ttl", 300))
        if not refresh and cached and time.time() - cached[0] <= ttl:
            return cached[1]

        max_folders = max(1, self._int("max_folders", 50))
        max_pages = max(1, self._int("max_pages", 5))
        page_size = min(100, max(1, self._int("page_size", 100)))

        queue: List[Tuple[int, str]] = [(0, "")]
        visited: set = set()
        videos: List[Dict[str, Any]] = []
        seen_files: set = set()

        while queue and len(visited) < max_folders:
            folder_id, inherited = queue.pop(0)
            if folder_id in visited:
                continue
            visited.add(folder_id)

            try:
                first = await self._public_list_folder(folder_id, inherited)
            except ApiError as exc:
                logger.warning(f"[有兽焉] 列公共目录 {folder_id} 失败：{exc}")
                continue
            data, password = first
            if not data:
                continue  # 锁着且没有可用密码

            batch = data.get("list") or []
            pages = 1
            while (
                isinstance(batch, list)
                and len(batch) >= page_size
                and pages < max_pages
            ):
                pages += 1
                try:
                    more = await self._public_list_folder_page(
                        folder_id, password, pages
                    )
                except ApiError:
                    break
                extra = more.get("list") or []
                if not isinstance(extra, list) or not extra:
                    break
                batch.extend(extra)

            for raw in batch if isinstance(batch, list) else []:
                if not isinstance(raw, dict):
                    continue
                item = self._normalize_public_file(raw, password)
                if not item:
                    continue
                fingerprint = item.get("id")
                if fingerprint in seen_files:
                    continue
                seen_files.add(fingerprint)
                videos.append(item)

            for sub in data.get("folders") or []:
                if not isinstance(sub, dict):
                    continue
                try:
                    sub_id = int(sub.get("id") or 0)
                except (TypeError, ValueError):
                    sub_id = 0
                if sub_id and sub_id not in visited:
                    queue.append((sub_id, password))

        self._list_cache[cache_key] = (time.time(), videos)
        logger.info(
            f"[有兽焉] 公共网盘扫描 {len(visited)} 个目录，"
            f"得到 {len(videos)} 个视频"
        )
        return videos

    @staticmethod
    def _normalize_public_file(
        raw: Dict[str, Any], password: str
    ) -> Optional[Dict[str, Any]]:
        """把 public.php 的列表项补成 media.php 那种字段，方便复用匹配逻辑。"""
        name = str(raw.get("filename") or "")
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        file_type = str(raw.get("file_type") or ext).lower()
        if file_type not in _PUBLIC_VIDEO_TYPES:
            return None

        item = dict(raw)
        item["mime"] = f"video/{file_type or 'mp4'}"
        try:
            item["size"] = int(raw.get("file_size") or 0)
        except (TypeError, ValueError):
            item["size"] = 0
        item["size_text"] = str(raw.get("file_size_text") or "")
        item["_password"] = password
        return item

    async def public_play_url(self, video: Dict[str, Any]) -> EpisodeResult:
        """公共网盘给的是预览地址：带签名优先，否则带上目录密码。"""
        try:
            video_id = int(video.get("id") or 0)
        except (TypeError, ValueError):
            video_id = 0
        if not video_id:
            raise ApiError("视频缺少 id，无法生成播放地址")

        endpoint = self.public_endpoint
        password = str(video.get("_password") or "")
        now = int(time.time())

        params: Dict[str, Any] = {"action": "preview", "id": video_id}
        expires_at = now + self.play_ttl
        try:
            sig = str(video.get("preview_sig") or "")
            exp = int(video.get("preview_exp") or 0)
        except (TypeError, ValueError):
            sig, exp = "", 0

        if sig and exp > now + 120:
            params.update({"exp": exp, "sig": sig})
            expires_at = exp
        elif password:
            params["folder_password"] = password
        else:
            raise ApiError(
                "这个文件在受密码保护的目录里，请在插件配置里填「公共目录密码」", 403
            )

        url = f"{endpoint}?{urlencode(params)}"
        try:
            size_bytes = int(video.get("size") or video.get("file_size") or 0)
        except (TypeError, ValueError):
            size_bytes = 0

        result = EpisodeResult(
            episode=0,
            type="video",
            content=url,
            title=str(video.get("filename") or ""),
            file_id=video_id,
            size_bytes=size_bytes,
            size_text=str(video.get("size_text") or ""),
            expires_at=expires_at,
            expires_in=max(0, expires_at - now),
        )
        if not result.size_text and size_bytes:
            result.size_text = human_size(size_bytes)
        return result

    async def count_videos(self, source: str) -> int:
        """临时切到指定来源统计匹配到的视频数（自检用）。"""
        previous = self._source_override
        self._source_override = source if source in ("own", "public") else "own"
        try:
            return len(await self.fetch_videos())
        finally:
            self._source_override = previous

    async def find_episode(self, episode: int) -> EpisodeResult:
        """按话数找视频并换取播放直链。"""
        if not self.configured:
            raise ApiError("还没配置接口地址或 API 密钥")

        videos = await self.fetch_videos()
        candidate = _pick_best(videos, episode)
        if candidate is None:
            # 可能是刚上传、还没进缓存，强制刷新一次
            videos = await self.fetch_videos(refresh=True)
            candidate = _pick_best(videos, episode)
        if candidate is None:
            scope = self.keyword or "全部"
            suffix = f"（{self.source_label}共 {len(videos)} 个视频）" if videos else ""
            hint = ""
            if self.source == "public" and not self.folder_passwords:
                hint = "\n💡 公共网盘若有带密码的目录，需在插件配置里填「公共目录密码」"
            raise ApiError(
                f"{self.source_label}「{scope}」下没有第{episode}话的文件{suffix}{hint}",
                404,
            )

        result = await self.get_play_url(candidate)
        result.episode = episode
        return result

    def clear_cache(self) -> None:
        self._list_cache.clear()
        self._url_cache.clear()
        self._video_pwd.clear()

    def purge_url(self, video_id: int) -> None:
        self._url_cache.pop((self.source, video_id), None)


def _pick_best(
    videos: List[Dict[str, Any]], episode: int
) -> Optional[Dict[str, Any]]:
    scored: List[Tuple[int, int, Dict[str, Any]]] = []
    for video in videos:
        score = match_episode(str(video.get("filename") or ""), episode)
        if not score:
            continue
        try:
            created = int(video.get("create_time") or 0)
        except (TypeError, ValueError):
            created = 0
        scored.append((score, created, video))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def human_size(size: int) -> str:
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"
