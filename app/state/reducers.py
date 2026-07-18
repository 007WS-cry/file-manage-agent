from __future__ import annotations

from typing import Any

"""本模块提供 LangGraph 状态列表使用的确定性 reducer。"""


def merge_by_id(
    old_items: list[dict[str, Any]] | None,
    new_items: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """按照记录 ``id`` 合并状态列表，并让新字段覆盖同名旧字段。

    该函数不会修改输入列表或其中的字典，适用于 LangGraph 循环节点，
    也为后续通过 ``Send`` 并行返回局部记录预留了稳定合并语义。

    Args:
        old_items: 状态中已经存在的记录；首次调用时可以为 ``None``。
        new_items: 节点新返回的记录；没有新结果时可以为 ``None``。

    Returns:
        按首次出现顺序排列的合并结果。相同 ``id`` 的新记录会覆盖旧记录
        中的同名字段，同时保留旧记录中未被覆盖的字段。

    Raises:
        ValueError: 任意记录缺少非空字符串形式的 ``id`` 时抛出。
    """
    merged: dict[str, dict[str, Any]] = {}

    for item in old_items or []:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("参与 merge_by_id 的每条记录都必须包含非空字符串 id")
        merged[item_id] = dict(item)

    for item in new_items or []:
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("参与 merge_by_id 的每条记录都必须包含非空字符串 id")
        old_item = merged.get(item_id, {})
        merged[item_id] = {**old_item, **item}

    return list(merged.values())
