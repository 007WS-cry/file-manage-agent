from __future__ import annotations

import argparse
import json
import os
import socket
from collections.abc import Sequence
from pathlib import Path

from app.runtime.job_queue import JobQueue
from app.runtime.worker import BackgroundWorker
from app.storage.database import DEFAULT_APPLICATION_DATABASE_PATH

"""本模块解析独立 Worker 参数，并启动一次领取或长期后台任务轮询循环。"""

# 应用数据库路径使用的环境变量名称。
APPLICATION_DATABASE_PATH_ENV = "FILE_GOVERNANCE_DATABASE_PATH"


# Worker 轮询间隔使用的环境变量名称。
WORKER_POLL_INTERVAL_ENV = "FILE_GOVERNANCE_WORKER_POLL_INTERVAL_SECONDS"

# Worker 租约时长使用的环境变量名称。
WORKER_LEASE_SECONDS_ENV = "FILE_GOVERNANCE_WORKER_LEASE_SECONDS"

# Worker 心跳间隔使用的环境变量名称。
WORKER_HEARTBEAT_INTERVAL_ENV = "FILE_GOVERNANCE_WORKER_HEARTBEAT_INTERVAL_SECONDS"

# Worker 失败重新入队等待时长使用的环境变量名称。
WORKER_RETRY_DELAY_ENV = "FILE_GOVERNANCE_WORKER_RETRY_DELAY_SECONDS"


def build_argument_parser() -> argparse.ArgumentParser:
    """构建独立 Background Worker 的命令行参数解析器。

    Returns:
        包含数据库、租约、心跳、轮询和单次执行选项的解析器。
    """
    parser = argparse.ArgumentParser(
        prog="file-governance-worker",
        description="从 SQLite 持久化队列领取并执行文件治理任务。",
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
        help="已执行 Alembic 迁移的应用数据库路径。",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help="可选稳定 Worker ID；省略时根据主机和进程生成。",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.environ.get(WORKER_POLL_INTERVAL_ENV, "1.0")),
        help="当前无任务时的轮询等待秒数。",
    )
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=float(os.environ.get(WORKER_LEASE_SECONDS_ENV, "30.0")),
        help="未续租前任务保持锁定的秒数。",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(os.environ.get(WORKER_HEARTBEAT_INTERVAL_ENV, "10.0")),
        help="执行任务期间续租的间隔秒数。",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=float(os.environ.get(WORKER_RETRY_DELAY_ENV, "1.0")),
        help="执行异常后重新允许领取任务的等待秒数。",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只尝试领取一个任务后退出，适合集成测试和手工演示。",
    )
    return parser


def build_default_worker_id() -> str:
    """根据当前主机和进程生成不包含业务信息的 Worker ID。

    Returns:
        适合写入租约和结构化日志的 Worker 标识。
    """
    return f"worker-{socket.gethostname()}-{os.getpid()}"


def main(argv: Sequence[str] | None = None) -> int:
    """启动一次或长期运行的 Background Worker。

    Args:
        argv: 可选命令行参数序列；省略时读取当前进程参数。

    Returns:
        Worker 正常退出时返回零。
    """
    arguments = build_argument_parser().parse_args(argv)
    queue = JobQueue(arguments.database_path)
    worker = BackgroundWorker(
        queue,
        worker_id=arguments.worker_id or build_default_worker_id(),
        poll_interval_seconds=arguments.poll_interval,
        lease_seconds=arguments.lease_seconds,
        heartbeat_interval_seconds=arguments.heartbeat_interval,
        retry_delay_seconds=arguments.retry_delay,
    )
    try:
        if arguments.once:
            processed = worker.run_once()
            print(
                json.dumps(
                    {
                        "worker_id": worker.worker_id,
                        "processed": processed,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        worker.run_forever()
        return 0
    except KeyboardInterrupt:
        worker.stop()
        return 0
    finally:
        queue.close()


if __name__ == "__main__":
    raise SystemExit(main())
