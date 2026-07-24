from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.state.models import ErrorRecord, NodeExecutionRecord
from app.storage.orm_models import (
    ContextSummaryModel,
    ErrorRecoveryRecordModel,
    GovernanceRunModel,
    HumanReviewModel,
    MemoryItemModel,
    NodeExecutionRecordModel,
    ToolCallAuditModel,
)

"""本模块通过 Repository 隔离七张应用表的数据访问，不负责创建 Session 或提交事务。"""


# Repository 泛型使用的 SQLAlchemy ORM 模型类型。
ModelT = TypeVar("ModelT")

# 错误恢复记录允许持久化的固定动作，禁止动态函数名或任意图节点名称。
ERROR_RECOVERY_ACTIONS = frozenset(
    {
        "none",
        "retry",
        "reuse_result",
        "skip_file",
        "fallback",
        "continue_partial",
        "wait_human",
        "abort",
    }
)

# 允许直接复用持久化结果的节点执行终态。
REUSABLE_NODE_EXECUTION_STATUSES = frozenset({"succeeded", "reused"})

# 同一次节点尝试允许的状态转换；重新执行必须先增加 attempt_count。
NODE_EXECUTION_STATUS_TRANSITIONS = {
    "pending": frozenset({"pending", "running", "succeeded", "failed"}),
    "running": frozenset({"running", "succeeded", "failed"}),
    "failed": frozenset({"failed"}),
    "succeeded": frozenset({"succeeded", "reused"}),
    "reused": frozenset({"reused"}),
}

# 错误恢复生命周期允许的状态转换，最终 recovered 或 failed 不得重新打开。
ERROR_RECOVERY_STATUS_TRANSITIONS = {
    "pending": frozenset(
        {
            "pending",
            "retrying",
            "fallback_applied",
            "waiting_human",
            "recovered",
            "failed",
        }
    ),
    "retrying": frozenset(
        {
            "retrying",
            "pending",
            "fallback_applied",
            "waiting_human",
            "recovered",
            "failed",
        }
    ),
    "fallback_applied": frozenset({"fallback_applied", "recovered", "failed"}),
    "waiting_human": frozenset(
        {
            "waiting_human",
            "retrying",
            "fallback_applied",
            "recovered",
            "failed",
        }
    ),
    "recovered": frozenset({"recovered"}),
    "failed": frozenset({"failed"}),
}


def _normalize_required_identifier(value: str, *, field_name: str) -> str:
    """校验并规范化 Repository 查询使用的必需标识。

    Args:
        value: 等待校验的标识字符串。
        field_name: 用于异常说明的字段名称。

    Returns:
        去除首尾空白后的非空标识。

    Raises:
        TypeError: 标识不是字符串时抛出。
        ValueError: 标识为空时抛出。
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} 必须是字符串")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} 不得为空")
    return normalized


def _normalize_limit(limit: int) -> int:
    """校验 Repository 列表查询的结果数量上限。

    Args:
        limit: 调用方希望返回的最大记录数。

    Returns:
        位于 1 到 1000 之间的结果数量上限。

    Raises:
        TypeError: ``limit`` 不是整数或错误地使用布尔值时抛出。
        ValueError: ``limit`` 不在允许范围内时抛出。
    """
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit 必须是整数")
    if limit < 1 or limit > 1000:
        raise ValueError("limit 必须位于 1 到 1000 之间")
    return limit


def _normalize_nonnegative_integer(value: int, *, field_name: str) -> int:
    """校验恢复记录使用的非负整数。

    Args:
        value: 等待校验的整数。
        field_name: 用于异常说明的字段名称。

    Returns:
        已确认不是布尔值的非负整数。

    Raises:
        TypeError: 输入不是整数或错误地使用布尔值时抛出。
        ValueError: 输入为负数时抛出。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须是整数")
    if value < 0:
        raise ValueError(f"{field_name} 不得为负数")
    return value


