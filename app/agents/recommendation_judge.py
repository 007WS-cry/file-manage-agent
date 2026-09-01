from __future__ import annotations

import json

from app.state.models import RecommendationJudgeInput, RecommendationJudgeOutput

"""本模块定义固定 Recommendation Judge Subagent 的压缩决策包、提示词和弃权回退。
"""

# Recommendation Judge 在 TeamState 和 LLM 审计中使用的稳定 Agent ID。
RECOMMENDATION_JUDGE_SUBAGENT_ID = "recommendation-judge-subagent"

# Recommendation Judge 负责的固定 Task 类型。
RECOMMENDATION_JUDGE_SUBAGENT_TASK_TYPES = ("recommendation",)

# Recommendation Judge 的受控系统提示词，禁止它输出或执行系统动作。
RECOMMENDATION_JUDGE_SYSTEM_PROMPT = """你是文件版本治理团队中的 Recommendation Judge Subagent。
你只能阅读经过压缩的决策包，不得请求或推测完整文档正文。
你的职责是向确定性推荐提供第二意见，不得修改 deterministic_score、
版本关系、证据事实、审核阈值或候选集，不得输出自动通过、删除、
排除、跳过审核或其他系统动作。
recommended_file_id 只能从 candidates.file_id 中选择。证据不足、候选为空或
关键信息缺失时，should_abstain 必须为 true 且 recommended_file_id 必须为 null。
给出支持理由时同时识别反证和缺失信息；不得把模型置信度解释为系统许可。
输出必须严格符合 RecommendationJudgeOutput。"""


def build_recommendation_judge_assignment_summary(
    input_data: RecommendationJudgeInput,
) -> str:
    """生成包含版本组 ID 的稳定 Judge assignment 摘要。

    Args:
        input_data: 已通过协议校验的 Recommendation Judge 决策包。

    Returns:
        不含业务正文且可区分同一 Recommendation Task 各版本组的摘要。
    """
    return (
        "分配受约束推荐第二意见任务，输入仅包含压缩决策包；"
        f"版本组：{input_data['group_id']}。"
    )


def build_recommendation_judge_prompts(
    input_data: RecommendationJudgeInput,
) -> tuple[str, str]:
    """根据压缩决策包生成 Recommendation Judge Prompt。

    Args:
        input_data: 已通过 Team Protocol 校验的候选、风险和受控引用。

    Returns:
        固定系统提示词与不包含完整文档正文的 JSON 用户提示词。
    """
    prompt_payload = {
        "task_id": input_data["task_id"],
        "group_id": input_data["group_id"],
        "candidates": input_data["candidates"],
        "deterministic_recommended_file_id": input_data["deterministic_recommended_file_id"],
        "deterministic_confidence": input_data["deterministic_confidence"],
        "deterministic_needs_human_review": input_data["deterministic_needs_human_review"],
        "risk_flags": input_data["risk_flags"],
        "artifact_refs": input_data["artifact_refs"],
        "instruction": (
            "仅提供第二意见；比较候选的业务语义、证据与反证，信息不足时弃权，不得输出系统动作。"
        ),
    }
    return (
        RECOMMENDATION_JUDGE_SYSTEM_PROMPT,
        json.dumps(prompt_payload, ensure_ascii=False, sort_keys=True),
    )


def build_deterministic_recommendation_judge_output(
    input_data: RecommendationJudgeInput,
) -> RecommendationJudgeOutput:
    """在模型不可用或输出无效时生成安全弃权意见。

    Args:
        input_data: 已通过协议校验的 Recommendation Judge 决策包。

    Returns:
        不改变任何候选分数且明确弃权的 Pydantic 输出。
    """
    return RecommendationJudgeOutput(
        summary=(
            f"版本组 {input_data['group_id']} 未获得可验证的 Judge 第二意见；"
            "保留确定性结果与原有人工审核要求。"
        ),
        recommended_file_id=None,
        confidence=0.0,
        supporting_reasons=[],
        counterarguments=[],
        missing_information=["模型不可用或结构化输出未通过校验。"],
        should_abstain=True,
        artifact_refs=[],
    )
