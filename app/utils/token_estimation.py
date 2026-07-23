from __future__ import annotations

import json
import math
from typing import Any

"""本模块提供不调用模型或分词服务的确定性上下文 Token 近似估算。"""


# 连续 ASCII 文本平均每四个字符近似一个 Token。
ASCII_CHARACTERS_PER_TOKEN = 4

# 非 ASCII 字符使用一字符一 Token 的保守估算，避免低估中文上下文。
NON_ASCII_CHARACTERS_PER_TOKEN = 1


def estimate_text_tokens(text: str) -> int:
    """估算一段文本占用的 Token 数量。

    该函数不连接外部 tokenizer，不读取模型配置，也不会记录输入文本。ASCII
    字符按每四个字符一个 Token 估算，中文等非 ASCII 字符按一字符一个 Token
    保守估算，结果只用于决定是否触发 Context Compact。

    Args:
        text: 等待估算的文本。

    Returns:
        非负的确定性近似 Token 数。

    Raises:
        TypeError: ``text`` 不是字符串时抛出。
    """
    if not isinstance(text, str):
        raise TypeError("text 必须是字符串")
    ascii_count = sum(1 for character in text if character.isascii())
    non_ascii_count = len(text) - ascii_count
    return math.ceil(ascii_count / ASCII_CHARACTERS_PER_TOKEN) + (
        non_ascii_count // NON_ASCII_CHARACTERS_PER_TOKEN
    )


def estimate_value_tokens(value: Any) -> int:
    """把 JSON 兼容值稳定序列化后估算 Token 数。

    Args:
        value: 字符串、数字、布尔值、null、列表或字典等 JSON 兼容值。

    Returns:
        稳定 JSON 表示的近似 Token 数。

    Raises:
        TypeError: 值无法由标准 JSON 编码器序列化时抛出。
    """
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise TypeError("value 必须是 JSON 兼容值") from exc
    return estimate_text_tokens(serialized)
