from __future__ import annotations

from app.services.document_grouping import (
    group_related_documents as group_related_documents_service,
)
from app.services.version_graph import (
    build_version_chains as build_version_chains_service,
)
from app.services.version_graph import (
    build_version_edges as build_version_edges_service,
)
from app.services.version_graph import (
    compare_document_pair as compare_document_pair_service,
)
from app.services.version_graph import (
    detect_version_branches as detect_version_branches_service,
)
from app.services.version_graph import (
    generate_candidate_pairs as generate_candidate_pairs_service,
)
from app.services.version_graph import (
    infer_version_direction as infer_version_direction_service,
)
from app.state.models import ComparisonJob, DiffRecord, VersionAnalysisGraphState
from app.utils.runtime import create_error_record
from app.utils.state_lookup import find_comparison_job_by_id

"""本模块实现版本分组、文件对比较、关系建边和版本链校验的 LangGraph 节点。"""


def group_related_documents(state: VersionAnalysisGraphState) -> dict:
    """根据文件名、标准化内容和关键字段建立互不重叠的版本组。"""
    try:
        parsed_file_ids = {
            item["id"]
            for item in state.get("files", [])
            if item["parse_status"] == "parsed"
        }
        analyzable_files = [
            item
            for item in state.get("files", [])
            if item["id"] in parsed_file_ids
            or (
                item["parse_status"] == "duplicate"
                and item["duplicate_of"] in parsed_file_ids
            )
        ]
        groups = group_related_documents_service(
            analyzable_files,
            state.get("documents", []),
            similarity_threshold=state["request"]["grouping_similarity_threshold"],
        )
        return {
            "version_groups": groups,
            "comparison_jobs": [],
            "comparison_queue": [],
            "current_comparison_id": None,
            "current_diff": None,
            "current_comparison_error": None,
        }
    except (OSError, TypeError, ValueError) as exc:
        return {
            "version_groups": [],
            "comparison_jobs": [],
            "comparison_queue": [],
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="group_related_documents",
                    category="comparison",
                    message=str(exc),
                    fatal=True,
                )
            ],
        }


def add_duplicate_version_edges(state: VersionAnalysisGraphState) -> dict:
    """在内容比较前为 SHA-256 完全一致的文件建立重复关系边。"""
    try:
        edges = build_version_edges_service(
            state.get("version_groups", []),
            state.get("files", []),
            [],
        )
        return {"version_edges": edges}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="add_duplicate_version_edges",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ]
        }


def generate_candidate_pairs(state: VersionAnalysisGraphState) -> dict:
    """为每个版本组生成去除完全重复项后的候选文件对任务。"""
    try:
        jobs = generate_candidate_pairs_service(
            state.get("version_groups", []),
            state.get("files", []),
        )
        return {"comparison_jobs": jobs}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "comparison_jobs": [],
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="generate_candidate_pairs",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ],
        }


def build_comparison_queue(state: VersionAnalysisGraphState) -> dict:
    """按比较任务生成顺序建立尚未处理的任务 ID 队列。"""
    queue = [
        item["id"]
        for item in state.get("comparison_jobs", [])
        if item["status"] == "pending"
    ]
    return {
        "comparison_queue": queue,
        "current_comparison_id": None,
        "current_diff": None,
        "current_comparison_error": None,
    }


def load_next_comparison(state: VersionAnalysisGraphState) -> dict:
    """从队列取出下一个文件对，并把对应任务标记为运行中。"""
    queue = list(state.get("comparison_queue", []))
    if not queue:
        return {
            "current_comparison_id": None,
            "current_diff": None,
            "current_comparison_error": "比较队列为空",
        }
    job_id = queue[0]
    job = next(
        (item for item in state.get("comparison_jobs", []) if item["id"] == job_id),
        None,
    )
    if job is None:
        return {
            "comparison_queue": queue[1:],
            "current_comparison_id": job_id,
            "current_diff": None,
            "current_comparison_error": "比较队列引用了不存在的任务",
        }
    updated_job = dict(job)
    updated_job["status"] = "running"
    return {
        "comparison_queue": queue[1:],
        "comparison_jobs": [ComparisonJob(**updated_job)],
        "current_comparison_id": job_id,
        "current_diff": None,
        "current_comparison_error": None,
    }


