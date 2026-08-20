from __future__ import annotations

from collections.abc import Iterable

from app.state.models import (
    DiffRecord,
    FileRecord,
    RelationEvidenceRecord,
    VersionRelationAssessmentRecord,
    VersionRelationConstraints,
    VersionRelationResolution,
    VersionRelationType,
)

"""本模块构造版本关系证据，并以确定性约束融合 LLM 候选关系。"""


# 支持判定为可编辑源文件的扩展名集合。
EDITABLE_SOURCE_EXTENSIONS = frozenset({".docx", ".xlsx"})
# 支持父子修订关系所需的最低内容相似度。
MIN_PARENT_CONTENT_SIMILARITY = 0.55
# 支持父子修订关系所需的最低结构相似度。
MIN_PARENT_STRUCTURE_SIMILARITY = 0.35
# 支持源文件到 PDF 导出关系所需的最低内容相似度。
MIN_EXPORT_CONTENT_SIMILARITY = 0.75
# 支持语义重复候选所需的最低内容相似度。
MIN_SEMANTIC_DUPLICATE_CONTENT_SIMILARITY = 0.97
# 支持语义重复候选所需的最低结构相似度。
MIN_SEMANTIC_DUPLICATE_STRUCTURE_SIMILARITY = 0.90
# 支持无关文件候选所允许的最高内容相似度。
MAX_UNRELATED_CONTENT_SIMILARITY = 0.45
# 支持无关文件候选所允许的最高结构相似度。
MAX_UNRELATED_STRUCTURE_SIMILARITY = 0.50
# 确定性关系不明确时采纳 LLM 候选所需的最低置信度。
MIN_LLM_RELATION_CONFIDENCE = 0.70
# 确定性算法直接建立有向关系所需的最低方向证据置信度。
MIN_DETERMINISTIC_DIRECTION_CONFIDENCE = 0.68
# 单次 Version Subagent 输入允许携带的最大关系证据数量。
MAX_RELATION_EVIDENCE_ITEMS = 50
# 单次 Version Subagent 输入中关系证据文本的最大总字符数。
MAX_RELATION_EVIDENCE_CHARACTERS = 8_000


