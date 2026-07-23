from __future__ import annotations

from pathlib import Path

from app.graphs.context_compact import (
    build_context_compact_graph,
    context_compact_graph,
)
from app.state.factories import create_context_compact_state
from app.state.models import ContextCompactGraphState, RequestState
from app.storage.artifacts import load_json_artifact
from app.storage.database import (
    create_application_engine,
    create_session_factory,
    open_application_session,
)
from app.storage.orm_models import Base
from app.storage.repositories import create_repository_bundle

"""本模块验证独立 Context Compact 子图的条件路由、产物隔离和数据库摘要持久化。"""


def create_context_graph_state(
    tmp_path: Path,
    *,
    enabled: bool,
    persist_summaries: bool,
    threshold: int,
) -> ContextCompactGraphState:
    """创建独立 Context Compact 子图集成测试状态。

    Args:
        tmp_path: 当前 pytest 临时目录。
        enabled: 是否启用 Context Compact。
        persist_summaries: 是否写入应用数据库。
        threshold: 触发压缩的 Token 阈值。

    Returns:
        可直接提交给独立子图的完整状态。
    """
    input_root = tmp_path / "input"
    input_root.mkdir(exist_ok=True)
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
    context_compact = create_context_compact_state(
        request,
        {
            "enabled": enabled,
            "trigger_token_threshold": threshold,
            "retained_preview_characters": 0,
            "persist_summaries": persist_summaries,
            "database_path": str(database_path),
        },
    )
    return ContextCompactGraphState(
        run={
            "run_id": "run-context-graph",
            "status": "running",
            "current_stage": "evidence",
            "started_at": "2026-07-23T08:00:00+00:00",
            "finished_at": None,
        },
        workspace={
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
        prompt={
            "enabled": True,
            "version": "test-v1",
            "source_path": str(tmp_path / "prompt.md"),
            "content": "PROMPT-CONTENT-MUST-BE-DISCARDED" * 30,
            "content_sha256": "a" * 64,
            "dynamic_rules": ["测试规则"],
            "status": "loaded",
        },
        documents=[
            {
                "id": "document-1",
                "file_id": "file-1",
                "parser_name": "docx-v1",
                "content_ref": str(tmp_path / "artifacts" / "normalized" / "document-1.json"),
                "content_preview": "DOCUMENT-PREVIEW-MOVED-TO-ARTIFACT" * 50,
                "normalized_digest": "b" * 64,
                "structure_summary": {"paragraph_count": 20},
                "key_fields": {"amount": "1200"},
                "warnings": [],
            }
        ],
        context_compact=context_compact,
        stage="after_evidence",
        plan=None,
        compaction_payload=None,
        summary_draft=None,
        errors=[],
    )


def test_context_compact_graph_uses_conditional_router() -> None:
    """独立子图必须通过 conditional_edge 选择压缩或跳过分支。"""
    graph = build_context_compact_graph().get_graph()
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert ("__start__", "estimate_context_tokens") in edges
    assert ("estimate_context_tokens", "compact_context") in edges
    assert (
        "estimate_context_tokens",
        "mark_context_compaction_skipped",
    ) in edges
    assert (
        "compact_context",
        "persist_context_compaction_artifact",
    ) in edges
    assert (
        "persist_context_compaction_artifact",
        "persist_context_summary",
    ) in edges


def test_context_compact_graph_persists_artifact_and_bounded_summary(
    tmp_path: Path,
) -> None:
    """触发压缩时应保存文档详情产物，并在数据库只写有界摘要。"""
    state = create_context_graph_state(
        tmp_path,
        enabled=True,
        persist_summaries=True,
        threshold=1,
    )
    database_path = Path(state["context_compact"]["database_path"] or "")
    engine = create_application_engine(
        database_path,
        input_root=state["workspace"]["input_root"],
    )
    Base.metadata.create_all(engine)
    engine.dispose()

    result = context_compact_graph.invoke(state)

    assert result["prompt"]["content"] == ""
    assert result["documents"][0]["content_preview"] == ""
    assert result["documents"][0]["content_ref"] == state["documents"][0]["content_ref"]
    assert result["context_compact"]["status"] == "ready"
    assert len(result["context_compact"]["summaries"]) == 1
    summary = result["context_compact"]["summaries"][0]
    assert summary["estimated_tokens"] < result["plan"]["estimated_tokens_before"]
    assert len(summary["artifact_refs"]) == 1
    artifact = load_json_artifact(summary["artifact_refs"][0])
    removed = artifact["payload"]["removed_documents"][0]
    assert removed["content_preview"] == state["documents"][0]["content_preview"]
    assert artifact["payload"]["prompt_content_discarded"] is True

    verification_engine = create_application_engine(
        database_path,
        input_root=state["workspace"]["input_root"],
    )
    session_factory = create_session_factory(verification_engine)
    with open_application_session(session_factory) as session:
        records = create_repository_bundle(session).context_summaries.list_by_run(
            "run-context-graph"
        )
        assert len(records) == 1
        assert records[0].summary == summary["summary"]
        assert records[0].estimated_tokens == summary["estimated_tokens"]
    verification_engine.dispose()

    database_bytes = database_path.read_bytes()
    assert b"DOCUMENT-PREVIEW-MOVED-TO-ARTIFACT" not in database_bytes
    assert b"PROMPT-CONTENT-MUST-BE-DISCARDED" not in database_bytes


def test_context_compact_graph_skips_below_threshold(tmp_path: Path) -> None:
    """未达到阈值时不得写产物、摘要或改变 Prompt 和文档。"""
    state = create_context_graph_state(
        tmp_path,
        enabled=True,
        persist_summaries=False,
        threshold=10_000_000,
    )

    result = context_compact_graph.invoke(state)

    assert result["prompt"] == state["prompt"]
    assert result["documents"] == state["documents"]
    assert result["context_compact"]["summaries"] == []
    assert result["context_compact"]["status"] == "ready"
