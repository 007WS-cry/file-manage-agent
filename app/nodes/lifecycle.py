from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

from app.hooks.runner import (
    execute_after_run_hooks as run_after_run_hooks,
)
from app.hooks.runner import (
    execute_before_run_hooks as run_before_run_hooks,
)
from app.llm.config import create_llm_config_state
from app.llm.prompt_loader import (
    load_system_prompt as read_system_prompt,
)
from app.llm.prompt_loader import (
    record_prompt_load_error,
)
from app.state.factories import (
    create_hook_config_state,
    create_prompt_state,
    create_team_state,
)
from app.state.models import FileGovernanceState
from app.utils.lifecycle import update_run_stage, with_lifecycle_defaults
from app.utils.runtime import create_error_record, paths_overlap, utc_now_iso

"""本模块只定义顶层运行初始化、Hook、Prompt、请求校验和最终收口图节点。"""


def initialize_run(state: FileGovernanceState) -> dict:
    """初始化运行 ID、开始时间和所有可安全补齐的顶层默认字段。

    Args:
        state: 调用方提交的顶层状态；推荐由 ``create_initial_state`` 创建。

    Returns:
        进入 ``running`` 状态的运行信息、规范化模型 Profile 及缺省 Team、Task、
        证据和报告字段。
    """
    previous_run = state.get("run", {})
    run_id = previous_run.get("run_id") or uuid4().hex
    started_at = previous_run.get("started_at") or utc_now_iso()
    return {
        "run": {
            "run_id": run_id,
            "status": "running",
            "current_stage": "initialize_run",
            "started_at": started_at,
            "finished_at": None,
        },
        "human_review": state.get(
            "human_review",
            {"pending_group_ids": [], "selections": {}, "review_note": None},
        ),
        "report": state.get(
            "report",
            {
                "summary": "",
                "report_markdown": "",
                "warnings": [],
                "report_path": None,
                "generated_at": None,
            },
        ),
        "pdf_exports": state.get("pdf_exports", []),
        "deliveries": state.get("deliveries", []),
        "prompt": state.get("prompt", create_prompt_state()),
        "hooks": state.get("hooks", create_hook_config_state()),
        "llm": create_llm_config_state(state.get("llm")),
        "team": state.get("team", create_team_state()),
        "hook_events": state.get("hook_events", []),
        "tasks": state.get("tasks", []),
        "todos": state.get("todos", []),
        "team_messages": state.get("team_messages", []),
        "llm_calls": state.get("llm_calls", []),
    }


def execute_before_run_hooks(state: FileGovernanceState) -> dict:
    """执行业务校验前的 Hook，并把基础设施异常转换为致命状态错误。

    Args:
        state: 已完成运行初始化的顶层治理状态。

    Returns:
        Hook 受限状态更新、执行事件、运行阶段和可选阻断错误。
    """
    normalized_state = with_lifecycle_defaults(state)
    try:
        result = run_before_run_hooks(normalized_state)
        working_state = cast(FileGovernanceState, {**normalized_state, **result})
        result["run"] = update_run_stage(
            working_state,
            "before_run_hooks_complete",
        )
        return result
    except Exception as exc:
        return {
            "run": update_run_stage(normalized_state, "before_run_hooks_failed"),
            "errors": [
                create_error_record(
                    stage="before_run_hooks",
                    node_name="execute_before_run_hooks",
                    category="hook",
                    message=f"before_run Hook 基础设施执行失败：{exc}",
                    fatal=True,
                )
            ],
        }


