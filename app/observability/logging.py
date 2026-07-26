from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TextIO

from app.observability.context import LOG_CONTEXT_KEYS, get_log_context

"""本模块配置统一单行 JSON 日志，并限制四个服务输出的结构化公共字段。"""


# 所有服务允许使用的标准日志级别。
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# 业务调用可以通过 LogRecord extra 显式附加的脱敏字段。
LOG_RECORD_EXTRA_KEYS = LOG_CONTEXT_KEYS | frozenset(
    {
        "event",
        "processed",
        "synchronized_schedules",
        "status",
        "exception_type",
    }
)

# 未显式传入 logger 时记录运行时事件使用的固定 Logger 名称。
RUNTIME_LOGGER_NAME = "file_governance.runtime"


class JsonLogFormatter(logging.Formatter):
    """把标准 LogRecord 格式化为字段稳定、UTF-8 友好的单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        """序列化一条日志，并避免输出堆栈、请求正文或业务文件内容。

        Args:
            record: Python logging 创建的日志记录。

        Returns:
            包含 UTC 时间、级别、服务、Logger、事件和脱敏上下文的 JSON 行。
        """
        context = get_log_context()
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", context.get("service", "unknown")),
            "logger": record.name,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        for key in sorted(LOG_RECORD_EXTRA_KEYS - {"service", "event"}):
            value = getattr(record, key, context.get(key))
            if value is not None:
                payload[key] = value
        if record.exc_info is not None and "exception_type" not in payload:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


class ServiceFieldFilter(logging.Filter):
    """为当前进程的所有 LogRecord 注入固定服务名称。"""

    def __init__(self, service: str) -> None:
        """保存已经由配置函数校验的服务名称。

        Args:
            service: 当前独立进程的固定服务名称。
        """
        super().__init__()
        self.service = service
        # 当前进程所有日志共用的服务名称。

    def filter(self, record: logging.LogRecord) -> bool:
        """在格式化前为日志记录补充服务字段。

        Args:
            record: 即将交给当前处理器的日志记录。

        Returns:
            注入服务字段后始终返回 True。
        """
        record.service = self.service
        return True


def configure_structured_logging(
    service: str,
    *,
    level: str = "INFO",
    stream: TextIO | None = None,
) -> logging.Logger:
    """为当前独立进程安装唯一的统一 JSON 根日志处理器。

    Args:
        service: ``api``、``worker``、``scheduler`` 或 ``mock_email_mcp``。
        level: 标准 Python 日志级别。
        stream: 可选测试输出流；省略时写入标准输出。

    Returns:
        已绑定服务字段、可直接记录入口生命周期事件的 Logger。

    Raises:
        ValueError: 服务名或日志级别不合法时抛出。
    """
    normalized_service = service.strip() if isinstance(service, str) else ""
    normalized_level = level.strip().upper() if isinstance(level, str) else ""
    if not normalized_service or len(normalized_service) > 64:
        raise ValueError("service 必须是长度不超过 64 的非空字符串")
    if normalized_level not in LOG_LEVELS:
        raise ValueError("level 必须是标准日志级别")
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(ServiceFieldFilter(normalized_service))
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(normalized_level)
    logger = logging.getLogger(f"file_governance.{normalized_service}")
    logger.setLevel(normalized_level)
    return logger


def log_runtime_event(
    logger: logging.Logger,
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    fields: Mapping[str, object] | None = None,
) -> None:
    """记录一条只允许固定脱敏字段的统一运行时事件。

    Args:
        logger: 已由入口或调用方配置的 Logger。
        event: 稳定机器可读事件名称。
        message: 简短中文事件说明，不得包含正文、凭据或文件内容。
        level: Python logging 数值级别。
        fields: 可选运行、任务、Worker、计划或有限统计字段。

    Raises:
        ValueError: 事件名、消息或扩展字段不符合白名单边界时抛出。
    """
    normalized_event = event.strip() if isinstance(event, str) else ""
    normalized_message = message.strip() if isinstance(message, str) else ""
    if not normalized_event or len(normalized_event) > 128:
        raise ValueError("event 必须是长度不超过 128 的非空字符串")
    if not normalized_message or len(normalized_message) > 1_000:
        raise ValueError("message 必须是长度不超过 1000 的非空字符串")
    extra = {"event": normalized_event}
    if fields:
        unknown_fields = set(fields) - LOG_RECORD_EXTRA_KEYS
        if unknown_fields:
            raise ValueError(f"日志事件包含未知字段：{sorted(unknown_fields)}")
        extra.update(fields)
    logger.log(level, normalized_message, extra=extra)
