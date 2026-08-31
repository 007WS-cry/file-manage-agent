from __future__ import annotations

from app.services.business_evidence import (
    apply_business_evidence_review,
    apply_business_evidence_scoring,
    build_business_evidence_records,
    validate_business_evidence_analyses,
)
from app.services.recommendation import select_recommended_file
from app.state.models import DecisionRecord, EvidenceSubagentOutput, FileRecord

"""本模块验证 Evidence 语义候选与确定性推荐动作之间的受控边界。"""

# 业务证据单元测试使用的稳定证据引用。
EVIDENCE_REF = "email-mcp://message-982:sentence-3"


def make_output(evidence_type: str, status: str) -> EvidenceSubagentOutput:
    """构造指向 contract-v4 的单条业务证据模型输出。

    Args:
        evidence_type: 测试使用的封闭业务证据类型。
        status: 与证据类型对应的规范化状态。

    Returns:
        可供业务证据校验和规则固化使用的 Pydantic 输出。
    """
    return EvidenceSubagentOutput.model_validate(
        {
            "summary": "客户业务证据摘要。",
            "business_evidence": [
                {
                    "evidence_type": evidence_type,
                    "target_file_id": "contract-v4",
                    "status": status,
                    "actor_role": "customer",
                    "effective_time": "2026-07-12T10:30:00+08:00",
                    "reason": "受控摘录明确表达当前业务状态。",
                    "evidence_refs": [EVIDENCE_REF],
                    "confidence": 0.96,
                }
            ],
            "artifact_refs": [EVIDENCE_REF],
        }
    )


def make_decision() -> DecisionRecord:
    """构造两个文件参与竞争的基础推荐记录。

    Returns:
        contract-v3 与 contract-v4 分数相同的未决推荐记录。
    """
    return DecisionRecord(
        id="decision:contract",
        group_id="group-contract",
        candidate_scores={"contract-v3": 0.60, "contract-v4": 0.60},
        recommended_file_id=None,
        reasons=[],
        confidence=0.0,
        needs_human_review=True,
        selected_by="unresolved",
        preserve_file_ids=[],
    )


def make_file(file_id: str, modified_at: str) -> FileRecord:
    """构造不读取真实磁盘内容的候选文件记录。

    Args:
        file_id: 测试候选文件 ID。
        modified_at: 用于稳定排序的带时区修改时间。

    Returns:
        可供推荐选择函数使用的最小完整文件记录。
    """
    return FileRecord(
        id=file_id,
        absolute_path=f"/readonly/{file_id}.docx",
        file_name=f"{file_id}.docx",
        normalized_stem="contract",
        extension=".docx",
        size_bytes=1,
        modified_at=modified_at,
        sha256=("3" if file_id.endswith("3") else "4") * 64,
        duplicate_of=None,
        parse_status="parsed",
        parse_error=None,
    )


def test_approved_evidence_uses_fixed_confidence_weighted_boost() -> None:
    """明确批准应按代码固定的 0.18 权重加分，模型不能指定分值。"""
    output = make_output("approved", "approved")
    snippets = [
        {
            "evidence_ref": EVIDENCE_REF,
            "target_file_id": "contract-v4",
            "source": "email_mcp",
            "text": "报价已确认。",
            "effective_time": "2026-07-12T10:30:00+08:00",
        }
    ]
    validate_business_evidence_analyses(output, snippets)
    records = build_business_evidence_records("group-contract", output)

    weighted = apply_business_evidence_scoring(make_decision(), records, [])

    assert records[0]["rule_action"] == "score_boost"
    assert records[0]["score_adjustment"] == 0.18
    assert weighted["candidate_scores"]["contract-v4"] == 0.7728


def test_superseded_evidence_excludes_target_from_selection() -> None:
    """作废证据应把目标分数置零，并使选择函数跳过该文件。"""
    records = build_business_evidence_records(
        "group-contract",
        make_output("superseded", "superseded"),
    )
    scored = apply_business_evidence_scoring(make_decision(), records, [])
    selected = select_recommended_file(
        scored,
        [
            make_file("contract-v3", "2026-07-11T10:00:00+08:00"),
            make_file("contract-v4", "2026-07-12T10:00:00+08:00"),
        ],
        records,
    )

    assert scored["candidate_scores"]["contract-v4"] == 0.0
    assert selected["recommended_file_id"] == "contract-v3"


def test_requires_revision_forces_human_review() -> None:
    """继续修改证据必须覆盖自动选择状态并强制进入人工审核。"""
    records = build_business_evidence_records(
        "group-contract",
        make_output("requires_revision", "revision_required"),
    )
    decision = make_decision()
    decision["recommended_file_id"] = "contract-v4"
    decision["confidence"] = 0.95
    decision["needs_human_review"] = False
    decision["selected_by"] = "rule"

    reviewed = apply_business_evidence_review(decision, records)

    assert reviewed["needs_human_review"] is True
    assert reviewed["selected_by"] == "unresolved"
    assert any("强制进入人工审核" in reason for reason in reviewed["reasons"])
