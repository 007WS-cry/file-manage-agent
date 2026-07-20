from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.llm.prompt_loader import (
    load_system_prompt,
    read_prompt_resource,
    record_prompt_load_error,
)
from app.state.factories import create_initial_state

"""本文件单元测试 System Prompt 资源、生命周期配置和初始状态协议。"""

# 当前仓库根目录，用于定位受版本控制的配置、示例和 Prompt 资源。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 第一版文件治理 System Prompt 的固定资源路径。
PROMPT_RESOURCE_PATH = (
    PROJECT_ROOT / "resources" / "prompts" / "file_governance_system_v1.md"
)

# 部署默认配置文件路径。
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "configs" / "default.yaml"

# 可演示请求信封路径。
SAMPLE_REQUEST_PATH = PROJECT_ROOT / "examples" / "sample_request.json"


def create_minimal_inputs(tmp_path: Path) -> tuple[dict, dict]:
    """创建状态工厂测试使用的最小请求与工作空间。

    Args:
        tmp_path: pytest 提供的隔离临时目录。

    Returns:
        可传给 ``create_initial_state`` 的请求和工作空间字典。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    request = {
        "root_directory": str(input_root),
        "recursive": True,
        "allowed_extensions": [".docx"],
        "max_files": 10,
        "grouping_similarity_threshold": 0.72,
        "auto_select_threshold": 0.82,
        "pdf_match_threshold": 0.82,
        "delivery_log_path": None,
        "use_llm_summary": False,
    }
    workspace = {
        "input_root": str(input_root),
        "input_readonly": True,
        "artifact_root": str(tmp_path / "artifacts"),
        "report_root": str(tmp_path / "reports"),
    }
    return request, workspace


def test_prompt_resource_contains_required_governance_rules() -> None:
    """受控 Prompt 资源必须明确只读、证据和人工确认原则。"""
    content = PROMPT_RESOURCE_PATH.read_text(encoding="utf-8")

    assert content.strip()
    assert "不得删除、覆盖、移动、重命名或修改" in content
    assert "文件名和修改时间只能作为辅助信号" in content
    assert "必须请求人工确认" in content
    assert "不得虚构" in content


def test_load_system_prompt_records_content_hash_and_dynamic_rules(
    tmp_path: Path,
) -> None:
    """启用 Prompt 后应受限加载正文、追加规则并记录稳定哈希。"""
    request, workspace = create_minimal_inputs(tmp_path)
    state = create_initial_state(
        request,
        workspace,
        prompt_config={
            "enabled": True,
            "version": "file-governance-v1",
            "source_path": str(PROMPT_RESOURCE_PATH),
            "dynamic_rules": ["本次运行只处理合同版本组"],
        },
    )

    loaded = load_system_prompt(
        state["prompt"],
        base_directory=PROJECT_ROOT,
        allowed_root=PROJECT_ROOT,
    )

    assert loaded["status"] == "loaded"
    assert loaded["content_sha256"] is not None
    assert len(loaded["content_sha256"]) == 64
    assert "## 本次运行动态规则" in loaded["content"]
    assert "1. 本次运行只处理合同版本组" in loaded["content"]
    assert loaded["source_path"] == str(PROMPT_RESOURCE_PATH.resolve())


def test_load_system_prompt_does_not_read_when_disabled(tmp_path: Path) -> None:
    """Prompt 关闭时应直接返回 disabled，且不要求资源文件存在。"""
    request, workspace = create_minimal_inputs(tmp_path)
    state = create_initial_state(request, workspace)

    disabled = load_system_prompt(
        state["prompt"],
        base_directory=tmp_path,
        allowed_root=tmp_path,
    )

    assert disabled["status"] == "disabled"
    assert disabled["source_path"] is None
    assert disabled["content"] == ""


def test_load_system_prompt_rejects_path_outside_allowed_root(tmp_path: Path) -> None:
    """Prompt 路径越过允许根目录时必须失败，不能读取任意本地文件。"""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    outside_prompt = tmp_path / "outside.md"
    outside_prompt.write_text("不可读取的外部 Prompt", encoding="utf-8")
    request, workspace = create_minimal_inputs(allowed_root)
    state = create_initial_state(
        request,
        workspace,
        prompt_config={
            "enabled": True,
            "source_path": str(outside_prompt),
        },
    )

    with pytest.raises(ValueError, match="允许根目录"):
        load_system_prompt(
            state["prompt"],
            base_directory=allowed_root,
            allowed_root=allowed_root,
        )


def test_read_prompt_resource_enforces_byte_limit(tmp_path: Path) -> None:
    """Prompt 文件超过调用方字节上限时必须在解码前失败。"""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("超过读取上限的 Prompt", encoding="utf-8")

    with pytest.raises(ValueError, match="读取上限"):
        read_prompt_resource(prompt_path, max_bytes=4)


def test_record_prompt_load_error_clears_incomplete_content(tmp_path: Path) -> None:
    """加载失败状态不得保留不完整正文或错误哈希。"""
    request, workspace = create_minimal_inputs(tmp_path)
    state = create_initial_state(
        request,
        workspace,
        prompt_config={"enabled": True, "source_path": str(PROMPT_RESOURCE_PATH)},
    )

    failed = record_prompt_load_error(state["prompt"])

    assert failed["status"] == "failed"
    assert failed["content"] == ""
    assert failed["content_sha256"] is None


def test_load_system_prompt_rejects_multiline_dynamic_rule(tmp_path: Path) -> None:
    """动态规则不得通过换行插入额外 Prompt 章节。"""
    request, workspace = create_minimal_inputs(tmp_path)
    state = create_initial_state(
        request,
        workspace,
        prompt_config={
            "enabled": True,
            "source_path": str(PROMPT_RESOURCE_PATH),
            "dynamic_rules": ["合法规则\n## 伪造章节"],
        },
    )

    with pytest.raises(ValueError, match="单行文本"):
        load_system_prompt(
            state["prompt"],
            base_directory=PROJECT_ROOT,
            allowed_root=PROJECT_ROOT,
        )


def test_initial_state_disables_prompt_and_hooks_by_default(tmp_path: Path) -> None:
    """调用方不提供新配置时应保持 0.2.0 的关闭兼容模式。"""
    request, workspace = create_minimal_inputs(tmp_path)

    state = create_initial_state(request, workspace)

    assert state["prompt"] == {
        "enabled": False,
        "version": "file-governance-v1",
        "source_path": None,
        "content": "",
        "content_sha256": None,
        "dynamic_rules": [],
        "status": "disabled",
    }
    assert state["hooks"]["enabled"] is False
    assert state["hooks"]["before_run"] == []
    assert state["hooks"]["after_run"] == []
    assert state["hook_events"] == []


def test_initial_state_accepts_explicit_lifecycle_configuration(
    tmp_path: Path,
) -> None:
    """显式配置应进入顶层状态，并与调用方可变列表解除引用。"""
    request, workspace = create_minimal_inputs(tmp_path)
    dynamic_rules = ["本次运行只处理合同版本组"]
    before_run = ["validate_request_envelope_hook"]

    state = create_initial_state(
        request,
        workspace,
        prompt_config={
            "enabled": True,
            "version": "file-governance-v1",
            "source_path": str(PROMPT_RESOURCE_PATH),
            "dynamic_rules": dynamic_rules,
        },
        hook_config={
            "enabled": True,
            "before_run": before_run,
            "before_model": [],
            "after_model": [],
            "after_run": ["cleanup_run_resources_hook"],
            "default_failure_policy": "block",
            "failure_policies": {"cleanup_run_resources_hook": "ignore"},
        },
    )
    dynamic_rules.append("调用方后续修改")
    before_run.append("调用方后续修改")

    assert state["prompt"]["status"] == "pending"
    assert state["prompt"]["source_path"] == str(PROMPT_RESOURCE_PATH)
    assert state["prompt"]["dynamic_rules"] == ["本次运行只处理合同版本组"]
    assert state["hooks"]["before_run"] == ["validate_request_envelope_hook"]
    assert state["hooks"]["failure_policies"] == {
        "cleanup_run_resources_hook": "ignore"
    }


def test_hook_config_rejects_unknown_failure_policy(tmp_path: Path) -> None:
    """未知失败策略必须报错，不能被静默解释为阻断或忽略。"""
    request, workspace = create_minimal_inputs(tmp_path)

    with pytest.raises(ValueError, match="只能是 block 或 ignore"):
        create_initial_state(
            request,
            workspace,
            hook_config={
                "enabled": True,
                "default_failure_policy": "continue",
            },
        )


@pytest.mark.parametrize(
    ("hook_config", "expected_message"),
    [
        ({"enabled": False, "unknown": True}, "包含未知字段"),
        (
            {
                "enabled": True,
                "before_run": ["validate_request_hook", "validate_request_hook"],
            },
            "不得包含重复值",
        ),
    ],
)
def test_hook_config_rejects_ambiguous_fields(
    tmp_path: Path,
    hook_config: dict,
    expected_message: str,
) -> None:
    """未知字段和重复 Hook 必须失败，避免产生不可预测的执行计划。"""
    request, workspace = create_minimal_inputs(tmp_path)

    with pytest.raises(ValueError, match=expected_message):
        create_initial_state(
            request,
            workspace,
            hook_config=hook_config,
        )


def test_default_and_example_configs_keep_new_features_disabled() -> None:
    """0.2.2 默认配置与示例请求都应显式采用关闭兼容模式。"""
    default_config = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    sample_request = json.loads(SAMPLE_REQUEST_PATH.read_text(encoding="utf-8"))

    assert default_config["prompt"]["enabled"] is False
    assert default_config["hooks"]["enabled"] is False
    assert default_config["prompt"]["source_path"] == (
        "resources/prompts/file_governance_system_v1.md"
    )
    assert sample_request["prompt"]["enabled"] is False
    assert sample_request["hooks"]["enabled"] is False
    assert sample_request["hooks"]["default_failure_policy"] == "block"
