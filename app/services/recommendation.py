from __future__ import annotations

import re
from collections.abc import Iterable

from app.state.models import (
    BranchRecord,
    DecisionRecord,
    FileRecord,
    VersionChainRecord,
    VersionGroupRecord,
)

"""本模块使用透明规则为每个版本组评分，并决定是否需要人工确认。"""


# 用于识别文件名中 final、最终版、定稿和终稿弱信号的正则表达式。
FINAL_MARKER_PATTERN = re.compile(r"(?i)(?:final|最终版?|定稿|终稿)")
# 用于识别已经确认、核准、发布或明确要求不再修改的强流程标记。
CONFIRMED_MARKER_PATTERN = re.compile(
    r"(?i)(?:确认稿|确认版|已确认|已批准|已审批|核准|发布版|发全员|别再?改(?:了)?|别改)"
)
# 默认优先推荐为主版本的可编辑文档扩展名集合。
DEFAULT_EDITABLE_EXTENSIONS = frozenset({".xlsx", ".docx"})


def score_version_candidates(
    group: VersionGroupRecord,
    files: Iterable[FileRecord],
    chain: VersionChainRecord,
    *,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """依据叶子节点、可编辑性、时间和流程标记为主版本候选评分。

    完全重复文件不单独参与候选评分。存在可编辑文件时，PDF 会被视为交付或
    导出件并受到轻微扣分；确认或发布标记强于普通最终版标记，但文件名和修改
    时间仍只贡献有限分数，不能单独形成高置信度结论。

    Args:
        group: 当前文档版本组。
        files: 全部或当前组的文件记录。
        chain: 当前组的版本链记录。
        editable_extensions: 优先作为主版本的可编辑扩展名。

    Returns:
        ``(候选评分, 每个候选的评分理由)``，评分范围为零到一。

    Raises:
        ValueError: 版本组或版本链引用未知文件时抛出。
    """
    file_by_id = {item["id"]: item for item in files}
    try:
        candidates = [
            file_by_id[file_id]
            for file_id in group["file_ids"]
            if file_by_id[file_id]["duplicate_of"] is None
        ]
    except KeyError as exc:
        raise ValueError(f"版本组引用未知文件：{exc}") from exc
    if chain["group_id"] != group["id"]:
        raise ValueError("版本链与版本组不匹配")
    if not candidates:
        return {}, {}

    normalized_editable = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in editable_extensions
    }
    has_editable = any(item["extension"] in normalized_editable for item in candidates)
    latest_modified_at = max(item["modified_at"] for item in candidates)
    leaf_ids = set(chain["leaf_file_ids"])
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    if len(candidates) == 1:
        only = candidates[0]
        return {only["id"]: 1.0}, {only["id"]: ["版本组只有一个非重复候选文件"]}

    for candidate in candidates:
        score = 0.15
        candidate_reasons = ["基础候选分 0.15"]
        if candidate["id"] in leaf_ids:
            score += 0.35
            candidate_reasons.append("位于版本链叶子节点 +0.35")
        if candidate["extension"] in normalized_editable:
            score += 0.18
            candidate_reasons.append("属于可编辑格式 +0.18")
        if candidate["modified_at"] == latest_modified_at:
            score += 0.18
            candidate_reasons.append("修改时间为组内最新 +0.18")
        if CONFIRMED_MARKER_PATTERN.search(candidate["file_name"]):
            score += 0.14
            candidate_reasons.append("文件名包含确认/核准/发布标记 +0.14")
        elif FINAL_MARKER_PATTERN.search(candidate["file_name"]):
            score += 0.08
            candidate_reasons.append("文件名包含 final/最终/定稿弱标记 +0.08")
        if chain["is_complete"]:
            score += 0.05
            candidate_reasons.append("版本链关系完整 +0.05")
        if has_editable and candidate["extension"] == ".pdf":
            score -= 0.10
            candidate_reasons.append("存在可编辑候选时 PDF 视为导出件 -0.10")

        scores[candidate["id"]] = round(min(1.0, max(0.0, score)), 4)
        reasons[candidate["id"]] = candidate_reasons
    return scores, reasons


