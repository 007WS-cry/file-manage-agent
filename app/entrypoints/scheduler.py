from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path

from app.observability.logging import (
    configure_structured_logging,
    log_runtime_event,
)
from app.runtime.scheduler import (
    DEFAULT_SCHEDULE_MAX_ATTEMPTS,
    DEFAULT_SCHEDULE_RECONCILE_INTERVAL_SECONDS,
    DEFAULT_SCHEDULER_TIMEZONE,
    SchedulerService,
)
from app.storage.database import DEFAULT_APPLICATION_DATABASE_PATH

"""本模块解析独立 APScheduler 进程参数，并启动持久化 Cron 计划同步和入队循环。"""


# Scheduler 读取应用数据库路径使用的环境变量名称。
APPLICATION_DATABASE_PATH_ENV = "FILE_GOVERNANCE_DATABASE_PATH"

# Scheduler 为后台任务写入的 checkpoint 路径环境变量名称。
CHECKPOINT_PATH_ENV = "FILE_GOVERNANCE_CHECKPOINT_PATH"

# 未通过环境变量覆盖时使用的默认后台 checkpoint 路径。
DEFAULT_RUNTIME_CHECKPOINT_PATH = Path(".artifacts/checkpoints/file-governance-background.sqlite3")

# Scheduler 默认 IANA 时区使用的环境变量名称。
SCHEDULER_TIMEZONE_ENV = "FILE_GOVERNANCE_SCHEDULER_TIMEZONE"

# Scheduler 数据库同步间隔使用的环境变量名称。
SCHEDULER_RECONCILE_INTERVAL_ENV = "FILE_GOVERNANCE_SCHEDULER_RECONCILE_INTERVAL_SECONDS"

# Cron 后台任务最大尝试次数使用的环境变量名称。
SCHEDULER_MAX_ATTEMPTS_ENV = "FILE_GOVERNANCE_SCHEDULER_MAX_ATTEMPTS"

# 四类运行时服务共享的 JSON 日志级别环境变量名称。
LOG_LEVEL_ENV = "FILE_GOVERNANCE_LOG_LEVEL"


def build_argument_parser() -> argparse.ArgumentParser:
    """构建独立 Scheduler 的命令行参数解析器。

    Returns:
        包含数据库、checkpoint、时区、同步间隔和单次同步选项的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="file-governance-scheduler",
        description="恢复持久化 Cron 计划，并在触发时只创建后台队列任务。",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path(
            os.environ.get(
                APPLICATION_DATABASE_PATH_ENV,
                str(DEFAULT_APPLICATION_DATABASE_PATH),
            )
        ),
        help="已执行 0003 Alembic 迁移的应用数据库路径。",
    )
    parser.add_argument(
        "--checkpoint-path",
        type=Path,
        default=Path(
            os.environ.get(
                CHECKPOINT_PATH_ENV,
                str(DEFAULT_RUNTIME_CHECKPOINT_PATH),
            )
        ),
        help="Cron 后台任务与 Worker 共用的 SQLite checkpoint 路径。",
    )
    parser.add_argument(
        "--timezone",
        default=os.environ.get(
            SCHEDULER_TIMEZONE_ENV,
            DEFAULT_SCHEDULER_TIMEZONE,
        ),
        help="Scheduler 内部同步任务使用的 IANA 时区。",
    )
    parser.add_argument(
        "--reconcile-interval",
        type=float,
        default=float(
            os.environ.get(
                SCHEDULER_RECONCILE_INTERVAL_ENV,
                str(DEFAULT_SCHEDULE_RECONCILE_INTERVAL_SECONDS),
            )
        ),
        help="从 scheduled_jobs 同步 API 变更的间隔秒数。",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=int(
            os.environ.get(
                SCHEDULER_MAX_ATTEMPTS_ENV,
                str(DEFAULT_SCHEDULE_MAX_ATTEMPTS),
            )
        ),
        help="每个 Cron 后台任务允许的 Worker 总尝试次数。",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default=os.environ.get(LOG_LEVEL_ENV, "INFO").lower(),
        help="Scheduler 与 APScheduler 统一使用的 JSON 日志级别。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只同步一次持久化计划后退出，不等待或触发 LangGraph。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """启动一次计划同步或长期 APScheduler 入队循环。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        Scheduler 正常退出时返回零。
    """
    arguments = build_argument_parser().parse_args(argv)
    logger = configure_structured_logging("scheduler", level=arguments.log_level)
    service = SchedulerService(
        arguments.database_path,
        arguments.checkpoint_path,
        timezone_name=arguments.timezone,
        reconcile_interval_seconds=arguments.reconcile_interval,
        max_attempts=arguments.max_attempts,
    )
    log_runtime_event(logger, "service_starting", "APScheduler 服务正在启动。")
    try:
        if arguments.once:
            synchronized = service.sync_schedules()
            log_runtime_event(
                logger,
                "scheduler_once_completed",
                "APScheduler 已完成单次计划同步。",
                fields={"synchronized_schedules": synchronized},
            )
            return 0
        service.run_forever()
        return 0
    except KeyboardInterrupt:
        service.stop(wait=True)
        return 0
    finally:
        service.close()
        log_runtime_event(logger, "service_stopped", "APScheduler 服务已安全停止。")


if __name__ == "__main__":
    raise SystemExit(main())
