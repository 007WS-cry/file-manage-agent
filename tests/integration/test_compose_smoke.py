from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

"""本文件在不启动服务的情况下验证默认 SQLite 与 PostgreSQL override 部署模型。"""


# 当前仓库根目录，用于确保 Compose 相对路径始终从基础文件所在目录解析。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_compose_model(*compose_files: str) -> dict:
    """调用 Docker Compose 合并配置并解析为 JSON 对象。

    Args:
        compose_files: 按基础文件到 override 文件顺序提供的受控文件名。

    Returns:
        Docker Compose 完成变量替换和合并后的应用模型。
    """
    if shutil.which("docker") is None:
        pytest.skip("当前环境没有 docker 命令，跳过 Compose 配置烟雾测试")
    environment = os.environ.copy()
    environment["FILE_GOVERNANCE_POSTGRES_PASSWORD"] = "compose-smoke-password"
    command = ["docker", "compose"]
    for compose_file in compose_files:
        command.extend(["-f", compose_file])
    command.extend(["config", "--format", "json"])
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_default_compose_keeps_sqlite_without_postgresql_service() -> None:
    """默认 Compose 应保留低成本 SQLite，不创建或依赖 PostgreSQL 服务。"""
    model = _load_compose_model("docker-compose.yml")
    services = model["services"]

    assert "postgres" not in services
    assert services["api"]["environment"]["FILE_GOVERNANCE_DATABASE_PATH"].endswith(
        "file-governance-app.sqlite3"
    )
    assert not services["api"]["environment"].get(
        "FILE_GOVERNANCE_DATABASE_URL"
    )
    assert services["worker"]["command"] == ["file-governance-worker"]
    assert services["scheduler"]["command"] == ["file-governance-scheduler"]
    assert "127.0.0.1:8000/health" in " ".join(
        services["api"]["healthcheck"]["test"]
    )


def test_postgresql_override_builds_complete_healthy_topology() -> None:
    """叠加 override 后迁移和三个运行服务应共享 PostgreSQL URL 与健康依赖。"""
    model = _load_compose_model(
        "docker-compose.yml",
        "docker-compose.postgresql.yml",
    )
    services = model["services"]

    assert set(services) == {
        "api",
        "migrate",
        "mock-email-mcp",
        "postgres",
        "scheduler",
        "worker",
    }
    assert services["postgres"]["image"] == "postgres:17.10-alpine"
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == (
        "service_healthy"
    )
    for service_name in ("migrate", "api", "worker", "scheduler"):
        database_url = services[service_name]["environment"][
            "FILE_GOVERNANCE_DATABASE_URL"
        ]
        assert database_url.startswith("postgresql+psycopg://")
        assert "@postgres:5432/" in database_url
    for service_name in ("api", "worker", "scheduler"):
        assert services[service_name]["depends_on"]["migrate"]["condition"] == (
            "service_completed_successfully"
        )
