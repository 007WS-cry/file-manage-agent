from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from langgraph.errors import NodeError
from langgraph.types import Command
from pydantic import BaseModel

from app.services.recovery_policy import (
    apply_recovery_policy_to_error,
    resolve_category_policy,
)
from app.state.factories import copy_recovery_state
from app.state.models import (
    ErrorRecord,
    FileGovernanceState,
    NodeExecutionRecord,
    RecoveryGraphState,
)
from app.storage.artifacts import (
    load_json_artifact,
    save_intermediate_artifact,
)
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.repositories import create_repository_bundle
from app.utils.error_context import (
    create_error_context,
    create_node_error,
    create_node_execution_id,
)
from app.utils.runtime import create_error_record, utc_now_iso

"""本模块实现恢复目标白名单、子图幂等执行、结果产物复用及短事务恢复持久化。"""


# 顶层可重试节点到正常后继节点的固定映射，禁止恢复状态注入任意节点名称。
RECOVERY_NODE_TRANSITIONS = {
    "execute_before_run_hooks": "validate_request",
    "validate_request": "load_system_prompt",
    "load_system_prompt": "load_skill_registry",
    "load_skill_registry": "recall_long_term_memory",
    "recall_long_term_memory": "plan_run_tasks",
    "plan_run_tasks": "run_inventory_subgraph",
    "run_inventory_subgraph": "sync_inventory_task_status",
    "sync_inventory_task_status": "run_context_compact_after_inventory",
    "run_context_compact_after_inventory": "dispatch_content_subagent_task",
    "dispatch_content_subagent_task": "run_version_analysis_subgraph",
    "run_version_analysis_subgraph": "sync_version_task_status",
    "sync_version_task_status": "run_evidence_subgraph",
    "run_evidence_subgraph": "sync_evidence_task_status",
    "sync_evidence_task_status": "dispatch_evidence_subagent_task",
    "dispatch_evidence_subagent_task": "run_context_compact_after_evidence",
    "run_context_compact_after_evidence": "run_recommendation_subgraph",
    "run_recommendation_subgraph": "sync_recommendation_task_status",
    "sync_recommendation_task_status": "generate_governance_report",
    "sync_human_review_task_status": "generate_governance_report",
    "persist_long_term_memory": "execute_after_run_hooks",
    "execute_after_run_hooks": "finalize_run",
}

# 子图内部错误阶段到顶层包装节点的固定回退映射。
RECOVERY_STAGE_NODES = {
    "before_run": "execute_before_run_hooks",
    "before_run_hooks": "execute_before_run_hooks",
    "request_validation": "validate_request",
    "system_prompt": "load_system_prompt",
    "skills": "load_skill_registry",
    "memory_recall": "recall_long_term_memory",
    "memory_persist": "persist_long_term_memory",
    "team_orchestration": "plan_run_tasks",
    "inventory": "run_inventory_subgraph",
    "inventory_subgraph": "run_inventory_subgraph",
    "content_subagent": "dispatch_content_subagent_task",
    "context_compact_after_inventory": "run_context_compact_after_inventory",
    "version_analysis": "run_version_analysis_subgraph",
    "version_analysis_subgraph": "run_version_analysis_subgraph",
    "version_subagent": "run_version_analysis_subgraph",
    "evidence": "run_evidence_subgraph",
    "evidence_subgraph": "run_evidence_subgraph",
    "evidence_subagent": "dispatch_evidence_subagent_task",
    "context_compact_after_evidence": "run_context_compact_after_evidence",
    "recommendation": "run_recommendation_subgraph",
    "recommendation_subgraph": "run_recommendation_subgraph",
    "human_review": "sync_human_review_task_status",
    "after_run": "execute_after_run_hooks",
    "after_run_hooks": "execute_after_run_hooks",
}

# 固定 Task 类型到对应业务包装节点的映射，用于 team_orchestration 错误消歧。
RECOVERY_TASK_NODES = {
    "inventory": "run_inventory_subgraph",
    "version_analysis": "run_version_analysis_subgraph",
    "evidence": "run_evidence_subgraph",
    "recommendation": "run_recommendation_subgraph",
    "human_review": "sync_human_review_task_status",
    "report": "generate_failure_report",
}

# 允许在顶层条件边重新执行的节点集合。
RECOVERY_RETRY_NODES = frozenset(RECOVERY_NODE_TRANSITIONS)

# 允许在复用或降级后继续执行的节点集合。
RECOVERY_RESUME_AFTER_NODES = frozenset(RECOVERY_NODE_TRANSITIONS.values())

# 业务子图包装节点对应的 Task 类型，用于生成稳定执行身份。
RECOVERABLE_NODE_TASK_TYPES = {
    "run_inventory_subgraph": "inventory",
    "run_context_compact_after_inventory": "inventory",
    "run_version_analysis_subgraph": "version_analysis",
    "run_evidence_subgraph": "evidence",
    "run_context_compact_after_evidence": "evidence",
    "run_recommendation_subgraph": "recommendation",
    "recall_long_term_memory": "inventory",
    "persist_long_term_memory": "report",
}

