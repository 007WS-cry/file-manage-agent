from __future__ import annotations

import re
from collections.abc import Iterable

from app.state.models import (
    BranchRecord,
    DecisionRecord,
    DeliveryRecord,
    FileRecord,
    PdfExportRecord,
    RecommendationCandidateSet,
    VersionChainRecord,
    VersionGroupRecord,
)

"""本模块提供 Recommendation 子图使用的确定性候选、证据和置信度规则。"""


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


def find_editable_leaf_versions(
    group: VersionGroupRecord,
    files: Iterable[FileRecord],
    chain: VersionChainRecord,
    *,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> RecommendationCandidateSet:
    """建立一个版本组的非重复候选集合并标记可编辑叶子版本。

    Args:
        group: 当前文档版本组。
        files: 全部扫描文件记录。
        chain: 当前组的版本链记录。
        editable_extensions: 可以作为可编辑主版本的扩展名集合。

    Returns:
        包含全部非重复候选以及可编辑叶子候选的集合。

    Raises:
        ValueError: 版本链不属于当前组或状态引用未知文件时抛出。
    """
    if chain["group_id"] != group["id"]:
        raise ValueError("版本链与版本组不匹配")

    file_by_id = {item["id"]: item for item in files}
    unknown_file_ids = [file_id for file_id in group["file_ids"] if file_id not in file_by_id]
    if unknown_file_ids:
        raise ValueError(f"版本组引用未知文件：{unknown_file_ids[0]}")
    if not set(chain["leaf_file_ids"]).issubset(group["file_ids"]):
        raise ValueError("版本链叶子文件不属于当前版本组")

    normalized_editable = {
        value.lower() if value.startswith(".") else f".{value.lower()}"
        for value in editable_extensions
    }
    candidate_file_ids = [
        file_id
        for file_id in group["file_ids"]
        if file_by_id[file_id]["duplicate_of"] is None
    ]
    leaf_file_ids = set(chain["leaf_file_ids"])
    editable_leaf_file_ids = [
        file_id
        for file_id in candidate_file_ids
        if file_id in leaf_file_ids
        and file_by_id[file_id]["extension"] in normalized_editable
    ]
    return RecommendationCandidateSet(
        id=f"candidate-set:{group['id']}",
        group_id=group["id"],
        candidate_file_ids=candidate_file_ids,
        editable_leaf_file_ids=editable_leaf_file_ids,
    )


def create_scored_decision(
    group: VersionGroupRecord,
    files: Iterable[FileRecord],
    chain: VersionChainRecord,
    candidate_set: RecommendationCandidateSet,
    *,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> DecisionRecord:
    """把一个候选集合转换为尚未选择主版本的基础评分记录。

    Args:
        group: 当前文档版本组。
        files: 全部扫描文件记录。
        chain: 当前组的版本链记录。
        candidate_set: 已建立的组内候选集合。
        editable_extensions: 可以作为可编辑主版本的扩展名集合。

    Returns:
        只包含基础候选分、尚未应用外部证据的推荐记录。

    Raises:
        ValueError: 候选集合与版本组不一致时抛出。
    """
    if candidate_set["group_id"] != group["id"]:
        raise ValueError("推荐候选集合与版本组不匹配")
    scores, _ = score_version_candidates(
        group,
        files,
        chain,
        editable_extensions=editable_extensions,
    )
    expected_candidate_ids = set(candidate_set["candidate_file_ids"])
    if set(scores) != expected_candidate_ids:
        raise ValueError("候选集合与基础评分引用的文件不一致")
    return DecisionRecord(
        id=f"decision:{group['id']}",
        group_id=group["id"],
        candidate_scores=scores,
        recommended_file_id=None,
        reasons=[],
        confidence=0.0,
        needs_human_review=True,
        selected_by="unresolved",
        preserve_file_ids=[],
    )


def apply_delivery_rules(
    decision: DecisionRecord,
    deliveries: Iterable[DeliveryRecord],
) -> DecisionRecord:
    """用已发送和客户确认记录增强对应文件的候选评分。

    普通发送证据最多按其置信度增加 0.10；客户确认是更强流程信号，最多
    增加 0.18。未匹配记录不会影响评分，也不会凭附件名称猜测版本。

    Args:
        decision: 当前版本组的基础推荐记录。
        deliveries: 已由 Evidence 子图匹配到具体文件的发送记录。

    Returns:
        应用发送证据增量后的新推荐记录。

    Raises:
        ValueError: 发送证据把当前候选文件关联到其他版本组时抛出。
    """
    updated = dict(decision)
    scores = dict(decision["candidate_scores"])
    reasons = list(decision["reasons"])
    for delivery in deliveries:
        file_id = delivery["file_id"]
        if file_id is None or file_id not in scores:
            continue
        if delivery["group_id"] != decision["group_id"]:
            raise ValueError(f"发送证据 {delivery['id']} 与候选所属版本组不一致")
        weight = 0.18 if delivery["customer_confirmed"] else 0.10
        boost = round(weight * delivery["confidence"], 4)
        scores[file_id] = round(min(1.0, scores[file_id] + boost), 4)
        signal = "客户已确认" if delivery["customer_confirmed"] else "存在发送记录"
        reasons.append(
            f"发送证据：文件 {file_id} {signal}，候选分 +{boost:.2f}"
        )
    updated["candidate_scores"] = scores
    updated["reasons"] = list(dict.fromkeys(reasons))
    return DecisionRecord(**updated)


def apply_pdf_source_rules(
    decision: DecisionRecord,
    pdf_exports: Iterable[PdfExportRecord],
) -> DecisionRecord:
    """用 PDF 与可编辑来源关系增强源文件并降低导出件优先级。

    Args:
        decision: 已应用发送证据的推荐记录。
        pdf_exports: PDF 与其可编辑来源版本的匹配结果。

    Returns:
        应用 PDF 来源证据增量后的新推荐记录。

    Raises:
        ValueError: 来源记录引用当前组内不存在的源候选时抛出。
    """
    updated = dict(decision)
    scores = dict(decision["candidate_scores"])
    reasons = list(decision["reasons"])
    for export in pdf_exports:
        if export["group_id"] != decision["group_id"]:
            continue
        source_file_id = export["source_file_id"]
        if source_file_id is None:
            continue
        if source_file_id not in scores:
            raise ValueError(f"PDF 来源记录 {export['id']} 引用非候选源文件")
        boost = round(0.10 * export["confidence"], 4)
        scores[source_file_id] = round(min(1.0, scores[source_file_id] + boost), 4)
        pdf_file_id = export["pdf_file_id"]
        penalty = round(0.06 * export["confidence"], 4)
        if pdf_file_id in scores and pdf_file_id != source_file_id:
            scores[pdf_file_id] = round(max(0.0, scores[pdf_file_id] - penalty), 4)
        reasons.append(
            f"PDF 来源证据：文件 {source_file_id} 是 {pdf_file_id} 的可编辑来源，"
            f"候选分 +{boost:.2f}"
        )
    updated["candidate_scores"] = scores
    updated["reasons"] = list(dict.fromkeys(reasons))
    return DecisionRecord(**updated)


def apply_branch_rules(
    decision: DecisionRecord,
    branches: Iterable[BranchRecord],
) -> DecisionRecord:
    """把当前版本组的分叉事实加入推荐解释并保留候选竞争关系。

    分叉本身不靠排序消解，也不直接篡改候选分数；后续置信度阶段会强制该组
    进入人工确认并施加确定性惩罚。

    Args:
        decision: 已应用外部证据的推荐记录。
        branches: 全部版本分叉记录。

    Returns:
        带有版本分叉解释的新推荐记录。
    """
    group_branches = [
        item for item in branches if item["group_id"] == decision["group_id"]
    ]
    if not group_branches:
        return DecisionRecord(**dict(decision))
    updated = dict(decision)
    updated["reasons"] = list(
        dict.fromkeys(
            [
                *decision["reasons"],
                f"检测到 {len(group_branches)} 个版本分叉，不能仅凭评分自动消解",
            ]
        )
    )
    return DecisionRecord(**updated)


def select_recommended_file(
    decision: DecisionRecord,
    files: Iterable[FileRecord],
) -> DecisionRecord:
    """使用候选分、修改时间、文件名和稳定 ID 确定当前最高候选。

    Args:
        decision: 已完成全部规则加权的推荐记录。
        files: 全部扫描文件记录。

    Returns:
        写入当前推荐文件 ID 的新推荐记录；没有候选时保持 ``None``。

    Raises:
        ValueError: 候选评分引用未知文件时抛出。
    """
    updated = dict(decision)
    if not decision["candidate_scores"]:
        updated["recommended_file_id"] = None
        return DecisionRecord(**updated)

    file_by_id = {item["id"]: item for item in files}
    unknown_file_ids = [
        file_id for file_id in decision["candidate_scores"] if file_id not in file_by_id
    ]
    if unknown_file_ids:
        raise ValueError(f"候选评分引用未知文件：{unknown_file_ids[0]}")
    ranked = sorted(
        decision["candidate_scores"],
        key=lambda file_id: (
            decision["candidate_scores"][file_id],
            file_by_id[file_id]["modified_at"],
            file_by_id[file_id]["file_name"].casefold(),
            file_id,
        ),
        reverse=True,
    )
    updated["recommended_file_id"] = ranked[0]
    return DecisionRecord(**updated)


def explain_recommendation(
    decision: DecisionRecord,
    group: VersionGroupRecord,
    files: Iterable[FileRecord],
    chain: VersionChainRecord,
    *,
    editable_extensions: Iterable[str] = DEFAULT_EDITABLE_EXTENSIONS,
) -> DecisionRecord:
    """组合获胜候选的基础评分依据、证据规则和分叉说明。

    Args:
        decision: 已选择当前最高候选的推荐记录。
        group: 推荐记录所属版本组。
        files: 全部扫描文件记录。
        chain: 当前组的版本链记录。
        editable_extensions: 可以作为可编辑主版本的扩展名集合。

    Returns:
        带有面向人工审核的确定性解释列表的新推荐记录。

    Raises:
        ValueError: 推荐记录与版本组不匹配或候选引用无效时抛出。
    """
    if decision["group_id"] != group["id"]:
        raise ValueError("推荐结果与版本组不匹配")
    updated = dict(decision)
    winner_id = decision["recommended_file_id"]
    if winner_id is None:
        updated["reasons"] = list(
            dict.fromkeys([*decision["reasons"], "版本组没有可用的非重复候选文件"])
        )
        return DecisionRecord(**updated)

    file_by_id = {item["id"]: item for item in files}
    if winner_id not in file_by_id:
        raise ValueError("推荐结果引用未知文件")
    _, scoring_reasons = score_version_candidates(
        group,
        file_by_id.values(),
        chain,
        editable_extensions=editable_extensions,
    )
    if winner_id not in scoring_reasons:
        raise ValueError("推荐文件不属于当前候选集合")
    updated["reasons"] = list(
        dict.fromkeys(
            [
                f"当前最高候选为 {file_by_id[winner_id]['file_name']}",
                *scoring_reasons[winner_id],
                *decision["reasons"],
            ]
        )
    )
    return DecisionRecord(**updated)


def calculate_decision_confidence(
    decision: DecisionRecord,
    chain: VersionChainRecord,
    branches: Iterable[BranchRecord],
    *,
    auto_select_threshold: float = 0.82,
) -> DecisionRecord:
    """根据最高候选分差、版本链完整性和分叉计算最终置信度。

    Args:
        decision: 已完成候选选择和解释的推荐记录。
        chain: 当前组的版本链记录。
        branches: 全部版本分叉记录。
        auto_select_threshold: 允许规则自动选择的最低综合置信度。

    Returns:
        写入置信度、人工审核标记和选择来源的新推荐记录。

    Raises:
        ValueError: 阈值非法或版本链与推荐记录不匹配时抛出。
    """
    if not 0.0 <= auto_select_threshold <= 1.0:
        raise ValueError("auto_select_threshold 必须位于 0.0 到 1.0 之间")
    if chain["group_id"] != decision["group_id"]:
        raise ValueError("版本链与推荐结果不匹配")

    updated = dict(decision)
    reasons = list(decision["reasons"])
    winner_id = decision["recommended_file_id"]
    scores = decision["candidate_scores"]
    if winner_id is None or not scores:
        updated["confidence"] = 0.0
        updated["needs_human_review"] = True
        updated["selected_by"] = "unresolved"
        updated["reasons"] = reasons
        return DecisionRecord(**updated)

    ranked_scores = sorted(scores.values(), reverse=True)
    top_score = scores[winner_id]
    margin = 1.0 if len(ranked_scores) == 1 else top_score - ranked_scores[1]
    confidence = (
        1.0
        if len(ranked_scores) == 1
        else 0.65 * top_score + 0.35 * min(max(margin, 0.0) / 0.25, 1.0)
    )
    group_branches = [
        item for item in branches if item["group_id"] == decision["group_id"]
    ]
    if not chain["is_complete"]:
        confidence -= 0.15
        reasons.append("版本链不完整或存在不确定关系")
    if group_branches:
        confidence -= 0.25
    confidence = round(min(1.0, max(0.0, confidence)), 4)
    near_tie = len(ranked_scores) > 1 and margin < 0.08
    if near_tie:
        reasons.append(f"最高分与次高分差距仅为 {margin:.2f}")
    if confidence < auto_select_threshold:
        reasons.append(
            f"综合置信度 {confidence:.2f} 低于自动选择阈值 "
            f"{auto_select_threshold:.2f}"
        )

    needs_review = bool(
        group_branches
        or not chain["is_complete"]
        or near_tie
        or confidence < auto_select_threshold
    )
    updated["confidence"] = confidence
    updated["needs_human_review"] = needs_review
    updated["selected_by"] = "unresolved" if needs_review else "rule"
    updated["reasons"] = list(dict.fromkeys(reasons))
    return DecisionRecord(**updated)


def preserve_complete_version_chain(
    decision: DecisionRecord,
    group: VersionGroupRecord,
    chain: VersionChainRecord,
) -> DecisionRecord:
    """将版本组全部成员写入保留清单，避免推荐被解释为清理授权。

    Args:
        decision: 已完成置信度计算的推荐记录。
        group: 推荐记录所属版本组。
        chain: 当前组的版本链记录。

    Returns:
        保留完整组内版本、重复件和孤立成员的新推荐记录。

    Raises:
        ValueError: 推荐、版本组和版本链关系不一致时抛出。
    """
    if decision["group_id"] != group["id"] or chain["group_id"] != group["id"]:
        raise ValueError("推荐结果、版本组和版本链不匹配")
    chain_file_ids = set(chain["ordered_file_ids"]) | set(chain["leaf_file_ids"])
    if not chain_file_ids.issubset(group["file_ids"]):
        raise ValueError("版本链引用版本组之外的文件")
    updated = dict(decision)
    updated["preserve_file_ids"] = list(group["file_ids"])
    updated["reasons"] = list(
        dict.fromkeys(
            [
                *decision["reasons"],
                "安全策略：保留版本组内完整版本链及重复文件",
            ]
        )
    )
    return DecisionRecord(**updated)


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
    file_list = list(files)
    branch_list = list(branches)
    candidate_set = find_editable_leaf_versions(
        group,
        file_list,
        chain,
        editable_extensions=editable_extensions,
    )
    decision = create_scored_decision(
        group,
        file_list,
        chain,
        candidate_set,
        editable_extensions=editable_extensions,
    )
    decision = apply_branch_rules(decision, branch_list)
    decision = select_recommended_file(decision, file_list)
    decision = explain_recommendation(
        decision,
        group,
        file_list,
        chain,
        editable_extensions=editable_extensions,
    )
    decision = calculate_decision_confidence(
        decision,
        chain,
        branch_list,
        auto_select_threshold=auto_select_threshold,
    )
    return preserve_complete_version_chain(decision, group, chain)


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