def infer_deterministic_relation(
    left_file: FileRecord,
    right_file: FileRecord,
    *,
    content_similarity: float,
    structural_similarity: float,
    older_file_id: str | None,
    newer_file_id: str | None,
    ordering_confidence: float,
) -> tuple[
    VersionRelationType,
    float,
    VersionRelationConstraints,
    list[str],
]:
    """根据哈希、方向、格式及相似度生成确定性关系和硬约束。

    Args:
        left_file: 当前比较的第一个文件记录。
        right_file: 当前比较的第二个文件记录。
        content_similarity: 确定性内容相似度。
        structural_similarity: 确定性结构相似度。
        older_file_id: 已由元数据规则确定的较早文件 ID。
        newer_file_id: 已由元数据规则确定的较新文件 ID。
        ordering_confidence: 文件名、显式版本号和修改时间形成的方向置信度。

    Returns:
        确定性关系、置信度、约束快照和可解释理由。
    """
    exact_hash_match = bool(
        left_file["sha256"]
        and left_file["sha256"] == right_file["sha256"]
    )
    version_order_known = bool(older_file_id and newer_file_id)
    same_document_stem = bool(
        left_file["normalized_stem"]
        and left_file["normalized_stem"] == right_file["normalized_stem"]
    )
    file_by_id = {left_file["id"]: left_file, right_file["id"]: right_file}
    older_file = file_by_id.get(older_file_id or "")
    newer_file = file_by_id.get(newer_file_id or "")
    parent_child_supported = bool(
        version_order_known
        and structural_similarity >= MIN_PARENT_STRUCTURE_SIMILARITY
        and (
            content_similarity >= MIN_PARENT_CONTENT_SIMILARITY
            or same_document_stem
        )
    )
    export_supported = bool(
        parent_child_supported
        and older_file is not None
        and newer_file is not None
        and older_file["extension"] in EDITABLE_SOURCE_EXTENSIONS
        and newer_file["extension"] == ".pdf"
        and content_similarity >= MIN_EXPORT_CONTENT_SIMILARITY
    )
    semantic_duplicate_supported = bool(
        content_similarity >= MIN_SEMANTIC_DUPLICATE_CONTENT_SIMILARITY
        and structural_similarity >= MIN_SEMANTIC_DUPLICATE_STRUCTURE_SIMILARITY
    )
    unrelated_supported = bool(
        content_similarity < MAX_UNRELATED_CONTENT_SIMILARITY
        and structural_similarity < MAX_UNRELATED_STRUCTURE_SIMILARITY
    )
    parallel_branch_supported = bool(
        not exact_hash_match
        and same_document_stem
        and content_similarity >= MIN_PARENT_CONTENT_SIMILARITY
        and structural_similarity >= MIN_PARENT_STRUCTURE_SIMILARITY
    )
    constraints = VersionRelationConstraints(
        exact_hash_match=exact_hash_match,
        parent_child_supported=parent_child_supported,
        export_supported=export_supported,
        semantic_duplicate_supported=semantic_duplicate_supported,
        parallel_branch_supported=parallel_branch_supported,
        unrelated_supported=unrelated_supported,
    )

    if exact_hash_match:
        return (
            "semantic_duplicate",
            1.0,
            constraints,
            ["原始文件 SHA-256 完全一致，重复事实不可由 LLM 覆盖"],
        )
    deterministic_direction_supported = bool(
        version_order_known
        and ordering_confidence >= MIN_DETERMINISTIC_DIRECTION_CONFIDENCE
    )
    if export_supported and deterministic_direction_supported:
        return (
            "derived_export",
            round(0.55 + 0.30 * content_similarity + 0.15 * structural_similarity, 4),
            constraints,
            ["可编辑源到 PDF 的格式方向、版本先后和内容相似度一致"],
        )
    if semantic_duplicate_supported and content_similarity == 1.0:
        return (
            "semantic_duplicate",
            round(0.65 * content_similarity + 0.35 * structural_similarity, 4),
            constraints,
            ["内容与结构高度相似，支持语义重复关系"],
        )
    if parent_child_supported and deterministic_direction_supported:
        return (
            "direct_revision",
            round(0.50 + 0.30 * content_similarity + 0.20 * structural_similarity, 4),
            constraints,
            ["版本方向、内容相似度和结构相似度共同支持直接修订"],
        )
    if semantic_duplicate_supported:
        return (
            "semantic_duplicate",
            round(0.65 * content_similarity + 0.35 * structural_similarity, 4),
            constraints,
            ["内容与结构高度相似，支持语义重复关系"],
        )
    if unrelated_supported:
        return (
            "unrelated",
            round(
                0.55
                + 0.25 * (1.0 - content_similarity)
                + 0.20 * (1.0 - structural_similarity),
                4,
            ),
            constraints,
            ["内容与结构相似度均不足以支持同一文档的版本关系"],
        )
    return (
        "uncertain",
        round(0.30 + 0.20 * max(content_similarity, structural_similarity), 4),
        constraints,
        ["确定性证据不足以把文件对归入受控关系类型"],
    )


def _context_supports_parallel_branch(
    current_diff: DiffRecord,
    prior_diffs: Iterable[DiffRecord],
) -> bool:
    """判断既有比较是否为当前文件对提供共同基础版本证据。

    Args:
        current_diff: 当前等待 LLM 分析的文件对差异。
        prior_diffs: 同组已经完成的其他文件对差异。

    Returns:
        两个当前文件均与同一外部基础文件存在受支持父子关系时返回 ``True``。
    """
    current_ids = {current_diff["file_a_id"], current_diff["file_b_id"]}
    possible_roots: dict[str, set[str]] = {}
    for prior in prior_diffs:
        if prior["group_id"] != current_diff["group_id"]:
            continue
        if prior.get("resolved_relation", prior.get("deterministic_relation")) not in {
            "direct_revision",
            "derived_export",
        }:
            continue
        older_id = prior.get("older_file_id")
        newer_id = prior.get("newer_file_id")
        if older_id is None or newer_id not in current_ids or older_id in current_ids:
            continue
        possible_roots.setdefault(older_id, set()).add(newer_id)
    return any(children == current_ids for children in possible_roots.values())


