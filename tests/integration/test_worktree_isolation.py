from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from app.graphs.routers import needs_worktree_isolation
from app.graphs.team_orchestration import build_team_orchestration_graph
from app.nodes.team_orchestration import (
    close_task_worktree,
    prepare_readonly_workspace,
    prepare_task_worktree,
)
from app.services.task_system import create_task_dag
from app.state.models import TaskItem, TeamOrchestrationGraphState, WorktreeState
from app.tools.worktree import (
    close_task_worktree as close_isolated_worktree,
)
from app.tools.worktree import (
    create_task_worktree as create_isolated_worktree,
)
from app.tools.worktree import inspect_task_worktree

"""本文件集成验证普通 Task 保持只读，以及显式写仓库 Task 的 Worktree 隔离与关闭。"""


# Worktree 集成测试统一使用的运行 ID。
RUN_ID = "run-worktree-isolation-001"

# Worktree 集成测试统一使用的带时区创建时间。
CREATED_AT = "2026-07-26T02:00:00+00:00"


def run_test_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """在临时测试仓库中执行初始化和断言所需的 Git 命令。

    Args:
        repository: 临时测试仓库目录。
        arguments: 测试固定提供的 Git 参数。

    Returns:
        已成功完成且包含文本输出的子进程结果。
    """
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
        shell=False,
    )


def create_git_repository(tmp_path: Path) -> Path:
    """创建包含一个初始提交的临时 Git 仓库。

    Args:
        tmp_path: pytest 为当前测试提供的临时目录。

    Returns:
        已配置本地测试身份并提交 README 的仓库根目录。
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
    (repository / "README.md").write_text("main repository\n", encoding="utf-8")
    run_test_git(repository, "add", "README.md")
    run_test_git(repository, "commit", "-m", "initial")
    return repository


def build_team_state(
    repository: Path,
    temporary_root: Path,
    *,
    requires_repository_write: bool,
) -> TeamOrchestrationGraphState:
    """构造当前 Inventory Task 的 Worktree 路由状态。

    Args:
        repository: 显式写任务允许使用的临时 Git 仓库。
        temporary_root: Worktree 只能创建在其中的临时根目录。
        requires_repository_write: 当前 Task 是否明确申请仓库写权限。

    Returns:
        可直接调用 Worktree 路由和图节点的团队编排状态。
    """
    tasks = create_task_dag(RUN_ID, created_at=CREATED_AT)
    inventory = cast(TaskItem, dict(tasks[0]))
    inventory["requires_repository_write"] = requires_repository_write
    tasks[0] = inventory
    input_root = temporary_root.parent / "business-input"
    input_root.mkdir(exist_ok=True)
    return TeamOrchestrationGraphState(
        run={
            "run_id": RUN_ID,
            "status": "running",
            "current_stage": "team_orchestration",
            "started_at": CREATED_AT,
            "finished_at": None,
        },
        workspace={
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(temporary_root.parent / "artifacts"),
            "report_root": str(temporary_root.parent / "reports"),
            "temporary_root": str(temporary_root),
            "project_git_root": str(repository),
        },
        task_update=None,
        dispatch_request={
            "task_id": inventory["task_id"],
            "document_id": "document-worktree-001",
            "content_preview": "有限内容预览",
            "structure_summary": {},
            "key_fields": {},
            "artifact_refs": [],
        },
        dispatch_result=None,
        active_worktree_id=None,
        tasks=tasks,
        todos=[],
        team_messages=[],
        worktrees=[],
        llm_calls=[],
        errors=[],
    )


def test_regular_governance_task_does_not_create_worktree(tmp_path: Path) -> None:
    """普通治理 Task 即使存在 Git 根目录也必须走 readonly 分支且不创建目录。"""
    repository = create_git_repository(tmp_path)
    temporary_root = tmp_path / "worktrees"
    state = build_team_state(
        repository,
        temporary_root,
        requires_repository_write=False,
    )

    assert needs_worktree_isolation(state) == "readonly"
    assert prepare_readonly_workspace(state) == {"active_worktree_id": None}
    assert not temporary_root.exists()

    node_names = set(build_team_orchestration_graph().get_graph().nodes)
    assert {
        "prepare_task_workspace",
        "prepare_task_worktree",
        "prepare_readonly_workspace",
        "close_task_worktree",
    }.issubset(node_names)


def test_explicit_repository_write_task_is_isolated_and_dirty_tree_is_retained(
    tmp_path: Path,
) -> None:
    """显式写仓库 Task 应在隔离分支修改，主仓库不变且脏 Worktree 安全保留。"""
    repository = create_git_repository(tmp_path)
    temporary_root = tmp_path / "worktrees"
    state = build_team_state(
        repository,
        temporary_root,
        requires_repository_write=True,
    )

    assert needs_worktree_isolation(state) == "worktree"
    prepared = prepare_task_worktree(state)
    assert prepared.get("errors") is None
    worktree = cast(WorktreeState, prepared["worktrees"][0])
    assert worktree["status"] == "in_use"
    inspection = inspect_task_worktree(worktree)
    assert inspection["clean"] is True

    isolated_file = Path(worktree["path"]) / "isolated-change.txt"
    isolated_file.write_text("worktree only\n", encoding="utf-8")
    assert not (repository / "isolated-change.txt").exists()

    state["worktrees"] = [worktree]
    state["active_worktree_id"] = worktree["id"]
    closed_update = close_task_worktree(state)
    retained = cast(WorktreeState, closed_update["worktrees"][0])
    assert retained["status"] == "completed"
    assert retained["clean"] is False
    assert Path(retained["path"]).is_dir()
    assert (repository / "README.md").read_text(encoding="utf-8") == "main repository\n"


def test_clean_worktree_is_removed_but_branch_is_preserved(tmp_path: Path) -> None:
    """干净 Worktree 应安全移除目录，同时保留未自动合并的隔离分支。"""
    repository = create_git_repository(tmp_path)
    worktree = create_isolated_worktree(
        repository,
        tmp_path / "worktrees",
        task_id=f"{RUN_ID}:inventory",
    )

    assert inspect_task_worktree(worktree)["clean"] is True
    closed = close_isolated_worktree(worktree)

    assert closed["status"] == "closed"
    assert not Path(closed["path"]).exists()
    branches = run_test_git(repository, "branch", "--list", closed["branch"])
    assert closed["branch"] in branches.stdout
