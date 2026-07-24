from __future__ import annotations

from app.services.memory_policy import (
    apply_recalled_choices,
    copy_memory_state,
    select_persistable_long_term_items,
)
from app.services.memory_policy import (
    capture_evidence_memory as capture_evidence_memory_service,
)
from app.services.memory_policy import (
    capture_recommendation_memory as capture_recommendation_memory_service,
)
from app.state.models import (
    EvidenceGraphState,
    FileGovernanceState,
    RecommendationGraphState,
)
from app.storage.memory_repository import MemoryRepository
from app.utils.error_context import create_node_error

"""本模块只定义在治理主图或业务子图中注册的 Memory 召回、应用、捕获和持久化节点。"""


def recall_long_term_memory(state: FileGovernanceState) -> dict:
    """从独立应用数据库召回当前工作空间的长期 Memory。

    Memory 默认关闭；启用后的数据库异常采用 fail-open 策略，只记录固定脱敏
    错误并继续确定性治理流程，不会把数据库异常内容写回长期 Memory。

    Args:
        state: 已初始化且包含 Memory 配置的顶层治理状态。

    Returns:
        更新后的 Memory 状态，以及失败时的非致命结构化错误。
    """
    memory = copy_memory_state(state.get("memory"))
    if not memory["enabled"]:
        return {"memory": memory}
    database_path = memory.get("database_path")
    if database_path is None:
        memory["status"] = "failed"
        memory["last_error"] = "长期 Memory 数据库路径未配置。"
        return {
            "memory": memory,
            "errors": [
                create_node_error(
                    state,
                    stage="memory_recall",
                    node_name="recall_long_term_memory",
                    category="memory",
                    message="长期 Memory 数据库路径未配置，已跳过历史召回。",
                    fatal=False,
                )
            ],
        }

    repository: MemoryRepository | None = None
    try:
        repository = MemoryRepository(
            database_path,
            input_root=state["workspace"]["input_root"],
            checkpoint_path=memory.get("checkpoint_path"),
        )
        memory["recalled_items"] = repository.recall(
            memory["namespace"],
            limit=memory["recall_limit"],
        )
        memory["status"] = "ready"
        memory["last_error"] = None
        return {"memory": memory}
    except Exception:
        memory["status"] = "failed"
        memory["last_error"] = "长期 Memory 召回失败，已安全降级。"
        return {
            "memory": memory,
            "errors": [
                create_node_error(
                    state,
                    stage="memory_recall",
                    node_name="recall_long_term_memory",
                    category="memory",
                    message="长期 Memory 召回失败，已继续使用当前运行事实。",
                    fatal=False,
                )
            ],
        }
    finally:
        if repository is not None:
            repository.close()


def capture_evidence_memory(state: EvidenceGraphState) -> dict:
    """把 Evidence 阶段计数和可靠关系写入安全 Memory 缓冲区。

    节点只传递记录 ID、匹配类型、置信度和固定模板摘要；发送对象、证据自由
    文本、文档正文及模型 Prompt 均不会进入 Memory。

    Args:
        state: 已完成证据置信度校验的 Evidence 子图状态。

    Returns:
        追加短期阶段摘要和待持久化长期关系后的 Memory 状态。
    """
    memory = copy_memory_state(state.get("memory"))
    if not memory["enabled"]:
        return {"memory": memory}
    source_run_id = state.get("run", {}).get("run_id")
    if not source_run_id:
        memory["status"] = "failed"
        memory["last_error"] = "Evidence Memory 缺少运行 ID。"
        return {"memory": memory}
    return {
        "memory": capture_evidence_memory_service(
            memory,
            source_run_id=source_run_id,
            pdf_exports=state.get("pdf_exports", []),
            deliveries=state.get("deliveries", []),
            confidence_threshold=float(
                state["request"].get("pdf_match_threshold", 0.82)
            ),
        )
    }


