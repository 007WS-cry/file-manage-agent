from app.mcp_servers.mock_email import (
    create_mock_email_mcp_server,
    load_mock_email_records,
)

"""本包导出可独立部署的模拟邮件 MCP 服务构造与脱敏数据加载接口。"""


# 本包允许其他模块导入的模拟邮件 MCP 公共接口。
__all__ = [
    "create_mock_email_mcp_server",
    "load_mock_email_records",
]
