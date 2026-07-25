from app.runtime.job_queue import JobQueue

"""本包提供 HTTP 提交入口之外的后台任务队列、分派和 Worker 运行能力。"""


# 本包当前公开的持久化后台任务队列服务。
__all__ = ["JobQueue"]