# 计算子图输入摘要时允许读取的顶层状态字段。
RECOVERABLE_INPUT_FIELDS = (
    "request",
    "prompt",
    "context_compact",
    "files",
    "documents",
    "version_groups",
    "diffs",
    "version_edges",
    "branches",
    "version_chains",
    "pdf_exports",
    "deliveries",
    "decisions",
    "tasks",
)


def _json_safe(value: Any) -> Any:
    """把状态值转换为可稳定 JSON 编码的普通对象。

    Args:
        value: 状态、Pydantic 模型、路径或普通 JSON 值。

    Returns:
        不包含 Python 专有对象且保持字典键排序语义的值。

    Raises:
        TypeError: 值无法安全转换为 JSON 结构时抛出。
    """
    if isinstance(value, BaseModel):
        return _json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"状态值包含不可序列化类型：{type(value).__name__}")


def _stable_digest(value: Any) -> str:
    """计算普通状态对象的稳定 SHA-256 摘要。

    Args:
        value: 等待摘要的可 JSON 化状态值。

    Returns:
        不包含原始正文的十六进制 SHA-256 摘要。
    """
    payload = json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _task_for_node(
    state: Mapping[str, Any],
    node_name: str,
) -> Mapping[str, Any] | None:
    """读取业务包装节点对应的固定 Task。

    Args:
        state: 顶层或恢复图状态。
        node_name: 已进入白名单的业务包装节点名称。

    Returns:
        匹配的 Task 映射；生命周期或 Task 尚未创建时返回 None。
    """
    task_type = RECOVERABLE_NODE_TASK_TYPES.get(node_name)
    if task_type is None:
        return None
    return next(
        (task for task in state.get("tasks", []) if task.get("task_type") == task_type),
        None,
    )


def build_node_execution_identity(
    state: Mapping[str, Any],
    node_name: str,
) -> tuple[str, str]:
    """为业务子图包装调用生成稳定幂等键和安全输入摘要。

    Args:
        state: 当前顶层状态。
        node_name: 固定业务子图包装节点名称。

    Returns:
        ``(幂等键, 输入摘要)`` 二元组。

    Raises:
        ValueError: 节点不在业务包装白名单或运行 ID 为空时抛出。
    """
    if node_name not in RECOVERABLE_NODE_TASK_TYPES:
        raise ValueError(f"节点不支持幂等包装：{node_name}")
    run_id = str(state.get("run", {}).get("run_id", "")).strip()
    if not run_id:
        raise ValueError("生成节点执行身份前必须存在 run_id")
    task = _task_for_node(state, node_name)
    stable_state = {
        field_name: state.get(field_name)
        for field_name in RECOVERABLE_INPUT_FIELDS
        if field_name in state and field_name != "tasks"
    }
    stable_state["tasks"] = [
        {
            "task_id": item.get("task_id"),
            "execution_id": item.get("execution_id"),
            "task_type": item.get("task_type"),
            "input_refs": item.get("input_refs", []),
        }
        for item in state.get("tasks", [])
    ]
    input_payload = {
        "node_name": node_name,
        "task_execution_id": task.get("execution_id") if task is not None else None,
        "state": stable_state,
    }
    input_digest = _stable_digest(input_payload)
    identity_payload = "\x1f".join(
        (
            run_id,
            str(task.get("execution_id", "")) if task is not None else "",
            node_name,
        )
    )
    idempotency_key = "node-" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
    return idempotency_key, input_digest


def _copy_node_execution(
    execution: Mapping[str, Any],
    *,
    status: str | None = None,
) -> NodeExecutionRecord:
    """复制节点执行记录并解除结果引用列表的可变共享。

    Args:
        execution: 状态或 ORM 转换得到的节点执行映射。
        status: 可选替换状态。

    Returns:
        可写回 LangGraph reducer 的完整节点执行记录。
    """
    return cast(
        NodeExecutionRecord,
        {
            **dict(execution),
            "status": status or execution["status"],
            "result_refs": list(execution.get("result_refs", [])),
        },
    )


def _find_state_execution(
    state: Mapping[str, Any],
    idempotency_key: str,
) -> NodeExecutionRecord | None:
    """按幂等键读取图状态中的节点执行记录。

    Args:
        state: 顶层或恢复图状态。
        idempotency_key: 等待查找的稳定节点执行 ID。

    Returns:
        找到时返回独立副本，否则返回 None。
    """
    for execution in state.get("node_executions", []):
        if execution.get("id") == idempotency_key:
            return _copy_node_execution(execution)
    return None


