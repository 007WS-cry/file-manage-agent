from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

"""本文件为显式启用的 PostgreSQL 集成测试启动并回收独占 Docker Compose 数据库。"""


# 当前仓库根目录，用于解析 Compose、Alembic 和测试专属项目路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 只有显式设置该变量时才启动 PostgreSQL 容器，默认测试不会依赖 Docker daemon。
POSTGRESQL_TEST_SWITCH_ENV = "FILE_GOVERNANCE_RUN_POSTGRESQL_TESTS"

# Alembic、API、Worker 和图状态统一读取的应用数据库 URL 环境变量。
APPLICATION_DATABASE_URL_ENV = "FILE_GOVERNANCE_DATABASE_URL"


def _reserve_local_port() -> int:
    """请求操作系统分配一个当前可用的本机 TCP 端口。

    Returns:
        释放监听套接字后可交给测试专属 PostgreSQL 容器映射的端口。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_compose(
    arguments: list[str],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """执行限定在测试项目名和两份受控 Compose 文件内的 Docker 命令。

    Args:
        arguments: ``docker compose`` 基础参数之后的子命令参数。
        environment: 包含测试数据库名、用户、密码和端口的独立环境。

    Returns:
        已捕获标准输出和标准错误的完成进程。
    """
    return subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            f"file-governance-pg-test-{os.getpid()}",
            "-f",
            "docker-compose.yml",
            "-f",
            "docker-compose.postgresql.yml",
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="session")
def postgresql_database_url() -> Iterator[str]:
    """提供由 Docker Compose 启动、完成全部 Alembic 迁移的 PostgreSQL URL。

    Yields:
        只在测试进程内使用、连接本机映射端口的 PostgreSQL SQLAlchemy URL。
    """
    if os.environ.get(POSTGRESQL_TEST_SWITCH_ENV) != "1":
        pytest.skip(
            f"设置 {POSTGRESQL_TEST_SWITCH_ENV}=1 后才运行 Docker PostgreSQL 集成测试"
        )
    if shutil.which("docker") is None:
        pytest.fail("已要求运行 PostgreSQL 集成测试，但未找到 docker 命令")
    if importlib.util.find_spec("psycopg") is None:
        pytest.fail("已要求运行 PostgreSQL 集成测试，但当前环境未安装 psycopg")

    host_port = _reserve_local_port()
    database_name = f"file_governance_test_{os.getpid()}"
    database_user = "file_governance_test"
    database_password = "integration-test-password"
    compose_environment = os.environ.copy()
    compose_environment.update(
        {
            "FILE_GOVERNANCE_POSTGRES_DB": database_name,
            "FILE_GOVERNANCE_POSTGRES_USER": database_user,
            "FILE_GOVERNANCE_POSTGRES_PASSWORD": database_password,
            "FILE_GOVERNANCE_POSTGRES_PORT": str(host_port),
        }
    )
    started = _run_compose(
        ["up", "--detach", "--wait", "--wait-timeout", "90", "postgres"],
        environment=compose_environment,
    )
    if started.returncode != 0:
        pytest.fail(
            "PostgreSQL 测试容器启动失败："
            f"{started.stdout}\n{started.stderr}"
        )

    database_url = (
        f"postgresql+psycopg://{database_user}:{database_password}"
        f"@127.0.0.1:{host_port}/{database_name}"
    )
    previous_database_url = os.environ.get(APPLICATION_DATABASE_URL_ENV)
    os.environ[APPLICATION_DATABASE_URL_ENV] = database_url
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    try:
        command.upgrade(alembic_config, "head")
        yield database_url
    finally:
        if previous_database_url is None:
            os.environ.pop(APPLICATION_DATABASE_URL_ENV, None)
        else:
            os.environ[APPLICATION_DATABASE_URL_ENV] = previous_database_url
        stopped = _run_compose(
            ["down", "--volumes", "--remove-orphans"],
            environment=compose_environment,
        )
        if stopped.returncode != 0:
            raise RuntimeError(
                "PostgreSQL 测试容器清理失败："
                f"{stopped.stdout}\n{stopped.stderr}"
            )
