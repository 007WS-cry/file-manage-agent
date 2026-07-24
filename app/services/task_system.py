from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypedDict, cast

from app.state.models import TaskItem, TodoItem

"""本模块实现固定治理 Task DAG、拓扑校验、角色分配和 Todo 纯投影。"""


class TaskDefinition(TypedDict):
    """描述固定 Task DAG 中一个不可变的任务模板。"""

    task_type: str
    # Task 的稳定类型名称。

    title: str
    # Task 面向用户和日志展示的中文标题。

    dependency_types: tuple[str, ...]
    # 当前 Task 依赖的其他 Task 类型。

    input_refs: tuple[str, ...]
    # Task 默认读取的顶层状态字段引用。


class TodoDefinition(TypedDict):
    """描述一个由若干 Task 状态共同推导的固定 Todo 模板。"""

    key: str
    # Todo ID 使用的稳定短名称。

    title: str
    # Todo 面向用户展示的中文标题。

    task_types: tuple[str, ...]
    # 决定 Todo 状态的 Task 类型。

    order: int
    # Todo 的固定展示顺序。


# 固定 Task 类型到实际负责角色的映射；前三类 Task 可由固定 Subagent 执行。
TASK_ROLE_BY_TYPE: dict[str, str] = {
    "inventory": "content",
    "version_analysis": "version",
    "evidence": "evidence",
    "recommendation": "coordinator",
    "human_review": "coordinator",
    "report": "coordinator",
}

# 允许通过 Team Orchestration 分派给固定 Subagent 的 Task 类型。
SUBAGENT_TASK_TYPES = frozenset({"inventory", "version_analysis", "evidence"})

# 文件治理运行使用的固定 Task DAG 模板，元组顺序同时作为稳定展示顺序。
TASK_DAG_TEMPLATE: tuple[TaskDefinition, ...] = (
    {
        "task_type": "inventory",
        "title": "扫描文件并提取内容",
        "dependency_types": (),
        "input_refs": ("request", "workspace"),
    },
    {
        "task_type": "version_analysis",
        "title": "分析版本关系与分叉",
        "dependency_types": ("inventory",),
        "input_refs": ("files", "documents"),
    },
    {
        "task_type": "evidence",
        "title": "匹配 PDF 与客户发送证据",
        "dependency_types": ("version_analysis",),
        "input_refs": ("files", "documents", "version_groups"),
    },
    {
        "task_type": "recommendation",
        "title": "生成主版本推荐",
        "dependency_types": ("evidence",),
        "input_refs": ("version_chains", "pdf_exports", "deliveries"),
    },
    {
        "task_type": "human_review",
        "title": "完成人工审核",
        "dependency_types": ("recommendation",),
        "input_refs": ("decisions", "human_review"),
    },
    {
        "task_type": "report",
        "title": "生成治理报告",
        "dependency_types": ("human_review",),
        "input_refs": ("decisions", "errors", "human_review"),
    },
)

# 用户可见的固定 Todo 模板；Todo 状态始终从关联 Task 重新计算。
TODO_TEMPLATE: tuple[TodoDefinition, ...] = (
    {
        "key": "prepare_file_facts",
        "title": "准备文件事实",
        "task_types": ("inventory",),
        "order": 1,
    },
    {
        "key": "build_governance_conclusion",
        "title": "建立版本治理结论",
        "task_types": ("version_analysis", "evidence", "recommendation"),
        "order": 2,
    },
    {
        "key": "complete_human_review",
        "title": "完成人工确认",
        "task_types": ("human_review",),
        "order": 3,
    },
    {
        "key": "produce_report",
        "title": "输出治理报告",
        "task_types": ("report",),
        "order": 4,
    },
)


