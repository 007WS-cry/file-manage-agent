from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

"""本模块集中生成和校验 LLM 的 Pydantic 结构化输出，不执行模型或文件调用。"""

# 泛型结构化输出必须继承 Pydantic BaseModel。
StructuredOutputModel = TypeVar("StructuredOutputModel", bound=BaseModel)


def build_structured_output_schema(
    output_model: type[StructuredOutputModel],
) -> dict:
    """生成传给模型 Provider 的 Pydantic JSON Schema。

    Args:
        output_model: 期望模型返回的 Pydantic 输出类型。

    Returns:
        可序列化为 JSON 的结构化输出 Schema 副本。

    Raises:
        TypeError: 参数不是 Pydantic BaseModel 子类时抛出。
    """
    if not isinstance(output_model, type) or not issubclass(output_model, BaseModel):
        raise TypeError("output_model 必须是 Pydantic BaseModel 子类")
    return dict(output_model.model_json_schema())


def validate_structured_output(
    payload: object,
    output_model: type[StructuredOutputModel],
) -> StructuredOutputModel:
    """把 Provider 返回值严格校验为指定 Pydantic 输出类型。

    字符串仅作为完整 JSON 对象解析，不执行 Markdown 提取、正则修补或容错字段
    猜测；格式错误应显式失败并交由后续确定性回退处理。

    Args:
        payload: Provider 返回的 Python 对象、Pydantic 对象或 JSON 字符串。
        output_model: 期望得到的 Pydantic 输出类型。

    Returns:
        通过指定输出模型校验的新 Pydantic 对象。

    Raises:
        TypeError: 输出类型不是 Pydantic 模型或返回值类型不受支持时抛出。
        ValueError: JSON 或字段内容无法通过 Pydantic 校验时抛出。
    """
    build_structured_output_schema(output_model)
    try:
        if isinstance(payload, str):
            return output_model.model_validate_json(payload)
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="python")
        return output_model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"LLM 结构化输出校验失败：{exc}") from exc


def validate_output_artifact_refs(
    output: StructuredOutputModel,
    *,
    allowed_refs: Iterable[str],
) -> StructuredOutputModel:
    """校验结构化输出中的产物引用只能来自调用方白名单。

    Args:
        output: 已通过 Pydantic 校验的 Subagent 输出。
        allowed_refs: 当前任务允许返回的已有或新建产物引用。

    Returns:
        引用全部合法时原样返回输出对象。

    Raises:
        TypeError: 输出没有列表形式的 ``artifact_refs`` 字段时抛出。
        ValueError: 输出包含空引用、重复引用或白名单之外的引用时抛出。
    """
    raw_refs = getattr(output, "artifact_refs", None)
    if not isinstance(raw_refs, list) or any(
        not isinstance(item, str) for item in raw_refs
    ):
        raise TypeError("结构化输出必须包含字符串列表 artifact_refs")

    normalized_allowed = {
        item.strip()
        for item in allowed_refs
        if isinstance(item, str) and item.strip()
    }
    normalized_refs: list[str] = []
    for raw_ref in raw_refs:
        artifact_ref = raw_ref.strip()
        if not artifact_ref:
            raise ValueError("artifact_refs 不得包含空字符串")
        if artifact_ref in normalized_refs:
            raise ValueError(f"artifact_refs 不得包含重复引用：{artifact_ref}")
        if artifact_ref not in normalized_allowed:
            raise ValueError(f"LLM 返回了未授权产物引用：{artifact_ref}")
        normalized_refs.append(artifact_ref)
    return output
