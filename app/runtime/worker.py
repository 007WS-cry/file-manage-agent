from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.types import Command

from app.graphs.file_governance import build_background_file_governance_graph
from app.runtime.dispatcher import build_runtime_initial_state
from app.runtime.job_queue import JobQueue, utc_now
from app.state.models import BackgroundJobState
from app.storage.checkpoints import open_checkpointer
from app.utils.background_resume import serialize_pending_interrupt

"""本模块实现独立 Background Worker 的领取循环、租约心跳和 LangGraph 执行边界。"""


# 测试或嵌入式 Worker 可以注入的单任务执行函数类型。
JobExecutor = Callable[[BackgroundJobState], Mapping[str, Any]]

# 顶层图允许直接映射到后台任务终态的运行状态。
GRAPH_FINAL_STATUSES = frozenset({"completed", "partial", "failed"})


class BackgroundWorker:
    """从持久化队列领取任务并在独立进程中执行文件治理 LangGraph。"""

    def __init__(
        self,
        queue: JobQueue,
        *,
        worker_id: str | None = None,
        poll_interval_seconds: float = 1.0,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        retry_delay_seconds: float = 1.0,
        job_executor: JobExecutor | None = None,
    ) -> None:
        """创建一个具有稳定身份和有限租约参数的后台 Worker。

        Args:
            queue: API 与 Worker 共用的持久化后台任务队列。
            worker_id: 可选稳定 Worker ID；省略时为当前进程生成随机 ID。
            poll_interval_seconds: 当前没有任务时的轮询等待秒数。
            lease_seconds: 未续租前任务保持锁定的秒数。
            heartbeat_interval_seconds: 执行任务期间续租的间隔秒数。
            retry_delay_seconds: Worker 执行异常后重新入队的最小等待秒数。
            job_executor: 测试可注入的单任务执行函数；省略时执行真实 LangGraph。

        Raises:
            ValueError: 轮询、租约、心跳或重试间隔不符合安全边界时抛出。
        """
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds 必须大于零")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于零")
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds 必须大于零")
        if heartbeat_interval_seconds >= lease_seconds:
            raise ValueError("heartbeat_interval_seconds 必须小于 lease_seconds")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不得为负数")

        self.queue = queue
        # 当前 Worker 使用的持久化任务队列。

        self.worker_id = (worker_id or f"worker-{uuid4().hex}").strip()
        # 当前 Worker 的跨日志、租约和任务领取稳定 ID。

        if not self.worker_id:
            raise ValueError("worker_id 不得为空")

        self.poll_interval_seconds = float(poll_interval_seconds)
        # 当前无任务时等待下一次领取的秒数。

        self.lease_seconds = float(lease_seconds)
        # 未收到心跳前任务保持锁定的秒数。

        self.heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        # 执行任务期间写入独立心跳事务的间隔秒数。

        self.retry_delay_seconds = float(retry_delay_seconds)
        # Worker 执行异常后重新允许领取任务的等待秒数。

        self._job_executor = job_executor or self._invoke_graph
        # 当前 Worker 实际调用的单任务执行函数。

        self._stop_event = threading.Event()
        # 请求停止 Worker 轮询循环的线程安全事件。

        self._heartbeat_error: Exception | None = None
        # 心跳线程最近一次不可恢复异常，由主执行线程在图调用后检查。

    def _invoke_graph(self, job: BackgroundJobState) -> Mapping[str, Any]:
        """使用任务持久化信封和显式 SQLite Checkpointer 执行顶层图。

        Args:
            job: 当前 Worker 已领取并推进为 running 的后台任务。

        Returns:
            顶层 LangGraph 的内部结果映射。

        Raises:
            ValueError: 任务没有持久化 SQLite checkpoint 路径时抛出。
        """
        envelope = dict(job["request_payload"])
        checkpoint = envelope.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise ValueError("后台任务缺少 checkpoint 对象")
        checkpoint_path = checkpoint.get("database_path")
        if not isinstance(checkpoint_path, str) or not checkpoint_path.strip():
            raise ValueError("后台任务缺少持久化 checkpoint.database_path")
        workspace = envelope.get("workspace")
        if not isinstance(workspace, Mapping):
            raise ValueError("后台任务缺少 workspace 对象")
        input_root = workspace.get("input_root")
        if not isinstance(input_root, str) or not input_root.strip():
            raise ValueError("后台任务缺少 workspace.input_root")
        with open_checkpointer(
            "sqlite",
            database_path=Path(checkpoint_path),
            input_root=input_root,
        ) as checkpointer:
            graph = build_background_file_governance_graph(checkpointer=checkpointer)
            config = {"configurable": {"thread_id": job["thread_id"]}}
            resume_state = job.get("resume")
            if isinstance(resume_state, Mapping) and resume_state.get("status") == "pending":
                resume_value = resume_state.get("value")
                if not isinstance(resume_value, Mapping):
                    raise ValueError("后台恢复任务缺少可应用的 resume.value")
                snapshot = graph.get_state(config)
                if not snapshot.next:
                    raise ValueError("当前 checkpoint 没有可恢复的 LangGraph 中断")
                return graph.invoke(Command(resume=dict(resume_value)), config=config)
            state = build_runtime_initial_state(
                envelope,
                run_id=job["run_id"],
                thread_id=job["thread_id"],
                execution_mode="background",
                trigger_source=job["trigger_source"],
                background_job_id=job["id"],
                worker_id=self.worker_id,
            )
            return graph.invoke(state, config=config)

    def _heartbeat_loop(
        self,
        job_id: str,
        stop_event: threading.Event,
    ) -> None:
        """在图执行期间使用独立短事务定期续租。

        Args:
            job_id: 当前执行的后台任务 ID。
            stop_event: 图执行完成后由主线程设置的停止事件。
        """
        while not stop_event.wait(self.heartbeat_interval_seconds):
            try:
                self.queue.heartbeat(
                    job_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                self._heartbeat_error = exc
                stop_event.set()
                return

    def _start_heartbeat(
        self,
        job_id: str,
    ) -> tuple[threading.Event, threading.Thread]:
        """启动一个不持有 LangGraph Session 的租约心跳线程。

        Args:
            job_id: 当前执行的后台任务 ID。

        Returns:
            用于停止心跳的事件和已经启动的守护线程。
        """
        stop_event = threading.Event()
        self._heartbeat_error = None
        thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job_id, stop_event),
            name=f"lease-heartbeat-{job_id}",
            daemon=True,
        )
        thread.start()
        return stop_event, thread

    def _finish_from_result(
        self,
        job: BackgroundJobState,
        result: Mapping[str, Any],
    ) -> BackgroundJobState:
        """把 LangGraph 运行状态和 interrupt 映射为后台任务收口状态。

        Args:
            job: 当前 Worker 执行的后台任务。
            result: 顶层 LangGraph 返回的内部结果映射。

        Returns:
            已持久化完成、部分完成、失败或等待人工输入的后台任务。
        """
        run = result.get("run")
        run_status = run.get("status") if isinstance(run, Mapping) else None
        interrupts = result.get("__interrupt__", ())
        pending_interrupt = serialize_pending_interrupt(
            interrupts,
            created_at=utc_now().isoformat(),
        )
        if interrupts or run_status == "waiting_human":
            status = "waiting_human"
        elif run_status in GRAPH_FINAL_STATUSES:
            status = str(run_status)
        else:
            status = "failed"
        report = result.get("report")
        report_path = report.get("report_path") if isinstance(report, Mapping) else None
        error_summary = (
            "LangGraph 未返回可收口的最终运行状态。"
            if status == "failed" and run_status not in GRAPH_FINAL_STATUSES
            else None
        )
        return self.queue.finish(
            job["id"],
            worker_id=self.worker_id,
            status=status,
            report_path=str(report_path) if report_path is not None else None,
            error_summary=error_summary,
            pending_interrupt=pending_interrupt,
        )

    def run_once(self) -> bool:
        """恢复过期租约并尝试领取、执行一个后台任务。

        Returns:
            成功领取过任务时返回 True；当前队列为空时返回 False。
        """
        self.queue.requeue_expired()
        job = self.queue.claim(
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        self.queue.mark_running(
            job["id"],
            worker_id=self.worker_id,
        )
        stop_heartbeat, heartbeat_thread = self._start_heartbeat(job["id"])
        try:
            result = self._job_executor(job)
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval_seconds + 1.0)
            if self._heartbeat_error is not None:
                raise RuntimeError("Worker 租约心跳失败") from self._heartbeat_error
            self._finish_from_result(job, result)
        except Exception as exc:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=self.heartbeat_interval_seconds + 1.0)
            self.queue.fail_or_requeue(
                job["id"],
                worker_id=self.worker_id,
                error_summary=f"{type(exc).__name__}: 后台任务执行失败。",
                retry_delay_seconds=self.retry_delay_seconds,
            )
        return True

    def run_forever(self) -> None:
        """持续轮询后台队列，直到调用方请求停止 Worker。"""
        while not self._stop_event.is_set():
            processed = self.run_once()
            if not processed:
                self._stop_event.wait(self.poll_interval_seconds)

    def stop(self) -> None:
        """请求当前 Worker 在本次安全边界后停止轮询。"""
        self._stop_event.set()