def build_task_id(run_id: str, task_type: str) -> str:
    """根据运行 ID 和 Task 类型生成确定性的 Task ID。

    Args:
        run_id: 当前治理运行的非空唯一标识。
        task_type: 固定 DAG 模板中的 Task 类型。

    Returns:
        形如 ``run_id:task_type`` 的稳定 Task ID。

    Raises:
        ValueError: 运行 ID 为空或 Task 类型不在固定模板中时抛出。
    """
    normalized_run_id = run_id.strip() if isinstance(run_id, str) else ""
    if not normalized_run_id:
        raise ValueError("run_id 必须是非空字符串")
    if task_type not in TASK_ROLE_BY_TYPE:
        raise ValueError(f"未知 Task 类型：{task_type}")
    return f"{normalized_run_id}:{task_type}"


def build_task_execution_id(run_id: str, task_type: str) -> str:
    """根据运行 ID 和 Task 类型生成稳定的逻辑执行 ID。

    该 ID 标识一次逻辑 Task，而不是某一次尝试，因此有限重试不会改变它。单次
    节点执行应继续使用独立的 ``NodeExecutionRecord.id``。

    Args:
        run_id: 当前治理运行的非空唯一标识。
        task_type: 固定 DAG 模板中的 Task 类型。

    Returns:
        形如 ``run_id:task_type:execution`` 的稳定执行 ID。

    Raises:
        ValueError: 运行 ID 为空或 Task 类型不在固定模板中时抛出。
    """
    return f"{build_task_id(run_id, task_type)}:execution"


def resolve_error_task(
    tasks: Sequence[TaskItem],
    *,
    task_id: str | None = None,
    task_type: str | None = None,
) -> TaskItem | None:
    """按显式 ID 或固定类型查找错误所属 Task，并返回与输入解耦的副本。

    Args:
        tasks: 当前运行的固定 Task DAG。
        task_id: 可选的精确 Task ID，存在时优先匹配。
        task_type: 可选的固定 Task 类型，仅在精确 ID 未命中时使用。

    Returns:
        命中的 Task 副本；两个条件均未命中时返回 None。
    """
    if task_id is not None:
        matched = next(
            (task for task in tasks if task.get("task_id") == task_id),
            None,
        )
        if matched is not None:
            return cast(TaskItem, dict(matched))
    if task_type is None:
        return None
    matched = next(
        (task for task in tasks if task.get("task_type") == task_type),
        None,
    )
    return cast(TaskItem, dict(matched)) if matched is not None else None


def _index_tasks(tasks: Sequence[TaskItem]) -> dict[str, TaskItem]:
    """按照 task_id 建立索引，并拒绝空 ID 和重复 Task。

    Args:
        tasks: 等待建立索引的 Task 序列。

    Returns:
        Task ID 到独立 Task 副本的映射。

    Raises:
        ValueError: Task ID 为空或重复时抛出。
    """
    indexed: dict[str, TaskItem] = {}
    for task in tasks:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Task DAG 中的每个 Task 都必须包含非空 task_id")
        if task_id in indexed:
            raise ValueError(f"Task DAG 包含重复 task_id：{task_id}")
        indexed[task_id] = cast(TaskItem, dict(task))
    return indexed


