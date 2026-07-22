from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from collections.abc import Iterable
from datetime import datetime
from itertools import combinations
from typing import Any

from app.services.content_normalizer import load_normalized_content
from app.services.document_grouping import calculate_text_similarity
from app.state.models import (
    BranchRecord,
    ComparisonJob,
    DiffRecord,
    DocumentRecord,
    FileRecord,
    VersionChainRecord,
    VersionEdge,
    VersionGroupRecord,
)

"""本模块生成文件对比较结果，并构建可解释的版本边、分叉和版本链。"""


# 用于从带明确版本前缀的文件名中提取数字版本号的正则表达式。
VERSION_NUMBER_PATTERN = re.compile(
    r"(?i)(?:^|[\s_\-.（(【\[])(?:v(?:er(?:sion)?)?|版本|rev(?:ision)?)\s*"
    r"(\d+(?:\.\d+)*)"
)
# 用于识别文件名中最终版相关弱信号的正则表达式。
FINAL_MARKER_PATTERN = re.compile(r"(?i)(?:final|最终版?|定稿|终稿)")
# 用于识别确认、核准、发布和“别改”等比普通最终版更强的流程阶段标记。
CONFIRMED_MARKER_PATTERN = re.compile(
    r"(?i)(?:确认稿|确认版|已确认|已批准|已审批|核准|发布版|发全员|别再?改(?:了)?|别改)"
)
# 用于识别反馈、修改和修订等尚处于流转过程的流程阶段标记。
REVIEW_MARKER_PATTERN = re.compile(r"(?i)(?:反馈|修改版?|修订版?|review)" )


