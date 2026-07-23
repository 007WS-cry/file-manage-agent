from __future__ import annotations

from pathlib import Path
from typing import cast

from app.nodes.memory import persist_long_term_memory, recall_long_term_memory
from app.services.memory_policy import capture_human_choice_memory
from app.state.factories import create_memory_state
from app.state.models import FileGovernanceState, RequestState
from app.storage.database import create_application_engine
from app.storage.orm_models import Base

"""本模块验证释放数据库连接并创建新运行后，长期 Memory 仍可按命名空间召回。"""


def test_long_term_memory_survives_repository_restart(tmp_path: Path) -> None:
    """第一次运行持久化的人工选择应被独立的新运行召回。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    database_path = tmp_path / "database" / "application.sqlite3"
    request = RequestState(
        root_directory=str(input_root),
        recursive=True,
        allowed_extensions=[".docx"],
        max_files=20,
        grouping_similarity_threshold=0.72,
        auto_select_threshold=0.82,
        pdf_match_threshold=0.82,
        delivery_log_path=None,
        use_llm_summary=False,
    )
    engine = create_application_engine(database_path, input_root=input_root)
    Base.metadata.create_all(engine)
    engine.dispose()

    first_memory = create_memory_state(
        request,
        {
            "enabled": True,
            "database_path": str(database_path),
            "recall_limit": 10,
        },
    )
    first_memory = capture_human_choice_memory(
        first_memory,
        source_run_id="run-first",
        version_groups=[
            {
                "id": "group-stable",
                "label": "合同",
                "file_ids": ["file-v1", "file-v2"],
                "grouping_signals": [],
                "confidence": 0.9,
            }
        ],
        selections={"group-stable": "file-v2"},
    )
    first_state = cast(
        FileGovernanceState,
        {
            "run": {"run_id": "run-first"},
            "workspace": {"input_root": str(input_root)},
            "memory": first_memory,
            "errors": [],
        },
    )

    persisted = persist_long_term_memory(first_state)

    assert persisted["memory"]["status"] == "ready"
    assert len(persisted["memory"]["persisted_item_ids"]) == 1
    assert persisted["memory"]["pending_long_term_items"] == []

    second_memory = create_memory_state(
        request,
        {
            "enabled": True,
            "database_path": str(database_path),
            "recall_limit": 10,
        },
    )
    second_state = cast(
        FileGovernanceState,
        {
            "run": {"run_id": "run-second"},
            "workspace": {"input_root": str(input_root)},
            "memory": second_memory,
            "errors": [],
        },
    )

    recalled = recall_long_term_memory(second_state)

    assert recalled["memory"]["status"] == "ready"
    assert len(recalled["memory"]["recalled_items"]) == 1
    item = recalled["memory"]["recalled_items"][0]
    assert item["kind"] == "confirmed_version_choice"
    assert item["structured_data"] == {
        "group_id": "group-stable",
        "selected_file_id": "file-v2",
    }