def topologically_sort_tasks(tasks: Sequence[TaskItem]) -> list[TaskItem]:
    """对 Task DAG 执行稳定拓扑排序，并检测非法依赖和循环。

    当多个 Task 同时没有未完成依赖时，本函数保留它们在输入序列中的相对顺序，
    从而保证相同 DAG 在不同调用中得到一致结果。函数不会修改输入 Task。

    Args:
        tasks: 需要校验和排序的 Task 序列。

    Returns:
        按依赖顺序排列的 Task 独立副本列表。

    Raises:
        ValueError: DAG 为空，或者存在重复 ID、重复依赖、未知依赖、自依赖或环时抛出。
    """
    if not tasks:
        raise ValueError("Task DAG 不能为空")

    indexed = _index_tasks(tasks)
    original_order = {task_id: index for index, task_id in enumerate(indexed)}
    indegree = {task_id: 0 for task_id in indexed}
    dependents = {task_id: [] for task_id in indexed}

    for task_id, task in indexed.items():
        dependencies = task.get("dependencies")
        if not isinstance(dependencies, list):
            raise ValueError(f"Task {task_id} 的 dependencies 必须是列表")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"Task {task_id} 包含重复依赖")
        for dependency_id in dependencies:
            if dependency_id == task_id:
                raise ValueError(f"Task {task_id} 不得依赖自身")
            if dependency_id not in indexed:
                raise ValueError(f"Task {task_id} 引用了未知依赖：{dependency_id}")
            indegree[task_id] += 1
            dependents[dependency_id].append(task_id)

    ready = [task_id for task_id, degree in indegree.items() if degree == 0]
    ready.sort(key=original_order.__getitem__)
    sorted_ids: list[str] = []

    while ready:
        current_id = ready.pop(0)
        sorted_ids.append(current_id)
        for dependent_id in sorted(
            dependents[current_id],
            key=original_order.__getitem__,
        ):
            indegree[dependent_id] -= 1
            if indegree[dependent_id] == 0:
                ready.append(dependent_id)
                ready.sort(key=original_order.__getitem__)

    if len(sorted_ids) != len(indexed):
        cyclic_ids = [task_id for task_id, degree in indegree.items() if degree > 0]
        raise ValueError(f"Task DAG 存在循环依赖：{', '.join(cyclic_ids)}")

    return [cast(TaskItem, dict(indexed[task_id])) for task_id in sorted_ids]


def validate_task_dag(tasks: Sequence[TaskItem]) -> None:
    """验证 Task 集合是否构成合法的有向无环图。

    Args:
        tasks: 需要验证的 Task 序列。

    Raises:
        ValueError: Task DAG 为空或存在重复、未知、自引用、重复依赖或环时抛出。
    """
    topologically_sort_tasks(tasks)


def create_task_dag(
    run_id: str,
    *,
    created_at: str,
    existing_tasks: Sequence[TaskItem] | None = None,
) -> list[TaskItem]:
    """幂等创建一次治理运行使用的固定 Task DAG。

    已存在的 Task 会按 task_id 原样保留其状态、输出、错误和时间字段，并为旧记录
    补齐稳定执行 ID 与零次尝试；缺少的固定 Task 才会被补齐。函数拒绝不属于当前
    运行或偏离固定依赖模板的 Task，避免恢复 checkpoint 时静默改变治理执行顺序。

    Args:
        run_id: 当前治理运行的非空唯一标识。
        created_at: 新建 Task 使用的带时区 ISO 8601 时间，重放时应传入运行开始时间。
        existing_tasks: checkpoint 或已有状态中的 Task；首次创建时省略。

    Returns:
        按固定模板顺序排列且通过 DAG 校验的 Task 列表。

    Raises:
        ValueError: 参数为空，已有 Task 重复、归属错误或依赖结构不符合固定模板时抛出。
    """
    if not isinstance(created_at, str) or not created_at.strip():
        raise ValueError("created_at 必须是非空 ISO 8601 时间字符串")

    normalized_run_id = run_id.strip() if isinstance(run_id, str) else ""
    if not normalized_run_id:
        raise ValueError("run_id 必须是非空字符串")

    existing = _index_tasks(existing_tasks or [])
    expected_ids = {
        build_task_id(normalized_run_id, definition["task_type"])
        for definition in TASK_DAG_TEMPLATE
    }
    unexpected_ids = [task_id for task_id in existing if task_id not in expected_ids]
    if unexpected_ids:
        raise ValueError("已有 Task 不属于当前固定 DAG：" + ", ".join(unexpected_ids))

    result: list[TaskItem] = []
    for definition in TASK_DAG_TEMPLATE:
        task_type = definition["task_type"]
        task_id = build_task_id(normalized_run_id, task_type)
        dependencies = [
            build_task_id(normalized_run_id, dependency_type)
            for dependency_type in definition["dependency_types"]
        ]
        existing_task = existing.get(task_id)
        if existing_task is not None:
            if existing_task.get("task_type") != task_type:
                raise ValueError(f"Task {task_id} 的 task_type 与固定模板不一致")
            if existing_task.get("dependencies") != dependencies:
                raise ValueError(f"Task {task_id} 的 dependencies 与固定模板不一致")
            expected_execution_id = build_task_execution_id(normalized_run_id, task_type)
            execution_id = existing_task.get("execution_id", expected_execution_id)
            if execution_id != expected_execution_id:
                raise ValueError(f"Task {task_id} 的 execution_id 与当前运行不一致")
            attempt_count = existing_task.get("attempt_count", 0)
            if (
                isinstance(attempt_count, bool)
                or not isinstance(attempt_count, int)
                or attempt_count < 0
            ):
                raise ValueError(f"Task {task_id} 的 attempt_count 必须是非负整数")
            normalized_task = dict(existing_task)
            normalized_task["execution_id"] = expected_execution_id
            normalized_task["attempt_count"] = attempt_count
            result.append(cast(TaskItem, normalized_task))
            continue

        result.append(
            TaskItem(
                task_id=task_id,
                execution_id=build_task_execution_id(normalized_run_id, task_type),
                task_type=cast(
                    Literal[
                        "inventory",
                        "version_analysis",
                        "evidence",
                        "recommendation",
                        "human_review",
                        "report",
                    ],
                    task_type,
                ),
                title=definition["title"],
                status="pending",
                attempt_count=0,
                dependencies=dependencies,
                assigned_role=cast(
                    Literal["coordinator", "content", "version", "evidence"],
                    TASK_ROLE_BY_TYPE[task_type],
                ),
                input_refs=list(definition["input_refs"]),
                output_refs=[],
                error=None,
                created_at=created_at,
                updated_at=created_at,
            )
        )

    validate_task_dag(result)
    return result