def build_relation_evidence(
    current_diff: DiffRecord,
    files: Iterable[FileRecord],
    prior_diffs: Iterable[DiffRecord],
) -> tuple[list[RelationEvidenceRecord], VersionRelationConstraints]:
    """为关系判断构造当前文件对和同组上下文的有界证据白名单。

    Args:
        current_diff: 当前文件对的确定性差异记录。
        files: 当前运行的文件记录，用于生成安全格式说明。
        prior_diffs: 已经完成的文件对差异，仅同组记录会进入上下文。

    Returns:
        关系证据列表和补充并行分支支持后的约束快照。

    Raises:
        ValueError: 当前差异引用未知文件时抛出。
    """
    file_by_id = {item["id"]: item for item in files}
    try:
        left_file = file_by_id[current_diff["file_a_id"]]
        right_file = file_by_id[current_diff["file_b_id"]]
    except KeyError as exc:
        raise ValueError(f"关系证据引用未知文件：{exc}") from exc

    prior_list = list(prior_diffs)
    constraints = VersionRelationConstraints(**current_diff["relation_constraints"])
    constraints["parallel_branch_supported"] = bool(
        constraints["parallel_branch_supported"]
        or _context_supports_parallel_branch(current_diff, prior_list)
    )
    comparison_id = current_diff["id"]
    candidates: list[RelationEvidenceRecord] = [
        RelationEvidenceRecord(
            evidence_ref=f"diff:{comparison_id}:similarity",
            comparison_id=comparison_id,
            evidence_type="similarity",
            description=(
                f"内容相似度 {current_diff['content_similarity']:.4f}；"
                f"结构相似度 {current_diff['structural_similarity']:.4f}"
            ),
        ),
        RelationEvidenceRecord(
            evidence_ref=f"diff:{comparison_id}:ordering",
            comparison_id=comparison_id,
            evidence_type="ordering",
            description=(
                "版本方向已确定：" + "；".join(current_diff["ordering_signals"])
                if current_diff["older_file_id"] and current_diff["newer_file_id"]
                else "版本方向未知：缺少足够时间或文件名先后证据"
            ),
        ),
        RelationEvidenceRecord(
            evidence_ref=f"diff:{comparison_id}:file-type",
            comparison_id=comparison_id,
            evidence_type="file_type",
            description=(
                f"候选 A 格式 {left_file['extension']}；"
                f"候选 B 格式 {right_file['extension']}"
            ),
        ),
    ]
    candidates.extend(
        RelationEvidenceRecord(
            evidence_ref=item["evidence_ref"],
            comparison_id=comparison_id,
            evidence_type="change",
            description=(
                f"{item['source']}：{str(item.get('old_value'))[:300]} → "
                f"{str(item.get('new_value'))[:300]}"
            ),
        )
        for item in current_diff.get("change_evidence", [])
    )
    for prior in prior_list:
        if prior["group_id"] != current_diff["group_id"] or prior["id"] == comparison_id:
            continue
        candidates.append(
            RelationEvidenceRecord(
                evidence_ref=f"diff:{prior['id']}:context",
                comparison_id=prior["id"],
                evidence_type="context",
                description=(
                    f"同组比较 {prior['file_a_id']} ↔ {prior['file_b_id']}："
                    f"关系 {prior.get('resolved_relation', prior.get('deterministic_relation', 'uncertain'))}；"
                    f"内容相似度 {prior['content_similarity']:.4f}；"
                    f"方向 {prior.get('older_file_id')} → {prior.get('newer_file_id')}"
                ),
            )
        )

    evidence: list[RelationEvidenceRecord] = []
    seen_refs: set[str] = set()
    total_characters = 0
    for item in candidates:
        if item["evidence_ref"] in seen_refs:
            continue
        item_characters = len(item["evidence_ref"]) + len(item["description"])
        if (
            len(evidence) >= MAX_RELATION_EVIDENCE_ITEMS
            or total_characters + item_characters > MAX_RELATION_EVIDENCE_CHARACTERS
        ):
            break
        seen_refs.add(item["evidence_ref"])
        total_characters += item_characters
        evidence.append(item)
    return evidence, constraints


