"""指令文本解析：从 /看 有兽焉第12话 这类消息里提取片名与话数。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List

# 中文数字
_CN_DIGIT = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}

_NUM = r"(?:\d{1,4}|[零〇一二两三四五六七八九十百千]{1,6})"

# 第1-3话 / 第1~3话 / 第1至3话
_RANGE_RE = re.compile(
    rf"第?\s*({_NUM})\s*(?:[-~～—－]|至|到)\s*({_NUM})\s*[话話集期回]?"
)
# 第12话 / 第十二集 / 第 12 期
_EPISODE_RE = re.compile(rf"第\s*({_NUM})\s*[话話集期回]?")
# 裸数字：/看 有兽焉 12
_BARE_NUM_RE = re.compile(r"(?<![\d.])(\d{1,4})(?![\d.])")

# 命令词（用来把正文前置词剥掉，顺序从长到短）
_COMMAND_WORDS = (
    "看有兽焉",
    "有兽焉",
    "youshouyan",
    "看",
    "kan",
)

# parse_query 只剥「动词」，片名要留下来当检索关键词
_PREFIX_WORDS = (
    "看有兽焉",
    "youshouyan",
    "看看",
    "看",
    "kan",
    "找",
    "搜索",
    "搜",
    "search",
    "find",
    "我要看",
    "想看",
)

_TITLE_TAIL = re.compile(r"[\s/／!！。.,，、:：~～\-—_+]+$")
_TITLE_HEAD = re.compile(r"^[\s/／!！。.,，、:：~～\-—_+]+")
_NOISE_TAIL = ("全集", "在线观看", "在线", "视频", "的")

DEFAULT_MAX_RANGE = 3


class ParseError(ValueError):
    """指令参数无法解析。"""


@dataclass
class Query:
    """一次指令的解析结果：话数列表 + 片名关键词。"""

    episodes: List[int] = field(default_factory=list)
    keyword: str = ""


def cn_to_int(text: str) -> int:
    """中文数字/阿拉伯数字转整数，支持 十二、二十、一百零八 等写法。"""
    text = text.strip()
    if not text:
        raise ParseError("话数不能为空")
    if text.isdigit():
        return int(text)

    section = 0
    num = 0
    for ch in text:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if num == 0:
                num = 1
            if unit == 10:
                section += num * 10
            else:
                section = (section + num) * unit
            num = 0
        else:
            raise ParseError(f"无法识别的话数：{text}")
    return section + num


def strip_command(text: str) -> str:
    """去掉消息开头的命令词与标点，返回指令正文。"""
    body = (text or "").strip()
    body = re.sub(r"^[\s/／!！。.,，、:：~～]+", "", body)

    changed = True
    while changed and body:
        changed = False
        body = re.sub(r"^[\s/／!！。.,，、:：~～]+", "", body)
        lowered = body.lower()
        for word in _COMMAND_WORDS:
            if lowered.startswith(word):
                body = body[len(word):]
                changed = True
                break
    return body.strip()


def strip_prefix(text: str) -> str:
    """只剥掉开头的命令动词与标点，保留片名（如「有兽焉第12话」）。"""
    body = (text or "").strip()
    changed = True
    while changed and body:
        changed = False
        body = _TITLE_HEAD.sub("", body)
        lowered = body.lower()
        for word in _PREFIX_WORDS:
            if lowered.startswith(word):
                body = body[len(word):]
                changed = True
                break
    return body.strip()


def _clean_title(text: str) -> str:
    title = _TITLE_TAIL.sub("", _TITLE_HEAD.sub("", text or ""))
    for tail in _NOISE_TAIL:
        if title.endswith(tail) and len(title) > len(tail):
            title = title[: -len(tail)]
    return title.strip()


def parse_query(text: str, max_range: int = DEFAULT_MAX_RANGE) -> Query:
    """解析指令，同时给出话数与片名关键词。

    「/看 斗破苍穹第5话」→ Query([5], "斗破苍穹")
    「/找 有兽焉」       → Query([], "有兽焉")    ← 搜索模式
    「/看 第12话」       → Query([12], "")        ← 关键词留空，用配置里的默认片名
    """
    body = strip_prefix(text)
    if not body:
        raise ParseError("请告诉我要看什么")

    match = _RANGE_RE.search(body)
    if match:
        start = cn_to_int(match.group(1))
        end = cn_to_int(match.group(2))
        if start <= 0 or end <= 0:
            raise ParseError("话数必须大于 0")
        if end < start:
            start, end = end, start
        count = end - start + 1
        limit = max(1, int(max_range or DEFAULT_MAX_RANGE))
        if count > limit:
            raise ParseError(f"一次最多查询 {limit} 话，当前请求了 {count} 话")
        return Query(list(range(start, end + 1)), _clean_title(body[: match.start()]))

    match = _EPISODE_RE.search(body)
    if match:
        number = cn_to_int(match.group(1))
        if number <= 0:
            raise ParseError("话数必须大于 0")
        return Query([number], _clean_title(body[: match.start()]))

    # 没有「第N话」：末尾的裸数字当话数，其余部分当片名
    match = _BARE_NUM_RE.search(body)
    if match:
        number = int(match.group(1))
        if number <= 0:
            raise ParseError("话数必须大于 0")
        title = _clean_title(body[: match.start()] + body[match.end():])
        return Query([number], title)

    # 一个数字都没有 → 纯搜索
    return Query([], _clean_title(body))


def parse_request(text: str, max_range: int = DEFAULT_MAX_RANGE) -> List[int]:
    """解析一条指令，返回需要查询的话数列表。

    支持：
      /看 有兽焉第12话
      /看 有兽焉 第12集
      /看 有兽焉 12
      /看 有兽焉第1-3话
    """
    body = strip_command(text)
    if not body:
        raise ParseError("请告诉我要看第几话")

    match = _RANGE_RE.search(body)
    if match:
        start = cn_to_int(match.group(1))
        end = cn_to_int(match.group(2))
        if start <= 0 or end <= 0:
            raise ParseError("话数必须大于 0")
        if end < start:
            start, end = end, start
        count = end - start + 1
        limit = max(1, int(max_range or DEFAULT_MAX_RANGE))
        if count > limit:
            raise ParseError(f"一次最多查询 {limit} 话，当前请求了 {count} 话")
        return list(range(start, end + 1))

    match = _EPISODE_RE.search(body)
    if match:
        number = cn_to_int(match.group(1))
        if number <= 0:
            raise ParseError("话数必须大于 0")
        return [number]

    match = _BARE_NUM_RE.search(body)
    if match:
        number = int(match.group(1))
        if number <= 0:
            raise ParseError("话数必须大于 0")
        return [number]

    raise ParseError("没看懂要看第几话，试试「有兽焉第12话」")