def assign_tasks_to_roles(tasks: Sequence[TaskItem]) -> list[TaskItem]:
    """按照固定职责映射设置 Task 的 assigned_role。

    本函数只修正角色字段，不修改 Task 状态、依赖、输入输出、错误或时间。前三类
    Task 的角色会用于 Team Orchestration 选择实际固定 Subagent。

    Args:
        tasks: 已创建并等待分配逻辑角色的合法 Task DAG。

    Returns:
        保持原顺序且角色字段符合固定映射的 Task 独立副本列表。

    Raises:
        ValueError: Task DAG 非法或包含未知 Task 类型时抛出。
    """
    validate_task_dag(tasks)
    assigned: list[TaskItem] = []
    for task in tasks:
        task_type = task.get("task_type")
        role = TASK_ROLE_BY_TYPE.get(str(task_type))
        if role is None:
            raise ValueError(f"无法为未知 Task 类型分配角色：{task_type}")
        updated_task = dict(task)
        updated_task["assigned_role"] = role
        assigned.append(cast(TaskItem, updated_task))
    return assigned


def resolve_subagent_task(
    tasks: Sequence[TaskItem],
    task_id: str,
) -> TaskItem:
    """解析并校验一次 Subagent 分派所对应的真实 Task。

    Args:
        tasks: 当前运行的完整合法 Task DAG。
        task_id: Subagent 最小输入信封中声明的 Task ID。

    Returns:
        与输入 ID 对应、角色正确且允许分派的 Task 独立副本。

    Raises:
        ValueError: Task ID 为空、未知、不可分派、角色不一致或已经失败终结时抛出。
    """
    validate_task_dag(tasks)
    normalized_task_id = task_id.strip() if isinstance(task_id, str) else ""
    if not normalized_task_id:
        raise ValueError("Subagent 分派必须提供非空 task_id")

    task = next(
        (item for item in tasks if item.get("task_id") == normalized_task_id),
        None,
    )
    if task is None:
        raise ValueError(f"Subagent 分派引用了未知 Task：{normalized_task_id}")

    task_type = str(task.get("task_type", ""))
    if task_type not in SUBAGENT_TASK_TYPES:
        raise ValueError(f"Task {normalized_task_id} 不允许分派给 Subagent")
    expected_role = TASK_ROLE_BY_TYPE[task_type]
    if task.get("assigned_role") != expected_role:
        raise ValueError(f"Task {normalized_task_id} 的 assigned_role 与固定职责不一致")
    if task.get("status") in {"failed", "skipped"}:
        raise ValueError(f"终态 Task {normalized_task_id} 不允许再次分派")
    return cast(TaskItem, dict(task))


