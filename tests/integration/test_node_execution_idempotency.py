from __future__ import annotations

from pathlib import Path

import pytest

from app.nodes import subgraphs_nodes
from app.nodes.subgraphs_nodes import run_inventory_subgraph
from app.state.factories import create_initial_state
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import Base
from app.storage.repositories import create_repository_bundle

"""本文件验证节点执行幂等键同时约束图状态复用、受控产物和数据库唯一记录。"""


def create_idempotency_state(tmp_path: Path) -> dict:
    """创建应用数据库就绪且具有稳定运行 ID 的 Inventory 状态。

    Args:
        tmp_path: pytest 为当前测试提供的隔离目录。

    Returns:
        七张表已创建、可直接执行可恢复包装节点的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir()
    database_path = tmp_path / "database" / "application.sqlite3"
    engine = create_application_engine(database_path, input_root=input_root)
    Base.metadata.create_all(engine)
    engine.dispose()
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 20,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "pdf_match_threshold": 0.82,
            "delivery_log_path": None,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
        application_database_config={
            "enabled": True,
            "database_path": str(database_path),
        },
        thread_id="node-execution-idempotency",
    )
    state["run"].update(
        {
            "run_id": "node-execution-idempotency-run",
            "thread_id": "node-execution-idempotency",
            "status": "running",
            "current_stage": "inventory",
            "started_at": "2026-07-24T08:00:00+00:00",
        }
    )
    state["application_database"]["status"] = "ready"
    return state


def test_node_execution_replay_reuses_one_persisted_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同输入重放不得再次执行子图，数据库也只能保留同一幂等键的一条记录。"""
    state = create_idempotency_state(tmp_path)
    original_invoke = subgraphs_nodes.inventory_graph.invoke
    invoke_count = 0

    def count_inventory_invocation(*args, **kwargs):
        """记录 Inventory 子图是否发生真实调用。"""
        nonlocal invoke_count
        invoke_count += 1
        return original_invoke(*args, **kwargs)

    monkeypatch.setattr(
        subgraphs_nodes.inventory_graph,
        "invoke",
        count_inventory_invocation,
    )

    first_update = run_inventory_subgraph(state)
    replay_state = {
        **state,
        "node_executions": list(first_update["node_executions"]),
    }
    second_update = run_inventory_subgraph(replay_state)
    first_execution = first_update["node_executions"][0]
    reused_execution = second_update["node_executions"][0]

    database_path = Path(state["application_database"]["database_path"])
    engine = create_application_engine(
        database_path,
        input_root=state["workspace"]["input_root"],
    )
    session_factory = create_session_factory(engine)
    with open_application_session(session_factory) as session:
        records = create_repository_bundle(
            session
        ).node_execution_records.list_by_run(state["run"]["run_id"])
    engine.dispose()

    assert invoke_count == 1
    assert reused_execution["id"] == first_execution["id"]
    assert reused_execution["input_digest"] == first_execution["input_digest"]
    assert reused_execution["result_digest"] == first_execution["result_digest"]
    assert reused_execution["attempt_count"] == 1
    assert reused_execution["status"] == "reused"
    assert second_update["files"] == first_update["files"]
    assert second_update["documents"] == first_update["documents"]
    assert len(records) == 1
    assert records[0].idempotency_key == first_execution["id"]
    assert records[0].status == "reused"
    assert records[0].attempt_count == 1