def _parse_required_datetime(value: str, *, field_name: str) -> datetime:
    """把状态中的 ISO 8601 时间转换为带时区 datetime。

    Args:
        value: 状态中保存的 ISO 8601 时间字符串。
        field_name: 用于异常说明的字段名称。

    Returns:
        已确认带时区信息的 datetime。

    Raises:
        TypeError: 输入不是字符串时抛出。
        ValueError: 输入为空、格式非法或缺少时区时抛出。
    """
    normalized = _normalize_required_identifier(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} 必须是合法 ISO 8601 时间") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _parse_optional_datetime(
    value: str | None,
    *,
    field_name: str,
) -> datetime | None:
    """把可选 ISO 8601 时间转换为带 UTC 时区 datetime。

    Args:
        value: 可选状态时间字符串。
        field_name: 用于异常说明的字段名称。

    Returns:
        输入为 None 时返回 None，否则返回带 UTC 时区的 datetime。
    """
    if value is None:
        return None
    return _parse_required_datetime(value, field_name=field_name)


def _normalize_reference_list(
    references: Sequence[str],
    *,
    field_name: str,
) -> list[str]:
    """复制并校验节点执行记录中的受控引用列表。

    Args:
        references: 等待持久化的产物引用序列。
        field_name: 用于异常说明的字段名称。

    Returns:
        保持输入顺序且不含重复值的独立字符串列表。

    Raises:
        TypeError: 输入不是非字符串序列时抛出。
        ValueError: 引用为空或重复时抛出。
    """
    if isinstance(references, (str, bytes)) or not isinstance(
        references,
        Sequence,
    ):
        raise TypeError(f"{field_name} 必须是字符串序列")
    normalized: list[str] = []
    for reference in references:
        normalized_reference = _normalize_required_identifier(
            reference,
            field_name=field_name,
        )
        if normalized_reference in normalized:
            raise ValueError(f"{field_name} 不得包含重复引用")
        normalized.append(normalized_reference)
    return normalized


def build_error_recovery_record_id(run_id: str, error_id: str) -> str:
    """根据运行和错误 ID 生成跨运行隔离的恢复记录主键。

    Args:
        run_id: 错误所属治理运行 ID。
        error_id: 顶层 ErrorRecord 的稳定 ID。

    Returns:
        不暴露业务内容的 SHA-256 十六进制主键。
    """
    normalized_run_id = _normalize_required_identifier(
        run_id,
        field_name="run_id",
    )
    normalized_error_id = _normalize_required_identifier(
        error_id,
        field_name="error_id",
    )
    payload = f"{normalized_run_id}\x1f{normalized_error_id}".encode()
    return hashlib.sha256(payload).hexdigest()