def _orm_execution_to_state(record: Any) -> NodeExecutionRecord:
    """把节点执行 ORM 记录转换为图状态协议。

    Args:
        record: ``NodeExecutionRecordModel`` 查询结果。

    Returns:
        ISO 时间和结果引用均已规范化的节点执行状态。
    """
    return cast(
        NodeExecutionRecord,
        {
            "id": record.idempotency_key,
            "task_execution_id": record.task_execution_id,
            "run_id": record.run_id,
            "task_id": record.task_id,
            "stage": record.stage,
            "node_name": record.node_name,
            "input_digest": record.input_digest,
            "status": record.status,
            "attempt_count": record.attempt_count,
            "state_update_ref": record.state_update_ref,
            "result_refs": list(record.result_refs or []),
            "result_digest": record.result_digest,
            "last_error_id": record.last_error_id,
            "started_at": record.started_at.isoformat(),
            "finished_at": (
                record.finished_at.isoformat() if record.finished_at is not None else None
            ),
        },
    )


def _database_is_ready(state: Mapping[str, Any]) -> bool:
    """判断恢复持久化是否已显式启用并完成初始化。

    Args:
        state: 包含 ``application_database`` 的顶层或恢复图状态。

    Returns:
        配置启用、状态可用且数据库路径存在时返回 True。
    """
    database = state.get("application_database", {})
    return bool(
        database.get("enabled")
        and database.get("status") == "ready"
        and database.get("database_path")
    )


def load_persisted_node_execution(
    state: Mapping[str, Any],
    idempotency_key: str,
) -> NodeExecutionRecord | None:
    """使用独立短事务读取一个持久化节点执行记录。

    Args:
        state: 包含应用数据库和工作空间配置的图状态。
        idempotency_key: 等待读取的节点幂等键。

    Returns:
        数据库关闭或记录不存在时返回 None，否则返回状态副本。
    """
    if not _database_is_ready(state):
        return None
    database = state["application_database"]
    engine = create_application_engine(
        database["database_path"],
        input_root=state.get("workspace", {}).get("input_root"),
        checkpoint_path=database.get("checkpoint_path"),
        echo=bool(database.get("echo", False)),
        timeout_seconds=float(database.get("timeout_seconds", 30.0)),
    )
    try:
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            record = create_repository_bundle(session).node_execution_records.get(idempotency_key)
            return _orm_execution_to_state(record) if record is not None else None
    finally:
        engine.dispose()


def persist_node_execution(
    state: Mapping[str, Any],
    execution: NodeExecutionRecord,
) -> None:
    """在单个短事务内持久化节点执行状态。

    Args:
        state: 包含应用数据库、工作空间和运行配置的图状态。
        execution: 等待幂等写入的完整节点执行记录。
    """
    if not _database_is_ready(state):
        return
    database = state["application_database"]
    engine = create_application_engine(
        database["database_path"],
        input_root=state.get("workspace", {}).get("input_root"),
        checkpoint_path=database.get("checkpoint_path"),
        echo=bool(database.get("echo", False)),
        timeout_seconds=float(database.get("timeout_seconds", 30.0)),
    )
    try:
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            run = state["run"]
            repositories.governance_runs.get_or_create_minimal(
                execution["run_id"],
                thread_id=str(run.get("thread_id") or execution["run_id"]),
                current_stage=str(run.get("current_stage") or execution["stage"]),
                request_summary={"recovery_persistence": True},
            )
            repositories.node_execution_records.upsert_state(execution)
    finally:
        engine.dispose()


def persist_recovery_error(
    state: Mapping[str, Any],
    error: ErrorRecord,
    *,
    action: str,
) -> None:
    """在单个短事务内持久化错误恢复生命周期。

    Args:
        state: 包含应用数据库、工作空间和运行配置的图状态。
        error: 等待幂等写入的完整错误记录。
        action: 当前固定恢复动作。
    """
    if not _database_is_ready(state):
        return
    database = state["application_database"]
    engine = create_application_engine(
        database["database_path"],
        input_root=state.get("workspace", {}).get("input_root"),
        checkpoint_path=database.get("checkpoint_path"),
        echo=bool(database.get("echo", False)),
        timeout_seconds=float(database.get("timeout_seconds", 30.0)),
    )
    try:
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            run = state["run"]
            repositories.governance_runs.get_or_create_minimal(
                str(run["run_id"]),
                thread_id=str(run.get("thread_id") or run["run_id"]),
                current_stage=str(run.get("current_stage") or "error_recovery"),
                request_summary={"recovery_persistence": True},
            )
            repositories.error_recovery_records.upsert_state(
                str(run["run_id"]),
                error,
                action=action,
            )
    finally:
        engine.dispose()