def _derive_todo_status(
    related_tasks: Sequence[TaskItem],
) -> Literal["pending", "in_progress", "completed", "blocked"]:
    """仅根据关联 Task 的当前状态计算一个 Todo 状态。

    Args:
        related_tasks: 当前 Todo 关联的一个或多个 Task。

    Returns:
        根据失败、正常终态、运行进度或等待状态得到的 Todo 状态。

    Raises:
        ValueError: Todo 没有关联任何 Task 时抛出。
    """
    if not related_tasks:
        raise ValueError("Todo 至少需要关联一个 Task")
    if any(task["status"] == "failed" for task in related_tasks):
        return "blocked"
    if any(task["status"] == "skipped" and bool(task.get("error")) for task in related_tasks):
        return "blocked"
    if all(task["status"] in {"completed", "partial", "skipped"} for task in related_tasks):
        return "completed"
    if any(task["status"] != "pending" for task in related_tasks):
        return "in_progress"
    return "pending"


def update_todos_from_tasks(run_id: str, tasks: Sequence[TaskItem]) -> list[TodoItem]:
    """丢弃旧 Todo 状态并仅根据完整 Task DAG 生成用户进度视图。

    该函数不接收旧 Todo，也不会修改输入 Task，因此 Task 始终是执行状态的唯一
    事实来源。相同 run_id 和 Task 状态必然生成内容、顺序都相同的 Todo 列表。

    Args:
        run_id: 当前治理运行的非空唯一标识。
        tasks: 用于生成 Todo 的完整固定 Task DAG。

    Returns:
        按固定 order 排列的四个 Todo 独立记录。

    Raises:
        ValueError: Task DAG 非法、Task ID 不属于当前运行、类型重复或缺失时抛出。
    """
    normalized_run_id = run_id.strip() if isinstance(run_id, str) else ""
    if not normalized_run_id:
        raise ValueError("run_id 必须是非空字符串")
    validate_task_dag(tasks)

    task_by_type: dict[str, TaskItem] = {}
    expected_types = {definition["task_type"] for definition in TASK_DAG_TEMPLATE}
    for task in tasks:
        task_type = str(task.get("task_type", ""))
        if task_type not in expected_types:
            raise ValueError(f"Todo 投影遇到未知 Task 类型：{task_type}")
        if task_type in task_by_type:
            raise ValueError(f"Todo 投影遇到重复 Task 类型：{task_type}")
        expected_id = build_task_id(normalized_run_id, task_type)
        if task["task_id"] != expected_id:
            raise ValueError(f"Task {task['task_id']} 不属于运行 {normalized_run_id}")
        task_by_type[task_type] = task

    missing_types = expected_types.difference(task_by_type)
    if missing_types:
        raise ValueError("Todo 投影缺少 Task 类型：" + ", ".join(sorted(missing_types)))

    todos: list[TodoItem] = []
    for definition in TODO_TEMPLATE:
        related_tasks = [task_by_type[task_type] for task_type in definition["task_types"]]
        todos.append(
            TodoItem(
                id=f"{normalized_run_id}:todo:{definition['key']}",
                title=definition["title"],
                status=_derive_todo_status(related_tasks),
                related_task_ids=[task["task_id"] for task in related_tasks],
                order=definition["order"],
            )
        )
    return todos