def apply_recalled_memory(state: RecommendationGraphState) -> dict:
    """把历史人工确认作为当前推荐的有界加分信号。

    节点不会直接选择主版本，也不会覆盖当前文件、版本链和外部证据；只有相同
    版本组且候选文件仍存在时才增加固定小分值。

    Args:
        state: 已生成基础候选评分的 Recommendation 子图状态。

    Returns:
        应用历史人工选择信号后的推荐记录。
    """
    memory = copy_memory_state(state.get("memory"))
    return {
        "decisions": apply_recalled_choices(
            state.get("decisions", []),
            memory["recalled_items"],
        )
    }


def capture_recommendation_memory(state: RecommendationGraphState) -> dict:
    """把 Recommendation 阶段结果计数写入当前运行的短期 Memory。

    Args:
        state: 已完成推荐结果校验的 Recommendation 子图状态。

    Returns:
        追加固定模板短期阶段摘要后的 Memory 状态。
    """
    memory = copy_memory_state(state.get("memory"))
    if not memory["enabled"]:
        return {"memory": memory}
    source_run_id = state.get("run", {}).get("run_id")
    if not source_run_id:
        memory["status"] = "failed"
        memory["last_error"] = "Recommendation Memory 缺少运行 ID。"
        return {"memory": memory}
    return {
        "memory": capture_recommendation_memory_service(
            memory,
            source_run_id=source_run_id,
            decisions=state.get("decisions", []),
        )
    }


def persist_long_term_memory(state: FileGovernanceState) -> dict:
    """把安全策略复验后的长期 Memory 幂等写入独立应用数据库。

    节点只持久化白名单结构化事实，短期摘要始终停留在当前 LangGraph 状态。
    数据库异常采用 fail-open 策略，不阻断报告和生命周期收口。

    Args:
        state: 已完成推荐和可选人工审核的顶层治理状态。

    Returns:
        更新持久化 ID、待写缓冲区和状态后的 Memory，以及可选非致命错误。
    """
    memory = copy_memory_state(state.get("memory"))
    if not memory["enabled"]:
        return {"memory": memory}
    try:
        items = select_persistable_long_term_items(memory)
    except (KeyError, TypeError, ValueError):
        memory["status"] = "failed"
        memory["last_error"] = "长期 Memory 内容安全复验失败。"
        return {
            "memory": memory,
            "errors": [
                create_node_error(
                    state,
                    stage="memory_persist",
                    node_name="persist_long_term_memory",
                    category="memory",
                    message="长期 Memory 内容未通过安全复验，已拒绝持久化。",
                    fatal=False,
                )
            ],
        }
    if not items:
        memory["status"] = "ready"
        memory["last_error"] = None
        return {"memory": memory}

    database_path = memory.get("database_path")
    if database_path is None:
        memory["status"] = "failed"
        memory["last_error"] = "长期 Memory 数据库路径未配置。"
        return {
            "memory": memory,
            "errors": [
                create_node_error(
                    state,
                    stage="memory_persist",
                    node_name="persist_long_term_memory",
                    category="memory",
                    message="长期 Memory 数据库路径未配置，已跳过持久化。",
                    fatal=False,
                )
            ],
        }

    repository: MemoryRepository | None = None
    try:
        repository = MemoryRepository(
            database_path,
            input_root=state["workspace"]["input_root"],
            checkpoint_path=memory.get("checkpoint_path"),
        )
        persisted_ids = repository.persist(
            run_id=state["run"]["run_id"],
            namespace=memory["namespace"],
            items=items,
        )
        persisted_set = set(memory["persisted_item_ids"]) | set(persisted_ids)
        memory["persisted_item_ids"] = sorted(persisted_set)
        memory["pending_long_term_items"] = [
            item
            for item in memory["pending_long_term_items"]
            if item["id"] not in persisted_set
        ]
        memory["status"] = "ready"
        memory["last_error"] = None
        return {"memory": memory}
    except Exception:
        memory["status"] = "failed"
        memory["last_error"] = "长期 Memory 持久化失败，已安全降级。"
        return {
            "memory": memory,
            "errors": [
                create_node_error(
                    state,
                    stage="memory_persist",
                    node_name="persist_long_term_memory",
                    category="memory",
                    message="长期 Memory 持久化失败，治理报告仍已正常收口。",
                    fatal=False,
                )
            ],
        }
    finally:
        if repository is not None:
            repository.close()
