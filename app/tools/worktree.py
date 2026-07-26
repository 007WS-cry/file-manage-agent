from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import cast

from app.state.models import WorktreeInspectionState, WorktreeState
from app.tools.git_process import DEFAULT_GIT_TIMEOUT_SECONDS, run_git_process
from app.utils.runtime import utc_now_iso

"""本模块创建、只读检查并安全关闭显式仓库写入 Task 使用的隔离 Git Worktree。"""


# Worktree 分支和基础引用允许使用的保守 Git 引用字符。
SAFE_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")

# Worktree 目录和分支中最多保留的 Task ID 可读字符数。
MAX_WORKTREE_SLUG_CHARACTERS = 48


def _normalize_git_ref(value: str, *, field_name: str) -> str:
    """校验 Worktree 创建使用的基础引用或分支前缀。

    Args:
        value: 等待用于 Git argv 的引用字符串。
        field_name: 用于错误说明的参数名称。

    Returns:
        去除首尾空白且符合保守字符集的引用。

    Raises:
        ValueError: 引用可能被解释为选项或包含危险引用序列时抛出。
    """
    normalized = value.strip() if isinstance(value, str) else ""
    if (
        not SAFE_GIT_REF_PATTERN.fullmatch(normalized)
        or normalized.startswith("-")
        or ".." in normalized
        or "//" in normalized
        or "@{" in normalized
        or normalized.endswith(("/", "."))
    ):
        raise ValueError(f"{field_name} 不是允许的保守 Git 引用")
    return normalized


def _task_slug(task_id: str) -> str:
    """把 Task ID 转换为有限且可用于目录和分支的可读片段。

    Args:
        task_id: Worktree 所属的非空 Task ID。

    Returns:
        仅包含小写字母、数字和连字符的有限片段。

    Raises:
        ValueError: Task ID 为空时抛出。
    """
    normalized = task_id.strip() if isinstance(task_id, str) else ""
    if not normalized:
        raise ValueError("task_id 必须是非空字符串")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()
    return (slug or "task")[:MAX_WORKTREE_SLUG_CHARACTERS]