class BaseRepository(Generic[ModelT]):
    """为具体应用表 Repository 提供受控的新增、按主键读取和列表查询能力。"""

    model_type: type[ModelT]
    # 当前 Repository 负责的 ORM 模型类型，由具体子类固定声明。

    def __init__(self, session: Session) -> None:
        """保存当前短生命周期事务独占使用的 Session。

        Args:
            session: 由 ``open_application_session()`` 创建的 SQLAlchemy Session。

        Raises:
            TypeError: ``session`` 不是 SQLAlchemy Session 时抛出。
        """
        if not isinstance(session, Session):
            raise TypeError("session 必须是 SQLAlchemy Session")
        self._session = session
        # 当前 Repository 独占使用且不得跨线程共享的 Session。

    def add(self, record: ModelT) -> ModelT:
        """新增一条 ORM 记录并立即 flush 以暴露约束错误。

        Repository 不调用 commit；事务由外层 ``open_application_session()``
        统一提交或回滚。

        Args:
            record: 与当前 Repository 模型类型一致的新 ORM 对象。

        Returns:
            已加入 Session 并完成 flush 的同一 ORM 对象。

        Raises:
            TypeError: ``record`` 类型与当前 Repository 不一致时抛出。
            sqlalchemy.exc.IntegrityError: 主键、外键或检查约束不满足时抛出。
        """
        if not isinstance(record, self.model_type):
            raise TypeError(
                f"record 必须是 {self.model_type.__name__}，实际为 {type(record).__name__}"
            )
        self._session.add(record)
        self._session.flush()
        return record

    def get(self, record_id: str) -> ModelT | None:
        """按照主键读取一条记录。

        Args:
            record_id: 等待查询的非空主键。

        Returns:
            找到时返回 ORM 对象，否则返回 None。
        """
        normalized_id = _normalize_required_identifier(
            record_id,
            field_name="record_id",
        )
        return self._session.get(self.model_type, normalized_id)

    def get_required(self, record_id: str) -> ModelT:
        """按照主键读取记录，并在记录不存在时明确失败。

        Args:
            record_id: 等待查询的非空主键。

        Returns:
            已找到的 ORM 对象。

        Raises:
            LookupError: 数据库不存在对应主键时抛出。
        """
        record = self.get(record_id)
        if record is None:
            raise LookupError(f"{self.model_type.__name__} 不存在记录：{record_id}")
        return record

    def _list(self, statement: Select[tuple[ModelT]], *, limit: int) -> list[ModelT]:
        """执行由具体 Repository 构造的受限 ORM 查询。

        Args:
            statement: 只选择当前 ORM 模型的 SQLAlchemy Select。
            limit: 允许返回的最大记录数。

        Returns:
            按查询声明顺序返回的 ORM 对象列表。
        """
        normalized_limit = _normalize_limit(limit)
        return list(self._session.scalars(statement.limit(normalized_limit)).all())


class GovernanceRunRepository(BaseRepository[GovernanceRunModel]):
    """读写 governance_runs 表中的治理运行生命周期摘要。"""

    model_type = GovernanceRunModel
    # 当前 Repository 固定管理治理运行 ORM 模型。

    def get_or_create_minimal(
        self,
        run_id: str,
        *,
        thread_id: str,
        current_stage: str,
        request_summary: dict[str, object] | None = None,
    ) -> GovernanceRunModel:
        """读取治理运行，或创建不含业务正文的最小运行摘要。

        Args:
            run_id: 当前治理运行 ID。
            thread_id: 用于应用数据库审计隔离的线程标识。
            current_stage: 当前持久化节点所在阶段。
            request_summary: 可选固定布尔值、计数或哈希摘要。

        Returns:
            已存在或本次新增并完成 flush 的治理运行 ORM 记录。
        """
        existing = self.get(run_id)
        if existing is not None:
            return existing
        return self.add(
            GovernanceRunModel(
                run_id=_normalize_required_identifier(
                    run_id,
                    field_name="run_id",
                ),
                thread_id=_normalize_required_identifier(
                    thread_id,
                    field_name="thread_id",
                ),
                status="running",
                current_stage=_normalize_required_identifier(
                    current_stage,
                    field_name="current_stage",
                ),
                request_summary=dict(request_summary or {}),
            )
        )

    def list_by_thread(
        self,
        thread_id: str,
        *,
        limit: int = 100,
    ) -> list[GovernanceRunModel]:
        """按 LangGraph thread_id 读取最近的治理运行。

        Args:
            thread_id: LangGraph Checkpointer 使用的线程 ID。
            limit: 允许返回的最大记录数。

        Returns:
            按创建时间倒序排列的治理运行列表。
        """
        normalized_thread_id = _normalize_required_identifier(
            thread_id,
            field_name="thread_id",
        )
        statement = (
            select(GovernanceRunModel)
            .where(GovernanceRunModel.thread_id == normalized_thread_id)
            .order_by(
                GovernanceRunModel.created_at.desc(),
                GovernanceRunModel.run_id.desc(),
            )
        )
        return self._list(statement, limit=limit)

    def update_status(
        self,
        run_id: str,
        *,
        status: str,
        current_stage: str,
        report_path: str | None = None,
        error_summary: str | None = None,
        finished_at: datetime | None = None,
    ) -> GovernanceRunModel:
        """更新一条已存在治理运行的生命周期字段。

        Args:
            run_id: 等待更新的治理运行 ID。
            status: 新运行状态，由数据库检查约束执行最终白名单校验。
            current_stage: 新的主流程阶段名称。
            report_path: 可选最终报告路径。
            error_summary: 可选脱敏错误摘要。
            finished_at: 可选运行结束时间。

        Returns:
            已更新并完成 flush 的治理运行 ORM 对象。

        Raises:
            LookupError: 治理运行不存在时抛出。
            TypeError: 状态或阶段不是字符串时抛出。
            ValueError: 状态或阶段为空时抛出。
        """
        run = self.get_required(run_id)
        run.status = _normalize_required_identifier(status, field_name="status")
        run.current_stage = _normalize_required_identifier(
            current_stage,
            field_name="current_stage",
        )
        if report_path is not None:
            run.report_path = report_path
        if error_summary is not None:
            run.error_summary = error_summary
        if finished_at is not None:
            run.finished_at = finished_at
        self._session.flush()
        return run


