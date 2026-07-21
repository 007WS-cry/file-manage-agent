from __future__ import annotations

import ast
from pathlib import Path

"""本文件验证 app/nodes 只定义在 LangGraph 流程中显式注册的节点函数。"""

# 项目根目录，用于定位节点与图源码。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 只允许存放 LangGraph 节点函数的源码目录。
NODE_DIRECTORY = PROJECT_ROOT / "app" / "nodes"

# 包含全部 StateGraph.add_node 注册语句的源码目录。
GRAPH_DIRECTORY = PROJECT_ROOT / "app" / "graphs"


def collect_node_function_definitions() -> dict[str, set[str]]:
    """收集 app.nodes 各模块中定义的全部同步和异步函数。

    Returns:
        节点模块名到函数名称集合的映射；嵌套函数和类方法同样会被纳入约束。
    """
    definitions: dict[str, set[str]] = {}
    for path in sorted(NODE_DIRECTORY.glob("*.py")):
        module_name = f"app.nodes.{path.stem}"
        syntax_tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        definitions[module_name] = {
            node.name
            for node in ast.walk(syntax_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
    return definitions


def collect_registered_node_functions() -> set[tuple[str, str]]:
    """解析图模块导入和 add_node 调用，收集真实注册的节点函数。

    Returns:
        ``(节点模块, 原始函数名)`` 二元组集合。
    """
    registrations: set[tuple[str, str]] = set()
    for path in sorted(GRAPH_DIRECTORY.glob("*.py")):
        syntax_tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported_nodes: dict[str, tuple[str, str]] = {}
        for statement in syntax_tree.body:
            if not isinstance(statement, ast.ImportFrom):
                continue
            if statement.module is None or not statement.module.startswith("app.nodes."):
                continue
            for imported_name in statement.names:
                local_name = imported_name.asname or imported_name.name
                imported_nodes[local_name] = (statement.module, imported_name.name)

        for expression in ast.walk(syntax_tree):
            if not isinstance(expression, ast.Call):
                continue
            if not isinstance(expression.func, ast.Attribute) or expression.func.attr != "add_node":
                continue
            action = expression.args[1] if len(expression.args) >= 2 else None
            if action is None:
                action = next(
                    (
                        keyword.value
                        for keyword in expression.keywords
                        if keyword.arg in {"action", "node"}
                    ),
                    None,
                )
            if isinstance(action, ast.Name) and action.id in imported_nodes:
                registrations.add(imported_nodes[action.id])
    return registrations


def test_every_nodes_function_is_registered_in_a_langgraph() -> None:
    """nodes 目录内不得保留未在任一流程图注册的工具性函数。"""
    definitions = collect_node_function_definitions()
    registrations = collect_registered_node_functions()
    unregistered = {
        module_name: sorted(
            function_name
            for function_name in function_names
            if (module_name, function_name) not in registrations
        )
        for module_name, function_names in definitions.items()
    }
    unregistered = {
        module_name: function_names
        for module_name, function_names in unregistered.items()
        if function_names
    }

    assert unregistered == {}
