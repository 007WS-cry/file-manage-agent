from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.hooks import HookResult
from app.state.models import FileGovernanceState
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import ToolCallAuditModel
from app.storage.repositories import create_repository_bundle

"""本模块实现请求预检、状态补充、报告检查、审计入口和安全清理内置 Hook。"""


# 工具审计摘要使用固定短文本，不允许复制文档正文或完整工具输出。
MAX_TOOL_AUDIT_SUMMARY_CHARACTERS = 300

# 工具审计错误只保存固定类型说明，不回显解析器原始异常。
MAX_TOOL_AUDIT_ERROR_CHARACTERS = 300


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


def _create_tool_audit_id(
    run_id: str,
    tool_name: str,
    identity: str,
) -> str:
    """根据运行、工具和受控对象标识生成幂等审计 ID。

    Args:
        run_id: 当前治理运行 ID。
        tool_name: 静态 Python Tool 函数名。
        identity: 文件 ID、文档 ID 或固定调用标识，不得包含正文。

    Returns:
        带 ``tool-`` 前缀的稳定 SHA-256 标识。
    """
    digest = hashlib.sha256("\x1f".join((run_id, tool_name, identity)).encode("utf-8")).hexdigest()
    return f"tool-{digest}"


def _resolve_inventory_task_id(state: FileGovernanceState) -> str | None:
    """读取 Inventory Task ID，供文件扫描和解析审计建立关联。

    Args:
        state: 已执行或尝试执行 Inventory 阶段的顶层治理状态。

    Returns:
        找到时返回 Task ID，否则返回 None。
    """
    for task in state.get("tasks", []):
        if task.get("task_type") == "inventory":
            return task.get("task_id")
    return None


def _resolve_controlled_output_ref(
    content_ref: object,
    artifact_root: object,
) -> tuple[str | None, int]:
    """校验大型工具输出引用位于受控产物目录，并读取文件字节数。

    函数不会读取产物正文；引用越界、符号链接、文件缺失或类型不合法时返回空
    引用，避免把不受控路径写入应用数据库。

    Args:
        content_ref: DocumentRecord 保存的标准化内容产物路径。
        artifact_root: 当前运行配置的可写产物根目录。

    Returns:
        ``(绝对产物引用, 文件字节数)``；不安全时返回 ``(None, 0)``。
    """
    if not isinstance(content_ref, str) or not content_ref.strip():
        return None, 0
    if not isinstance(artifact_root, str) or not artifact_root.strip():
        return None, 0
    original_path = Path(content_ref).expanduser()
    if original_path.is_symlink():
        return None, 0
    try:
        resolved_path = original_path.resolve(strict=True)
        resolved_root = Path(artifact_root).expanduser().resolve(strict=True)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError):
        return None, 0
    if not resolved_path.is_file():
        return None, 0
    return str(resolved_path), resolved_path.stat().st_size