def validate_request(state: FileGovernanceState) -> dict:
    """验证输入目录、判断阈值、可选证据路径和输出目录隔离。

    节点不会创建、移动或修改业务文件。校验失败会写入致命
    ``ErrorRecord``，由条件路由转入失败报告，而不是让图以未捕获异常退出。

    Args:
        state: 已完成运行初始化的顶层治理状态。

    Returns:
        规范化后的请求、工作空间、运行阶段以及可选校验错误。
    """
    try:
        request = dict(state["request"])
        workspace = dict(state["workspace"])
        root_input = Path(request["root_directory"]).expanduser()
        workspace_input = Path(workspace["input_root"]).expanduser()

        if root_input.is_symlink() or workspace_input.is_symlink():
            raise ValueError("输入根目录不得是符号链接")
        resolved_root = root_input.resolve(strict=True)
        resolved_workspace_input = workspace_input.resolve(strict=True)
        if not resolved_root.is_dir():
            raise NotADirectoryError(f"治理根路径不是目录：{resolved_root}")
        if resolved_root != resolved_workspace_input:
            raise ValueError("request.root_directory 必须与 workspace.input_root 指向同一目录")
        if workspace.get("input_readonly") is not True:
            raise ValueError("文件治理要求 workspace.input_readonly 必须为 True")

        allowed_extensions = []
        for extension in request.get("allowed_extensions", []):
            value = str(extension).strip().lower()
            if not value:
                continue
            normalized = value if value.startswith(".") else f".{value}"
            if normalized not in allowed_extensions:
                allowed_extensions.append(normalized)
        if not allowed_extensions:
            raise ValueError("allowed_extensions 至少需要包含一个扩展名")
        if int(request.get("max_files", 0)) <= 0:
            raise ValueError("max_files 必须大于零")

        grouping_threshold = float(request.get("grouping_similarity_threshold", -1))
        auto_select_threshold = float(request.get("auto_select_threshold", -1))
        pdf_match_threshold = float(request.get("pdf_match_threshold", 0.82))
        if not 0.0 <= grouping_threshold <= 1.0:
            raise ValueError("grouping_similarity_threshold 必须位于 0.0 到 1.0 之间")
        if not 0.0 <= auto_select_threshold <= 1.0:
            raise ValueError("auto_select_threshold 必须位于 0.0 到 1.0 之间")
        if not 0.0 <= pdf_match_threshold <= 1.0:
            raise ValueError("pdf_match_threshold 必须位于 0.0 到 1.0 之间")

        delivery_log_path = request.get("delivery_log_path")
        if delivery_log_path in (None, ""):
            resolved_delivery_log: str | None = None
        else:
            if not isinstance(delivery_log_path, str) or not delivery_log_path.strip():
                raise ValueError("delivery_log_path 必须是非空路径字符串或 null")
            delivery_log_candidate = Path(delivery_log_path).expanduser()
            if delivery_log_candidate.is_symlink():
                raise ValueError("delivery_log_path 不得是符号链接")
            resolved_delivery_path = delivery_log_candidate.resolve(strict=True)
            if not resolved_delivery_path.is_file():
                raise ValueError("delivery_log_path 必须指向普通 JSON 文件")
            if resolved_delivery_path.suffix.casefold() != ".json":
                raise ValueError("delivery_log_path 必须指向 .json 文件")
            resolved_delivery_log = str(resolved_delivery_path)

        artifact_root = Path(workspace["artifact_root"]).expanduser().resolve()
        report_root = Path(workspace["report_root"]).expanduser().resolve()
        if paths_overlap(resolved_root, artifact_root):
            raise ValueError("artifact_root 与只读输入目录不得相同或互为上下级目录")
        if paths_overlap(resolved_root, report_root):
            raise ValueError("report_root 与只读输入目录不得相同或互为上下级目录")

        request.update(
            {
                "root_directory": str(resolved_root),
                "allowed_extensions": allowed_extensions,
                "max_files": int(request["max_files"]),
                "grouping_similarity_threshold": grouping_threshold,
                "auto_select_threshold": auto_select_threshold,
                "pdf_match_threshold": pdf_match_threshold,
                "delivery_log_path": resolved_delivery_log,
                "use_llm_summary": bool(request.get("use_llm_summary", False)),
            }
        )
        workspace.update(
            {
                "input_root": str(resolved_root),
                "artifact_root": str(artifact_root),
                "report_root": str(report_root),
                "input_readonly": True,
            }
        )
        run = dict(state["run"])
        run["current_stage"] = "request_valid"
        return {"request": request, "workspace": workspace, "run": run}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        run = dict(state["run"])
        run["current_stage"] = "request_invalid"
        return {
            "run": run,
            "errors": [
                create_error_record(
                    stage="request_validation",
                    node_name="validate_request",
                    category="validation",
                    message=str(exc),
                    fatal=True,
                )
            ],
        }