def _candidate_supported(
    relation: VersionRelationType,
    constraints: VersionRelationConstraints,
) -> bool:
    """判断 LLM 候选是否满足对应的确定性硬约束。

    Args:
        relation: LLM 提出的受控关系类型。
        constraints: 当前文件对的确定性约束快照。

    Returns:
        候选不需要额外约束或已经满足相应约束时返回 ``True``。
    """
    if relation == "direct_revision":
        return constraints["parent_child_supported"]
    if relation == "derived_export":
        return constraints["export_supported"]
    if relation == "semantic_duplicate":
        return constraints["semantic_duplicate_supported"]
    if relation == "parallel_branch":
        return constraints["parallel_branch_supported"]
    if relation == "unrelated":
        return constraints["unrelated_supported"]
    return True


def fuse_version_relations(
    deterministic_relation: VersionRelationType,
    deterministic_confidence: float,
    constraints: VersionRelationConstraints,
    llm_assessment: VersionRelationAssessmentRecord | None,
) -> tuple[
    VersionRelationType,
    VersionRelationResolution,
    float,
    bool,
    list[str],
]:
    """在硬约束内融合确定性关系和 LLM 候选，不让模型直接改图。

    Args:
        deterministic_relation: 确定性算法给出的关系候选。
        deterministic_confidence: 确定性关系置信度。
        constraints: 当前文件对不可由 LLM 覆盖的约束快照。
        llm_assessment: 已通过 Schema 和证据白名单校验的 LLM 候选。

    Returns:
        最终关系、融合方式、置信度、人工审核标记和解释理由。
    """
    if constraints["exact_hash_match"]:
        return (
            "semantic_duplicate",
            "deterministic_only",
            1.0,
            False,
            ["哈希完全一致：固定为重复文件，忽略任何 LLM 覆盖候选"],
        )
    if llm_assessment is None or llm_assessment["relation"] == "uncertain":
        return (
            deterministic_relation,
            "deterministic_only",
            round(deterministic_confidence, 4),
            False,
            ["LLM 未提出可采纳关系，保留确定性结果"],
        )

    llm_relation = llm_assessment["relation"]
    llm_confidence = llm_assessment["confidence"]
    if not _candidate_supported(llm_relation, constraints):
        return (
            "uncertain",
            "constrained_rejection",
            round(max(deterministic_confidence, llm_confidence), 4),
            True,
            [
                f"LLM 候选 {llm_relation} 不满足确定性建边约束，禁止写入版本图",
                llm_assessment["reason"],
            ],
        )
    if deterministic_relation == llm_relation:
        boosted_confidence = min(
            1.0,
            0.65 * deterministic_confidence + 0.35 * llm_confidence + 0.10,
        )
        return (
            deterministic_relation,
            "consensus",
            round(boosted_confidence, 4),
            False,
            [f"LLM 与确定性算法一致：{llm_relation}，关系置信度已提高"],
        )
    if (
        deterministic_relation == "uncertain"
        and llm_confidence >= MIN_LLM_RELATION_CONFIDENCE
    ):
        resolved_confidence = min(
            0.90,
            0.25 * deterministic_confidence + 0.75 * llm_confidence,
        )
        return (
            llm_relation,
            "llm_supported",
            round(resolved_confidence, 4),
            False,
            [
                f"确定性结果不明确，在硬约束内采纳 LLM 候选 {llm_relation}",
                llm_assessment["reason"],
            ],
        )
    if deterministic_relation == "uncertain":
        return (
            "uncertain",
            "deterministic_only",
            round(deterministic_confidence, 4),
            False,
            [
                f"LLM 候选 {llm_relation} 置信度低于 {MIN_LLM_RELATION_CONFIDENCE:.2f}，"
                "未用于版本图",
            ],
        )
    return (
        "uncertain",
        "conflict_review",
        round(max(deterministic_confidence, llm_confidence), 4),
        True,
        [
            f"确定性关系 {deterministic_relation} 与 LLM 候选 {llm_relation} 冲突",
            llm_assessment["reason"],
        ],
    )


def group_has_relation_review(
    diffs: Iterable[DiffRecord],
    group_id: str,
) -> bool:
    """判断一个版本组是否存在必须人工复核的关系冲突。

    Args:
        diffs: 当前运行全部文件对差异。
        group_id: 等待检查的版本组 ID。

    Returns:
        组内任一差异要求关系审核时返回 ``True``。
    """
    return any(
        item["group_id"] == group_id and item.get("relation_review_required", False)
        for item in diffs
    )