def _build_tool_audit_records(
    state: FileGovernanceState,
) -> list[ToolCallAuditModel]:
    """从最终治理事实构造不含参数、正文和完整输出的工具审计记录。

    文件扫描只保存计数。每个成功解析的文档只保存固定摘要和受控
    ``content_ref``；大型标准化正文继续留在产物文件中。解析错误仅保存固定错误
    类型和说明，不复制原始异常。

    Args:
        state: 已生成报告且准备执行 after_run Hooks 的顶层治理状态。

    Returns:
        可在一个应用数据库事务中幂等写入的 ORM 审计记录列表。
    """
    run = _require_mapping(state, "run")
    workspace = _require_mapping(state, "workspace")
    request = _require_mapping(state, "request")
    run_id = str(run.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("工具审计要求 run.run_id 不得为空")
    inventory_task_id = _resolve_inventory_task_id(state)
    files = state.get("files", [])
    records: list[ToolCallAuditModel] = []
    if inventory_task_id is not None or files:
        records.append(
            ToolCallAuditModel(
                id=_create_tool_audit_id(
                    run_id,
                    "discover_input_files",
                    "inventory-scan",
                ),
                run_id=run_id,
                task_id=inventory_task_id,
                tool_name="discover_input_files",
                status="success",
                output_summary=(f"只读目录扫描完成，共发现 {len(files)} 个文件。")[
                    :MAX_TOOL_AUDIT_SUMMARY_CHARACTERS
                ],
                output_ref=None,
                output_size_bytes=0,
                duration_ms=0,
                error_type=None,
                error_message=None,
            )
        )

    files_by_id = {str(file_record.get("id")): file_record for file_record in files}
    for document in state.get("documents", []):
        document_id = str(document.get("id", "")).strip()
        if not document_id:
            continue
        file_record = files_by_id.get(str(document.get("file_id")))
        extension = (
            str(file_record.get("extension", "")).casefold() if file_record is not None else ""
        )
        tool_name = {
            ".docx": "parse_docx_document",
            ".xlsx": "parse_xlsx_document",
            ".pdf": "parse_pdf_document",
        }.get(extension, "parse_document")
        output_ref, output_size_bytes = _resolve_controlled_output_ref(
            document.get("content_ref"),
            workspace.get("artifact_root"),
        )
        status = "success" if output_ref is not None else "failed"
        records.append(
            ToolCallAuditModel(
                id=_create_tool_audit_id(run_id, tool_name, document_id),
                run_id=run_id,
                task_id=inventory_task_id,
                tool_name=tool_name,
                status=status,
                output_summary=(
                    "标准化文档输出已保存为受控产物引用。"
                    if output_ref is not None
                    else "标准化文档输出引用未通过受控目录校验。"
                )[:MAX_TOOL_AUDIT_SUMMARY_CHARACTERS],
                output_ref=output_ref,
                output_size_bytes=output_size_bytes,
                duration_ms=0,
                error_type=None if output_ref is not None else "UnsafeOutputReference",
                error_message=(
                    None if output_ref is not None else "工具输出引用缺失、越界或不可读取。"
                ),
            )
        )

    for file_record in files:
        if file_record.get("parse_status") != "failed":
            continue
        file_id = str(file_record.get("id", "")).strip()
        if not file_id:
            continue
        records.append(
            ToolCallAuditModel(
                id=_create_tool_audit_id(
                    run_id,
                    "parse_document",
                    f"failed:{file_id}",
                ),
                run_id=run_id,
                task_id=inventory_task_id,
                tool_name="parse_document",
                status="failed",
                output_summary="文档解析未产生可用标准化产物。",
                output_ref=None,
                output_size_bytes=0,
                duration_ms=0,
                error_type="DocumentParseError",
                error_message=("文档解析失败；脱敏详情见治理错误记录。")[
                    :MAX_TOOL_AUDIT_ERROR_CHARACTERS
                ],
            )
        )

    delivery_log_path = request.get("delivery_log_path")
    if delivery_log_path is not None:
        evidence_task_id = next(
            (
                task.get("task_id")
                for task in state.get("tasks", [])
                if task.get("task_type") == "evidence"
            ),
            None,
        )
        records.append(
            ToolCallAuditModel(
                id=_create_tool_audit_id(
                    run_id,
                    "load_local_delivery_log",
                    "delivery-log",
                ),
                run_id=run_id,
                task_id=evidence_task_id,
                tool_name="load_local_delivery_log",
                status="success",
                output_summary=(
                    f"本地发送记录读取完成，共形成 {len(state.get('deliveries', []))} "
                    "条脱敏匹配记录。"
                )[:MAX_TOOL_AUDIT_SUMMARY_CHARACTERS],
                output_ref=None,
                output_size_bytes=0,
                duration_ms=0,
                error_type=None,
                error_message=None,
            )
        )
    return records


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
    """验证本次运行的应用数据库工具审计入口。

    本 Hook 不读取工具参数或业务正文。应用数据库关闭或初始化失败时只返回安全
    降级说明；数据库可用时确认后续 after_run Hook 可以幂等写入脱敏审计。

    Args:
        state: 当前文件治理顶层状态。

    Returns:
        不修改顶层业务状态的审计入口检查结果。

    Raises:
        ValueError: 工作空间没有保持只读时抛出。
    """
    workspace = _require_mapping(state, "workspace")
    if workspace.get("input_readonly") is not True:
        raise ValueError("工具审计入口要求原始输入工作空间保持只读")
    application_database = _require_mapping(state, "application_database")
    if application_database.get("enabled") is not True:
        return HookResult(
            message="应用数据库未启用，本次运行不持久化工具审计。",
            state_update={},
        )
    if application_database.get("status") != "ready":
        return HookResult(
            message="应用数据库当前不可用，工具审计采用安全降级。",
            state_update={},
        )
    return HookResult(
        message="应用数据库工具审计入口已就绪。",
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
    """把文件扫描、文档解析和发送日志工具审计写入应用数据库。

    工具参数、完整输出、文档正文和凭据不会进入 ``tool_call_audits``。标准化
    文档等大型输出只保存受控产物引用和字节数，重复执行使用稳定 ID 幂等跳过。

    Args:
        state: 当前文件治理顶层状态。

    Returns:
        不修改顶层业务状态的持久化结果。

    Raises:
        ValueError: 应用数据库配置、运行 ID 或工作空间不合法时抛出。
        Exception: 数据库连接、约束或事务错误交由 Hook runner 按策略处理。
    """
    run = _require_mapping(state, "run")
    workspace = _require_mapping(state, "workspace")
    application_database = _require_mapping(state, "application_database")
    if application_database.get("enabled") is not True:
        return HookResult(
            message="应用数据库未启用，工具审计保持在非持久化模式。",
            state_update={},
        )
    if application_database.get("status") != "ready":
        return HookResult(
            message="应用数据库不可用，已跳过工具审计持久化。",
            state_update={},
        )
    database_path = application_database.get("database_path")
    if not isinstance(database_path, str) or not database_path.strip():
        raise ValueError("工具审计缺少应用数据库路径")

    engine = create_application_engine(
        database_path,
        input_root=workspace.get("input_root"),
        checkpoint_path=application_database.get("checkpoint_path"),
        echo=bool(application_database.get("echo", False)),
        timeout_seconds=float(application_database.get("timeout_seconds", 30.0)),
    )
    try:
        session_factory = create_session_factory(engine)
        records = _build_tool_audit_records(state)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            run_id = str(run.get("run_id", "")).strip()
            thread_id = str(run.get("thread_id") or run_id)
            repositories.governance_runs.get_or_create_minimal(
                run_id,
                thread_id=thread_id,
                current_stage="tool_audit_flush",
                request_summary={"tool_audit": True},
            )
            persisted_count = 0
            for record in records:
                if repositories.tool_call_audits.get(record.id) is None:
                    repositories.tool_call_audits.add(record)
                    persisted_count += 1
        return HookResult(
            message=f"已幂等持久化 {persisted_count} 条脱敏工具审计。",
            state_update={},
        )
    finally:
        engine.dispose()


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
