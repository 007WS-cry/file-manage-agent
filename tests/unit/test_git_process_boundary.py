from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.tools.git_process import run_git_process

"""本文件单元测试 Git 子进程的子命令、Shell、目录和绝对路径安全边界。"""


def test_git_process_rejects_non_allowlisted_subcommand(tmp_path: Path) -> None:
    """config 等非 Worktree 白名单子命令必须在启动进程前被拒绝。"""
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="不允许执行 Git 子命令"):
        run_git_process(repository, "config", ("user.name", "unsafe"))


def test_git_process_rejects_working_directory_outside_boundary(
    tmp_path: Path,
) -> None:
    """Git 工作目录越过仓库和显式临时根目录时必须被拒绝。"""
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="工作目录越过"):
        run_git_process(
            repository,
            "status",
            ("--porcelain=v1",),
            working_directory=outside,
        )


def test_git_process_uses_argv_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """允许命令必须按 argv 调用 subprocess，并显式保持 shell=False。"""
    repository = tmp_path / "repository"
    repository.mkdir()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        """记录 subprocess 调用参数并返回成功完成对象。"""
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

    def fake_which(_: str) -> str:
        """模拟当前环境已安装 Git 可执行程序。"""
        return "git"

    monkeypatch.setattr("app.tools.git_process.shutil.which", fake_which)
    monkeypatch.setattr("app.tools.git_process.subprocess.run", fake_run)

    result = run_git_process(
        repository,
        "status",
        ("--porcelain=v1",),
    )

    assert captured["command"] == ["git", "status", "--porcelain=v1"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"]["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert captured["kwargs"]["env"]["GIT_CONFIG_VALUE_0"] == str(Path(os.devnull))
    assert result["return_code"] == 0


def test_git_process_rejects_unapproved_worktree_operations(tmp_path: Path) -> None:
    """Git 边界必须拒绝 prune、强制移除和其他未授权 Worktree 参数形状。"""
    repository = tmp_path / "repository"
    repository.mkdir()

    for arguments in (
        ("prune",),
        ("remove", "--force", str(repository / "isolated")),
        ("move", str(repository / "one"), str(repository / "two")),
    ):
        with pytest.raises(ValueError, match="worktree 只允许"):
            run_git_process(repository, "worktree", arguments)


def test_git_process_rejects_absolute_path_argument_outside_boundary(
    tmp_path: Path,
) -> None:
    """绝对路径参数不得指向仓库和显式临时根目录之外。"""
    repository = tmp_path / "repository"
    outside = tmp_path / "outside"
    repository.mkdir()
    outside.mkdir()

    with pytest.raises(ValueError, match="绝对路径参数越过"):
        run_git_process(
            repository,
            "worktree",
            (
                "add",
                "-b",
                "agent-worktree/test",
                str(outside / "worktree"),
                "HEAD",
            ),
        )
