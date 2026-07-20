from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict

from app.state.models import FileGovernanceState

"""本包定义 before/after run/model 生命周期 Hook 的公共执行协议。"""


class HookResult(TypedDict):
    """单个 Hook 的返回结果：提供简短说明和受限顶层状态更新。"""

    message: str
    # Hook 执行结果的简短说明，不得包含文档正文或敏感工具输出。

    state_update: dict[str, Any]
    # Hook 建议合并到顶层状态的字段；runner 会校验允许修改的范围。


# Hook 所属的四个生命周期阶段。
HookPhase = Literal["before_run", "before_model", "after_model", "after_run"]

# 静态注册表中 Hook 函数必须遵守的调用签名。
HookFunction = Callable[[FileGovernanceState], HookResult]

# 本包允许其他模块直接导入的公共类型名称。
__all__ = ["HookFunction", "HookPhase", "HookResult"]
