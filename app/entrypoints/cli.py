from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from app.graphs.file_governance import build_file_governance_graph
from app.state.factories import create_initial_state
from app.storage.checkpoints import open_checkpointer

"""本模块提供带 Prompt、Hook 请求信封和人工审核恢复能力的命令行入口。"""


# CLI 未显式配置 SQLite 数据库时使用的默认 checkpoint 路径。
DEFAULT_CHECKPOINT_PATH = Path(".artifacts/checkpoints/file-governance.sqlite3")
# CLI 允许读取的请求或人工恢复 JSON 文件最大字节数。
MAX_CLI_JSON_BYTES = 1024 * 1024


def build_argument_parser() -> argparse.ArgumentParser:
    """构建文件版本治理 CLI 参数解析器。

    Returns:
        包含 ``run`` 和 ``resume`` 子命令的参数解析器。
    """
    parser = argparse.ArgumentParser(
        prog="file-governance",
        description="只读扫描真实目录并执行文件版本治理 LangGraph。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="从 JSON 请求启动一次新的治理运行。",
    )
    run_parser.add_argument("request_file", type=Path, help="治理请求 JSON 文件路径。")
    run_parser.add_argument(
        "--thread-id",
        help="LangGraph checkpoint 线程 ID；不提供时自动生成。",
    )
    run_parser.add_argument(
        "--checkpoint-backend",
        choices=("sqlite", "memory"),
        help="覆盖请求文件中的 checkpoint 后端。",
    )
    run_parser.add_argument(
        "--checkpoint-path",
        type=Path,
        help="覆盖请求文件中的 SQLite checkpoint 数据库路径。",
    )

    resume_parser = subparsers.add_parser(
        "resume",
        help="从 SQLite checkpoint 恢复一次人工审核。",
    )
    resume_parser.add_argument("response_file", type=Path, help="人工选择 JSON 文件路径。")
    resume_parser.add_argument(
        "--thread-id",
        required=True,
        help="启动运行时使用的同一个 LangGraph 线程 ID。",
    )
    resume_parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help="启动运行时使用的同一个 SQLite checkpoint 数据库路径。",
    )
    return parser


