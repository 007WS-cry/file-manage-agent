from __future__ import annotations

import io
import json

from app.observability.context import bind_log_context
from app.observability.logging import (
    configure_structured_logging,
    log_runtime_event,
)

"""本文件集成测试 API、Worker、Scheduler 与模拟 MCP 共用同一 JSON 日志协议。"""


# 0.8.0 Compose 中需要输出统一结构化日志的四个独立服务名称。
RUNTIME_SERVICES = ("api", "worker", "scheduler", "mock_email_mcp")


def test_four_runtime_services_emit_same_json_log_shape() -> None:
    """四类服务入口事件应具有相同公共字段和可关联运行上下文。"""
    required_keys = {
        "timestamp",
        "level",
        "service",
        "logger",
        "event",
        "message",
    }
    for service in RUNTIME_SERVICES:
        output = io.StringIO()
        logger = configure_structured_logging(service, stream=output)
        with bind_log_context(run_id="run-001", job_id="job-001"):
            log_runtime_event(
                logger,
                "service_test",
                "统一结构化日志测试。",
                fields={"status": "ok"},
            )

        payload = json.loads(output.getvalue())
        assert required_keys <= payload.keys()
        assert payload["service"] == service
        assert payload["event"] == "service_test"
        assert payload["run_id"] == "run-001"
        assert payload["job_id"] == "job-001"
        assert payload["status"] == "ok"
