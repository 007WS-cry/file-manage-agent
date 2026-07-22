from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import app
from app.llm.schemas import (
    build_structured_output_schema,
    validate_output_artifact_refs,
    validate_structured_output,
)
from app.state.factories import create_initial_state
from app.state.models import (
    ContentSubagentOutput,
    EvidenceSubagentOutput,
    VersionSubagentOutput,
)
from app.state.reducers import merge_by_message_id

"""本模块验证三个 Subagent 的 Pydantic 输出、引用白名单和新增顶层状态契约。"""

# 结构化输出测试允许返回的固定产物引用。
ALLOWED_ARTIFACT_REF = "artifact://normalized/document-001"

# 当前仓库根目录，用于验证 0.4.1 发布版本元数据一致性。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "output_model",
    [ContentSubagentOutput, VersionSubagentOutput, EvidenceSubagentOutput],
)
def test_subagent_output_models_forbid_unknown_fields(output_model: type) -> None:
    """三个 Subagent 输出均应拒绝摘要和引用之外的字段。

    Args:
        output_model: 当前参数化执行的 Pydantic Subagent 输出类型。
    """
    with pytest.raises(ValueError, match="结构化输出校验失败"):
        validate_structured_output(
            {
                "summary": "合法摘要",
                "artifact_refs": [],
                "raw_document": "禁止返回的完整正文",
            },
            output_model,
        )


def test_validate_structured_output_accepts_json_and_builds_schema() -> None:
    """合法 JSON 应转换为目标类型，Schema 应包含两个固定业务字段。"""
    output = validate_structured_output(
        '{"summary":"版本差异摘要","artifact_refs":[]}',
        VersionSubagentOutput,
    )
    schema = build_structured_output_schema(VersionSubagentOutput)

    assert isinstance(output, VersionSubagentOutput)
    assert output.summary == "版本差异摘要"
    assert set(schema["properties"]) == {"summary", "artifact_refs"}
    assert schema.get("additionalProperties") is False


def test_validate_structured_output_rejects_empty_summary() -> None:
    """空摘要不得通过 Pydantic 输出协议。"""
    for invalid_summary in ("", "   "):
        with pytest.raises(ValueError, match="结构化输出校验失败"):
            validate_structured_output(
                {"summary": invalid_summary, "artifact_refs": []},
                ContentSubagentOutput,
            )


def test_artifact_refs_must_be_within_caller_allowlist() -> None:
    """结构化输出不得凭空创建白名单之外的产物引用。"""
    valid_output = ContentSubagentOutput(
        summary="内容摘要",
        artifact_refs=[ALLOWED_ARTIFACT_REF],
    )
    invalid_output = ContentSubagentOutput(
        summary="内容摘要",
        artifact_refs=["artifact://invented/reference"],
    )

    assert validate_output_artifact_refs(
        valid_output,
        allowed_refs=[ALLOWED_ARTIFACT_REF],
    ) is valid_output
    with pytest.raises(ValueError, match="未授权产物引用"):
        validate_output_artifact_refs(
            invalid_output,
            allowed_refs=[ALLOWED_ARTIFACT_REF],
        )


def test_merge_by_message_id_updates_without_duplicates() -> None:
    """Team Message reducer 应按 message_id 更新且保留首次顺序。"""
    existing = [
        {
            "message_id": "message-001",
            "task_id": "run-001:inventory",
            "status": "created",
            "summary": "等待分派",
        }
    ]
    update = [
        {
            "message_id": "message-001",
            "status": "validated",
            "summary": "协议校验成功",
        }
    ]

    merged = merge_by_message_id(existing, update)

    assert len(merged) == 1
    assert merged[0]["task_id"] == "run-001:inventory"
    assert merged[0]["status"] == "validated"
    assert merged[0]["summary"] == "协议校验成功"


def test_initial_state_contains_safe_llm_and_fixed_team_contract(
    tmp_path: Path,
) -> None:
    """初始状态应关闭真实模型并提供固定 Team、消息和审计集合。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    )

    assert state["llm"]["enabled"] is False
    assert state["llm"]["provider"] == "mock"
    assert state["llm"]["api_key_env"] is None
    assert state["team"]["coordinator_id"] == "coordinator-agent"
    assert [member["role"] for member in state["team"]["members"]] == [
        "coordinator",
        "content",
        "version",
        "evidence",
    ]
    assert state["team_messages"] == []
    assert state["llm_calls"] == []


def test_release_version_is_consistent_across_package_and_docker() -> None:
    """Python 包、项目元数据、Docker 默认值和 README 应统一为 0.4.1。"""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert app.__version__ == "0.4.1"
    assert 'version = "0.4.1"' in pyproject
    assert "ARG APP_VERSION=0.4.1" in dockerfile
    assert "当前版本 `0.4.1`" in readme


def test_default_config_and_sample_request_disable_real_provider() -> None:
    """仓库默认配置和演示请求都必须关闭真实模型并选择 Mock Provider。"""
    default_config = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "default.yaml").read_text(encoding="utf-8")
    )
    sample_request = json.loads(
        (PROJECT_ROOT / "examples" / "sample_request.json").read_text(
            encoding="utf-8"
        )
    )

    assert default_config["llm"]["enabled"] is False
    assert default_config["llm"]["provider"] == "mock"
    assert default_config["llm"]["api_key_env"] is None
    assert sample_request["llm"] == default_config["llm"]