def compare_document_pair(state: VersionAnalysisGraphState) -> dict:
    """读取标准化产物并生成当前文件对的完整确定性差异草稿。"""
    job = find_comparison_job_by_id(
        state.get("comparison_jobs", []),
        state.get("current_comparison_id"),
    )
    if job is None:
        return {"current_diff": None, "current_comparison_error": "当前比较任务不存在"}

    file_by_id = {item["id"]: item for item in state.get("files", [])}
    document_by_file = {item["file_id"]: item for item in state.get("documents", [])}
    try:
        left_file = file_by_id[job["left_file_id"]]
        right_file = file_by_id[job["right_file_id"]]
        left_document = document_by_file[job["left_file_id"]]
        right_document = document_by_file[job["right_file_id"]]
        diff = compare_document_pair_service(
            job["group_id"],
            left_file,
            right_file,
            left_document,
            right_document,
        )
        return {"current_diff": diff, "current_comparison_error": None}
    except (KeyError, OSError, TypeError, ValueError) as exc:
        return {"current_diff": None, "current_comparison_error": str(exc)}


def infer_version_direction(state: VersionAnalysisGraphState) -> dict:
    """再次显式应用版本方向规则，并把可解释顺序证据写入差异草稿。"""
    job = find_comparison_job_by_id(
        state.get("comparison_jobs", []),
        state.get("current_comparison_id"),
    )
    diff = state.get("current_diff")
    if job is None or diff is None or state.get("current_comparison_error"):
        return {}
    file_by_id = {item["id"]: item for item in state.get("files", [])}
    try:
        older_id, newer_id, signals, ordering_confidence = infer_version_direction_service(
            file_by_id[job["left_file_id"]],
            file_by_id[job["right_file_id"]],
        )
        updated = dict(diff)
        updated.update(
            {
                "older_file_id": older_id,
                "newer_file_id": newer_id,
                "ordering_signals": signals,
                "confidence": round(
                    min(1.0, 0.5 * ordering_confidence + 0.5 * diff["confidence"]),
                    4,
                ),
            }
        )
        return {"current_diff": DiffRecord(**updated)}
    except (KeyError, TypeError, ValueError) as exc:
        return {"current_diff": None, "current_comparison_error": str(exc)}


def summarize_key_changes(state: VersionAnalysisGraphState) -> dict:
    """为当前差异生成不依赖 LLM 的稳定中文关键修改摘要。

    ``use_llm_summary`` 尚未接入模型客户端；即使请求该选项，节点也
    使用确定性摘要，避免因模型不可用而阻断版本治理。
    """
    diff = state.get("current_diff")
    if diff is None or state.get("current_comparison_error"):
        return {}
    updated = dict(diff)
    if diff["key_changes"]:
        summary = f"检测到 {len(diff['key_changes'])} 项关键字段变化。"
    elif diff["content_similarity"] == 1.0:
        summary = "标准化内容完全一致。"
    else:
        summary = f"标准化内容相似度为 {diff['content_similarity']:.2f}。"
    if diff["older_file_id"] is None:
        summary += " 当前证据不足以判断版本先后。"
    if state["request"].get("use_llm_summary"):
        summary += " 当前未配置 LLM 摘要器，已使用确定性摘要。"
    updated["summary"] = summary
    return {"current_diff": DiffRecord(**updated)}


