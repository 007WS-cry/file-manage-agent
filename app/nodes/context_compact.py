from __future__ import annotations

from app.services.context_compaction import (
    append_context_summary,
    apply_context_compaction,
    build_context_compaction_plan,
    build_context_summary,
    copy_context_compact_state,
    copy_context_summary,
    estimate_compacted_context_tokens,
)
from app.state.models import ContextCompactGraphState
from app.storage.artifacts import save_context_compaction_artifact
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import ContextSummaryModel
from app.storage.repositories import create_repository_bundle
from app.utils.error_context import create_node_error

"""本模块只定义独立 Context Compact 子图显式注册的估算、压缩和持久化节点。"""


def estimate_context_tokens(state: ContextCompactGraphState) -> dict:
    """估算当前阶段上下文并生成确定性压缩计划。

    Args:
        state: 包含 Prompt、文档和 Context Compact 配置的子图状态。

    Returns:
        当前压缩计划；估算失败时返回非致命 Context 错误。
    """
    try:
        return {
            "plan": build_context_compaction_plan(
                stage=state["stage"],
                prompt=state["prompt"],
                documents=state.get("documents", []),
                context_compact=state.get("context_compact"),
            )
        }
    except (KeyError, TypeError, ValueError):
        context_compact = copy_context_compact_state(state.get("context_compact"))
        context_compact["status"] = "failed"
        context_compact["current_stage"] = state.get("stage")
        context_compact["last_error"] = "上下文 Token 估算失败。"
        return {
            "context_compact": context_compact,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="estimate_context_tokens",
                    category="context",
                    message="上下文 Token 估算失败，已跳过本次压缩。",
                    fatal=False,
                )
            ],
        }


def compact_context(state: ContextCompactGraphState) -> dict:
    """按照计划释放 Prompt 正文并移出不再参与决策的文档详情。

    本节点不会修改文件记录、content_ref、内容哈希、版本组、版本边、分叉、
    Evidence、推荐记录或人工审核字段。

    Args:
        state: 已生成可执行压缩计划的子图状态。

    Returns:
        压缩后的 Prompt、文档、未跟踪产物载荷和有界摘要草稿。
    """
    plan = state.get("plan")
    if plan is None:
        return {
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="compact_context",
                    category="context",
                    message="Context Compact 缺少压缩计划，已保留原上下文。",
                    fatal=False,
                )
            ]
        }
    try:
        context_compact = copy_context_compact_state(state.get("context_compact"))
        prompt, documents, payload = apply_context_compaction(
            plan=plan,
            prompt=state["prompt"],
            documents=state.get("documents", []),
            retained_preview_characters=context_compact["retained_preview_characters"],
        )
        estimated_tokens_after = estimate_compacted_context_tokens(
            prompt,
            documents,
        )
        summary = build_context_summary(
            run_id=state["run"]["run_id"],
            plan=plan,
            estimated_tokens_after=estimated_tokens_after,
            compaction_index=len(context_compact["summaries"]) + 1,
        )
        return {
            "prompt": prompt,
            "documents": documents,
            "compaction_payload": payload,
            "summary_draft": summary,
        }
    except (KeyError, TypeError, ValueError):
        context_compact = copy_context_compact_state(state.get("context_compact"))
        context_compact["status"] = "failed"
        context_compact["current_stage"] = state.get("stage")
        context_compact["last_error"] = "上下文压缩执行失败。"
        return {
            "context_compact": context_compact,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="compact_context",
                    category="context",
                    message="上下文压缩执行失败，已保留当前治理流程。",
                    fatal=False,
                )
            ],
        }


def persist_context_compaction_artifact(
    state: ContextCompactGraphState,
) -> dict:
    """把移出图状态的文档详情写入受控中间产物。

    Prompt 正文只会被释放，不会写入 Context Compact 产物。没有文档详情需要
    移出时，本节点直接保留摘要草稿并清空临时载荷。

    Args:
        state: 已完成内存压缩并包含未跟踪临时载荷的子图状态。

    Returns:
        补充受控产物引用的摘要草稿，以及被清空的临时载荷。
    """
    summary = state.get("summary_draft")
    payload = state.get("compaction_payload")
    if summary is None or not isinstance(payload, dict):
        return {"compaction_payload": None}
    removed_documents = payload.get("removed_documents", [])
    if not removed_documents:
        return {
            "summary_draft": copy_context_summary(summary),
            "compaction_payload": None,
        }
    try:
        artifact_ref = save_context_compaction_artifact(
            state["workspace"]["artifact_root"],
            state["run"]["run_id"],
            summary["compaction_index"],
            payload,
            input_root=state["workspace"]["input_root"],
        )
        updated_summary = copy_context_summary(summary)
        updated_summary["artifact_refs"] = [artifact_ref]
        return {
            "summary_draft": updated_summary,
            "compaction_payload": None,
        }
    except (KeyError, TypeError, ValueError, OSError):
        return {
            "summary_draft": copy_context_summary(summary),
            "compaction_payload": None,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="persist_context_compaction_artifact",
                    category="context",
                    message="Context Compact 产物写入失败，完整内容仍可由 content_ref 重建。",
                    fatal=False,
                )
            ],
        }


