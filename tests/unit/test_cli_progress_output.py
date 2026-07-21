from __future__ import annotations

import json

from app.entrypoints.cli import count_task_statuses, print_result, serialize_todos

"""本文件验证 CLI 进度摘要的字段白名单、稳定计数和大型状态隔离。"""


def test_todo_serialization_uses_safe_fields_and_stable_order() -> None:
    """Todo 输出应按 order 排序并丢弃未声明的内部或大型字段。"""
    result = {
        "todos": [
            {
                "id": "run:todo:report",
                "title": "输出治理报告",
                "status": "pending",
                "related_task_ids": ["run:report"],
                "order": 4,
                "internal_large_value": "不得输出的 Todo 扩展内容",
            },
            {
                "id": "run:todo:facts",
                "title": "准备文件事实",
                "status": "completed",
                "related_task_ids": ["run:inventory"],
                "order": 1,
            },
        ]
    }

    todos = serialize_todos(result)

    assert [todo["order"] for todo in todos] == [1, 4]
    assert set(todos[0]) == {
        "id",
        "title",
        "status",
        "related_task_ids",
        "order",
    }
    assert "internal_large_value" not in todos[1]


def test_task_status_counts_include_zero_value_states() -> None:
    """Task 计数应固定包含五种状态，并忽略不属于 Task 对象的值。"""
    result = {
        "tasks": [
            {"task_id": "run:inventory", "status": "completed"},
            {"task_id": "run:version", "status": "completed"},
            {"task_id": "run:review", "status": "skipped"},
            "不是 Task 对象",
        ]
    }

    counts = count_task_statuses(result)

    assert counts == {
        "pending": 0,
        "running": 0,
        "completed": 2,
        "failed": 0,
        "skipped": 1,
    }


def test_print_result_excludes_documents_task_details_and_report_markdown(
    capsys,
) -> None:
    """CLI JSON 只能输出最小进度摘要，不得透传正文、产物引用或完整报告。"""
    sensitive_marker = "不得出现在 CLI 输出中的大型正文"
    result = {
        "run": {"status": "completed"},
        "report": {
            "summary": "治理完成。",
            "report_path": "/reports/governance.md",
            "report_markdown": sensitive_marker,
        },
        "documents": [{"content": sensitive_marker}],
        "files": [{"raw_content": sensitive_marker}],
        "tasks": [
            {
                "task_id": "run:inventory",
                "status": "completed",
                "output_refs": [sensitive_marker],
            }
        ],
        "todos": [
            {
                "id": "run:todo:facts",
                "title": "准备文件事实",
                "status": "completed",
                "related_task_ids": ["run:inventory"],
                "order": 1,
            }
        ],
        "__interrupt__": (),
    }

    print_result(result, thread_id="safe-cli-summary")
    captured = capsys.readouterr()
    output = json.loads(captured.out)

    assert set(output) == {
        "thread_id",
        "status",
        "summary",
        "report_path",
        "todos",
        "task_status_counts",
        "interrupts",
    }
    assert sensitive_marker not in captured.out
    assert output["todos"][0]["title"] == "准备文件事实"
    assert output["task_status_counts"]["completed"] == 1