def load_system_prompt(state: FileGovernanceState) -> dict:
    """加载本次运行的受控 System Prompt，并把加载失败写入顶层错误。

    相对路径只能从当前工作目录读取；CLI 已解析的绝对路径则仅允许读取其所在
    目录中的显式目标文件。节点不访问网络，也不执行 Prompt 中的任何内容。

    Args:
        state: 已通过请求校验且包含 Prompt 配置的顶层治理状态。

    Returns:
        已加载、已关闭或加载失败的 Prompt 状态、运行阶段和可选致命错误。
    """
    normalized_state = with_lifecycle_defaults(state)
    prompt_state = normalized_state["prompt"]
    try:
        source_path = prompt_state.get("source_path")
        if prompt_state.get("enabled") is True and source_path:
            source = Path(source_path).expanduser()
            base_directory = source.parent if source.is_absolute() else Path.cwd()
            allowed_root = base_directory
        else:
            base_directory = Path.cwd()
            allowed_root = base_directory
        loaded_prompt = read_system_prompt(
            prompt_state,
            base_directory=base_directory,
            allowed_root=allowed_root,
        )
        stage = (
            "system_prompt_loaded"
            if loaded_prompt["status"] == "loaded"
            else "system_prompt_disabled"
        )
        return {
            "prompt": loaded_prompt,
            "run": update_run_stage(normalized_state, stage),
        }
    except Exception as exc:
        return {
            "prompt": record_prompt_load_error(prompt_state),
            "run": update_run_stage(normalized_state, "system_prompt_failed"),
            "errors": [
                create_error_record(
                    stage="system_prompt",
                    node_name="load_system_prompt",
                    category="prompt",
                    message=f"System Prompt 加载失败：{exc}",
                    fatal=True,
                )
            ],
        }


def execute_after_run_hooks(state: FileGovernanceState) -> dict:
    """执行报告生成后的 Hook，并把基础设施异常转换为致命状态错误。

    Args:
        state: 已生成业务报告的顶层治理状态。

    Returns:
        Hook 受限状态更新、执行事件、运行阶段和可选阻断错误。
    """
    normalized_state = with_lifecycle_defaults(state)
    try:
        result = run_after_run_hooks(normalized_state)
        working_state = cast(FileGovernanceState, {**normalized_state, **result})
        result["run"] = update_run_stage(
            working_state,
            "after_run_hooks_complete",
        )
        return result
    except Exception as exc:
        return {
            "run": update_run_stage(normalized_state, "after_run_hooks_failed"),
            "errors": [
                create_error_record(
                    stage="after_run_hooks",
                    node_name="execute_after_run_hooks",
                    category="hook",
                    message=f"after_run Hook 基础设施执行失败：{exc}",
                    fatal=True,
                )
            ],
        }


def finalize_run(state: FileGovernanceState) -> dict:
    """根据错误严重程度设置最终状态和结束时间。

    Args:
        state: 已生成成功、无数据或失败报告的顶层状态。

    Returns:
        状态为 ``completed``、``partial`` 或 ``failed`` 的最终运行信息。
    """
    errors = state.get("errors", [])
    if any(error["fatal"] for error in errors):
        status = "failed"
    elif errors:
        status = "partial"
    else:
        status = "completed"

    run = dict(state["run"])
    run.update(
        {
            "status": status,
            "current_stage": "finished",
            "finished_at": utc_now_iso(),
        }
    )
    return {"run": run}
