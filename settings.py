"""把 _conf_schema.json 变成聊天里能改的配置。

设计要点：
- 配置项的键、类型、默认值、可选值全部从 _conf_schema.json 读，不重复维护；
- 只额外维护一张「口语别名表」和少量数值范围/校验，避免和面板配置打架；
- 密钥类字段在做展示时打码，但允许用指令写入。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "_conf_schema.json"
)

# 展示时打码的字段：密钥只留前缀，密码只说个数
SECRET_MASK = {"api_key"}
COUNT_MASK = {"folder_password"}

# 口语别名 → 配置键（用户不用记英文键名）
ALIASES: Dict[str, str] = {
    "接口": "api_base_url",
    "接口地址": "api_base_url",
    "地址": "api_base_url",
    "api": "api_base_url",
    "base_url": "api_base_url",
    "密钥": "api_key",
    "key": "api_key",
    "apikey": "api_key",
    "api_key": "api_key",
    "密钥头": "api_key_header",
    "头名称": "api_key_header",
    "前缀": "api_key_prefix",
    "片名": "keyword",
    "关键词": "keyword",
    "名字": "keyword",
    "来源": "source",
    "网盘": "source",
    "站点": "site_url",
    "网站": "site_url",
    "网址": "site_url",
    "密码": "folder_password",
    "目录密码": "folder_password",
    "公共密码": "folder_password",
    "直链有效期": "play_ttl",
    "有效期": "play_ttl",
    "ttl": "play_ttl",
    "每页": "page_size",
    "页数": "max_pages",
    "翻页": "max_pages",
    "缓存": "cache_ttl",
    "话数": "max_episode",
    "最大话数": "max_episode",
    "搜索条数": "search_limit",
    "搜索结果": "search_limit",
    "范围": "max_range",
    "上限": "max_send_size_mb",
    "体积": "max_send_size_mb",
    "大小": "max_send_size_mb",
    "发送方式": "upload_mode",
    "方式": "upload_mode",
    "下载超时": "download_timeout",
    "只发链接": "link_only",
    "仅链接": "link_only",
    "超时": "timeout",
    "附加请求头": "extra_headers",
    "请求头": "extra_headers",
}

# 数值范围 (最小, 最大)，超出直接拒绝，避免把插件调进死胡同
RANGES: Dict[str, Tuple[int, int]] = {
    "play_ttl": (60, 86400),
    "page_size": (1, 100),
    "max_pages": (1, 50),
    "cache_ttl": (0, 86400),
    "max_episode": (0, 99999),
    "search_limit": (1, 50),
    "max_range": (1, 20),
    "max_send_size_mb": (0, 2048),
    "download_timeout": (10, 3600),
    "timeout": (3, 300),
}

# 枚举值的中文说法
OPTION_WORDS: Dict[str, Dict[str, str]] = {
    "source": {
        "public": "public",
        "公共": "public",
        "公共网盘": "public",
        "公盘": "public",
        "own": "own",
        "自己": "own",
        "我的": "own",
        "我的网盘": "own",
        "自己的网盘": "own",
        "私有": "own",
    },
    "upload_mode": {
        "auto": "auto",
        "自动": "auto",
        "video": "video",
        "视频": "video",
        "视频消息": "video",
        "group_file": "group_file",
        "群文件": "group_file",
        "file": "group_file",
        "link": "link",
        "直链": "link",
        "链接": "link",
    },
}

TRUE_WORDS = {"1", "true", "yes", "y", "on", "开", "开启", "启用", "是", "真", "打开"}
FALSE_WORDS = {"0", "false", "no", "n", "off", "关", "关闭", "禁用", "否", "假", "停"}
RESET_WORDS = {"默认", "恢复默认", "default", "reset", "清除"}

# 分类展示顺序
GROUPS: List[Tuple[str, List[str]]] = [
    (
        "接口与站点",
        [
            "api_base_url",
            "api_key",
            "api_key_header",
            "api_key_prefix",
            "site_url",
            "timeout",
            "extra_headers",
        ],
    ),
    (
        "内容与检索",
        [
            "keyword",
            "source",
            "folder_password",
            "play_ttl",
            "page_size",
            "max_pages",
            "max_folders",
            "cache_ttl",
            "max_episode",
            "search_limit",
            "max_range",
        ],
    ),
    (
        "发送",
        ["upload_mode", "max_send_size_mb", "link_only", "download_timeout"],
    ),
]

# 指令前缀，从长到短剥
_COMMAND_WORDS = (
    "有兽焉设置项",
    "有兽焉配置",
    "有兽焉设置",
    "youshouyanconfig",
    "youshouyanconf",
    "ysy配置",
    "ysy设置",
    "ysyconf",
    "设置项",
    "配置项",
    "config",
    "settings",
    "setting",
    "conf",
    "设置",
    "配置",
)

_LEAD = re.compile(r"^[\s/／!！。.,，、:：~～\-—]+")
_END = re.compile(r"[\s/／!！。.,，、:：~～\-—]+$")

_SPEC_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


class ConfigError(ValueError):
    """配置项或值无法处理。"""


def strip_command(text: str) -> str:
    """去掉 /有兽焉设置 这类前缀，返回剩余参数。"""
    body = (text or "").strip().replace("\u3000", " ")
    body = _LEAD.sub("", body)
    for word in _COMMAND_WORDS:
        if body.lower().startswith(word):
            body = body[len(word) :]
            break
    body = _LEAD.sub("", body)
    return _END.sub("", body).strip()


def load_spec() -> Dict[str, Dict[str, Any]]:
    """读 _conf_schema.json；读不到就退回最小内置表。"""
    global _SPEC_CACHE
    if _SPEC_CACHE is not None:
        return _SPEC_CACHE
    spec: Dict[str, Dict[str, Any]] = {}
    try:
        with open(SCHEMA_FILE, encoding="utf-8-sig") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    spec[str(key)] = value
    except Exception:  # noqa: BLE001 - schema 缺失不该让插件起不来
        spec = {}
    if not spec:
        spec = {
            "api_base_url": {"type": "string", "default": ""},
            "api_key": {"type": "string", "default": ""},
            "keyword": {"type": "string", "default": "有兽焉"},
            "source": {"type": "string", "default": "public", "options": ["public", "own"]},
            "site_url": {"type": "string", "default": ""},
            "folder_password": {"type": "string", "default": ""},
            "upload_mode": {
                "type": "string",
                "default": "auto",
                "options": ["auto", "video", "group_file", "link"],
            },
            "max_send_size_mb": {"type": "int", "default": 50},
        }
    _SPEC_CACHE = spec
    return spec


def keys() -> List[str]:
    return list(load_spec().keys())


def default_of(key: str) -> Any:
    spec = load_spec().get(key) or {}
    return spec.get("default")


def resolve(name: str) -> Optional[str]:
    """把用户输入（英文键/中文别名/大小写变体）解析成真正的配置键。"""
    word = (name or "").strip().strip("「」『』\"'`")
    if not word:
        return None
    spec = load_spec()
    lowered = word.lower()
    for key in spec:
        if key.lower() == lowered:
            return key
    if lowered in ALIASES and ALIASES[lowered] in spec:
        return ALIASES[lowered]
    # 允许模糊：别名是键的一部分（如「目录密码」→ folder_password）
    for alias, key in ALIASES.items():
        if key in spec and word == alias:
            return key
    # 再退一步：键名或描述包含该词
    for key, item in spec.items():
        desc = str(item.get("description") or "")
        if word and (word in key or (desc and word in desc)):
            return key
    return None


def _as_int(key: str, text: str) -> int:
    raw = text.strip()
    if not re.fullmatch(r"[+-]?\d+", raw):
        raise ConfigError(f"「{key}」需要一个整数，收到的是「{text}」")
    value = int(raw)
    bounds = RANGES.get(key)
    if bounds and not (bounds[0] <= value <= bounds[1]):
        raise ConfigError(
            f"「{key}」只能取 {bounds[0]}~{bounds[1]}，收到 {value}"
        )
    return value


def _as_bool(key: str, text: str) -> bool:
    word = text.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(f"「{key}」要填开/关（也认 true/false、1/0），收到「{text}」")


def normalize_url(url: str) -> str:
    """中文域名转 punycode；单独实现一份，避免 settings 依赖 api.py。"""
    from urllib.parse import urlsplit, urlunsplit

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
    netloc = f"{ascii_host}:{parts.port}" if parts.port else ascii_host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def parse_value(key: str, text: str) -> Tuple[Any, List[str]]:
    """把用户输入转成配置值的真正类型，返回 (值, 提示列表)。"""
    spec = load_spec().get(key)
    if spec is None:
        raise ConfigError(f"没有「{key}」这个配置项")

    raw = (text or "").strip()
    kind = str(spec.get("type") or "string").lower()
    notes: List[str] = []

    # 「默认」= 恢复默认值，任何类型都认
    if raw.lower() in RESET_WORDS:
        return default_of(key), ["已恢复默认值"]

    if kind == "int":
        return _as_int(key, raw), notes

    if kind == "bool":
        return _as_bool(key, raw), notes

    if kind == "dict":
        if not raw:
            return {}, notes
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"「{key}」要填 JSON，例如 " + '{"Referer": "https://xxx"}' + f"（{exc.msg}）"
            )
        if not isinstance(value, dict):
            raise ConfigError(f"「{key}」需要 JSON 对象（花括号）")
        return value, notes

    # 字符串
    if not raw and key not in ("keyword", "folder_password", "api_key_prefix"):
        raise ConfigError(f"「{key}」不能为空")

    options = spec.get("options")
    if options:
        words = OPTION_WORDS.get(key, {})
        lowered = raw.lower()
        for option in options:
            if lowered == str(option).lower():
                return option, notes
        if lowered in words:
            return words[lowered], notes
        allowed = " / ".join(str(item) for item in options)
        raise ConfigError(f"「{key}」只能填 {allowed}，收到「{raw}」")

    if "url" in key:
        value = normalize_url(raw)
        if not value.lower().startswith(("http://", "https://")):
            raise ConfigError(f"「{key}」要以 http:// 或 https:// 开头")
        if value != raw:
            notes.append(f"已转成 {value}")
        return value, notes

    if key == "api_key":
        if not raw.startswith("pk_"):
            notes.append("接口密钥通常以 pk_ 开头，请确认没粘错")
        elif len(raw) < 20:
            notes.append("密钥长度偏短，可能没复制完整")

    if key == "folder_password":
        count = len([p for p in re.split(r"[\r\n,;|]+", raw) if p.strip()])
        if count > 1:
            notes.append(f"已识别 {count} 个密码")

    if key == "timeout":
        return _as_int(key, raw), notes

    return raw, notes


def value_of(config: Any, key: str) -> Any:
    try:
        value = config.get(key, None)
    except Exception:  # noqa: BLE001
        value = None
    return default_of(key) if value is None else value


def mask(key: str, value: Any) -> str:
    text = "" if value is None else str(value)
    if key in SECRET_MASK:
        if not text:
            return "（未配置）"
        head = text[:6]
        return f"{head}{'•' * 6}（共 {len(text)} 位）"
    if key in COUNT_MASK:
        count = len([p for p in re.split(r"[\r\n,;|]+", text) if p.strip()])
        return f"（已配置 {count} 个）" if count else "（未配置）"
    return display(key, value)


def display(key: str, value: Any) -> str:
    """列表里显示的值：短、可直接复制。"""
    if isinstance(value, bool):
        return "开" if value else "关"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False) if value else "（空）"
    text = "" if value is None else str(value)
    if not text:
        return "（空）"
    return text.replace("\n", " | ")


def is_default(config: Any, key: str) -> bool:
    try:
        return value_of(config, key) == default_of(key)
    except Exception:  # noqa: BLE001
        return False


def describe(key: str, config: Any) -> str:
    """单个配置项的详情（/有兽焉设置 密钥）。"""
    spec = load_spec().get(key) or {}
    lines = [f"⚙️ {spec.get('description') or key}（{key}）"]
    lines.append(f"· 当前：{mask(key, value_of(config, key))}")
    default = default_of(key)
    shown = "（空）" if default in ("", None) else display(key, default)
    lines.append(f"· 默认：{shown}")
    options = spec.get("options")
    if options:
        lines.append(f"· 可选：{' / '.join(str(o) for o in options)}")
    bounds = RANGES.get(key)
    if bounds:
        lines.append(f"· 范围：{bounds[0]} ~ {bounds[1]}")
    hint = str(spec.get("hint") or "").strip()
    if hint:
        lines.append(f"· 说明：{hint}")
    lines.append(f"改法：/有兽焉设置 {key} <值>")
    return "\n".join(lines)


def render_all(config: Any) -> str:
    """列出全部配置项，按分类分组。"""
    spec = load_spec()
    lines = ["⚙️ 有兽焉配置（仅管理员可改，改完立即生效并落盘）"]
    listed: set = set()
    for title, group in GROUPS:
        items = [key for key in group if key in spec]
        if not items:
            continue
        lines.append(f"【{title}】")
        for key in items:
            listed.add(key)
            mark = "" if is_default(config, key) else " *"
            lines.append(f"· {key} = {mask(key, value_of(config, key))}{mark}")
    rest = [key for key in spec if key not in listed]
    if rest:
        lines.append("【其他】")
        for key in rest:
            mark = "" if is_default(config, key) else " *"
            lines.append(f"· {key} = {mask(key, value_of(config, key))}{mark}")

    lines.append("* 表示与默认值不同")
    lines.append("查一项：/有兽焉设置 密钥")
    lines.append("改一项：/有兽焉设置 密钥 pk_xxxxxx")
    lines.append("还原：/有兽焉设置 密钥 默认")
    lines.append("可用中文名：接口 / 密钥 / 片名 / 来源 / 密码 / 上限 / 发送方式 …")
    return "\n".join(lines)


def save_config(config: Any) -> str:
    """把配置写回磁盘，返回一句给用户看的结果。"""
    saver = getattr(config, "save_config", None)
    if callable(saver):
        try:
            saver()
            return "💾 已保存到配置文件"
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 写入配置文件失败：{exc}（本次只在内存生效，重启会丢）"

    path = getattr(config, "config_path", "")
    if path:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(dict(config), handle, ensure_ascii=False, indent=2)
            return "💾 已保存到配置文件"
        except Exception as exc:  # noqa: BLE001
            return f"⚠️ 写入配置文件失败：{exc}（本次只在内存生效，重启会丢）"

    return "⚠️ 当前配置不支持落盘，重启后会丢"
