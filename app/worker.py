"""任务队列 Worker（多消费者 asyncio.Queue，并发度由 JOB_CONCURRENCY 控制）。

设计：
- 全局一个 asyncio.Queue，所有提交（新任务 / 重试）都是入队
- 启动 JOB_CONCURRENCY 个消费者协程共用队列，每个消费者串行执行自己拿到的任务
- 进程启动时 resume_pending 把 DB 里未完成的任务重新入队
- 前端看到的效果：最多 JOB_CONCURRENCY 个任务 status=running，其余 status=pending 排队
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from sqlalchemy import delete, select

from .config import settings
from .db import AsyncSessionLocal
from .models import Job, JobFile
from .pipeline import retry_file, run_job

logger = logging.getLogger(__name__)

_queue: asyncio.Queue | None = None
_worker_tasks: list[asyncio.Task] = []


def _get_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def _is_job_deleted(job_id: str) -> bool:
    """快速检测 job 是否已被用户软删（入队后删除的场景）。"""
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(Job.is_deleted).where(Job.id == job_id))).scalar_one_or_none()
        return bool(row)


async def _consume_one(item: dict) -> None:
    kind = item.get("kind")
    job_id = item.get("job_id")
    try:
        # 入队后用户如果点了删除，这里直接跳过，不浪费 LLM/browser 配额
        if job_id and await _is_job_deleted(job_id):
            logger.info("⏭️ 跳过已删除的任务 job=%s kind=%s", job_id, kind)
            return
        if kind == "job":
            logger.info("▶️ 开始任务 job=%s", job_id)
            await run_job(job_id)
            logger.info("✔️ 完成任务 job=%s", job_id)
        elif kind == "retry":
            logger.info("▶️ 开始重试 job=%s file=%s", job_id, item["file_id"])
            await retry_file(job_id, item["file_id"])
            logger.info("✔️ 完成重试 job=%s file=%s", job_id, item["file_id"])
        else:
            logger.warning("未知队列项: %s", item)
    except Exception:
        logger.exception("队列任务失败 %s", item)


async def _worker_loop(name: str) -> None:
    q = _get_queue()
    logger.info("Worker %s 启动，等待任务...", name)
    while True:
        item = await q.get()
        try:
            await _consume_one(item)
        finally:
            q.task_done()


def start_worker() -> None:
    """主进程启动时调用一次，起 JOB_CONCURRENCY 个消费者。"""
    global _worker_tasks
    _worker_tasks = [t for t in _worker_tasks if not t.done()]
    if _worker_tasks:
        return
    n = max(1, settings.JOB_CONCURRENCY)
    _worker_tasks = [
        asyncio.create_task(_worker_loop(f"job-worker-{i}"), name=f"soft-copyright-worker-{i}")
        for i in range(n)
    ]
    logger.info("已启动 %d 个 worker（JOB_CONCURRENCY=%d）", n, n)


def reconcile_workers() -> int:
    """JOB_CONCURRENCY 变更后调用：只扩容，不缩容（避免打断正在跑的 job）。

    返回当前活跃 worker 数量。若要缩容需重启进程。
    """
    global _worker_tasks
    _worker_tasks = [t for t in _worker_tasks if not t.done()]
    target = max(1, settings.JOB_CONCURRENCY)
    current = len(_worker_tasks)
    if current >= target:
        return current
    for i in range(current, target):
        _worker_tasks.append(
            asyncio.create_task(_worker_loop(f"job-worker-{i}"), name=f"soft-copyright-worker-{i}")
        )
    logger.info("worker 扩容至 %d（原 %d）", target, current)
    return target


def submit(job_id: str) -> None:
    """提交新任务到队列尾部，立即返回。"""
    _get_queue().put_nowait({"kind": "job", "job_id": job_id})


def submit_retry(job_id: str, file_id: int) -> None:
    """提交重试到队列尾部（和新任务共用队列，FIFO）。"""
    _get_queue().put_nowait({"kind": "retry", "job_id": job_id, "file_id": file_id})


def queue_size() -> int:
    q = _queue
    return 0 if q is None else q.qsize()


async def resume_pending() -> None:
    """进程启动时扫出 pending / running 的 Job 重新入队。

    由于没有 checkpoint 机制，重启等同于从头跑；先清空该 Job 的旧 JobFile 记录和已
    生成的磁盘文件，避免 run_job 重跑时产生重复 JobFile。
    按创建时间顺序入队，保持 FIFO 语义。
    """
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(Job)
            .where(Job.status.in_(("pending", "running")))
            .order_by(Job.created_at)
        )).scalars().all()
        for j in rows:
            logger.info("恢复任务 %s (旧状态 %s)，清理旧 JobFile / 输出目录", j.id, j.status)
            await s.execute(delete(JobFile).where(JobFile.job_id == j.id))
            out_dir = Path(settings.DATA_DIR) / j.id
            if out_dir.exists():
                shutil.rmtree(out_dir, ignore_errors=True)
            j.status = "pending"
            j.progress = 0
            j.error = None
        await s.commit()
        for j in rows:
            submit(j.id)
