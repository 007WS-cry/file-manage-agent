from app.observability.context import bind_log_context, get_log_context
from app.observability.logging import (
    JsonLogFormatter,
    configure_structured_logging,
    log_runtime_event,
)

"""本包统一导出四类独立服务共享的 JSON 日志格式、上下文绑定和事件记录能力。"""


# 可供入口进程、运行时服务和测试稳定导入的可观测性公共接口。
__all__ = [
    "JsonLogFormatter",
    "bind_log_context",
    "configure_structured_logging",
    "get_log_context",
    "log_runtime_event",
]