def resolve_git_repository_root(
    repository_root: str | Path,
    *,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> Path:
    """确认候选路径就是 Git 主仓库或 Worktree 的顶层目录。

    该函数只执行 ``git rev-parse --show-toplevel``，不会读取业务文件、修改仓库、
    运行 Hook 或解析任意 Shell 文本。

    Args:
        repository_root: 等待确认的已存在目录。
        timeout_seconds: Git 边界检查允许运行的最长秒数。

    Returns:
        Git 返回且与候选路径一致的规范化绝对根目录。

    Raises:
        ValueError: 候选路径不是仓库顶层目录时抛出。
        RuntimeError: Git 无法确认仓库根目录时抛出。
    """
    candidate = Path(repository_root).expanduser().resolve()
    result = run_git_process(
        candidate,
        "rev-parse",
        ("--show-toplevel",),
        timeout_seconds=timeout_seconds,
    )
    reported = Path(result["stdout"].strip()).expanduser().resolve()
    if reported != candidate:
        raise ValueError(f"repository_root 必须直接指向 Git 顶层目录：{reported}")
    return reported


def create_task_worktree(
    repository_root: str | Path,
    worktree_root: str | Path,
    *,
    task_id: str,
    base_ref: str = "HEAD",
    branch_prefix: str = "agent-worktree",
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> WorktreeState:
    """为一个明确获准写治理仓库的 Task 创建隔离 Git Worktree。

    本函数只能在调用方指定的受控临时根目录下创建一个新路径，并通过 argv 列表
    执行 ``git worktree add -b``。它不会修改原始业务文件，不会覆盖既有目录，
    不会自动合并分支，也不会删除已有分支。普通文件分析 Task 不应调用本函数。

    Args:
        repository_root: 治理项目主 Git 仓库的顶层目录。
        worktree_root: 允许创建隔离目录的专用临时根目录。
        task_id: 唯一拥有该 Worktree 的显式写仓库 Task ID。
        base_ref: 新隔离分支使用的基础 Git 引用。
        branch_prefix: 隔离分支使用的固定前缀。
        timeout_seconds: 单次 Git 调用允许运行的最长秒数。

    Returns:
        状态为 ``ready``、路径和分支均已确定的 Worktree 状态。

    Raises:
        FileExistsError: 目标 Worktree 路径已经存在时抛出。
        ValueError: 仓库、临时目录、Task 或引用不符合边界规则时抛出。
        RuntimeError: Git 创建隔离 Worktree 失败时抛出。
    """
    repository = resolve_git_repository_root(
        repository_root,
        timeout_seconds=timeout_seconds,
    )
    root = Path(worktree_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError("worktree_root 必须是目录")

    normalized_base_ref = _normalize_git_ref(base_ref, field_name="base_ref")
    normalized_branch_prefix = _normalize_git_ref(
        branch_prefix,
        field_name="branch_prefix",
    )
    normalized_task_id = task_id.strip() if isinstance(task_id, str) else ""
    slug = _task_slug(normalized_task_id)
    identity = hashlib.sha256(
        f"{repository}\x1f{normalized_task_id}".encode()
    ).hexdigest()[:12]
    worktree_id = f"worktree-{identity}"
    path = (root / f"{slug}-{identity}").resolve()
    if not path.is_relative_to(root):
        raise ValueError("Worktree 目标路径越过了受控临时根目录")
    if path.exists():
        raise FileExistsError(f"Worktree 目标路径已经存在：{path}")
    branch = f"{normalized_branch_prefix}/{slug}-{identity}"

    run_git_process(
        repository,
        "worktree",
        ("add", "-b", branch, str(path), normalized_base_ref),
        allowed_path_roots=(root,),
        timeout_seconds=timeout_seconds,
    )
    created_at = utc_now_iso()
    return WorktreeState(
        id=worktree_id,
        owner_task_id=normalized_task_id,
        repository_root=str(repository),
        path=str(path),
        branch=branch,
        base_ref=normalized_base_ref,
        status="ready",
        original_files_readonly=True,
        created_at=created_at,
        closed_at=None,
        clean=None,
        error_summary=None,
    )


def inspect_task_worktree(
    worktree: WorktreeState,
    *,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> WorktreeInspectionState:
    """只读检查隔离 Worktree 的归属、HEAD 和 porcelain 状态。

    本函数不会读取文件正文或执行仓库 Hook，只调用 ``rev-parse`` 和 ``status``。
    Worktree 路径必须仍位于其当前父目录边界内，并且 Git 顶层目录必须与状态一致。

    Args:
        worktree: 已创建且尚未安全关闭的 Worktree 状态。
        timeout_seconds: 每次只读 Git 检查允许运行的最长秒数。

    Returns:
        HEAD、是否干净和有限 porcelain 行组成的检查结果。

    Raises:
        ValueError: Worktree 状态路径、归属或只读边界不合法时抛出。
        RuntimeError: Git 无法读取 Worktree 状态时抛出。
    """
    if worktree.get("original_files_readonly") is not True:
        raise ValueError("Worktree 状态必须保持 original_files_readonly=true")
    repository = resolve_git_repository_root(
        worktree["repository_root"],
        timeout_seconds=timeout_seconds,
    )
    path = Path(worktree["path"]).expanduser().resolve()
    allowed_root = path.parent
    if not path.is_dir():
        raise ValueError(f"Worktree 路径不存在或不是目录：{path}")

    top_level = run_git_process(
        repository,
        "rev-parse",
        ("--show-toplevel",),
        working_directory=path,
        allowed_path_roots=(allowed_root,),
        timeout_seconds=timeout_seconds,
    )
    if Path(top_level["stdout"].strip()).expanduser().resolve() != path:
        raise ValueError("Worktree Git 顶层目录与状态路径不一致")
    head = run_git_process(
        repository,
        "rev-parse",
        ("--verify", "HEAD"),
        working_directory=path,
        allowed_path_roots=(allowed_root,),
        timeout_seconds=timeout_seconds,
    )
    status = run_git_process(
        repository,
        "status",
        ("--porcelain=v1", "--untracked-files=all"),
        working_directory=path,
        allowed_path_roots=(allowed_root,),
        timeout_seconds=timeout_seconds,
    )
    lines = [line for line in status["stdout"].splitlines() if line]
    return WorktreeInspectionState(
        path=str(path),
        head=head["stdout"].strip(),
        clean=not lines,
        status_porcelain=lines,
    )


def close_task_worktree(
    worktree: WorktreeState,
    *,
    timeout_seconds: float = DEFAULT_GIT_TIMEOUT_SECONDS,
) -> WorktreeState:
    """检查并安全关闭一个隔离 Worktree，保留脏目录和隔离分支。

    已关闭状态会幂等返回。Worktree 存在未提交、已暂存或未跟踪改动时，本函数
    将状态改为 ``completed`` 并保留目录供人工检查；只有完全干净时才执行不带
    ``--force`` 的 ``git worktree remove``。隔离分支始终保留，且不会自动合并。

    Args:
        worktree: 等待检查和关闭的 Worktree 状态。
        timeout_seconds: 每次 Git 检查或移除允许运行的最长秒数。

    Returns:
        ``closed``、``completed`` 或 ``failed`` 状态的独立 Worktree 副本。
    """
    result = cast(WorktreeState, dict(worktree))
    if result.get("status") == "closed":
        return result
    try:
        inspection = inspect_task_worktree(
            result,
            timeout_seconds=timeout_seconds,
        )
        result["clean"] = inspection["clean"]
        if not inspection["clean"]:
            result["status"] = "completed"
            result["closed_at"] = None
            result["error_summary"] = None
            return result

        repository = resolve_git_repository_root(
            result["repository_root"],
            timeout_seconds=timeout_seconds,
        )
        path = Path(result["path"]).expanduser().resolve()
        run_git_process(
            repository,
            "worktree",
            ("remove", str(path)),
            allowed_path_roots=(path.parent,),
            timeout_seconds=timeout_seconds,
        )
        result["status"] = "closed"
        result["closed_at"] = utc_now_iso()
        result["error_summary"] = None
        return result
    except (FileNotFoundError, TimeoutError, ValueError, RuntimeError) as error:
        result["status"] = "failed"
        result["closed_at"] = None
        result["error_summary"] = (
            f"{type(error).__name__}: Worktree 安全关闭失败，目录和分支未被强制删除。"
        )
        return result