def resolve_recovery_targets(
    error: Mapping[str, Any],
    state: Mapping[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """根据固定节点、Task 和阶段白名单解析恢复跳转目标。

    Args:
        error: 当前结构化错误。
        state: 可选顶层或恢复状态，用于根据 Task ID 消歧。

    Returns:
        ``(重新执行节点, 成功或降级后的后继节点)``；无法安全解析时均为 None。
    """
    node_name = str(error.get("node_name", ""))
    if node_name in RECOVERY_NODE_TRANSITIONS:
        return node_name, RECOVERY_NODE_TRANSITIONS[node_name]

    stage_node = RECOVERY_STAGE_NODES.get(str(error.get("stage", "")))
    if (
        error.get("stage") != "team_orchestration"
        and stage_node in RECOVERY_NODE_TRANSITIONS
    ):
        return stage_node, RECOVERY_NODE_TRANSITIONS[stage_node]

    if state is not None and error.get("task_id") is not None:
        task = next(
            (
                item
                for item in state.get("tasks", [])
                if item.get("task_id") == error.get("task_id")
            ),
            None,
        )
        if task is not None:
            task_node = RECOVERY_TASK_NODES.get(str(task.get("task_type", "")))
            if task_node in RECOVERY_NODE_TRANSITIONS:
                return task_node, RECOVERY_NODE_TRANSITIONS[task_node]

    if stage_node in RECOVERY_NODE_TRANSITIONS:
        return stage_node, RECOVERY_NODE_TRANSITIONS[stage_node]
    return None, None


def _build_execution_record(
    state: Mapping[str, Any],
    node_name: str,
    *,
    status: str,
    attempt_count: int,
    started_at: str,
    finished_at: str | None = None,
    state_update_ref: str | None = None,
    result_refs: list[str] | None = None,
    result_digest: str | None = None,
    last_error_id: str | None = None,
) -> NodeExecutionRecord:
    """构造一个完整的节点执行状态。

    Args:
        state: 当前顶层状态。
        node_name: 固定业务包装节点名称。
        status: 节点执行生命周期状态。
        attempt_count: 包含首次调用的累计尝试次数。
        started_at: 首次开始时间。
        finished_at: 可选完成时间。
        state_update_ref: 可选状态更新产物引用。
        result_refs: 可选业务结果引用。
        result_digest: 可选结果完整性摘要。
        last_error_id: 可选最近错误 ID。

    Returns:
        可写入图状态和持久化仓储的记录。
    """
    idempotency_key, input_digest = build_node_execution_identity(
        state,
        node_name,
    )
    task = _task_for_node(state, node_name)
    return cast(
        NodeExecutionRecord,
        {
            "id": idempotency_key,
            "task_execution_id": (str(task["execution_id"]) if task is not None else None),
            "run_id": str(state["run"]["run_id"]),
            "task_id": str(task["task_id"]) if task is not None else None,
            "stage": node_name.removeprefix("run_").removesuffix("_subgraph"),
            "node_name": node_name,
            "input_digest": input_digest,
            "status": status,
            "attempt_count": attempt_count,
            "state_update_ref": state_update_ref,
            "result_refs": list(result_refs or []),
            "result_digest": result_digest,
            "last_error_id": last_error_id,
            "started_at": started_at,
            "finished_at": finished_at,
        },
    )


def materialize_error_node_execution(
    state: Mapping[str, Any],
    error: Mapping[str, Any],
) -> NodeExecutionRecord:
    """为业务节点捕获的错误建立可持久化、可审计的失败执行记录。

    Args:
        state: 包含运行、Task DAG 和既有节点执行记录的顶层或恢复图状态。
        error: 已补齐 task_id 与 node_execution_id 的统一错误记录。

    Returns:
        与错误节点执行 ID 一致的既有记录副本，或新构造的失败记录。

    Raises:
        ValueError: 错误缺少运行、节点执行或节点名称等必要身份字段时抛出。
    """
    execution_id = str(error.get("node_execution_id") or "").strip()
    run_id = str(state.get("run", {}).get("run_id") or "").strip()
    node_name = str(error.get("node_name") or "").strip()
    if not execution_id or not run_id or not node_name:
        raise ValueError("错误节点执行记录缺少 node_execution_id、run_id 或 node_name")
    existing = next(
        (
            execution
            for execution in state.get("node_executions", [])
            if execution.get("id") == execution_id
        ),
        None,
    )
    if existing is not None:
        if existing.get("status") in {"succeeded", "reused"}:
            return _copy_node_execution(existing)
        copied = _copy_node_execution(existing, status="failed")
        copied["attempt_count"] = max(
            int(copied.get("attempt_count", 0)),
            int(error.get("retry_count", 0)) + 1,
        )
        copied["last_error_id"] = str(error.get("id") or "") or None
        copied["finished_at"] = utc_now_iso()
        return copied

    task_id = str(error.get("task_id") or "").strip() or None
    task = next(
        (
            item
            for item in state.get("tasks", [])
            if task_id is not None and item.get("task_id") == task_id
        ),
        None,
    )
    created_at = str(error.get("created_at") or utc_now_iso())
    return cast(
        NodeExecutionRecord,
        {
            "id": execution_id,
            "task_execution_id": (
                str(task["execution_id"]) if task is not None else None
            ),
            "run_id": run_id,
            "task_id": task_id,
            "stage": str(error.get("stage") or "unknown"),
            "node_name": node_name,
            "input_digest": _stable_digest(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "stage": error.get("stage"),
                    "node_name": node_name,
                }
            ),
            "status": "failed",
            "attempt_count": max(1, int(error.get("retry_count", 0)) + 1),
            "state_update_ref": None,
            "result_refs": [],
            "result_digest": None,
            "last_error_id": str(error.get("id") or "") or None,
            "started_at": created_at,
            "finished_at": utc_now_iso(),
        },
    )


def attach_error_execution_context(
    state: Mapping[str, Any],
    error: Mapping[str, Any],
    *,
    recovery_node: str | None,
) -> ErrorRecord:
    """为旧版或边界错误补齐 Task 与节点执行身份，同时保留原错误 ID。

    Args:
        state: 包含运行、恢复策略和可选 Task DAG 的顶层或恢复图状态。
        error: 已应用当前 Recovery Policy 的错误记录。
        recovery_node: 根据白名单解析出的顶层恢复节点。

    Returns:
        task_id 与 node_execution_id 均为非空字符串的错误副本。
    """
    task_type = (
        RECOVERABLE_NODE_TASK_TYPES.get(recovery_node)
        if recovery_node is not None
        else None
    )
    context = create_error_context(
        state,
        task_type=task_type,
        task_id=(
            str(error["task_id"])
            if error.get("task_id") is not None
            else None
        ),
    )
    node_name = str(error.get("node_name") or recovery_node or "unknown")
    return cast(
        ErrorRecord,
        {
            **dict(error),
            "task_id": str(error.get("task_id") or context["task_id"]),
            "node_execution_id": str(
                error.get("node_execution_id")
                or create_node_execution_id(context, node_name)
            ),
        },
    )


def _collect_result_references(value: Any) -> list[str]:
    """从状态更新中收集有界的产物引用。

    Args:
        value: 子图返回的状态更新。

    Returns:
        去重排序且最多包含 64 项的引用列表。
    """
    references: set[str] = set()

    def visit(item: Any, key_name: str = "") -> None:
        """递归读取引用字段，不解释或打开引用目标。"""
        if len(references) >= 64:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                visit(child, str(key))
            return
        if isinstance(item, list):
            for child in item:
                visit(child, key_name)
            return
        if isinstance(item, str) and (
            key_name.endswith("_ref")
            or key_name.endswith("_refs")
            or key_name in {"content_ref", "report_path"}
        ):
            references.add(item)

    visit(value)
    return sorted(references)[:64]


def _load_reusable_state_update(
    state: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> dict[str, Any]:
    """读取并校验成功节点保存的状态更新产物。

    Args:
        state: 包含受控产物根目录的顶层状态。
        execution: 成功或已复用的节点执行记录。

    Returns:
        通过结果摘要验证的状态更新。

    Raises:
        ValueError: 引用、包装结构或结果摘要不完整时抛出。
    """
    state_update_ref = execution.get("state_update_ref")
    result_digest = execution.get("result_digest")
    if not isinstance(state_update_ref, str) or not state_update_ref:
        raise ValueError("可复用节点缺少 state_update_ref")
    if not isinstance(result_digest, str) or not result_digest:
        raise ValueError("可复用节点缺少 result_digest")
    artifact = load_json_artifact(
        state_update_ref,
        expected_root=state["workspace"]["artifact_root"],
    )
    payload = artifact.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("节点状态更新产物缺少 payload")
    state_update = payload.get("state_update")
    if not isinstance(state_update, dict):
        raise ValueError("节点状态更新产物缺少 state_update")
    if _stable_digest(state_update) != result_digest:
        raise ValueError("节点状态更新产物摘要不匹配")
    return dict(state_update)


def _mark_active_error_recovered(
    state: Mapping[str, Any],
) -> tuple[list[ErrorRecord], dict[str, Any]] | None:
    """在重试节点成功后完成当前错误生命周期。

    Args:
        state: 进入业务包装节点前的顶层状态。

    Returns:
        存在当前重试错误时返回错误更新和 Recovery 更新，否则返回 None。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    error_id = recovery.get("current_error_id")
    if recovery.get("action") != "retry" or error_id is None:
        return None
    current_error = next(
        (error for error in state.get("errors", []) if error.get("id") == error_id),
        None,
    )
    if current_error is None:
        return None
    recovered_error = cast(
        ErrorRecord,
        {
            **dict(current_error),
            "status": "recovered",
            "fatal": False,
            "recovered_at": utc_now_iso(),
        },
    )
    recovery["pending_error_ids"] = [
        item_id for item_id in recovery["pending_error_ids"] if item_id != error_id
    ]
    recovery["current_error_id"] = None
    recovery["action"] = "none"
    recovery["resume_node"] = None
    recovery["resume_after_node"] = None
    recovery["retry_delay_seconds"] = 0.0
    recovery["fallback"] = None
    recovery["last_policy_reason"] = "重试节点已成功完成。"
    return [recovered_error], dict(recovery)


def _state_update_contains_active_error(
    state: Mapping[str, Any],
    state_update: Mapping[str, Any],
) -> bool:
    """判断重试结果是否再次返回当前恢复错误。

    Args:
        state: 进入重试包装节点前的顶层状态。
        state_update: 本次业务子图返回的公开状态更新。

    Returns:
        更新中仍包含同一错误 ID 且尚未进入恢复终态时返回 True。
    """
    recovery = state.get("recovery", {})
    error_id = recovery.get("current_error_id")
    if recovery.get("action") != "retry" or error_id is None:
        return False
    previous_error = next(
        (
            error
            for error in state.get("errors", [])
            if error.get("id") == error_id
        ),
        None,
    )
    if previous_error is None:
        return False
    for error in state_update.get("errors", []):
        if error.get("id") != error_id:
            continue
        if error.get("status") in {"recovered", "fallback_applied"}:
            continue
        return any(
            error.get(field_name) != previous_error.get(field_name)
            for field_name in ("status", "fatal", "retry_count", "exception_type", "message")
        )
    return False


def _reusable_execution_contains_active_error(
    state: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> bool:
    """判断成功执行产物是否仍包含当前等待重试的错误。

    Args:
        state: 准备重试业务包装节点的顶层状态。
        execution: 输入摘要一致且表面状态为成功的既有执行记录。

    Returns:
        产物仍携带当前未解决错误时返回 True，此时必须重新执行而不能复用。
    """
    recovery = state.get("recovery", {})
    error_id = recovery.get("current_error_id")
    if recovery.get("action") != "retry" or error_id is None:
        return False
    state_update = _load_reusable_state_update(state, execution)
    return any(
        error.get("id") == error_id
        and error.get("status") not in {"recovered", "fallback_applied"}
        for error in state_update.get("errors", [])
    )


def execute_recoverable_subgraph(
    state: FileGovernanceState,
    *,
    node_name: str,
    invoke_subgraph: Callable[[], Mapping[str, Any]],
    convert_result: Callable[[Mapping[str, Any]], dict],
) -> dict:
    """以短事务和受控产物包装一次业务子图调用。

    调用前先按幂等键查询成功记录；只有输入摘要一致、产物位于受控根目录且结果
    摘要匹配时才复用。数据库 Session 在查询、开始记录和完成记录各自的短事务
    结束时关闭，绝不会跨越子图执行或 interrupt。独立单元调用尚未经过
    ``initialize_run`` 时保留旧行为，直接执行子图且不创建幂等记录。

    Args:
        state: 当前顶层治理状态。
        node_name: 已在顶层图注册的业务子图包装节点名称。
        invoke_subgraph: 不带参数、执行隔离子图并返回子图状态的调用器。
        convert_result: 把子图结果过滤为顶层白名单更新的转换器。

    Returns:
        子图或复用的状态更新，以及本次节点执行记录。
    """
    if not str(state.get("run", {}).get("run_id", "")).strip():
        return convert_result(invoke_subgraph())
    idempotency_key, input_digest = build_node_execution_identity(
        state,
        node_name,
    )
    existing = _find_state_execution(state, idempotency_key)
    persisted = load_persisted_node_execution(state, idempotency_key)
    if persisted is not None and (
        existing is None or persisted["attempt_count"] >= existing["attempt_count"]
    ):
        existing = persisted

    if (
        existing is not None
        and existing["input_digest"] == input_digest
        and existing["status"] in {"succeeded", "reused"}
        and not _reusable_execution_contains_active_error(state, existing)
    ):
        state_update = _load_reusable_state_update(state, existing)
        reused = _copy_node_execution(existing, status="reused")
        reused["finished_at"] = utc_now_iso()
        persist_node_execution(state, reused)
        state_update["node_executions"] = [reused]
        recovered = (
            None
            if _state_update_contains_active_error(state, state_update)
            else _mark_active_error_recovered(state)
        )
        if recovered is not None:
            error_updates, recovery_update = recovered
            state_update["errors"] = [
                *state_update.get("errors", []),
                *error_updates,
            ]
            state_update["recovery"] = recovery_update
            persist_recovery_error(
                {**state, "recovery": recovery_update},
                error_updates[0],
                action="reuse_result",
            )
        return state_update

    attempt_count = existing["attempt_count"] + 1 if existing is not None else 1
    started_at = existing["started_at"] if existing is not None else utc_now_iso()
    running = _build_execution_record(
        state,
        node_name,
        status="running",
        attempt_count=attempt_count,
        started_at=started_at,
    )
    persist_node_execution(state, running)

    subgraph_result = invoke_subgraph()
    state_update = convert_result(subgraph_result)
    json_state_update = cast(dict[str, Any], _json_safe(state_update))
    result_digest = _stable_digest(json_state_update)
    artifact_name = (
        "node-result-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
    )
    state_update_ref = save_intermediate_artifact(
        state["workspace"]["artifact_root"],
        str(state["run"]["run_id"]),
        artifact_name,
        {
            "node_name": node_name,
            "input_digest": input_digest,
            "result_digest": result_digest,
            "state_update": json_state_update,
        },
        input_root=state["workspace"]["input_root"],
    )
    succeeded = _build_execution_record(
        state,
        node_name,
        status="succeeded",
        attempt_count=attempt_count,
        started_at=started_at,
        finished_at=utc_now_iso(),
        state_update_ref=state_update_ref,
        result_refs=_collect_result_references(json_state_update),
        result_digest=result_digest,
    )
    persist_node_execution(state, succeeded)
    state_update["node_executions"] = [succeeded]

    recovered = (
        None
        if _state_update_contains_active_error(state, state_update)
        else _mark_active_error_recovered(state)
    )
    if recovered is not None:
        error_updates, recovery_update = recovered
        state_update["errors"] = [
            *state_update.get("errors", []),
            *error_updates,
        ]
        state_update["recovery"] = recovery_update
        persist_recovery_error(
            {**state, "recovery": recovery_update},
            error_updates[0],
            action="retry",
        )
    return state_update


def _classify_exception(exception: BaseException) -> str:
    """把未捕获异常映射到固定恢复错误类别。

    Args:
        exception: 子图边界捕获的异常对象。

    Returns:
        timeout、filesystem、validation、protocol 或 unknown 中的一个类别。
    """
    if isinstance(exception, TimeoutError):
        return "timeout"
    if isinstance(exception, OSError):
        return "filesystem"
    if isinstance(exception, (TypeError, ValueError)):
        return "validation"
    if isinstance(exception, (KeyError, LookupError)):
        return "protocol"
    return "unknown"


def capture_subgraph_exception(
    state: FileGovernanceState,
    error: NodeError,
) -> Command:
    """把子图包装节点的未捕获异常转换为 Recovery 入口。

    本处理器只记录脱敏错误、失败执行和固定恢复目标，然后跳到 Error Recovery。
    它不判断是否重试、不选择业务降级，也不吞掉 Recovery 自身抛出的异常。

    Args:
        state: 异常发生前的顶层治理状态。
        error: LangGraph 注入的失败节点名称和原始异常。

    Returns:
        更新结构化恢复状态并跳转到恢复包装节点的 ``Command``。
    """
    node_name = error.node
    try:
        idempotency_key, _ = build_node_execution_identity(state, node_name)
        existing = _find_state_execution(state, idempotency_key)
        persisted = load_persisted_node_execution(state, idempotency_key)
        if persisted is not None and (
            existing is None or persisted["attempt_count"] >= existing["attempt_count"]
        ):
            existing = persisted
        attempt_count = max(existing["attempt_count"], 1) if existing is not None else 1
        started_at = existing["started_at"] if existing is not None else utc_now_iso()
        category = _classify_exception(error.error)
        category_policy = resolve_category_policy(
            state["recovery"]["policy"],
            category,
        )
        boundary_context = create_error_context(
            state,
            task_type=RECOVERABLE_NODE_TASK_TYPES.get(node_name),
        )
        raw_error = create_error_record(
            stage=node_name.removeprefix("run_").removesuffix("_subgraph"),
            node_name=node_name,
            category=cast(Any, category),
            message=f"{node_name} 子图边界捕获到未处理异常。",
            task_id=boundary_context["task_id"],
            node_execution_id=idempotency_key,
            exception_type=type(error.error).__name__,
            retryable=category_policy["retryable"],
            retry_count=0,
            max_retries=category_policy["max_retries"],
            fallback=category_policy["fallback"],
            requires_human=category_policy["requires_human"],
            status="pending",
            fatal=True,
        )
        recovery_error = apply_recovery_policy_to_error(
            raw_error,
            state["recovery"]["policy"],
        )
        failed = _build_execution_record(
            state,
            node_name,
            status="failed",
            attempt_count=attempt_count,
            started_at=started_at,
            finished_at=utc_now_iso(),
            last_error_id=recovery_error["id"],
        )
        recovery = copy_recovery_state(state.get("recovery"))
        retry_node, resume_after_node = resolve_recovery_targets(
            recovery_error,
            state,
        )
        recovery["pending_error_ids"] = list(
            dict.fromkeys(
                [
                    *recovery["pending_error_ids"],
                    recovery_error["id"],
                ]
            )
        )
        recovery["current_error_id"] = recovery_error["id"]
        recovery["action"] = "none"
        recovery["resume_node"] = retry_node
        recovery["resume_after_node"] = resume_after_node
        recovery["last_policy_reason"] = "子图未捕获异常已进入统一恢复流程。"
        run = dict(state["run"])
        run.update(
            {
                "status": "recovering",
                "current_stage": "error_recovery",
            }
        )
        persistence_state = {
            **state,
            "run": run,
            "recovery": recovery,
            "errors": [*state.get("errors", []), recovery_error],
            "node_executions": [
                *state.get("node_executions", []),
                failed,
            ],
        }
        try:
            persist_node_execution(persistence_state, failed)
            persist_recovery_error(
                persistence_state,
                recovery_error,
                action="none",
            )
        except Exception:
            pass
        return Command(
            update={
                "run": run,
                "errors": [recovery_error],
                "node_executions": [failed],
                "recovery": recovery,
            },
            goto="run_error_recovery_subgraph",
        )
    except Exception:
        fallback_error = apply_recovery_policy_to_error(
            create_node_error(
                state,
                stage="error_recovery",
                node_name=node_name,
                category="unknown",
                message="子图异常入口无法建立完整幂等上下文。",
                exception=error.error,
                fatal=True,
            ),
            state["recovery"]["policy"],
        )
        recovery = copy_recovery_state(state.get("recovery"))
        retry_node, resume_after_node = resolve_recovery_targets(
            fallback_error,
            state,
        )
        recovery["pending_error_ids"] = list(
            dict.fromkeys(
                [
                    *recovery["pending_error_ids"],
                    fallback_error["id"],
                ]
            )
        )
        recovery["current_error_id"] = fallback_error["id"]
        recovery["action"] = "none"
        recovery["resume_node"] = retry_node
        recovery["resume_after_node"] = resume_after_node
        recovery["last_policy_reason"] = "子图异常已进入统一恢复流程，但缺少幂等执行上下文。"
        run = dict(state["run"])
        run.update({"status": "recovering", "current_stage": "error_recovery"})
        return Command(
            update={
                "run": run,
                "errors": [fallback_error],
                "recovery": recovery,
            },
            goto="run_error_recovery_subgraph",
        )


def hydrate_recovery_graph_state(
    state: RecoveryGraphState,
    *,
    top_state: FileGovernanceState | None = None,
) -> RecoveryGraphState:
    """从应用数据库补充当前错误关联的最新节点执行记录。

    Args:
        state: 已由顶层转换得到的恢复子图状态。
        top_state: 可选完整顶层状态，用于复验当前输入摘要。

    Returns:
        数据库关闭时原样复制，否则合并较新执行记录后的状态。
    """
    recovery = copy_recovery_state(state.get("recovery"))
    current_error_id = recovery.get("current_error_id")
    current_error = next(
        (error for error in state.get("errors", []) if error.get("id") == current_error_id),
        None,
    )
    if current_error is None or current_error.get("node_execution_id") is None:
        return state
    execution_id = str(current_error["node_execution_id"])
    expected_digest: str | None = None
    filtered_executions = list(state.get("node_executions", []))
    if top_state is not None:
        try:
            expected_key, expected_digest = build_node_execution_identity(
                top_state,
                str(current_error["node_name"]),
            )
        except ValueError:
            return state
        if execution_id != expected_key:
            return cast(
                RecoveryGraphState,
                {
                    **state,
                    "node_executions": [
                        item
                        for item in state.get("node_executions", [])
                        if item.get("id") != execution_id
                    ],
                },
            )
        filtered_executions = [
            item
            for item in filtered_executions
            if item.get("id") != execution_id or item.get("input_digest") == expected_digest
        ]
    persisted = load_persisted_node_execution(
        state,
        execution_id,
    )
    if persisted is None:
        return cast(
            RecoveryGraphState,
            {
                **state,
                "node_executions": filtered_executions,
            },
        )
    if expected_digest is not None and persisted["input_digest"] != expected_digest:
        return cast(
            RecoveryGraphState,
            {
                **state,
                "node_executions": [
                    item for item in filtered_executions if item.get("id") != execution_id
                ],
            },
        )
    return cast(
        RecoveryGraphState,
        {
            **state,
            "node_executions": [
                *filtered_executions,
                persisted,
            ],
        },
    )


def load_recovery_reused_update(
    top_state: FileGovernanceState,
    recovery_state: RecoveryGraphState,
) -> dict[str, Any]:
    """在 Recovery 选择结果复用后加载已验证的顶层状态更新。

    Args:
        top_state: 进入恢复包装节点前的完整顶层状态。
        recovery_state: 已完成恢复决策的子图状态。

    Returns:
        未选择复用时为空字典；选择复用时为受摘要保护的状态更新。
    """
    if recovery_state["recovery"].get("action") != "reuse_result":
        return {}
    error_id = recovery_state["recovery"].get("current_error_id")
    error = next(
        (item for item in recovery_state.get("errors", []) if item.get("id") == error_id),
        None,
    )
    if error is None or error.get("node_execution_id") is None:
        raise ValueError("结果复用缺少关联节点执行 ID")
    execution = next(
        (
            item
            for item in recovery_state.get("node_executions", [])
            if item.get("id") == error["node_execution_id"]
            and item.get("status") in {"succeeded", "reused"}
        ),
        None,
    )
    if execution is None:
        raise ValueError("结果复用缺少成功节点执行记录")
    expected_key, expected_digest = build_node_execution_identity(
        top_state,
        str(error["node_name"]),
    )
    if execution.get("id") != expected_key:
        raise ValueError("结果复用的节点幂等键与当前调用不一致")
    if execution.get("input_digest") != expected_digest:
        raise ValueError("结果复用的输入摘要与当前调用不一致")
    return _load_reusable_state_update(top_state, execution)