def recommend_main_version(
    group: VersionGroupRecord,
    files: Iterable[FileRecord],
    chain: VersionChainRecord,
    branches: Iterable[BranchRecord],
    *,
    auto_select_threshold: float = 0.82,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> DecisionRecord:
    """为一个版本组生成可解释的主版本建议和人工确认标记。

    自动确认必须同时满足：没有分叉、版本链完整、最高分没有近似并列且综合
    置信度达到阈值。否则仍给出“当前候选”，但 ``selected_by`` 为
    ``unresolved``，调用方必须进入人工确认，不能据此删除或覆盖文件。

    Args:
        group: 当前文档版本组。
        files: 全部扫描文件记录。
        chain: 当前组的版本链。
        branches: 所有或当前组的分叉记录。
        auto_select_threshold: 自动确认所需最低置信度。
        editable_extensions: 优先作为主版本的可编辑扩展名。

    Returns:
        保留完整版本链的主版本推荐记录。

    Raises:
        ValueError: 阈值非法或状态引用不一致时抛出。
    """
    if not 0.0 <= auto_select_threshold <= 1.0:
        raise ValueError("auto_select_threshold 必须位于 0.0 到 1.0 之间")

    file_list = list(files)
    file_by_id = {item["id"]: item for item in file_list}
    scores, scoring_reasons = score_version_candidates(
        group,
        file_list,
        chain,
        editable_extensions=editable_extensions,
    )
    group_branches = [item for item in branches if item["group_id"] == group["id"]]

    if not scores:
        return DecisionRecord(
            id=f"decision:{group['id']}",
            group_id=group["id"],
            candidate_scores={},
            recommended_file_id=None,
            reasons=["版本组没有可用的非重复候选文件"],
            confidence=0.0,
            needs_human_review=True,
            selected_by="unresolved",
            preserve_file_ids=list(group["file_ids"]),
        )

    ranked = sorted(
        scores,
        key=lambda file_id: (
            scores[file_id],
            file_by_id[file_id]["modified_at"],
            file_by_id[file_id]["file_name"].casefold(),
            file_id,
        ),
        reverse=True,
    )
    winner_id = ranked[0]
    top_score = scores[winner_id]

    if len(ranked) == 1:
        margin = 1.0
        confidence = 1.0
    else:
        margin = top_score - scores[ranked[1]]
        confidence = 0.65 * top_score + 0.35 * min(max(margin, 0.0) / 0.25, 1.0)
    if not chain["is_complete"]:
        confidence -= 0.15
    if group_branches:
        confidence -= 0.25
    confidence = round(min(1.0, max(0.0, confidence)), 4)

    review_reasons: list[str] = list(scoring_reasons[winner_id])
    if group_branches:
        review_reasons.append(f"检测到 {len(group_branches)} 个版本分叉")
    if not chain["is_complete"]:
        review_reasons.append("版本链不完整或存在不确定关系")
    if len(ranked) > 1 and margin < 0.08:
        review_reasons.append(f"最高分与次高分差距仅为 {margin:.2f}")
    if confidence < auto_select_threshold:
        review_reasons.append(
            f"综合置信度 {confidence:.2f} 低于自动选择阈值 "
            f"{auto_select_threshold:.2f}"
        )

    needs_review = bool(
        group_branches
        or not chain["is_complete"]
        or (len(ranked) > 1 and margin < 0.08)
        or confidence < auto_select_threshold
    )
    return DecisionRecord(
        id=f"decision:{group['id']}",
        group_id=group["id"],
        candidate_scores=scores,
        recommended_file_id=winner_id,
        reasons=review_reasons,
        confidence=confidence,
        needs_human_review=needs_review,
        selected_by="unresolved" if needs_review else "rule",
        preserve_file_ids=list(group["file_ids"]),
    )


def recommend_main_versions(
    groups: Iterable[VersionGroupRecord],
    files: Iterable[FileRecord],
    chains: Iterable[VersionChainRecord],
    branches: Iterable[BranchRecord],
    *,
    auto_select_threshold: float = 0.82,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> list[DecisionRecord]:
    """为全部版本组分别生成主版本推荐，不跨文档组竞争。"""
    file_list = list(files)
    branch_list = list(branches)
    chain_by_group = {item["group_id"]: item for item in chains}
    decisions: list[DecisionRecord] = []
    for group in groups:
        chain = chain_by_group.get(group["id"])
        if chain is None:
            raise ValueError(f"版本组 {group['id']} 缺少版本链")
        decisions.append(
            recommend_main_version(
                group,
                file_list,
                chain,
                branch_list,
                auto_select_threshold=auto_select_threshold,
                editable_extensions=editable_extensions,
            )
        )
    return decisions


def apply_human_selection(
    decision: DecisionRecord,
    group: VersionGroupRecord,
    selected_file_id: str,
) -> DecisionRecord:
    """校验并应用用户对单个版本组做出的明确主版本选择。

    该函数只更新推荐状态，不删除、移动、重命名或覆盖任何文件。用户可以选择
    组内重复件，但完整版本链仍会通过 ``preserve_file_ids`` 保留。

    Args:
        decision: 当前待确认的推荐结果。
        group: 推荐结果所属版本组。
        selected_file_id: 用户明确选择的组内文件 ID。

    Returns:
        标记为人工选择、无需继续确认的新推荐记录。

    Raises:
        ValueError: 推荐与版本组不匹配，或选择文件不属于该组时抛出。
    """
    if decision["group_id"] != group["id"]:
        raise ValueError("推荐结果与版本组不匹配")
    if selected_file_id not in group["file_ids"]:
        raise ValueError("用户选择的文件不属于当前版本组")

    updated = dict(decision)
    updated["recommended_file_id"] = selected_file_id
    updated["reasons"] = [
        *decision["reasons"],
        "用户在人工确认阶段明确选择该文件作为主版本",
    ]
    updated["confidence"] = 1.0
    updated["needs_human_review"] = False
    updated["selected_by"] = "human"
    return DecisionRecord(**updated)
