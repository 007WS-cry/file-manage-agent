from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from app.state.models import GitProcessResultState

"""本模块封装 Worktree Isolation 唯一允许使用的受控 Git 子进程边界。"""


# Worktree 生命周期允许调用的 Git 子命令；禁止任意配置、Hook 或外部命令执行入口。
ALLOWED_GIT_SUBCOMMANDS = frozenset({"rev-parse", "status", "worktree"})

# Git 标准输出和错误输出各自允许保留的最大字符数。
MAX_GIT_OUTPUT_CHARACTERS = 100_000

# 单次 Git 子进程默认允许运行的最长秒数。
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0


def _normalize_existing_directory(value: str | Path, *, field_name: str) -> Path:
    """解析并校验一个必须已经存在的目录。

    Args:
        value: 等待解析的目录路径。
        field_name: 用于错误说明的参数名称。

    Returns:
        已解析符号链接的绝对目录路径。

    Raises:
        ValueError: 路径不存在、不是目录或格式为空时抛出。
    """
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{field_name} 必须是非空目录路径")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{field_name} 不是已经存在的目录：{path}")
    return path


def _is_within(path: Path, root: Path) -> bool:
    """判断路径是否等于或位于指定受控根目录内。

    Args:
        path: 已规范化的待检查路径。
        root: 已规范化的允许根目录。

    Returns:
        路径没有越过根目录边界时返回 True。
    """
    return path == root or path.is_relative_to(root)


def _normalize_git_argument(value: object) -> str:
    """把单个 Git 参数转换为禁止控制字符的独立字符串。

    Args:
        value: 调用方准备作为一个 argv 元素传给 Git 的值。

    Returns:
        可直接加入 subprocess 参数列表的字符串。

    Raises:
        ValueError: 参数为空或包含 NUL、换行和回车控制字符时抛出。
    """
    argument = str(value)
    if not argument or any(character in argument for character in ("\x00", "\n", "\r")):
        raise ValueError("Git 参数不得为空或包含控制字符")
    return argument


def _validate_git_arguments(subcommand: str, arguments: list[str]) -> None:
    """限制白名单子命令只能使用 Worktree 生命周期需要的参数形状。

    Args:
        subcommand: 已通过白名单检查的 Git 子命令。
        arguments: 已完成控制字符检查的 argv 参数。

    Raises:
        ValueError: 参数可能启用强制删除、任意 Worktree 操作或未授权模式时抛出。
    """
    if subcommand == "rev-parse":
        if tuple(arguments) not in {
            ("--show-toplevel",),
            ("--verify", "HEAD"),
        }:
            raise ValueError("rev-parse 只允许查询仓库顶层目录或验证 HEAD")
        return
    if subcommand == "status":
        if tuple(arguments) not in {
            ("--porcelain=v1",),
            ("--porcelain=v1", "--untracked-files=all"),
        }:
            raise ValueError("status 只允许读取 porcelain v1 工作区状态")
        return

    is_add = (
        len(arguments) == 5
        and arguments[0] == "add"
        and arguments[1] == "-b"
        and not arguments[2].startswith("-")
        and Path(arguments[3]).is_absolute()
        and not arguments[4].startswith("-")
    )
    is_remove = (
        len(arguments) == 2
        and arguments[0] == "remove"
        and Path(arguments[1]).is_absolute()
    )
    if not (is_add or is_remove):
        raise ValueError(
            "worktree 只允许使用新分支创建，或不带 --force 的绝对路径安全移除"
        )


