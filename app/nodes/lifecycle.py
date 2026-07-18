from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.state.models import FileGovernanceState
from app.utils.runtime import create_error_record, paths_overlap, utc_now_iso

"""本模块仅实现顶层治理运行的初始化、请求校验和最终状态收口节点。"""


def initialize_run(state: FileGovernanceState) -> dict:
    """初始化运行 ID、开始时间和所有可安全补齐的顶层默认字段。

    Args:
        state: 调用方提交的顶层状态；推荐由 ``create_initial_state`` 创建。

    Returns:
        进入 ``running`` 状态的运行信息及缺省人工审核、报告字段。
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
    }


def validate_request(state: FileGovernanceState) -> dict:
    """验证输入目录、只读约束、扩展名、数量上限和输出目录隔离。

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
        if not 0.0 <= grouping_threshold <= 1.0:
            raise ValueError("grouping_similarity_threshold 必须位于 0.0 到 1.0 之间")
        if not 0.0 <= auto_select_threshold <= 1.0:
            raise ValueError("auto_select_threshold 必须位于 0.0 到 1.0 之间")

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
