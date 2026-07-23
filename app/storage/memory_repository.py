from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

from app.services.memory_policy import validate_memory_item
from app.state.models import MemoryItemState
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import GovernanceRunModel, MemoryItemModel
from app.storage.repositories import create_repository_bundle

"""本模块以安全 Memory 协议封装应用数据库事务，实现跨运行召回和幂等持久化。"""


class MemoryRepository:
    """管理独立应用数据库中的长期 Memory，不创建或迁移数据表。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        input_root: str | Path | None = None,
        checkpoint_path: str | Path | None = None,
    ) -> None:
        """创建专用于短生命周期事务的应用数据库 Engine。

        Args:
            database_path: 已由配置指定的应用数据库 SQLite 文件路径。
            input_root: 可选只读业务输入目录，用于数据库路径隔离校验。
            checkpoint_path: 可选 LangGraph Checkpointer 数据库路径。
        """
        self._engine = create_application_engine(
            database_path,
            input_root=input_root,
            checkpoint_path=checkpoint_path,
        )
        # 当前 Repository 持有且可在完成后显式释放的 SQLAlchemy Engine。

        self._session_factory = create_session_factory(self._engine)
        # 为每次召回或持久化创建独立 Session 的同步工厂。

    @property
    def engine(self) -> Engine:
        """返回底层 Engine，供测试释放连接或执行只读诊断。

        Returns:
            当前 Memory Repository 使用的 SQLAlchemy Engine。
        """
        return self._engine

    def close(self) -> None:
        """释放当前 Repository 的数据库连接池资源。"""
        self._engine.dispose()

    def recall(
        self,
        namespace: str,
        *,
        limit: int = 50,
    ) -> list[MemoryItemState]:
        """按命名空间召回最近长期 Memory，并再次执行内容安全校验。

        Args:
            namespace: 当前工作空间的哈希 Memory 命名空间。
            limit: 最多召回的长期条目数量。

        Returns:
            按创建时间倒序排列的安全长期 Memory 条目。
        """
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            records = repositories.memory_items.list_by_namespace(
                namespace,
                scope="long_term",
                limit=limit,
            )
            return [self._model_to_state(record) for record in records]

    def persist(
        self,
        *,
        run_id: str,
        namespace: str,
        items: Sequence[MemoryItemState],
    ) -> list[str]:
        """在一个事务中幂等写入经过复验的长期 Memory 条目。

        若治理运行摘要尚不存在，本方法只创建包含哈希命名空间的最小运行记录，
        不保存输入目录、文档正文、模型 Prompt、密钥或自由文本审核说明。

        Args:
            run_id: 产生这些 Memory 条目的治理运行 ID。
            namespace: 当前工作空间的哈希命名空间。
            items: 等待写入数据库的长期 Memory 条目。

        Returns:
            已存在或本次成功写入的 Memory 条目 ID。

        Raises:
            ValueError: 条目不是长期 Memory，或其运行、命名空间不一致时抛出。
        """
        validated_items = [validate_memory_item(item) for item in items]
        for item in validated_items:
            if item["scope"] != "long_term":
                raise ValueError("应用数据库只允许持久化 long_term Memory")
            if item["source_run_id"] != run_id:
                raise ValueError("Memory source_run_id 与当前运行不一致")
            if item["namespace"] != namespace:
                raise ValueError("Memory namespace 与当前工作空间不一致")

        persisted_ids: list[str] = []
        with open_application_session(self._session_factory) as session:
            repositories = create_repository_bundle(session)
            if repositories.governance_runs.get(run_id) is None:
                repositories.governance_runs.add(
                    GovernanceRunModel(
                        run_id=run_id,
                        thread_id=f"memory:{namespace.removeprefix('workspace:')}",
                        status="running",
                        current_stage="memory_persist",
                        request_summary={"memory_namespace": namespace},
                    )
                )
            for item in validated_items:
                if repositories.memory_items.get(item["id"]) is None:
                    repositories.memory_items.add(
                        MemoryItemModel(
                            id=item["id"],
                            namespace=item["namespace"],
                            scope=item["scope"],
                            kind=item["kind"],
                            summary=item["summary"],
                            structured_data=dict(item["structured_data"]),
                            artifact_refs=list(item["artifact_refs"]),
                            source_run_id=item["source_run_id"],
                            confirmed_by_human=item["confirmed_by_human"],
                            confidence=item["confidence"],
                        )
                    )
                persisted_ids.append(item["id"])
        return persisted_ids

    @staticmethod
    def _model_to_state(record: MemoryItemModel) -> MemoryItemState:
        """把 ORM 记录转换为经过安全策略复验的状态条目。

        Args:
            record: 从应用数据库读取的 Memory ORM 记录。

        Returns:
            可安全进入 LangGraph 状态的 Memory 条目。
        """
        raw_item: dict[str, Any] = {
            "id": record.id,
            "namespace": record.namespace,
            "scope": record.scope,
            "kind": record.kind,
            "summary": record.summary,
            "structured_data": dict(record.structured_data),
            "artifact_refs": list(record.artifact_refs),
            "source_run_id": record.source_run_id,
            "confirmed_by_human": record.confirmed_by_human,
            "confidence": record.confidence,
            "created_at": record.created_at.isoformat(),
        }
        return validate_memory_item(raw_item)