def run_git_process(
    repository_root: str | Path,
    subcommand: str,
    arguments: Sequence[object] = (),
    *,
    working_directory: str | Path | None = None,
    allowed_path_roots: Sequence[str | Path] = (),
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
    check: bool = True,
) -> GitProcessResultState:
    """在受控目录中以 argv 列表执行一个白名单 Git 子命令。

    本函数不会接受或拼接 Shell 命令，也不会启用 ``shell=True``。调用方只能使用
    Worktree 生命周期所需的 ``rev-parse``、``status`` 和 ``worktree`` 子命令；
    工作目录及绝对路径参数必须位于主仓库或显式允许的临时根目录内。该函数不是
    面向 LLM 的任意命令执行工具，不得用来运行 Hook、配置别名或调用外部程序。

    Args:
        repository_root: 已存在的主 Git 仓库根目录候选路径。
        subcommand: 白名单中的单个 Git 子命令。
        arguments: 不经过 Shell 解释的独立参数序列。
        working_directory: 可选 Git 工作目录；省略时使用主仓库目录。
        allowed_path_roots: Worktree 等允许位于主仓库外的额外受控根目录。
        timeout_seconds: 子进程允许运行的最长秒数。
        check: Git 返回非零退出码时是否抛出 RuntimeError。

    Returns:
        子命令、参数、工作目录、退出码和有限标准输出摘要。

    Raises:
        FileNotFoundError: 当前环境找不到 Git 可执行文件时抛出。
        TimeoutError: Git 执行超过指定超时时间时抛出。
        ValueError: 子命令、参数、目录或路径边界不符合安全规则时抛出。
        RuntimeError: ``check`` 为 True 且 Git 返回非零退出码时抛出。
    """
    repository = _normalize_existing_directory(
        repository_root,
        field_name="repository_root",
    )
    normalized_subcommand = str(subcommand).strip()
    if normalized_subcommand not in ALLOWED_GIT_SUBCOMMANDS:
        raise ValueError(f"不允许执行 Git 子命令：{normalized_subcommand or '<empty>'}")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or timeout_seconds <= 0
        or timeout_seconds > 300
    ):
        raise ValueError("timeout_seconds 必须位于 0 到 300 秒之间")

    allowed_roots = [repository]
    for index, root in enumerate(allowed_path_roots):
        allowed_roots.append(
            _normalize_existing_directory(
                root,
                field_name=f"allowed_path_roots[{index}]",
            )
        )
    working_path = (
        repository
        if working_directory is None
        else _normalize_existing_directory(
            working_directory,
            field_name="working_directory",
        )
    )
    if not any(_is_within(working_path, root) for root in allowed_roots):
        raise ValueError("Git 工作目录越过了允许的仓库和临时目录边界")

    if isinstance(arguments, (str, bytes)):
        raise ValueError("Git arguments 必须是独立 argv 元素组成的序列")
    normalized_arguments = [_normalize_git_argument(argument) for argument in arguments]
    _validate_git_arguments(normalized_subcommand, normalized_arguments)
    for argument in normalized_arguments:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            continue
        resolved_candidate = candidate.resolve(strict=False)
        if not any(_is_within(resolved_candidate, root) for root in allowed_roots):
            raise ValueError(f"Git 绝对路径参数越过允许边界：{resolved_candidate}")

    git_executable = shutil.which("git")
    if git_executable is None:
        raise FileNotFoundError("当前环境找不到 Git 可执行文件")
    command = [git_executable, normalized_subcommand, *normalized_arguments]
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "core.hooksPath"
    environment["GIT_CONFIG_VALUE_0"] = os.devnull
    try:
        completed = subprocess.run(
            command,
            cwd=str(working_path),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise TimeoutError(
            f"Git {normalized_subcommand} 超过 {float(timeout_seconds):g} 秒限制"
        ) from error

    stdout = completed.stdout[:MAX_GIT_OUTPUT_CHARACTERS]
    stderr = completed.stderr[:MAX_GIT_OUTPUT_CHARACTERS]
    result = GitProcessResultState(
        subcommand=normalized_subcommand,
        arguments=list(normalized_arguments),
        working_directory=str(working_path),
        return_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    if check and completed.returncode != 0:
        message = stderr.strip() or stdout.strip() or "Git 未提供错误说明"
        raise RuntimeError(
            f"Git {normalized_subcommand} 执行失败，退出码 "
            f"{completed.returncode}：{message[:1_000]}"
        )
    return result