def record_diff_result(state: VersionAnalysisGraphState) -> dict:
    """提交当前差异记录，并把比较任务标记为完成。"""
    job = find_comparison_job_by_id(
        state.get("comparison_jobs", []),
        state.get("current_comparison_id"),
    )
    diff = state.get("current_diff")
    if job is None or diff is None:
        return record_comparison_error(
            {
                **state,
                "current_comparison_error": "无法提交缺失的比较任务或差异记录",
            }
        )
    updated_job = dict(job)
    updated_job["status"] = "completed"
    return {
        "comparison_jobs": [ComparisonJob(**updated_job)],
        "diffs": [diff],
        "current_comparison_id": None,
        "current_diff": None,
        "current_comparison_error": None,
    }


def record_comparison_error(state: VersionAnalysisGraphState) -> dict:
    """记录单个文件对的非致命比较错误，并继续后续比较任务。"""
    job = find_comparison_job_by_id(
        state.get("comparison_jobs", []),
        state.get("current_comparison_id"),
    )
    error_message = state.get("current_comparison_error") or "未知文件对比较错误"
    if job is None:
        jobs = []
        related_file_id = None
    else:
        updated_job = dict(job)
        updated_job["status"] = "failed"
        jobs = [ComparisonJob(**updated_job)]
        related_file_id = job["left_file_id"]
    return {
        "comparison_jobs": jobs,
        "errors": [
            create_error_record(
                stage="version_analysis",
                node_name="record_comparison_error",
                category="comparison",
                message=error_message,
                related_file_id=related_file_id,
                fatal=False,
            )
        ],
        "current_comparison_id": None,
        "current_diff": None,
        "current_comparison_error": None,
    }


def build_version_edges(state: VersionAnalysisGraphState) -> dict:
    """根据重复记录和成功差异构建稀疏版本关系边。"""
    try:
        edges = build_version_edges_service(
            state.get("version_groups", []),
            state.get("files", []),
            state.get("diffs", []),
        )
        return {"version_edges": edges}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="build_version_edges",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ]
        }


def detect_version_branches(state: VersionAnalysisGraphState) -> dict:
    """识别同一父版本拥有多个直接派生子版本的分叉。"""
    return {
        "branches": detect_version_branches_service(
            state.get("version_groups", []),
            state.get("version_edges", []),
        )
    }


def build_version_chains(state: VersionAnalysisGraphState) -> dict:
    """对确定方向的版本边拓扑排序并生成每组可读版本链。"""
    try:
        chains = build_version_chains_service(
            state.get("version_groups", []),
            state.get("files", []),
            state.get("version_edges", []),
        )
        return {"version_chains": chains}
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "errors": [
                create_error_record(
                    stage="version_analysis",
                    node_name="build_version_chains",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ]
        }


def validate_version_results(state: VersionAnalysisGraphState) -> dict:
    """校验每个版本组都具有且只具有一条对应版本链。

    主版本推荐已在第四批迁移到独立 Recommendation 子图，因此本节点不再读取
    或要求 ``decisions``，避免 Evidence 和 Recommendation 尚未执行时误报失败。

    Args:
        state: 已完成版本边、分叉和版本链构建的 Version Analysis 子图状态。

    Returns:
        版本组与版本链一一对应时返回空更新，否则返回致命校验错误。
    """
    group_ids = {item["id"] for item in state.get("version_groups", [])}
    chains = state.get("version_chains", [])
    chain_group_ids = {item["group_id"] for item in chains}
    messages = []
    if group_ids - chain_group_ids:
        messages.append(f"{len(group_ids - chain_group_ids)} 个版本组缺少版本链")
    if chain_group_ids - group_ids:
        messages.append(f"{len(chain_group_ids - group_ids)} 条版本链引用未知版本组")
    if len(chains) != len(chain_group_ids):
        messages.append("同一版本组存在多条版本链")
    if not messages:
        return {}
    return {
        "errors": [
            create_error_record(
                stage="version_analysis",
                node_name="validate_version_results",
                category="validation",
                message="；".join(messages),
                fatal=True,
            )
        ]
    }
