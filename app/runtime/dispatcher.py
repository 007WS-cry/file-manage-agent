from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from app.graphs.file_governance import build_file_governance_graph
from app.runtime.job_queue import JobQueue, utc_now
from app.state.factories import create_initial_state
from app.state.models import (
    BackgroundJobState,
    FileGovernanceState,
    RequestState,
    WorkspaceState,
)
from app.storage.checkpoints import open_checkpointer

"""本模块规范化运行请求信封，并把前台调用或后台入队与 LangGraph 状态创建隔离。"""


# HTTP API 和 Worker 允许接收的固定执行方式。
EXECUTION_MODES = frozenset({"foreground", "background"})

# API 请求信封中必须提供的顶层对象字段。
REQUIRED_ENVELOPE_OBJECTS = ("request", "workspace")

# 相对路径统一相对 API 或 Worker 当前工作目录解析的请求字段。
REQUEST_PATH_FIELDS = ("root_directory",)

# 相对路径统一相对 API 或 Worker 当前工作目录解析的工作区字段。
WORKSPACE_PATH_FIELDS = ("input_root", "artifact_root", "report_root")


def _copy_optional_mapping(
    envelope: Mapping[str, Any],
    field_name: str,
) -> dict[str, Any] | None:
    """复制请求信封中的可选对象字段。

    Args:
        envelope: 已通过顶层对象校验的请求信封。
        field_name: 等待复制的可选字段名称。

    Returns:
        独立字典；字段缺失或显式为 None 时返回 None。

    Raises:
        ValueError: 字段存在但不是对象时抛出。
    """
    value = envelope.get(field_name)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须是对象或 null")
    return dict(value)


