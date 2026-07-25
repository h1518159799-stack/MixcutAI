from __future__ import annotations

import re
import unicodedata


# Boundaries inside these expressions are never considered.  The list is kept
# deliberately local and dependency-free so desktop builds do not need a
# Chinese word-segmentation runtime.
PROTECTED_TERMS = (
    "洗发水", "沐浴露", "护发素", "护肤品", "产品定位", "产品功效", "使用效果", "使用方法", "核心成分",
    "定位", "功效", "效果", "方法", "成分", "完整体系", "整个流程", "流程", "第一步", "第二步",
    "原料采购", "研发生产", "生产包装", "品质管理", "质量管理", "供应体系", "源头工厂", "生产车间",
    "品牌客户", "目标客户", "使用场景", "用户需求", "核心卖点", "产品介绍", "品牌故事", "口播文案",
    "洗发水品牌", "建立生产车间", "生产包装", "共同设计", "理清流程", "实际情况", "找产品",
    "不是", "而是", "但其实", "根本不是",
    "一站式", "小批量", "快速交付", "定制服务", "免费打样", "智能配音", "视频生成", "自动混剪",
    "OEM", "ODM", "OEMODM",
)

AI_SEGMENTATION_INSTRUCTION = """
请对生成的文案进行智能断句和排版
最终文案不得包含任何标点符号
只使用一个半角空格表示断句和换行
空格只能放在语义自然停顿的位置
优先在完整短语结束处 动作转换处 逻辑转折处 或不同信息点之间断句
每段建议八到十四个中文字符 最长不得超过十六个中文字符
文案过长时需要提前主动分段 不得超过长度后机械截断
不得拆开词语 固定搭配 产品名称 或行业术语
不得把的了和等虚词单独留在上一段或下一段
除改写任务本身要求的表达优化外 不得遗漏事实或调整信息顺序
只输出文案正文 不要解释 不要标题 不要序号
""".strip()

CLAUSE_PREFIXES = (
    "但是", "不过", "然而", "所以", "因此", "同时", "然后", "接着", "另外", "而且", "如果", "那么",
    "不仅", "还要", "现在", "最后", "首先", "其次", "通过", "根据", "针对", "比如", "例如", "其实",
    "今天", "我们", "你们", "他们", "共同", "从而", "并且", "或者", "以及", "但其实", "而是",
    "从", "形成",
)

CLAUSE_SUFFIXES = (
    "以后", "之前", "之后", "同时", "开始", "完成", "解决", "设计", "生产", "包装", "交付", "服务",
    "升级", "展示", "切换", "选择", "使用", "实现", "提供", "包括", "保证", "支持",
)

# Avoid an ugly line edge such as ``产品的`` / ``了 全部`` when another legal
# boundary is available.
EDGE_PARTICLES = set("的了和与及或把被将为在对从向给跟而但并就都也还又很更最才只呢吗啊着过地得")


def _is_separator(char: str) -> bool:
    category = unicodedata.category(char)
    return char.isspace() or category.startswith("P") or category.startswith("S")


def _clean_with_boundaries(text: str) -> tuple[str, set[int]]:
    chars: list[str] = []
    boundaries: set[int] = set()
    pending_boundary = False
    for char in str(text or ""):
        if _is_separator(char):
            pending_boundary = bool(chars)
            continue
        if pending_boundary and chars:
            boundaries.add(len(chars))
        chars.append(char)
        pending_boundary = False
    return "".join(chars), boundaries


def _forbidden_boundaries(text: str) -> set[int]:
    forbidden: set[int] = set()
    for term in PROTECTED_TERMS + CLAUSE_PREFIXES + CLAUSE_SUFFIXES:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            forbidden.update(range(index + 1, index + len(term)))
            start = index + 1
    return forbidden


def _boundary_scores(text: str, explicit: set[int]) -> dict[int, float]:
    scores = {position: 80.0 for position in explicit if 0 < position < len(text)}

    for term in PROTECTED_TERMS:
        start = 0
        while True:
            index = text.find(term, start)
            if index < 0:
                break
            scores[index] = max(scores.get(index, 0), 2.0)
            scores[index + len(term)] = max(scores.get(index + len(term), 0), 3.0)
            start = index + 1

    for marker in CLAUSE_PREFIXES:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            if index:
                scores[index] = max(scores.get(index, 0), 7.0)
            start = index + 1

    for marker in CLAUSE_SUFFIXES:
        start = 0
        while True:
            index = text.find(marker, start)
            if index < 0:
                break
            end = index + len(marker)
            if end < len(text):
                scores[end] = max(scores.get(end, 0), 5.0)
            start = index + 1
    return scores


def _edge_penalty(text: str, position: int) -> float:
    penalty = 0.0
    if position <= 0 or position >= len(text):
        return penalty
    if text[position - 1] in EDGE_PARTICLES:
        penalty += 16.0
    if text[position] in EDGE_PARTICLES:
        penalty += 12.0
    return penalty


def _length_score(length: int, final: bool) -> float:
    if 8 <= length <= 14:
        return 7.0 - abs(11 - length) * 1.2
    if length in (15, 16):
        return 1.0 - (length - 15) * 2.0
    # A short final phrase is normal; short middle segments are strongly
    # discouraged but remain possible for explicit semantic cuts.
    if final:
        return -8.0 - abs(6 - length) * 2.0
    return -22.0 - abs(8 - length) * 3.0


def _segment_chunk(text: str) -> list[str]:
    if len(text) <= 16:
        return [text] if text else []

    forbidden = _forbidden_boundaries(text)
    scores = _boundary_scores(text, set())
    size = len(text)
    best = [float("-inf")] * (size + 1)
    previous = [-1] * (size + 1)
    best[0] = 0.0

    for end in range(1, size + 1):
        if end < size and end in forbidden:
            continue
        for start in range(max(0, end - 16), end):
            if best[start] == float("-inf") or (start and start in forbidden):
                continue
            length = end - start
            value = best[start] + _length_score(length, end == size)
            if end < size:
                value += scores.get(end, 0.0) - _edge_penalty(text, end)
            if value > best[end]:
                best[end] = value
                previous[end] = start

    if previous[size] < 0:
        # Protected terms longer than the normal limit are kept intact rather
        # than corrupted.  This fallback only inserts legal boundaries.
        parts, start = [], 0
        while size - start > 16:
            end = min(size, start + 16)
            while end > start + 1 and end in forbidden:
                end -= 1
            parts.append(text[start:end])
            start = end
        parts.append(text[start:])
        return parts

    parts: list[str] = []
    cursor = size
    while cursor > 0:
        start = previous[cursor]
        parts.append(text[start:cursor])
        cursor = start
    parts.reverse()
    return parts


def smart_segment_text(text: str) -> str:
    """Return punctuation-free speech text with one ASCII space per cut.

    Existing punctuation/whitespace is treated as a strong semantic boundary.
    Oversized clauses are segmented by semantic candidates and a bounded
    dynamic-programming pass; original non-separator characters are preserved.
    """
    clean, explicit = _clean_with_boundaries(text)
    if not clean:
        return ""

    # Existing author cuts remain hard cuts.  Only oversized blocks are split.
    cuts = sorted(position for position in explicit if 0 < position < len(clean))
    blocks: list[str] = []
    start = 0
    for end in cuts + [len(clean)]:
        block = clean[start:end]
        if block:
            blocks.extend(_segment_chunk(block))
        start = end
    return " ".join(blocks)
