from __future__ import annotations

import ast
from pathlib import Path

"""本文件验证 1.0.0 的路由、状态归属和中文文档字符串目录契约。"""


# 当前仓库根目录，用于读取应用、迁移、脚本和测试源码。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 必须具有中文模块说明以及类、函数文档字符串的 Python 源码目录。
DOCUMENTED_SOURCE_DIRECTORIES = (
    PROJECT_ROOT / "app",
    PROJECT_ROOT / "alembic",
    PROJECT_ROOT / "scripts",
    PROJECT_ROOT / "tests",
)

# 中文字符的基础 Unicode 范围，用于拒绝仅有占位英文说明的发布源码。
CHINESE_CHARACTER_RANGE = ("\u4e00", "\u9fff")


def _contains_chinese(value: str) -> bool:
    """判断说明文字中是否至少包含一个常用中文字符。

    Args:
        value: 等待检查的模块说明、类文档字符串或函数文档字符串。

    Returns:
        包含基础中文字符时返回 True，否则返回 False。
    """
    lower_bound, upper_bound = CHINESE_CHARACTER_RANGE
    return any(lower_bound <= character <= upper_bound for character in value)


def _read_syntax_tree(path: Path) -> ast.Module:
    """以 UTF-8 读取一个 Python 文件并解析为抽象语法树。

    Args:
        path: 位于当前仓库受控源码目录内的 Python 文件。

    Returns:
        可供目录契约测试遍历的模块语法树。
    """
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_note_after_imports(syntax_tree: ast.Module) -> str | None:
    """读取仓库约定中紧跟 import 区域之后的模块说明字符串。

    Args:
        syntax_tree: 已解析的 Python 模块语法树。

    Returns:
        import 之后首条语句是字符串时返回其内容，否则返回 None。
    """
    for statement in syntax_tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return statement.value.value
        return None
    return None


def test_every_router_function_is_used_by_a_conditional_edge() -> None:
    """routers.py 的每个函数都必须被某个图明确注册为条件路由。"""
    router_path = PROJECT_ROOT / "app" / "graphs" / "routers.py"
    router_tree = _read_syntax_tree(router_path)
    router_functions = {
        statement.name
        for statement in router_tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    registered_router_functions: set[str] = set()

    for graph_path in sorted((PROJECT_ROOT / "app" / "graphs").glob("*.py")):
        graph_tree = _read_syntax_tree(graph_path)
        imported_routers: dict[str, str] = {}
        for statement in graph_tree.body:
            if not isinstance(statement, ast.ImportFrom):
                continue
            if statement.module != "app.graphs.routers":
                continue
            for imported_name in statement.names:
                imported_routers[imported_name.asname or imported_name.name] = (
                    imported_name.name
                )

        for expression in ast.walk(graph_tree):
            if not isinstance(expression, ast.Call):
                continue
            if (
                not isinstance(expression.func, ast.Attribute)
                or expression.func.attr != "add_conditional_edges"
                or len(expression.args) < 2
            ):
                continue
            route_argument = expression.args[1]
            if isinstance(route_argument, ast.Name):
                registered_name = imported_routers.get(route_argument.id)
                if registered_name is not None:
                    registered_router_functions.add(registered_name)

    assert router_functions
    assert router_functions == registered_router_functions


def test_state_directory_defines_classes_only_in_models() -> None:
    """app/state 中的状态类及其子类只能在 models.py 定义。"""
    misplaced_classes: list[str] = []
    state_directory = PROJECT_ROOT / "app" / "state"
    for state_path in sorted(state_directory.glob("*.py")):
        if state_path.name == "models.py":
            continue
        for statement in _read_syntax_tree(state_path).body:
            if isinstance(statement, ast.ClassDef):
                misplaced_classes.append(f"{state_path.name}:{statement.name}")

    assert misplaced_classes == []


def test_python_sources_have_chinese_module_and_definition_docstrings() -> None:
    """发布源码必须具有 import 后中文模块说明以及中文类、函数文档字符串。"""
    missing_module_notes: list[str] = []
    missing_definition_docstrings: list[str] = []

    for source_directory in DOCUMENTED_SOURCE_DIRECTORIES:
        if not source_directory.exists():
            continue
        for source_path in sorted(source_directory.rglob("*.py")):
            syntax_tree = _read_syntax_tree(source_path)
            module_note = _module_note_after_imports(syntax_tree)
            if module_note is None or not _contains_chinese(module_note):
                missing_module_notes.append(str(source_path.relative_to(PROJECT_ROOT)))

            for definition in ast.walk(syntax_tree):
                if not isinstance(
                    definition,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    continue
                definition_docstring = ast.get_docstring(definition)
                if (
                    definition_docstring is None
                    or not _contains_chinese(definition_docstring)
                ):
                    missing_definition_docstrings.append(
                        f"{source_path.relative_to(PROJECT_ROOT)}:"
                        f"{definition.lineno}:{definition.name}"
                    )

    assert missing_module_notes == []
    assert missing_definition_docstrings == []
