from __future__ import annotations

import hashlib
from datetime import datetime
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
from app.services.context_compaction import copy_context_compact_state
from app.services.memory_policy import copy_memory_state
from app.state.factories import (
    copy_application_database_state,
    copy_recovery_state,
    create_hook_config_state,
    create_prompt_state,
    create_team_state,
)
from app.state.models import FileGovernanceState
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import HumanReviewModel
from app.storage.repositories import create_repository_bundle
from app.utils.error_context import create_node_error, is_error_unresolved
from app.utils.lifecycle import update_run_stage, with_lifecycle_defaults
from app.utils.runtime import paths_overlap, utc_now_iso

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
    thread_id = previous_run.get("thread_id") or run_id
    started_at = previous_run.get("started_at") or utc_now_iso()
    application_database = copy_application_database_state(state.get("application_database"))
    report = {
        "summary": "",
        "report_markdown": "",
        "warnings": [],
        "report_path": None,
        "generated_at": None,
        "degradation_ids": [],
        "recovered_error_ids": [],
        **state.get("report", {}),
    }
    result = {
        "run": {
            "run_id": run_id,
            "thread_id": thread_id,
            "status": "running",
            "current_stage": "initialize_run",
            "started_at": started_at,
            "finished_at": None,
        },
        "human_review": state.get(
            "human_review",
            {"pending_group_ids": [], "selections": {}, "review_note": None},
        ),
        "report": report,
        "pdf_exports": state.get("pdf_exports", []),
        "deliveries": state.get("deliveries", []),
        "prompt": state.get("prompt", create_prompt_state()),
        "hooks": state.get("hooks", create_hook_config_state()),
        "llm": create_llm_config_state(state.get("llm")),
        "team": state.get("team", create_team_state()),
        "memory": copy_memory_state(state.get("memory")),
        "context_compact": copy_context_compact_state(state.get("context_compact")),
        "application_database": application_database,
        "recovery": copy_recovery_state(state.get("recovery")),
        "hook_events": state.get("hook_events", []),
        "tasks": state.get("tasks", []),
        "todos": state.get("todos", []),
        "team_messages": state.get("team_messages", []),
        "llm_calls": state.get("llm_calls", []),
        "node_executions": state.get("node_executions", []),
        "degradations": state.get("degradations", []),
    }
    if not application_database["enabled"]:
        return result

    engine = None
    try:
        database_path = application_database.get("database_path")
        if database_path is None:
            raise ValueError("应用数据库已启用但未配置 database_path")
        engine = create_application_engine(
            database_path,
            input_root=state["workspace"]["input_root"],
            checkpoint_path=application_database.get("checkpoint_path"),
            echo=application_database["echo"],
            timeout_seconds=application_database["timeout_seconds"],
        )
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            run_record = repositories.governance_runs.get_or_create_minimal(
                run_id,
                thread_id=thread_id,
                current_stage="initialize_run",
                request_summary={
                    "recursive": bool(state["request"].get("recursive", True)),
                    "max_files": int(state["request"].get("max_files", 0)),
                    "allowed_extension_count": len(state["request"].get("allowed_extensions", [])),
                    "use_llm_summary": bool(state["request"].get("use_llm_summary", False)),
                },
            )
            run_record.started_at = datetime.fromisoformat(started_at)
        application_database["status"] = "ready"
        application_database["last_error"] = None
        result["application_database"] = application_database
        return result
    except Exception:
        application_database["status"] = "failed"
        application_database["last_error"] = "治理运行初始化记录写入失败。"
        result["application_database"] = application_database
        result["errors"] = [
            create_node_error(
                state,
                stage="application_database",
                node_name="initialize_run",
                category="database",
                message="应用数据库初始化失败，治理流程已安全降级。",
                fatal=False,
            )
        ]
        return result
    finally:
        if engine is not None:
            engine.dispose()


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
                create_node_error(
                    state,
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
                create_node_error(
                    state,
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
                create_node_error(
                    state,
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
                create_node_error(
                    state,
                    stage="after_run_hooks",
                    node_name="execute_after_run_hooks",
                    category="hook",
                    message=f"after_run Hook 基础设施执行失败：{exc}",
                    fatal=True,
                )
            ],
        }


def finalize_run(state: FileGovernanceState) -> dict:
    """设置最终状态，并持久化运行摘要和脱敏人工选择。

    Args:
        state: 已生成成功、无数据或失败报告的顶层状态。

    Returns:
        最终运行信息、应用数据库状态以及可选非致命数据库错误。
    """
    errors = state.get("errors", [])
    if any(is_error_unresolved(error) for error in errors):
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
    application_database = copy_application_database_state(state.get("application_database"))
    result = {
        "run": run,
        "application_database": application_database,
    }
    if not application_database["enabled"] or application_database["status"] == "failed":
        return result

    engine = None
    try:
        database_path = application_database.get("database_path")
        if database_path is None:
            raise ValueError("应用数据库已启用但未配置 database_path")
        engine = create_application_engine(
            database_path,
            input_root=state["workspace"]["input_root"],
            checkpoint_path=application_database.get("checkpoint_path"),
            echo=application_database["echo"],
            timeout_seconds=application_database["timeout_seconds"],
        )
        session_factory = create_session_factory(engine)
        with open_application_session(session_factory) as session:
            repositories = create_repository_bundle(session)
            repositories.governance_runs.get_or_create_minimal(
                run["run_id"],
                thread_id=run["thread_id"],
                current_stage="finished",
                request_summary={"recovered_run": True},
            )
            repositories.governance_runs.update_status(
                run["run_id"],
                status=run["status"],
                current_stage=run["current_stage"],
                report_path=state.get("report", {}).get("report_path"),
                error_summary=(
                    "fatal="
                    f"{sum(is_error_unresolved(item) for item in errors)};"
                    f"total={len(errors)}"
                    if errors
                    else None
                ),
                finished_at=datetime.fromisoformat(run["finished_at"]),
            )
            human_review = state.get("human_review", {})
            selections = human_review.get("selections", {})
            for group_id, selected_file_id in sorted(selections.items()):
                identity = f"{run['run_id']}\x1f{group_id}"
                review_id = "review-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
                if repositories.human_reviews.get(review_id) is None:
                    repositories.human_reviews.add(
                        HumanReviewModel(
                            id=review_id,
                            run_id=run["run_id"],
                            group_id=group_id,
                            selected_file_id=selected_file_id,
                            review_note=None,
                            reviewer_label="user",
                        )
                    )
        application_database["status"] = "ready"
        application_database["last_error"] = None
        result["application_database"] = application_database
        return result
    except Exception:
        application_database["status"] = "failed"
        application_database["last_error"] = "治理运行收口记录写入失败。"
        if run["status"] == "completed":
            run["status"] = "partial"
        result["run"] = run
        result["application_database"] = application_database
        result["errors"] = [
            create_node_error(
                state,
                stage="application_database",
                node_name="finalize_run",
                category="database",
                message="应用数据库收口失败，治理报告与确定性结论仍然保留。",
                fatal=False,
            )
        ]
        return result
    finally:
        if engine is not None:
            engine.dispose()