def _resolve_required_path(value: object, *, field_name: str) -> str:
    """把请求信封中的必需路径规范化为绝对路径。

    Args:
        value: 等待解析的路径字段。
        field_name: 用于异常说明的字段名称。

    Returns:
        相对当前进程工作目录解析后的绝对路径字符串。

    Raises:
        ValueError: 字段不是非空路径字符串时抛出。
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 必须是非空路径字符串")
    return str(Path(value).expanduser().resolve())


def _resolve_optional_path(value: object, *, field_name: str) -> str | None:
    """把请求信封中的可选路径规范化为绝对路径。

    Args:
        value: None 或等待解析的路径字段。
        field_name: 用于异常说明的字段名称。

    Returns:
        规范化绝对路径；输入为 None 时返回 None。
    """
    if value is None:
        return None
    return _resolve_required_path(value, field_name=field_name)


def normalize_runtime_envelope(
    payload: Mapping[str, Any],
    *,
    application_database_path: str | Path,
    checkpoint_path: str | Path,
) -> dict[str, Any]:
    """规范化 API/Worker 共用的治理请求信封并强制持久化运行配置。

    相对路径统一相对当前服务工作目录解析。后台任务必须启用同一个应用数据库，
    并固定使用 SQLite checkpoint；函数只复制和校验 JSON 数据，不读取业务文件。

    Args:
        payload: HTTP API 接收或后台任务持久化的请求信封。
        application_database_path: 十张应用表共用的 SQLite 文件。
        checkpoint_path: Worker 跨进程恢复使用的 SQLite checkpoint 文件。

    Returns:
        可以安全写入 JSON 数据库列并交给状态工厂的独立请求信封。

    Raises:
        TypeError: payload 不是 Mapping 时抛出。
        ValueError: 必需对象、路径字段或 JSON 序列化不合法时抛出。
    """
    if not isinstance(payload, Mapping):
        raise TypeError("payload 必须是对象")
    envelope = copy.deepcopy(dict(payload))
    for field_name in REQUIRED_ENVELOPE_OBJECTS:
        if not isinstance(envelope.get(field_name), Mapping):
            raise ValueError(f"请求信封必须包含 {field_name} 对象")

    request = dict(envelope["request"])
    workspace = dict(envelope["workspace"])
    for field_name in REQUEST_PATH_FIELDS:
        request[field_name] = _resolve_required_path(
            request.get(field_name),
            field_name=f"request.{field_name}",
        )
    for field_name in WORKSPACE_PATH_FIELDS:
        workspace[field_name] = _resolve_required_path(
            workspace.get(field_name),
            field_name=f"workspace.{field_name}",
        )
    request["delivery_log_path"] = _resolve_optional_path(
        request.get("delivery_log_path"),
        field_name="request.delivery_log_path",
    )
    envelope["request"] = request
    envelope["workspace"] = workspace

    prompt = _copy_optional_mapping(envelope, "prompt")
    if prompt is not None and prompt.get("source_path") is not None:
        prompt["source_path"] = _resolve_required_path(
            prompt["source_path"],
            field_name="prompt.source_path",
        )
        envelope["prompt"] = prompt

    database_path = str(Path(application_database_path).expanduser().resolve())
    normalized_checkpoint_path = str(Path(checkpoint_path).expanduser().resolve())
    application_database = _copy_optional_mapping(envelope, "application_database") or {}
    application_database.update(
        {
            "enabled": True,
            "backend": "sqlite",
            "database_path": database_path,
        }
    )
    envelope["application_database"] = application_database

    for field_name in ("memory", "context_compact"):
        config = _copy_optional_mapping(envelope, field_name)
        if config is not None and config.get("enabled") is True:
            config["database_path"] = database_path
            envelope[field_name] = config

    checkpoint = _copy_optional_mapping(envelope, "checkpoint") or {}
    checkpoint.update(
        {
            "backend": "sqlite",
            "database_path": normalized_checkpoint_path,
        }
    )
    envelope["checkpoint"] = checkpoint
    try:
        json.dumps(envelope, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("请求信封必须只包含可 JSON 序列化的数据") from exc
    return envelope


def build_runtime_initial_state(
    envelope: Mapping[str, Any],
    *,
    run_id: str,
    thread_id: str,
    execution_mode: Literal["foreground", "background"],
    background_job_id: str | None,
    worker_id: str | None,
) -> FileGovernanceState:
    """从规范化请求信封创建带外层运行身份的完整 LangGraph 初始状态。

    Args:
        envelope: 已由 ``normalize_runtime_envelope`` 规范化的请求信封。
        run_id: 外层提交阶段预先生成的治理运行 ID。
        thread_id: 持久化 Checkpointer 使用的线程 ID。
        execution_mode: 当前运行以前台还是后台方式执行。
        background_job_id: 后台任务 ID；前台运行时为 None。
        worker_id: 实际执行任务的 Worker ID；前台运行时为 None。

    Returns:
        可直接提交给顶层文件治理 LangGraph 的完整状态。

    Raises:
        ValueError: 执行方式、请求对象或配置对象不符合状态工厂协议时抛出。
    """
    if execution_mode not in EXECUTION_MODES:
        raise ValueError("execution_mode 只能是 foreground 或 background")
    request = cast(RequestState, dict(envelope["request"]))
    workspace = cast(WorkspaceState, dict(envelope["workspace"]))
    checkpoint = _copy_optional_mapping(envelope, "checkpoint") or {}
    checkpoint_path = checkpoint.get("database_path")
    state = create_initial_state(
        request,
        workspace,
        prompt_config=_copy_optional_mapping(envelope, "prompt"),
        hook_config=_copy_optional_mapping(envelope, "hooks"),
        llm_config=_copy_optional_mapping(envelope, "llm"),
        skill_registry_path=envelope.get("skill_registry_path"),
        memory_config=_copy_optional_mapping(envelope, "memory"),
        context_compact_config=_copy_optional_mapping(envelope, "context_compact"),
        application_database_config=_copy_optional_mapping(
            envelope,
            "application_database",
        ),
        recovery_config=_copy_optional_mapping(envelope, "recovery"),
        checkpoint_path=(
            str(checkpoint_path) if checkpoint.get("backend") == "sqlite" else None
        ),
        thread_id=thread_id,
    )
    state["run"].update(
        {
            "run_id": run_id,
            "thread_id": thread_id,
            "trigger_source": "manual",
            "execution_mode": execution_mode,
            "background_job_id": background_job_id,
            "worker_id": worker_id,
            "status": "queued" if execution_mode == "background" else "created",
        }
    )
    return state


def create_background_submission(
    payload: Mapping[str, Any],
    *,
    queue: JobQueue,
    checkpoint_path: str | Path,
    max_attempts: int = 3,
) -> BackgroundJobState:
    """创建后台运行身份、验证状态配置并持久化入队。

    Args:
        payload: API 接收的文件治理请求信封。
        queue: 已连接迁移后应用数据库的持久化队列。
        checkpoint_path: 后台 Worker 共用的 SQLite checkpoint 文件。
        max_attempts: Worker 崩溃或执行失败后允许的总领取次数。

    Returns:
        已提交数据库且状态为 queued 的后台任务。
    """
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise TypeError("max_attempts 必须是整数")
    if max_attempts < 1 or max_attempts > 20:
        raise ValueError("max_attempts 必须位于 1 到 20 之间")
    envelope = normalize_runtime_envelope(
        payload,
        application_database_path=queue.database_path,
        checkpoint_path=checkpoint_path,
    )
    run_id = uuid4().hex
    thread_id = uuid4().hex
    job_id = uuid4().hex
    build_runtime_initial_state(
        envelope,
        run_id=run_id,
        thread_id=thread_id,
        execution_mode="background",
        background_job_id=job_id,
        worker_id=None,
    )
    created_at = utc_now().isoformat()
    job = BackgroundJobState(
        id=job_id,
        run_id=run_id,
        thread_id=thread_id,
        trigger_source="manual",
        status="queued",
        request_payload=envelope,
        current_worker_id=None,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=created_at,
        claimed_at=None,
        started_at=None,
        report_path=None,
        error_summary=None,
        created_at=created_at,
        updated_at=created_at,
        finished_at=None,
    )
    request = envelope["request"]
    return queue.enqueue(
        job,
        request_summary={
            "recursive": bool(request.get("recursive", True)),
            "max_files": int(request.get("max_files", 0)),
            "allowed_extension_count": len(request.get("allowed_extensions", [])),
            "use_llm_summary": bool(request.get("use_llm_summary", False)),
            "execution_mode": "background",
        },
    )


def execute_foreground_submission(
    payload: Mapping[str, Any],
    *,
    application_database_path: str | Path,
    checkpoint_path: str | Path,
) -> dict[str, Any]:
    """在当前请求进程内执行一次治理图并返回完整内部结果。

    本函数供受信任的 Python 入口复用；HTTP API 应通过响应 Schema 过滤结果，
    不得直接返回文档状态、Prompt、Task 引用或完整图状态。

    Args:
        payload: API 或嵌入式调用提供的治理请求信封。
        application_database_path: 应用数据库路径。
        checkpoint_path: 当前前台运行使用的 SQLite checkpoint 路径。

    Returns:
        顶层 LangGraph 的完整内部执行结果。
    """
    envelope = normalize_runtime_envelope(
        payload,
        application_database_path=application_database_path,
        checkpoint_path=checkpoint_path,
    )
    run_id = uuid4().hex
    thread_id = uuid4().hex
    state = build_runtime_initial_state(
        envelope,
        run_id=run_id,
        thread_id=thread_id,
        execution_mode="foreground",
        background_job_id=None,
        worker_id=None,
    )
    with open_checkpointer(
        "sqlite",
        database_path=checkpoint_path,
        input_root=state["workspace"]["input_root"],
    ) as checkpointer:
        graph = build_file_governance_graph(checkpointer=checkpointer)
        return graph.invoke(
            state,
            config={"configurable": {"thread_id": thread_id}},
        )
