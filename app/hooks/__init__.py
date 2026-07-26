from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from app.state.models import FileGovernanceState, HookResult

"""本包定义 before/after run/model 生命周期 Hook 的公共执行协议。"""


# Hook 所属的四个生命周期阶段。
HookPhase = Literal["before_run", "before_model", "after_model", "after_run"]

# 静态注册表中 Hook 函数必须遵守的调用签名。
HookFunction = Callable[[FileGovernanceState], HookResult]

# 本包允许其他模块直接导入的公共类型名称。
__all__ = ["HookFunction", "HookPhase", "HookResult"]
