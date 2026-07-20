from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.hooks import HookResult
from app.state.models import FileGovernanceState

"""本模块实现请求预检、状态补充、报告检查、审计入口和安全清理内置 Hook。"""


def _require_mapping(
    state: FileGovernanceState,
    field_name: str,
) -> Mapping[str, Any]:
    """读取并校验顶层状态中的对象字段。

    Args:
        state: 当前文件治理顶层状态。
        field_name: 必须存在且值为对象的字段名称。

    Returns:
        对应字段的只读映射视图。

    Raises:
        ValueError: 字段缺失或不是对象时抛出。
    """
    value = state.get(field_name)
    if not isinstance(value, Mapping):
        raise ValueError(f"顶层状态缺少有效的 {field_name} 对象")
    return value


def validate_request_envelope_hook(state: FileGovernanceState) -> HookResult:
    """对治理请求信封执行不访问文件系统的只读安全预检。

    本 Hook 只检查请求与工作空间字段的基本类型和只读意图，不替代顶层图中不可
    关闭的 ``validate_request`` 路径解析与目录隔离校验，也不会读取或修改业务文件。

    Args:
        state: 初始化完成、尚未进入业务子图的顶层治理状态。

    Returns:
        不修改业务状态的预检成功结果。

    Raises:
        ValueError: 请求缺少必要字段、文件上限非法或未声明输入只读时抛出。
    """
    request = _require_mapping(state, "request")
    workspace = _require_mapping(state, "workspace")

    for field_name in ("root_directory",):
        value = request.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"request.{field_name} 必须是非空路径字符串")
    input_root = workspace.get("input_root")
    if not isinstance(input_root, str) or not input_root.strip():
        raise ValueError("workspace.input_root 必须是非空路径字符串")
    if workspace.get("input_readonly") is not True:
        raise ValueError("文件治理要求 workspace.input_readonly 必须为 True")

    allowed_extensions = request.get("allowed_extensions")
    if not isinstance(allowed_extensions, list) or not allowed_extensions:
        raise ValueError("request.allowed_extensions 必须是非空列表")
    if any(not isinstance(item, str) or not item.strip() for item in allowed_extensions):
        raise ValueError("request.allowed_extensions 只能包含非空字符串")

    max_files = request.get("max_files")
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files <= 0:
        raise ValueError("request.max_files 必须是大于零的整数")
    return HookResult(message="治理请求信封预检通过。", state_update={})


def enrich_run_state_hook(state: FileGovernanceState) -> HookResult:
    """复制运行信息并标记当前正在执行 before_run Hooks。

    Args:
        state: 包含初始化运行信息的顶层治理状态。

    Returns:
        仅更新 ``run.current_stage`` 的 Hook 结果。

    Raises:
        ValueError: 顶层状态缺少有效运行信息时抛出。
    """
    run = dict(_require_mapping(state, "run"))
    if not isinstance(run.get("status"), str):
        raise ValueError("run.status 必须是字符串")
    run["current_stage"] = "before_run_hooks"
    return HookResult(
        message="已补充 before_run 生命周期阶段。",
        state_update={"run": run},
    )


def initialize_tool_audit_hook(state: FileGovernanceState) -> HookResult:
    """初始化本次运行的最小工具审计入口。

    当前版本没有应用数据库和 ``tool_calls`` 顶层状态，本 Hook 只验证只读工作空间
    并由 runner 生成可追踪 HookEvent；不会读取工具参数、业务正文或创建审计文件。

    Args:
        state: 当前文件治理顶层状态。

    Returns:
        不修改顶层业务状态的审计入口初始化结果。

    Raises:
        ValueError: 工作空间没有保持只读时抛出。
    """
    workspace = _require_mapping(state, "workspace")
    if workspace.get("input_readonly") is not True:
        raise ValueError("工具审计入口要求原始输入工作空间保持只读")
    return HookResult(
        message="已初始化最小工具审计入口；详细工具记录将在后续版本接入。",
        state_update={},
    )


def validate_report_result_hook(state: FileGovernanceState) -> HookResult:
    """检查治理报告是否包含可交付的摘要、正文和生成时间。

    本 Hook 不修改报告内容，也不读取报告路径指向的文件；检查失败应由 runner 按
    配置的 ``block`` 或 ``ignore`` 策略记录，避免无报告结果被静默标记为完成。

    Args:
        state: 已生成成功、无数据或失败报告的顶层治理状态。

    Returns:
        不修改顶层状态的报告检查成功结果。

    Raises:
        ValueError: 报告摘要、Markdown 正文或生成时间缺失时抛出。
    """
    report = _require_mapping(state, "report")
    required_fields = {
        "summary": "报告摘要",
        "report_markdown": "Markdown 报告正文",
        "generated_at": "报告生成时间",
    }
    for field_name, label in required_fields.items():
        value = report.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}不得为空")
    return HookResult(message="治理报告结果检查通过。", state_update={})


def flush_tool_audit_hook(state: FileGovernanceState) -> HookResult:
    """完成当前版本的最小工具审计收口。

    当前版本只依靠 HookEvent 证明审计入口按顺序执行，不写数据库或本地审计文件，
    也不会把工具输出、文件正文或凭据复制到事件中。

    Args:
        state: 当前文件治理顶层状态。

    Returns:
        不修改顶层业务状态的最小审计收口结果。
    """
    _require_mapping(state, "run")
    return HookResult(
        message="最小工具审计已收口；未持久化业务正文或工具输出。",
        state_update={},
    )


def cleanup_run_resources_hook(state: FileGovernanceState) -> HookResult:
    """执行不会触碰原始业务文件的生命周期清理占位操作。

    第二批尚未创建临时 Worktree、后台任务或外部连接，因此本 Hook 只验证输入仍为
    只读并记录清理完成，不删除目录、不移动文件，也不修改任何原始业务内容。

    Args:
        state: 准备结束运行的顶层治理状态。

    Returns:
        不修改顶层业务状态的安全清理结果。

    Raises:
        ValueError: 工作空间不再声明输入只读时抛出。
    """
    workspace = _require_mapping(state, "workspace")
    if workspace.get("input_readonly") is not True:
        raise ValueError("生命周期清理拒绝在非只读输入工作空间中执行")
    return HookResult(
        message="生命周期资源清理检查完成；没有删除或修改原始文件。",
        state_update={},
    )