def persist_context_summary(state: ContextCompactGraphState) -> dict:
    """把有界 Context Summary 幂等写入应用数据库并更新顶层索引。

    数据库中只保存固定模板摘要、Token 估算、压缩序号和受控产物引用；文档详情
    和 Prompt 正文不会进入 ``context_summaries`` 表。数据库不可用时采用
    fail-open 策略，压缩结果和治理流程继续保留。

    Args:
        state: 已清空临时载荷并补齐可选产物引用的子图状态。

    Returns:
        追加摘要后的 Context Compact 状态，以及可选非致命数据库错误。
    """
    summary = state.get("summary_draft")
    context_compact = copy_context_compact_state(state.get("context_compact"))
    if summary is None:
        context_compact["status"] = "failed"
        context_compact["last_error"] = "Context Summary 草稿不存在。"
        return {
            "context_compact": context_compact,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="persist_context_summary",
                    category="context",
                    message="Context Summary 草稿不存在，已跳过持久化。",
                    fatal=False,
                )
            ],
        }

    result_context = append_context_summary(context_compact, summary)
    if not context_compact["persist_summaries"]:
        return {
            "context_compact": result_context,
            "summary_draft": None,
        }
    database_path = context_compact.get("database_path")
    if database_path is None:
        result_context["status"] = "failed"
        result_context["last_error"] = "Context Summary 数据库路径未配置。"
        return {
            "context_compact": result_context,
            "summary_draft": None,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="persist_context_summary",
                    category="context",
                    message="Context Summary 数据库路径未配置，已跳过持久化。",
                    fatal=False,
                )
            ],
        }

    engine = None
    try:
        engine = create_application_engine(
            database_path,
            input_root=state["workspace"]["input_root"],
            checkpoint_path=context_compact.get("checkpoint_path"),
        )
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            repositories.governance_runs.get_or_create_minimal(
                state["run"]["run_id"],
                thread_id=f"context:{state['run']['run_id']}",
                current_stage=summary["stage"],
                request_summary={"context_compact": True},
            )
            existing = repositories.context_summaries.find_by_run_and_index(
                state["run"]["run_id"],
                summary["compaction_index"],
            )
            if existing is None:
                repositories.context_summaries.add(
                    ContextSummaryModel(
                        id=summary["id"],
                        run_id=summary["run_id"],
                        stage=summary["stage"],
                        summary=summary["summary"],
                        artifact_refs=list(summary["artifact_refs"]),
                        estimated_tokens=summary["estimated_tokens"],
                        compaction_index=summary["compaction_index"],
                    )
                )
        return {
            "context_compact": result_context,
            "summary_draft": None,
        }
    except Exception:
        result_context["status"] = "failed"
        result_context["last_error"] = "Context Summary 持久化失败。"
        return {
            "context_compact": result_context,
            "summary_draft": None,
            "errors": [
                create_node_error(
                    state,
                    stage=f"context_compact_{state['stage']}",
                    node_name="persist_context_summary",
                    category="context",
                    message="Context Summary 持久化失败，治理流程继续执行。",
                    fatal=False,
                )
            ],
        }
    finally:
        if engine is not None:
            engine.dispose()


def mark_context_compaction_skipped(
    state: ContextCompactGraphState,
) -> dict:
    """记录当前阶段未达到压缩条件并正常结束子图。

    Args:
        state: 已完成 Token 估算但计划不要求压缩的子图状态。

    Returns:
        更新最近阶段和估算值、但不新增摘要的 Context Compact 状态。
    """
    context_compact = copy_context_compact_state(state.get("context_compact"))
    plan = state.get("plan")
    context_compact["current_stage"] = state.get("stage")
    context_compact["estimated_tokens"] = (
        int(plan["estimated_tokens_before"]) if plan is not None else 0
    )
    if context_compact["status"] != "failed":
        context_compact["status"] = "ready" if context_compact["enabled"] else "disabled"
        context_compact["last_error"] = None
    return {"context_compact": context_compact}
