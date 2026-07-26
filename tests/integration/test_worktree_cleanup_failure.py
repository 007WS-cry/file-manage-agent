from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import app.tools.worktree as worktree_module
from app.tools.worktree import close_task_worktree, create_task_worktree

"""本文件集成验证 Worktree 移除失败时保留目录、分支和可恢复错误状态。"""


def run_test_git(repository: Path, *arguments: str) -> None:
    """在临时测试仓库执行固定初始化命令。

    Args:
        repository: 临时 Git 仓库根目录。
        arguments: 测试代码固定提供的 Git 参数。
    """
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    )


def create_git_repository(tmp_path: Path) -> Path:
    """创建用于清理失败注入的最小 Git 仓库。

    Args:
        tmp_path: pytest 临时目录。

    Returns:
        包含一个初始提交的 Git 仓库根目录。
    """
    if shutil.which("git") is None:
        pytest.skip("当前环境没有 Git 可执行文件")
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    )
    run_test_git(repository, "config", "user.name", "Worktree Test")
    run_test_git(repository, "config", "user.email", "worktree-test@example.invalid")
    (repository / "README.md").write_text("cleanup failure\n", encoding="utf-8")
    run_test_git(repository, "add", "README.md")
    run_test_git(repository, "commit", "-m", "initial")
    return repository


def test_worktree_remove_failure_retains_directory_and_failed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git remove 失败时不得强制删除，状态应为 failed 并保留脱敏摘要。"""
    repository = create_git_repository(tmp_path)
    worktree = create_task_worktree(
        repository,
        tmp_path / "worktrees",
        task_id="run-cleanup-failure:inventory",
    )
    original_run_git_process = worktree_module.run_git_process

    def fail_worktree_remove(
        repository_root,
        subcommand,
        arguments=(),
        **kwargs,
    ):
        """仅对安全关闭阶段的 git worktree remove 注入失败。"""
        if subcommand == "worktree" and arguments and arguments[0] == "remove":
            raise RuntimeError("injected remove failure")
        return original_run_git_process(
            repository_root,
            subcommand,
            arguments,
            **kwargs,
        )

    monkeypatch.setattr(worktree_module, "run_git_process", fail_worktree_remove)
    closed = close_task_worktree(worktree)

    assert closed["status"] == "failed"
    assert closed["closed_at"] is None
    assert closed["error_summary"] is not None
    assert Path(closed["path"]).is_dir()
    branches = subprocess.run(
        ["git", "-C", str(repository), "branch", "--list", closed["branch"]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    )
    assert closed["branch"] in branches.stdout