class MemoryItemRepository(BaseRepository[MemoryItemModel]):
    """读写 memory_items 表中的结构化治理 Memory。"""

    model_type = MemoryItemModel
    # 当前 Repository 固定管理治理 Memory ORM 模型。

    def list_by_namespace(
        self,
        namespace: str,
        *,
        scope: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[MemoryItemModel]:
        """读取一个命名空间中的最近 Memory。

        Args:
            namespace: 隔离不同业务空间的非空 Memory 命名空间。
            scope: 可选短期或长期 Memory 范围过滤条件。
            kind: 可选 Memory 类型过滤条件。
            limit: 允许返回的最大记录数。

        Returns:
            按创建时间倒序排列的 Memory 列表。
        """
        normalized_namespace = _normalize_required_identifier(
            namespace,
            field_name="namespace",
        )
        statement = select(MemoryItemModel).where(MemoryItemModel.namespace == normalized_namespace)
        if scope is not None:
            statement = statement.where(
                MemoryItemModel.scope == _normalize_required_identifier(scope, field_name="scope")
            )
        if kind is not None:
            statement = statement.where(
                MemoryItemModel.kind == _normalize_required_identifier(kind, field_name="kind")
            )
        statement = statement.order_by(
            MemoryItemModel.created_at.desc(),
            MemoryItemModel.id.desc(),
        )
        return self._list(statement, limit=limit)


class ContextSummaryRepository(BaseRepository[ContextSummaryModel]):
    """读写 context_summaries 表中的上下文压缩摘要。"""

    model_type = ContextSummaryModel
    # 当前 Repository 固定管理上下文摘要 ORM 模型。

    def find_by_run_and_index(
        self,
        run_id: str,
        compaction_index: int,
    ) -> ContextSummaryModel | None:
        """按运行和压缩序号读取唯一 Context Summary。

        Args:
            run_id: 当前治理运行 ID。
            compaction_index: 当前运行内从一开始递增的压缩序号。

        Returns:
            找到时返回 ORM 记录，否则返回 None。

        Raises:
            TypeError: 压缩序号不是整数时抛出。
            ValueError: 压缩序号不大于零时抛出。
        """
        if isinstance(compaction_index, bool) or not isinstance(
            compaction_index,
            int,
        ):
            raise TypeError("compaction_index 必须是整数")
        if compaction_index < 1:
            raise ValueError("compaction_index 必须大于零")
        statement = select(ContextSummaryModel).where(
            ContextSummaryModel.run_id
            == _normalize_required_identifier(run_id, field_name="run_id"),
            ContextSummaryModel.compaction_index == compaction_index,
        )
        return self._session.scalars(statement).one_or_none()

    def list_by_run(
        self,
        run_id: str,
        *,
        limit: int = 100,
    ) -> list[ContextSummaryModel]:
        """读取一次治理运行产生的上下文摘要。

        Args:
            run_id: 治理运行 ID。
            limit: 允许返回的最大记录数。

        Returns:
            按压缩序号正序排列的上下文摘要列表。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        statement = (
            select(ContextSummaryModel)
            .where(ContextSummaryModel.run_id == normalized_run_id)
            .order_by(
                ContextSummaryModel.compaction_index.asc(),
                ContextSummaryModel.id.asc(),
            )
        )
        return self._list(statement, limit=limit)


class ToolCallAuditRepository(BaseRepository[ToolCallAuditModel]):
    """读写 tool_call_audits 表中的脱敏工具调用审计。"""

    model_type = ToolCallAuditModel
    # 当前 Repository 固定管理工具审计 ORM 模型。

    def list_by_run(
        self,
        run_id: str,
        *,
        limit: int = 500,
    ) -> list[ToolCallAuditModel]:
        """读取一次治理运行产生的工具调用审计。

        Args:
            run_id: 治理运行 ID。
            limit: 允许返回的最大记录数。

        Returns:
            按创建时间正序排列的工具调用审计列表。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        statement = (
            select(ToolCallAuditModel)
            .where(ToolCallAuditModel.run_id == normalized_run_id)
            .order_by(
                ToolCallAuditModel.created_at.asc(),
                ToolCallAuditModel.id.asc(),
            )
        )
        return self._list(statement, limit=limit)


class HumanReviewRepository(BaseRepository[HumanReviewModel]):
    """读写 human_reviews 表中的用户主版本确认记录。"""

    model_type = HumanReviewModel
    # 当前 Repository 固定管理人工审核 ORM 模型。

    def list_by_run(
        self,
        run_id: str,
        *,
        limit: int = 500,
    ) -> list[HumanReviewModel]:
        """读取一次治理运行保存的人工审核记录。

        Args:
            run_id: 治理运行 ID。
            limit: 允许返回的最大记录数。

        Returns:
            按创建时间正序排列的人工审核列表。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        statement = (
            select(HumanReviewModel)
            .where(HumanReviewModel.run_id == normalized_run_id)
            .order_by(
                HumanReviewModel.created_at.asc(),
                HumanReviewModel.id.asc(),
            )
        )
        return self._list(statement, limit=limit)


class NodeExecutionRecordRepository(BaseRepository[NodeExecutionRecordModel]):
    """读写 node_execution_records 表中的节点幂等执行记录。"""

    model_type = NodeExecutionRecordModel
    # 当前 Repository 固定管理节点幂等执行 ORM 模型。

    def upsert_state(
        self,
        execution: NodeExecutionRecord,
    ) -> NodeExecutionRecordModel:
        """幂等新增或推进一个节点执行状态。

        不可变的运行、Task、节点和输入摘要一旦写入不得改变；重放旧 checkpoint
        时也不得用更小的 attempt_count 覆盖已经持久化的新进度。方法只 flush，
        不提交事务。

        Args:
            execution: 顶层或恢复子图产生的完整 NodeExecutionRecord。

        Returns:
            已新增或更新并完成 flush 的 ORM 记录。

        Raises:
            ValueError: 幂等事实改变、尝试次数倒退或状态数据非法时抛出。
            sqlalchemy.exc.IntegrityError: 关联治理运行不存在或约束不满足时抛出。
        """
        idempotency_key = _normalize_required_identifier(
            execution["id"],
            field_name="execution.id",
        )
        run_id = _normalize_required_identifier(
            execution["run_id"],
            field_name="execution.run_id",
        )
        attempt_count = _normalize_nonnegative_integer(
            execution["attempt_count"],
            field_name="execution.attempt_count",
        )
        result_refs = _normalize_reference_list(
            execution["result_refs"],
            field_name="execution.result_refs",
        )
        existing = self.get(idempotency_key)
        if existing is None:
            return self.add(
                NodeExecutionRecordModel(
                    idempotency_key=idempotency_key,
                    run_id=run_id,
                    task_execution_id=execution["task_execution_id"],
                    task_id=execution["task_id"],
                    stage=_normalize_required_identifier(
                        execution["stage"],
                        field_name="execution.stage",
                    ),
                    node_name=_normalize_required_identifier(
                        execution["node_name"],
                        field_name="execution.node_name",
                    ),
                    input_digest=_normalize_required_identifier(
                        execution["input_digest"],
                        field_name="execution.input_digest",
                    ),
                    status=execution["status"],
                    attempt_count=attempt_count,
                    state_update_ref=execution["state_update_ref"],
                    result_refs=result_refs,
                    result_digest=execution["result_digest"],
                    last_error_id=execution["last_error_id"],
                    started_at=_parse_required_datetime(
                        execution["started_at"],
                        field_name="execution.started_at",
                    ),
                    finished_at=_parse_optional_datetime(
                        execution["finished_at"],
                        field_name="execution.finished_at",
                    ),
                )
            )

        immutable_fields = {
            "run_id": run_id,
            "task_execution_id": execution["task_execution_id"],
            "task_id": execution["task_id"],
            "stage": execution["stage"],
            "node_name": execution["node_name"],
            "input_digest": execution["input_digest"],
        }
        for field_name, expected_value in immutable_fields.items():
            if getattr(existing, field_name) != expected_value:
                raise ValueError(f"节点幂等记录 {idempotency_key} 的 {field_name} 不得改变")
        if attempt_count < existing.attempt_count:
            raise ValueError("execution.attempt_count 不得小于已持久化次数")
        incoming_status = execution["status"]
        if attempt_count == existing.attempt_count:
            allowed_statuses = NODE_EXECUTION_STATUS_TRANSITIONS.get(
                existing.status,
                frozenset(),
            )
            if incoming_status not in allowed_statuses:
                raise ValueError(f"节点执行状态不允许从 {existing.status} 回退到 {incoming_status}")
        if existing.status in REUSABLE_NODE_EXECUTION_STATUSES:
            if attempt_count != existing.attempt_count:
                raise ValueError("已成功节点不得增加 attempt_count 后重新执行")
            persisted_result = (
                existing.state_update_ref,
                existing.result_refs,
                existing.result_digest,
            )
            incoming_result = (
                execution["state_update_ref"],
                result_refs,
                execution["result_digest"],
            )
            if incoming_result != persisted_result:
                raise ValueError("已成功节点的持久化结果不得改变")

        existing.status = incoming_status
        existing.attempt_count = attempt_count
        existing.state_update_ref = execution["state_update_ref"]
        existing.result_refs = result_refs
        existing.result_digest = execution["result_digest"]
        existing.last_error_id = execution["last_error_id"]
        existing.finished_at = _parse_optional_datetime(
            execution["finished_at"],
            field_name="execution.finished_at",
        )
        self._session.flush()
        return existing

    def find_reusable(
        self,
        idempotency_key: str,
        *,
        input_digest: str,
    ) -> NodeExecutionRecordModel | None:
        """读取输入摘要一致且已经成功完成的可复用节点结果。

        Args:
            idempotency_key: 等待查询的节点幂等键。
            input_digest: 当前节点调用根据安全输入事实计算的摘要。

        Returns:
            成功或已复用且输入摘要一致的记录，否则返回 None。
        """
        record = self.get(idempotency_key)
        normalized_digest = _normalize_required_identifier(
            input_digest,
            field_name="input_digest",
        )
        if record is None:
            return None
        if record.input_digest != normalized_digest:
            return None
        if record.status not in REUSABLE_NODE_EXECUTION_STATUSES:
            return None
        return record

    def list_by_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[NodeExecutionRecordModel]:
        """读取一次治理运行中的节点执行记录。

        Args:
            run_id: 治理运行 ID。
            status: 可选节点执行状态过滤条件。
            limit: 允许返回的最大记录数。

        Returns:
            按更新时间和幂等键稳定排序的节点执行记录。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        statement = select(NodeExecutionRecordModel).where(
            NodeExecutionRecordModel.run_id == normalized_run_id
        )
        if status is not None:
            statement = statement.where(
                NodeExecutionRecordModel.status
                == _normalize_required_identifier(status, field_name="status")
            )
        statement = statement.order_by(
            NodeExecutionRecordModel.updated_at.asc(),
            NodeExecutionRecordModel.idempotency_key.asc(),
        )
        return self._list(statement, limit=limit)


class ErrorRecoveryRecordRepository(BaseRepository[ErrorRecoveryRecordModel]):
    """读写 error_recovery_records 表中的错误恢复生命周期。"""

    model_type = ErrorRecoveryRecordModel
    # 当前 Repository 固定管理错误恢复 ORM 模型。

    def find_by_error_id(
        self,
        run_id: str,
        error_id: str,
    ) -> ErrorRecoveryRecordModel | None:
        """按治理运行和 ErrorRecord ID 查询唯一恢复记录。

        Args:
            run_id: 错误所属治理运行 ID。
            error_id: 顶层 ErrorRecord 的稳定 ID。

        Returns:
            找到时返回 ORM 记录，否则返回 None。
        """
        statement = select(ErrorRecoveryRecordModel).where(
            ErrorRecoveryRecordModel.run_id
            == _normalize_required_identifier(run_id, field_name="run_id"),
            ErrorRecoveryRecordModel.error_id
            == _normalize_required_identifier(error_id, field_name="error_id"),
        )
        return self._session.scalars(statement).one_or_none()

    def upsert_state(
        self,
        run_id: str,
        error: ErrorRecord,
        *,
        action: str = "none",
    ) -> ErrorRecoveryRecordModel:
        """幂等新增或推进一个错误恢复状态。

        同一运行和 error_id 始终映射到同一条记录。错误身份事实不可改变，且旧
        checkpoint 不得用更小的 retry_count 覆盖已持久化恢复进度。方法只 flush，
        事务提交与关闭由当前图节点外层 ``open_application_session()`` 负责。

        Args:
            run_id: 错误所属治理运行 ID。
            error: 具有完整 0.6.1 恢复字段的 ErrorRecord。
            action: 当前或最近一次固定恢复动作。

        Returns:
            已新增或更新并完成 flush 的 ORM 记录。

        Raises:
            ValueError: 动作未知、错误事实改变或重试次数倒退时抛出。
            sqlalchemy.exc.IntegrityError: 关联运行、节点执行或数据库约束不满足时抛出。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        error_id = _normalize_required_identifier(
            error["id"],
            field_name="error.id",
        )
        normalized_action = _normalize_required_identifier(
            action,
            field_name="action",
        )
        if normalized_action not in ERROR_RECOVERY_ACTIONS:
            raise ValueError(f"action 不是允许的恢复动作：{normalized_action}")
        retry_count = _normalize_nonnegative_integer(
            error["retry_count"],
            field_name="error.retry_count",
        )
        max_retries = _normalize_nonnegative_integer(
            error["max_retries"],
            field_name="error.max_retries",
        )
        if retry_count > max_retries:
            raise ValueError("error.retry_count 不得大于 error.max_retries")

        existing = self.find_by_error_id(normalized_run_id, error_id)
        if existing is None:
            return self.add(
                ErrorRecoveryRecordModel(
                    record_id=build_error_recovery_record_id(
                        normalized_run_id,
                        error_id,
                    ),
                    run_id=normalized_run_id,
                    error_id=error_id,
                    task_id=error["task_id"],
                    node_execution_id=error["node_execution_id"],
                    stage=_normalize_required_identifier(
                        error["stage"],
                        field_name="error.stage",
                    ),
                    node_name=_normalize_required_identifier(
                        error["node_name"],
                        field_name="error.node_name",
                    ),
                    category=error["category"],
                    exception_type=error["exception_type"],
                    message=_normalize_required_identifier(
                        error["message"],
                        field_name="error.message",
                    ),
                    related_file_id=error["related_file_id"],
                    retryable=error["retryable"],
                    retry_count=retry_count,
                    max_retries=max_retries,
                    action=normalized_action,
                    fallback=error["fallback"],
                    requires_human=error["requires_human"],
                    status=error["status"],
                    fatal=error["fatal"],
                    created_at=_parse_required_datetime(
                        error["created_at"],
                        field_name="error.created_at",
                    ),
                    recovered_at=_parse_optional_datetime(
                        error["recovered_at"],
                        field_name="error.recovered_at",
                    ),
                )
            )

        immutable_fields = {
            "task_id": error["task_id"],
            "node_execution_id": error["node_execution_id"],
            "stage": error["stage"],
            "node_name": error["node_name"],
            "category": error["category"],
            "message": error["message"],
            "related_file_id": error["related_file_id"],
        }
        for field_name, expected_value in immutable_fields.items():
            if getattr(existing, field_name) != expected_value:
                raise ValueError(f"错误恢复记录 {error_id} 的 {field_name} 不得改变")
        if retry_count < existing.retry_count:
            raise ValueError("error.retry_count 不得小于已持久化次数")
        if max_retries < existing.max_retries:
            raise ValueError("error.max_retries 不得小于已持久化上限")
        allowed_statuses = ERROR_RECOVERY_STATUS_TRANSITIONS.get(
            existing.status,
            frozenset(),
        )
        if error["status"] not in allowed_statuses:
            raise ValueError(f"错误恢复状态不允许从 {existing.status} 回退到 {error['status']}")

        existing.exception_type = error["exception_type"]
        existing.retryable = error["retryable"]
        existing.retry_count = retry_count
        existing.max_retries = max_retries
        existing.action = normalized_action
        existing.fallback = error["fallback"]
        existing.requires_human = error["requires_human"]
        existing.status = error["status"]
        existing.fatal = error["fatal"]
        existing.recovered_at = _parse_optional_datetime(
            error["recovered_at"],
            field_name="error.recovered_at",
        )
        self._session.flush()
        return existing

    def list_by_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        limit: int = 500,
    ) -> list[ErrorRecoveryRecordModel]:
        """读取一次治理运行中的错误恢复记录。

        Args:
            run_id: 治理运行 ID。
            status: 可选错误恢复状态过滤条件。
            limit: 允许返回的最大记录数。

        Returns:
            按更新时间和错误 ID 稳定排序的恢复记录。
        """
        normalized_run_id = _normalize_required_identifier(
            run_id,
            field_name="run_id",
        )
        statement = select(ErrorRecoveryRecordModel).where(
            ErrorRecoveryRecordModel.run_id == normalized_run_id
        )
        if status is not None:
            statement = statement.where(
                ErrorRecoveryRecordModel.status
                == _normalize_required_identifier(status, field_name="status")
            )
        statement = statement.order_by(
            ErrorRecoveryRecordModel.updated_at.asc(),
            ErrorRecoveryRecordModel.error_id.asc(),
        )
        return self._list(statement, limit=limit)


class RepositoryBundle:
    """聚合一个短事务 Session 上的七个 Repository，供图节点依赖注入。"""

    def __init__(self, session: Session) -> None:
        """为同一事务创建全部应用数据库 Repository。

        Args:
            session: 当前短生命周期事务独占的 SQLAlchemy Session。
        """
        self.governance_runs = GovernanceRunRepository(session)
        # 治理运行生命周期 Repository。

        self.memory_items = MemoryItemRepository(session)
        # 结构化治理 Memory Repository。

        self.context_summaries = ContextSummaryRepository(session)
        # Context Compact 摘要 Repository。

        self.tool_call_audits = ToolCallAuditRepository(session)
        # 脱敏工具调用审计 Repository。

        self.human_reviews = HumanReviewRepository(session)
        # 用户人工确认 Repository。

        self.node_execution_records = NodeExecutionRecordRepository(session)
        # 节点幂等执行 Repository。

        self.error_recovery_records = ErrorRecoveryRecordRepository(session)
        # 错误恢复生命周期 Repository。


def create_repository_bundle(session: Session) -> RepositoryBundle:
    """创建共享同一事务的应用数据库 Repository 集合。

    Args:
        session: 当前短生命周期事务独占的 SQLAlchemy Session。

    Returns:
        包含七个表 Repository 的聚合对象。
    """
    return RepositoryBundle(session)
