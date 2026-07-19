from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.entrypoints.cli import resolve_request_payload
from app.nodes.lifecycle import validate_request
from app.state.factories import create_initial_state
from app.tools.delivery_log import load_local_delivery_log

"""本文件单元测试本地发送记录工具的只读边界和固定 JSON 协议。"""


def make_delivery_payload() -> dict:
    """构造本地发送记录工具测试使用的最小合法协议对象。

    Returns:
        包含一条发送记录的 JSON 可序列化字典。
    """
    return {
        "schema_version": "1.0",
        "deliveries": [
            {
                "id": "delivery-001",
                "attachment_name": "合同_最终版.docx",
                "attachment_sha256": "A" * 64,
                "normalized_digest": None,
                "sent_at": "2026-07-18T09:30:00+08:00",
                "recipient_label": "客户甲",
                "customer_confirmed": True,
                "evidence_ref": "local-log://delivery-001",
            }
        ],
    }


def write_payload(path: Path, payload: object) -> bytes:
    """把测试协议对象写为 UTF-8 JSON 并返回原始字节快照。

    Args:
        path: 测试 JSON 文件路径。
        payload: 等待序列化的 JSON 兼容对象。

    Returns:
        写入完成后的文件字节。
    """
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path.read_bytes()


def test_load_local_delivery_log_validates_and_never_modifies_source(
    tmp_path: Path,
) -> None:
    """合法日志应被规范化加载，工具不得改变源文件字节。"""
    log_path = tmp_path / "delivery_log.json"
    original_bytes = write_payload(log_path, make_delivery_payload())

    entries = load_local_delivery_log(log_path)

    assert entries[0]["id"] == "delivery-001"
    assert entries[0]["attachment_sha256"] == "a" * 64
    assert entries[0]["customer_confirmed"] is True
    assert log_path.read_bytes() == original_bytes


def test_load_local_delivery_log_rejects_unknown_schema_version(tmp_path: Path) -> None:
    """未知协议版本必须失败，避免把新旧字段静默解释为相同含义。"""
    payload = make_delivery_payload()
    payload["schema_version"] = "2.0"
    log_path = tmp_path / "delivery_log.json"
    write_payload(log_path, payload)

    with pytest.raises(ValueError, match="schema_version"):
        load_local_delivery_log(log_path)


def test_load_local_delivery_log_rejects_duplicate_ids(tmp_path: Path) -> None:
    """重复记录 ID 必须失败，防止 reducer 将两条证据错误覆盖。"""
    payload = make_delivery_payload()
    payload["deliveries"].append(dict(payload["deliveries"][0]))
    log_path = tmp_path / "delivery_log.json"
    write_payload(log_path, payload)

    with pytest.raises(ValueError, match="重复 id"):
        load_local_delivery_log(log_path)


def test_load_local_delivery_log_requires_timezone(tmp_path: Path) -> None:
    """发送时间缺少时区时必须失败，避免跨时区比较产生错误顺序。"""
    payload = make_delivery_payload()
    payload["deliveries"][0]["sent_at"] = "2026-07-18T09:30:00"
    log_path = tmp_path / "delivery_log.json"
    write_payload(log_path, payload)

    with pytest.raises(ValueError, match="必须包含时区"):
        load_local_delivery_log(log_path)


def test_load_local_delivery_log_enforces_size_limit(tmp_path: Path) -> None:
    """超过调用方上限的日志必须在解析前被拒绝。"""
    log_path = tmp_path / "delivery_log.json"
    write_payload(log_path, make_delivery_payload())

    with pytest.raises(ValueError, match="读取上限"):
        load_local_delivery_log(log_path, max_bytes=8)


def test_request_payload_resolves_delivery_log_relative_to_request(
    tmp_path: Path,
) -> None:
    """CLI 应把可选发送日志路径解析为相对请求文件目录的绝对路径。"""
    payload = {
        "request": {
            "root_directory": "input",
            "delivery_log_path": "evidence/delivery_log.json",
        },
        "workspace": {
            "input_root": "input",
            "artifact_root": "artifacts",
            "report_root": "reports",
        },
    }

    request, _, _ = resolve_request_payload(payload, base_directory=tmp_path)

    assert request["delivery_log_path"] == str(
        (tmp_path / "evidence" / "delivery_log.json").resolve()
    )


def test_request_validation_normalizes_evidence_defaults(tmp_path: Path) -> None:
    """旧请求缺少证据字段时应获得安全默认值并继续通过校验。"""
    input_root = tmp_path / "input"
    input_root.mkdir()
    state = create_initial_state(
        {
            "root_directory": str(input_root),
            "recursive": True,
            "allowed_extensions": [".docx"],
            "max_files": 10,
            "grouping_similarity_threshold": 0.72,
            "auto_select_threshold": 0.82,
            "use_llm_summary": False,
        },
        {
            "input_root": str(input_root),
            "input_readonly": True,
            "artifact_root": str(tmp_path / "artifacts"),
            "report_root": str(tmp_path / "reports"),
        },
    )

    update = validate_request(state)

    assert update.get("errors") is None
    assert update["request"]["pdf_match_threshold"] == 0.82
    assert update["request"]["delivery_log_path"] is None
    assert state["pdf_exports"] == []
    assert state["deliveries"] == []
