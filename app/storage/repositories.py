from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.storage.orm_models import (
    ContextSummaryModel,
    GovernanceRunModel,
    HumanReviewModel,
    MemoryItemModel,
    ToolCallAuditModel,
)

"""本模块通过 Repository 隔离五张应用表的数据访问，不负责创建 Session 或提交事务。"""


# Repository 泛型使用的 SQLAlchemy ORM 模型类型。
ModelT = TypeVar("ModelT")


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
                f"record 必须是 {self.model_type.__name__}，"
                f"实际为 {type(record).__name__}"
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
            raise LookupError(
                f"{self.model_type.__name__} 不存在记录：{record_id}"
            )
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
        return list(
            self._session.scalars(statement.limit(normalized_limit)).all()
        )


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
        statement = select(MemoryItemModel).where(
            MemoryItemModel.namespace == normalized_namespace
        )
        if scope is not None:
            statement = statement.where(
                MemoryItemModel.scope
                == _normalize_required_identifier(scope, field_name="scope")
            )
        if kind is not None:
            statement = statement.where(
                MemoryItemModel.kind
                == _normalize_required_identifier(kind, field_name="kind")
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


class RepositoryBundle:
    """聚合一个 Session 上的五个 Repository，供未来图节点依赖注入。"""

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


def create_repository_bundle(session: Session) -> RepositoryBundle:
    """创建共享同一事务的应用数据库 Repository 集合。

    Args:
        session: 当前短生命周期事务独占的 SQLAlchemy Session。

    Returns:
        包含五个表 Repository 的聚合对象。
    """
    return RepositoryBundle(session)