def load_cli_json(path: Path, *, label: str) -> dict[str, Any]:
    """受限读取 CLI 使用的本地 JSON 对象。

    函数只读取调用方显式提供的普通 ``.json`` 文件，拒绝符号链接和超限文件，
    并且只进行 JSON 解析，不执行文件中的命令、代码或模板表达式。

    Args:
        path: 请求文件或人工恢复文件路径。
        label: 用于错误信息的文件用途名称。

    Returns:
        JSON 顶层对象。

    Raises:
        ValueError: 路径、大小、扩展名或顶层结构不合法时抛出。
        OSError: 文件无法读取时由操作系统抛出。
        json.JSONDecodeError: 文件内容不是合法 JSON 时抛出。
    """
    original_path = path.expanduser()
    if original_path.is_symlink():
        raise ValueError(f"{label}不得是符号链接")
    resolved_path = original_path.resolve(strict=True)
    if not resolved_path.is_file() or resolved_path.suffix.lower() != ".json":
        raise ValueError(f"{label}必须是 JSON 普通文件：{resolved_path}")
    if resolved_path.stat().st_size > MAX_CLI_JSON_BYTES:
        raise ValueError(f"{label}超过 {MAX_CLI_JSON_BYTES} 字节读取上限")
    with resolved_path.open("r", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError(f"{label}顶层必须是对象")
    return payload


def resolve_request_payload(
    payload: dict[str, Any],
    *,
    base_directory: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """校验请求信封并把相对路径解析为相对请求文件的绝对路径。

    Args:
        payload: 包含 ``request``、``workspace`` 和可选 ``checkpoint`` 的对象。
        base_directory: 请求 JSON 所在目录。

    Returns:
        ``(request, workspace, checkpoint)`` 三个相互独立的字典。

    Raises:
        ValueError: 必需字段不是对象或路径字段不是非空字符串时抛出。
    """
    request = payload.get("request")
    workspace = payload.get("workspace")
    checkpoint = payload.get("checkpoint", {})
    if not isinstance(request, dict):
        raise ValueError("请求 JSON 必须包含 request 对象")
    if not isinstance(workspace, dict):
        raise ValueError("请求 JSON 必须包含 workspace 对象")
    if not isinstance(checkpoint, dict):
        raise ValueError("checkpoint 必须是对象")

    resolved_request = dict(request)
    resolved_workspace = dict(workspace)
    resolved_checkpoint = dict(checkpoint)
    path_fields = (
        (resolved_request, "root_directory"),
        (resolved_workspace, "input_root"),
        (resolved_workspace, "artifact_root"),
        (resolved_workspace, "report_root"),
    )
    for target, field_name in path_fields:
        raw_path = target.get(field_name)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"{field_name} 必须是非空路径字符串")
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = base_directory / candidate
        target[field_name] = str(candidate.resolve())

    delivery_log_path = resolved_request.get("delivery_log_path")
    if delivery_log_path is not None:
        if not isinstance(delivery_log_path, str) or not delivery_log_path.strip():
            raise ValueError("delivery_log_path 必须是非空路径字符串或 null")
        candidate = Path(delivery_log_path).expanduser()
        if not candidate.is_absolute():
            candidate = base_directory / candidate
        resolved_request["delivery_log_path"] = str(candidate.resolve())

    checkpoint_path = resolved_checkpoint.get("database_path")
    if checkpoint_path is not None:
        if not isinstance(checkpoint_path, str) or not checkpoint_path.strip():
            raise ValueError("checkpoint.database_path 必须是非空路径字符串")
        candidate = Path(checkpoint_path).expanduser()
        if not candidate.is_absolute():
            candidate = base_directory / candidate
        resolved_checkpoint["database_path"] = str(candidate.resolve())
    return resolved_request, resolved_workspace, resolved_checkpoint


def resolve_lifecycle_payload(
    payload: dict[str, Any],
    *,
    base_directory: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """解析请求信封中的可选 Prompt 和 Hook 配置。

    生命周期配置与业务 ``request`` 分开返回，确保 Prompt 内容和 Hook 执行计划
    不会被塞入 ``RequestState``。Prompt 相对路径以请求 JSON 所在目录为基准解析；
    具体文件存在性、范围、大小和内容仍由生命周期加载节点受限校验。

    Args:
        payload: CLI 已读取并验证为对象的完整请求信封。
        base_directory: 请求 JSON 所在目录，用于解析 Prompt 相对路径。

    Returns:
        ``(prompt_config, hook_config)``；缺省对象以 ``None`` 表示完全关闭。

    Raises:
        ValueError: ``prompt``、``hooks`` 或 Prompt 路径字段类型不合法时抛出。
    """
    raw_prompt = payload.get("prompt")
    raw_hooks = payload.get("hooks")
    if raw_prompt is not None and not isinstance(raw_prompt, dict):
        raise ValueError("prompt 必须是对象或 null")
    if raw_hooks is not None and not isinstance(raw_hooks, dict):
        raise ValueError("hooks 必须是对象或 null")

    prompt_config = dict(raw_prompt) if raw_prompt is not None else None
    hook_config = dict(raw_hooks) if raw_hooks is not None else None
    if prompt_config is not None and "source_path" in prompt_config:
        raw_source_path = prompt_config["source_path"]
        if raw_source_path is not None:
            if not isinstance(raw_source_path, str) or not raw_source_path.strip():
                raise ValueError("prompt.source_path 必须是非空路径字符串或 null")
            candidate = Path(raw_source_path).expanduser()
            if not candidate.is_absolute():
                candidate = base_directory / candidate
            prompt_config["source_path"] = str(candidate.resolve())
    return prompt_config, hook_config


def serialize_interrupts(result: dict[str, Any]) -> list[Any]:
    """把 LangGraph Interrupt 对象转换为可输出的 JSON 值。

    Args:
        result: 图调用返回的顶层状态对象。

    Returns:
        Interrupt 的 ``value`` 列表；不存在暂停时返回空列表。
    """
    values = []
    for item in result.get("__interrupt__", ()):
        values.append(getattr(item, "value", item))
    return values


def print_result(result: dict[str, Any], *, thread_id: str) -> None:
    """把运行结果压缩为适合 CLI 消费的 JSON 摘要并输出。

    Args:
        result: 顶层文件治理图返回的状态。
        thread_id: 本次调用使用的 checkpoint 线程 ID。
    """
    report = result.get("report", {})
    output = {
        "thread_id": thread_id,
        "status": result.get("run", {}).get("status", "unknown"),
        "summary": report.get("summary", ""),
        "report_path": report.get("report_path"),
        "interrupts": serialize_interrupts(result),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def run_command(arguments: argparse.Namespace) -> int:
    """执行 ``run`` 子命令并在需要时输出人工审核载荷。

    Args:
        arguments: ``argparse`` 解析后的运行参数。

    Returns:
        图成功完成或正常暂停时返回零。
    """
    request_path = arguments.request_file.expanduser().resolve(strict=True)
    payload = load_cli_json(request_path, label="治理请求文件")
    request, workspace, checkpoint = resolve_request_payload(
        payload,
        base_directory=request_path.parent,
    )
    prompt_config, hook_config = resolve_lifecycle_payload(
        payload,
        base_directory=request_path.parent,
    )
    backend = arguments.checkpoint_backend or checkpoint.get("backend", "sqlite")
    if backend not in {"memory", "sqlite"}:
        raise ValueError("checkpoint.backend 只能是 memory 或 sqlite")

    configured_path = checkpoint.get("database_path", DEFAULT_CHECKPOINT_PATH)
    database_path = arguments.checkpoint_path or Path(configured_path)
    thread_id = arguments.thread_id or uuid4().hex
    state = create_initial_state(
        request,
        workspace,
        prompt_config=prompt_config,
        hook_config=hook_config,
    )
    with open_checkpointer(
        backend,
        database_path=database_path,
        input_root=workspace["input_root"],
    ) as checkpointer:
        graph = build_file_governance_graph(checkpointer=checkpointer)
        result = graph.invoke(
            state,
            config={"configurable": {"thread_id": thread_id}},
        )
    print_result(result, thread_id=thread_id)
    return 0


def resume_command(arguments: argparse.Namespace) -> int:
    """执行 ``resume`` 子命令并从 SQLite 恢复人工审核。

    Args:
        arguments: ``argparse`` 解析后的恢复参数。

    Returns:
        恢复后的图成功完成或再次正常暂停时返回零。

    Raises:
        ValueError: 指定线程不存在或没有可恢复状态时抛出。
    """
    response = load_cli_json(arguments.response_file, label="人工审核恢复文件")
    config = {"configurable": {"thread_id": arguments.thread_id}}
    with open_checkpointer(
        "sqlite",
        database_path=arguments.checkpoint_path,
    ) as checkpointer:
        graph = build_file_governance_graph(checkpointer=checkpointer)
        snapshot = graph.get_state(config)
        if not snapshot.values:
            raise ValueError(f"checkpoint 中不存在 thread_id={arguments.thread_id} 的状态")
        result = graph.invoke(Command(resume=response), config=config)
    print_result(result, thread_id=arguments.thread_id)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """解析 CLI 参数并执行新的治理运行或人工审核恢复。

    Args:
        argv: 可选命令行参数序列；为 ``None`` 时读取当前进程参数。

    Returns:
        成功或正常暂停返回零，可预期的输入与存储错误返回一。
    """
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "run":
            return run_command(arguments)
        if arguments.command == "resume":
            return resume_command(arguments)
        parser.error(f"未知子命令：{arguments.command}")
    except (json.JSONDecodeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"file-governance: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