def _stable_id(namespace: str, *parts: str) -> str:
    """根据命名空间和稳定输入生成可重复的 SHA-256 记录 ID。"""
    payload = "\x1f".join((namespace, *parts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_datetime(value: str) -> datetime:
    """解析状态中的 ISO 8601 时间，兼容以 Z 结尾的 UTC 表示。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _file_sort_key(file_record: FileRecord) -> tuple[datetime, str, str]:
    """生成版本拓扑排序使用的稳定文件顺序键。"""
    return (
        _parse_datetime(file_record["modified_at"]),
        file_record["file_name"].casefold(),
        file_record["id"],
    )


def extract_version_number(file_name: str) -> tuple[int, ...] | None:
    """从文件名中提取显式版本号，不使用裸数字推断版本。

    Args:
        file_name: 包含或不包含扩展名的原始文件名。

    Returns:
        例如 ``v2.1`` 对应 ``(2, 1)``；没有显式版本标记时返回 ``None``。
    """
    match = VERSION_NUMBER_PATTERN.search(file_name)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def infer_filename_workflow_stage(file_name: str) -> tuple[int, str | None]:
    """从文件名弱标记推断文档所处的流转阶段。

    阶段顺序为普通稿、反馈或修改稿、最终稿、确认或发布稿。该结果只是弱证据，
    当它与文件修改时间冲突且没有显式版本号时，版本方向仍以时间顺序为准。

    Args:
        file_name: 包含扩展名的原始文件名。

    Returns:
        ``(阶段序号, 阶段说明)``；没有流程标记时返回 ``(0, None)``。
    """
    if CONFIRMED_MARKER_PATTERN.search(file_name):
        return 3, "确认、核准或发布标记"
    if FINAL_MARKER_PATTERN.search(file_name):
        return 2, "final、最终、定稿或终稿标记"
    if REVIEW_MARKER_PATTERN.search(file_name):
        return 1, "反馈、修改或修订标记"
    return 0, None


def infer_version_direction(
    left_file: FileRecord,
    right_file: FileRecord,
) -> tuple[str | None, str | None, list[str], float]:
    """根据显式版本号、final 标记和修改时间推断两个文件的先后关系。

    显式版本号优先级最高。确认、最终、反馈等文件名流程标记只作为弱证据；
    当弱标记与修改时间冲突时采用时间顺序并降低置信度。该函数不读取内容，
    也不会把文件名中的普通数字直接当作版本号。

    Args:
        left_file: 第一个候选版本文件记录。
        right_file: 第二个候选版本文件记录。

    Returns:
        ``(较早文件 ID, 较新文件 ID, 证据列表, 置信度)``；无法判断时
        两个 ID 都为 ``None``。
    """
    left_version = extract_version_number(left_file["file_name"])
    right_version = extract_version_number(right_file["file_name"])
    left_time = _parse_datetime(left_file["modified_at"])
    right_time = _parse_datetime(right_file["modified_at"])
    left_stage, left_stage_label = infer_filename_workflow_stage(left_file["file_name"])
    right_stage, right_stage_label = infer_filename_workflow_stage(right_file["file_name"])
    signals: list[str] = []
    older_id: str | None = None
    newer_id: str | None = None
    confidence = 0.0
    used_explicit_version = False

    if left_version is not None and right_version is not None and left_version != right_version:
        if left_version < right_version:
            older_id, newer_id = left_file["id"], right_file["id"]
        else:
            older_id, newer_id = right_file["id"], left_file["id"]
        signals.append(f"显式版本号 {left_version} 与 {right_version}")
        confidence = 0.95
        used_explicit_version = True
    elif left_stage != right_stage:
        if left_stage < right_stage:
            older_id, newer_id = left_file["id"], right_file["id"]
        else:
            older_id, newer_id = right_file["id"], left_file["id"]
        signals.append(
            "文件名流程阶段："
            f"{left_stage_label or '普通稿'} ↔ {right_stage_label or '普通稿'}"
        )
        confidence = 0.68

    if left_time != right_time:
        time_older, time_newer = (
            (left_file["id"], right_file["id"])
            if left_time < right_time
            else (right_file["id"], left_file["id"])
        )
        if older_id is None:
            older_id, newer_id = time_older, time_newer
            signals.append("文件修改时间先后，仅作为弱证据")
            confidence = 0.60
        elif (older_id, newer_id) == (time_older, time_newer):
            signals.append("文件修改时间与名称信号一致")
            confidence = min(1.0, confidence + 0.04)
        elif used_explicit_version:
            signals.append("文件修改时间与显式版本号冲突，保留显式版本顺序")
            confidence = max(0.65, confidence - 0.18)
        else:
            older_id, newer_id = time_older, time_newer
            signals.append("文件修改时间与弱流程标记冲突，采用时间顺序")
            confidence = 0.55

    return older_id, newer_id, signals, round(confidence, 4)


def calculate_structure_similarity(
    left_structure: dict[str, Any],
    right_structure: dict[str, Any],
) -> float:
    """比较文档类型和主要结构计数，返回零到一之间的相似度。"""
    if left_structure == right_structure:
        return 1.0
    left_type = left_structure.get("document_type")
    right_type = right_structure.get("document_type")
    type_score = 1.0 if left_type == right_type else 0.35

    comparable_keys = (
        "sheet_count",
        "paragraph_count",
        "table_count",
        "page_count",
        "visited_cells",
    )
    count_scores: list[float] = []
    for key in comparable_keys:
        left_value = left_structure.get(key)
        right_value = right_structure.get(key)
        if not isinstance(left_value, (int, float)) or not isinstance(
            right_value, (int, float)
        ):
            continue
        maximum = max(abs(float(left_value)), abs(float(right_value)), 1.0)
        count_scores.append(1.0 - abs(float(left_value) - float(right_value)) / maximum)

    count_score = sum(count_scores) / len(count_scores) if count_scores else 0.5
    return round(0.6 * type_score + 0.4 * count_score, 4)


def describe_key_field_changes(
    left_fields: dict[str, Any],
    right_fields: dict[str, Any],
    *,
    max_changes: int = 20,
) -> list[str]:
    """生成关键字段增删改的确定性摘要，不解释字段的法律或业务含义。"""
    if max_changes <= 0:
        raise ValueError("max_changes 必须大于零")

    changes: list[str] = []
    for key in sorted(set(left_fields) | set(right_fields)):
        left_value = left_fields.get(key)
        right_value = right_fields.get(key)
        if left_value == right_value:
            continue
        changes.append(f"字段 {key}：{left_value!r} → {right_value!r}")
        if len(changes) >= max_changes:
            changes.append("关键字段变化过多，其余内容已省略")
            break
    return changes


def generate_candidate_pairs(
    groups: Iterable[VersionGroupRecord],
    files: Iterable[FileRecord],
    *,
    max_pairs_per_group: int = 1_000,
) -> list[ComparisonJob]:
    """为每个版本组生成去除完全重复项后的稳定候选文件对。

    Args:
        groups: 已完成的版本组记录。
        files: 扫描文件记录。
        max_pairs_per_group: 每组最多生成的文件对数量。

    Returns:
        状态为 ``pending`` 的比较任务列表。达到上限时优先保留按修改时间
        相邻的文件对，再以稳定顺序补充其他组合。

    Raises:
        ValueError: 文件对上限不大于零或版本组引用未知文件时抛出。
    """
    if max_pairs_per_group <= 0:
        raise ValueError("max_pairs_per_group 必须大于零")

    file_by_id = {item["id"]: item for item in files}
    jobs: list[ComparisonJob] = []
    for group in groups:
        try:
            candidates = [
                file_by_id[file_id]
                for file_id in group["file_ids"]
                if file_by_id[file_id]["duplicate_of"] is None
            ]
        except KeyError as exc:
            raise ValueError(f"版本组 {group['id']} 引用了未知文件：{exc}") from exc

        ordered = sorted(candidates, key=_file_sort_key)
        priority_pairs = list(zip(ordered, ordered[1:], strict=False))
        all_pairs = list(combinations(ordered, 2))
        selected: list[tuple[FileRecord, FileRecord]] = []
        seen: set[frozenset[str]] = set()
        for left_file, right_file in [*priority_pairs, *all_pairs]:
            pair_key = frozenset((left_file["id"], right_file["id"]))
            if pair_key in seen:
                continue
            seen.add(pair_key)
            selected.append((left_file, right_file))
            if len(selected) >= max_pairs_per_group:
                break

        for left_file, right_file in selected:
            jobs.append(
                ComparisonJob(
                    id=_stable_id(
                        "comparison",
                        group["id"],
                        *sorted((left_file["id"], right_file["id"])),
                    ),
                    group_id=group["id"],
                    left_file_id=left_file["id"],
                    right_file_id=right_file["id"],
                    status="pending",
                )
            )
    return jobs


def compare_document_pair(
    group_id: str,
    left_file: FileRecord,
    right_file: FileRecord,
    left_document: DocumentRecord,
    right_document: DocumentRecord,
) -> DiffRecord:
    """比较两个标准化文档，并生成差异、先后证据和确定性摘要。

    该函数只读取 ``content_ref`` 指向的标准化 JSON 产物，不访问原始业务文件。
    版本先后由独立元数据信号推断，内容相似度不会被错误解释为时间方向。

    Args:
        group_id: 两个文件所属的版本组 ID。
        left_file: 第一个文件记录。
        right_file: 第二个文件记录。
        left_document: 第一个文件的标准化文档记录。
        right_document: 第二个文件的标准化文档记录。

    Returns:
        可直接写入 LangGraph 状态的差异记录。

    Raises:
        ValueError: 文档记录与文件 ID 不匹配或产物结构不合法时抛出。
        OSError: 标准化内容产物无法读取时抛出。
    """
    if left_document["file_id"] != left_file["id"]:
        raise ValueError("left_document 与 left_file 不匹配")
    if right_document["file_id"] != right_file["id"]:
        raise ValueError("right_document 与 right_file 不匹配")

    left_payload = load_normalized_content(left_document["content_ref"])
    right_payload = load_normalized_content(right_document["content_ref"])
    content_similarity = calculate_text_similarity(
        str(left_payload["normalized_text"]),
        str(right_payload["normalized_text"]),
    )
    structural_similarity = calculate_structure_similarity(
        dict(left_payload["structure"]),
        dict(right_payload["structure"]),
    )
    older_id, newer_id, signals, ordering_confidence = infer_version_direction(
        left_file,
        right_file,
    )
    key_changes = describe_key_field_changes(
        dict(left_payload["key_fields"]),
        dict(right_payload["key_fields"]),
    )

    if key_changes:
        summary = f"检测到 {len(key_changes)} 项关键字段变化。"
    elif content_similarity == 1.0:
        summary = "标准化内容完全一致。"
    else:
        summary = f"标准化内容相似度为 {content_similarity:.2f}，未发现结构化关键字段变化。"
    if older_id is None:
        summary += " 当前证据不足以判断版本先后。"

    confidence = (
        0.50 * ordering_confidence
        + 0.30 * content_similarity
        + 0.20 * structural_similarity
    )
    return DiffRecord(
        id=_stable_id(
            "diff",
            group_id,
            *sorted((left_file["id"], right_file["id"])),
        ),
        group_id=group_id,
        file_a_id=left_file["id"],
        file_b_id=right_file["id"],
        older_file_id=older_id,
        newer_file_id=newer_id,
        structural_similarity=round(structural_similarity, 4),
        content_similarity=round(content_similarity, 4),
        key_changes=key_changes,
        summary=summary,
        summary_source="deterministic",
        summary_message_id=None,
        summary_artifact_ref=None,
        ordering_signals=signals,
        confidence=round(min(1.0, confidence), 4),
    )


def build_version_edges(
    groups: Iterable[VersionGroupRecord],
    files: Iterable[FileRecord],
    diffs: Iterable[DiffRecord],
) -> list[VersionEdge]:
    """从重复记录和文件对差异中构建稀疏、可解释的版本边。

    对每个较新版本只选择综合证据最强的一个父版本，避免全文件对比较产生
    稠密传递边并把普通线性版本误判为分叉。无法判断方向的比较会保留为
    ``uncertain`` 关系，但不会参与版本链拓扑排序。

    Args:
        groups: 文档版本组。
        files: 文件记录。
        diffs: 文件对比较结果。

    Returns:
        完全重复、派生和不确定关系组成的版本边列表。
    """
    file_by_id = {item["id"]: item for item in files}
    diffs_by_group: dict[str, list[DiffRecord]] = defaultdict(list)
    for diff in diffs:
        diffs_by_group[diff["group_id"]].append(diff)

    edges: list[VersionEdge] = []
    for group in groups:
        member_ids = set(group["file_ids"])
        for file_id in sorted(member_ids):
            file_record = file_by_id[file_id]
            canonical_id = file_record["duplicate_of"]
            if canonical_id is None:
                continue
            edges.append(
                VersionEdge(
                    id=_stable_id("edge", group["id"], canonical_id, file_id, "duplicate"),
                    group_id=group["id"],
                    parent_file_id=canonical_id,
                    child_file_id=file_id,
                    relation="duplicate_of",
                    evidence=["原始文件 SHA-256 完全一致"],
                    confidence=1.0,
                )
            )

        directed_by_child: dict[str, list[DiffRecord]] = defaultdict(list)
        for diff in diffs_by_group.get(group["id"], []):
            if diff["older_file_id"] and diff["newer_file_id"]:
                directed_by_child[diff["newer_file_id"]].append(diff)
                continue
            edges.append(
                VersionEdge(
                    id=_stable_id(
                        "edge",
                        group["id"],
                        *sorted((diff["file_a_id"], diff["file_b_id"])),
                        "uncertain",
                    ),
                    group_id=group["id"],
                    parent_file_id=min(diff["file_a_id"], diff["file_b_id"]),
                    child_file_id=max(diff["file_a_id"], diff["file_b_id"]),
                    relation="uncertain",
                    evidence=diff["ordering_signals"] or ["缺少可用的版本先后信号"],
                    confidence=diff["confidence"],
                )
            )

        for child_id, candidates in directed_by_child.items():
            best_diff = max(
                candidates,
                key=lambda item: (
                    0.7 * item["content_similarity"] + 0.3 * item["confidence"],
                    _parse_datetime(file_by_id[item["older_file_id"]]["modified_at"]),
                    item["older_file_id"],
                ),
            )
            parent_id = best_diff["older_file_id"]
            if parent_id is None:
                continue
            edges.append(
                VersionEdge(
                    id=_stable_id("edge", group["id"], parent_id, child_id, "derived"),
                    group_id=group["id"],
                    parent_file_id=parent_id,
                    child_file_id=child_id,
                    relation="derived_from",
                    evidence=best_diff["ordering_signals"]
                    + [f"内容相似度 {best_diff['content_similarity']:.2f}"],
                    confidence=best_diff["confidence"],
                )
            )

    return sorted(
        edges,
        key=lambda item: (
            item["group_id"],
            item["relation"],
            item["parent_file_id"],
            item["child_file_id"],
        ),
    )


def detect_version_branches(
    groups: Iterable[VersionGroupRecord],
    edges: Iterable[VersionEdge],
) -> list[BranchRecord]:
    """识别一个父版本拥有多个直接派生子版本的版本分叉。"""
    edges_by_group: dict[str, list[VersionEdge]] = defaultdict(list)
    for edge in edges:
        if edge["relation"] == "derived_from":
            edges_by_group[edge["group_id"]].append(edge)

    branches: list[BranchRecord] = []
    for group in groups:
        outgoing: dict[str, list[VersionEdge]] = defaultdict(list)
        for edge in edges_by_group.get(group["id"], []):
            outgoing[edge["parent_file_id"]].append(edge)
        for parent_id, child_edges in outgoing.items():
            child_ids = sorted({edge["child_file_id"] for edge in child_edges})
            if len(child_ids) < 2:
                continue
            branches.append(
                BranchRecord(
                    id=_stable_id("branch", group["id"], parent_id, *child_ids),
                    group_id=group["id"],
                    root_file_id=parent_id,
                    child_file_ids=child_ids,
                    reason="同一父版本存在多个直接派生版本，需要人工判断是否合并",
                    confidence=round(
                        min(edge["confidence"] for edge in child_edges),
                        4,
                    ),
                )
            )
    return branches


def build_version_chains(
    groups: Iterable[VersionGroupRecord],
    files: Iterable[FileRecord],
    edges: Iterable[VersionEdge],
) -> list[VersionChainRecord]:
    """对确定方向的版本边执行拓扑排序，并把重复文件附在规范文件之后。

    循环、不确定关系和孤立版本会作为警告保留，函数不会为了构造完整链而
    猜测额外版本关系。``is_complete`` 只在全部规范文件形成无循环连通关系时
    为真。

    Args:
        groups: 文档版本组。
        files: 文件记录。
        edges: 已构建的版本关系。

    Returns:
        每个版本组对应的一条可读版本链记录。
    """
    file_by_id = {item["id"]: item for item in files}
    edges_by_group: dict[str, list[VersionEdge]] = defaultdict(list)
    for edge in edges:
        edges_by_group[edge["group_id"]].append(edge)

    chains: list[VersionChainRecord] = []
    for group in groups:
        member_files = [file_by_id[file_id] for file_id in group["file_ids"]]
        canonical_ids = {
            item["id"] for item in member_files if item["duplicate_of"] is None
        }
        group_edges = edges_by_group.get(group["id"], [])
        derived_edges = [
            edge
            for edge in group_edges
            if edge["relation"] == "derived_from"
            and edge["parent_file_id"] in canonical_ids
            and edge["child_file_id"] in canonical_ids
        ]
        uncertain_edges = [
            edge for edge in group_edges if edge["relation"] == "uncertain"
        ]

        adjacency: dict[str, set[str]] = {item_id: set() for item_id in canonical_ids}
        indegree = {item_id: 0 for item_id in canonical_ids}
        for edge in derived_edges:
            parent_id = edge["parent_file_id"]
            child_id = edge["child_file_id"]
            if child_id not in adjacency[parent_id]:
                adjacency[parent_id].add(child_id)
                indegree[child_id] += 1

        ready = deque(
            sorted(
                (item_id for item_id, degree in indegree.items() if degree == 0),
                key=lambda item_id: _file_sort_key(file_by_id[item_id]),
            )
        )
        canonical_order: list[str] = []
        while ready:
            item_id = ready.popleft()
            canonical_order.append(item_id)
            for child_id in sorted(
                adjacency[item_id],
                key=lambda candidate_id: _file_sort_key(file_by_id[candidate_id]),
            ):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(child_id)

        warnings: list[str] = []
        has_cycle = len(canonical_order) != len(canonical_ids)
        if has_cycle:
            warnings.append("检测到版本关系循环，循环内文件按修改时间附加展示")
            remaining = canonical_ids - set(canonical_order)
            canonical_order.extend(
                sorted(remaining, key=lambda item_id: _file_sort_key(file_by_id[item_id]))
            )
        if uncertain_edges:
            warnings.append(f"存在 {len(uncertain_edges)} 条无法确定方向的关系")

        weak_adjacency: dict[str, set[str]] = {
            item_id: set() for item_id in canonical_ids
        }
        for edge in derived_edges:
            weak_adjacency[edge["parent_file_id"]].add(edge["child_file_id"])
            weak_adjacency[edge["child_file_id"]].add(edge["parent_file_id"])
        visited: set[str] = set()
        if canonical_ids:
            stack = [next(iter(canonical_ids))]
            while stack:
                item_id = stack.pop()
                if item_id in visited:
                    continue
                visited.add(item_id)
                stack.extend(weak_adjacency[item_id] - visited)
        is_connected = len(visited) == len(canonical_ids)
        if len(canonical_ids) > 1 and not is_connected:
            warnings.append("版本组中存在尚未连接到版本链的孤立文件")

        duplicates_by_canonical: dict[str, list[str]] = defaultdict(list)
        for item in member_files:
            if item["duplicate_of"]:
                duplicates_by_canonical[item["duplicate_of"]].append(item["id"])
        ordered_ids: list[str] = []
        for canonical_id in canonical_order:
            ordered_ids.append(canonical_id)
            ordered_ids.extend(
                sorted(
                    duplicates_by_canonical.get(canonical_id, []),
                    key=lambda item_id: _file_sort_key(file_by_id[item_id]),
                )
            )

        missing_ids = set(group["file_ids"]) - set(ordered_ids)
        if missing_ids:
            warnings.append("部分重复文件未能关联到组内规范文件")
            ordered_ids.extend(sorted(missing_ids))

        leaf_ids = sorted(
            (item_id for item_id in canonical_ids if not adjacency[item_id]),
            key=lambda item_id: _file_sort_key(file_by_id[item_id]),
        )
        chains.append(
            VersionChainRecord(
                id=_stable_id("chain", group["id"]),
                group_id=group["id"],
                ordered_file_ids=ordered_ids,
                leaf_file_ids=leaf_ids,
                is_complete=not has_cycle and is_connected and not uncertain_edges,
                warnings=warnings,
            )
        )
    return chains
