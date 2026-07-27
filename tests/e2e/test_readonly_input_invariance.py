from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import app.graphs.evidence as evidence_graph_module
from app.nodes.evidence import (
    match_pdf_to_source_version as original_match_pdf_to_source_version,
)
from app.runtime.dispatcher import execute_foreground_submission
from scripts.backup_restore_demo import resolve_work_directory, run_sqlite_demo
from scripts.generate_demo_data import build_input_manifest, generate_demo_data
from scripts.run_e2e_demo import upgrade_database
from tests.integration.test_email_mcp_evidence import make_evidence_state

"""本文件端到端验收数十文件的扫描上限、并发上限、输入不变性及 SQLite 恢复副本。"""


# 只读验收实际生成的业务文件数量，覆盖“数十文件”规模。
SOURCE_FILE_COUNT = 32

# 单次治理允许读取的文件上限，与验收数据规模一致以验证显式有界处理。
GOVERNED_FILE_LIMIT = SOURCE_FILE_COUNT

# PDF Evidence fan-out 同时允许执行的最大 Worker 数量。
PDF_MATCH_CONCURRENCY_LIMIT = 4


def test_many_files_remain_byte_identical_and_work_is_bounded(tmp_path: Path) -> None:
    """按 32 文件显式上限治理后，数量、路径和 SHA-256 应完全不变。"""
    demo_root = tmp_path / "demo"
    generate_demo_data(demo_root, file_count=SOURCE_FILE_COUNT)
    baseline = json.loads(
        (demo_root / "manifest.before.json").read_text(encoding="utf-8")
    )
    envelope = json.loads((demo_root / "request.json").read_text(encoding="utf-8"))
    envelope["request"]["max_files"] = GOVERNED_FILE_LIMIT
    envelope["request"]["auto_select_threshold"] = 0.0
    database_path = demo_root / "database" / "application.sqlite3"
    upgrade_database(database_path)

    result = execute_foreground_submission(
        envelope,
        application_database_path=database_path,
        checkpoint_path=demo_root / "checkpoints" / "bounded.sqlite3",
    )

    after = build_input_manifest(demo_root / "input")
    assert result["run"]["status"] in {"completed", "partial"}
    assert len(result["files"]) == GOVERNED_FILE_LIMIT
    assert after == baseline
    assert not any(
        path.suffix.lower() in {".json", ".md", ".sqlite3"}
        for path in (demo_root / "input").rglob("*")
        if path.is_file()
    )

    backup_work_directory = resolve_work_directory(demo_root / "backup-restore-output")
    backup_result = run_sqlite_demo(
        action="roundtrip",
        work_directory=backup_work_directory,
        database_path=database_path,
        backup_path=None,
        restore_target=None,
        confirm_restore=False,
    )
    assert backup_result["verified"] is True
    assert backup_result["restored_table_counts"] == backup_result["table_counts"]
    assert "alembic_version" in backup_result["table_counts"]


def test_pdf_match_fanout_honors_max_concurrency(monkeypatch) -> None:
    """32 个 PDF 匹配任务应并行执行，但峰值不能超过显式上限。"""
    state = make_evidence_state("http://127.0.0.1:1/mcp")
    state["email_mcp"]["enabled"] = False
    source_file = dict(state["files"][0])
    source_document = dict(state["documents"][0])
    pdf_file_ids: list[str] = []
    for index in range(32):
        pdf_file_id = f"pdf-file-{index:02d}"
        pdf_file_ids.append(pdf_file_id)
        state["files"].append(
            {
                **source_file,
                "id": pdf_file_id,
                "absolute_path": f"/readonly/export-{index:02d}.pdf",
                "file_name": f"export-{index:02d}.pdf",
                "extension": ".pdf",
                "sha256": f"{index + 1:064x}",
            }
        )
        state["documents"].append(
            {
                **source_document,
                "id": f"pdf-document-{index:02d}",
                "file_id": pdf_file_id,
                "content_ref": f"/artifacts/export-{index:02d}.json",
            }
        )
    state["version_groups"][0]["file_ids"].extend(pdf_file_ids)
    active_count = 0
    peak_count = 0
    counter_lock = threading.Lock()

    def observed_match_pdf_to_source_version(worker_state):
        """记录测试节点的并发峰值并调用真实 PDF 匹配节点。"""
        nonlocal active_count, peak_count
        with counter_lock:
            active_count += 1
            peak_count = max(peak_count, active_count)
        try:
            time.sleep(0.02)
            return original_match_pdf_to_source_version(worker_state)
        finally:
            with counter_lock:
                active_count -= 1

    monkeypatch.setattr(
        evidence_graph_module,
        "match_pdf_to_source_version",
        observed_match_pdf_to_source_version,
    )
    graph = evidence_graph_module.build_evidence_graph()

    result = graph.invoke(
        state,
        config={"max_concurrency": PDF_MATCH_CONCURRENCY_LIMIT},
    )

    assert len(result["pdf_exports"]) == len(pdf_file_ids)
    assert 1 < peak_count <= PDF_MATCH_CONCURRENCY_LIMIT
