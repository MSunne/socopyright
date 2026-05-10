import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from . import pipeline, worker
from .auth import current_user
from .db import AsyncSessionLocal, get_session
from .models import Job, JobFile
from .schemas import JobCreate, JobDetailOut, JobListResp, JobOut

router = APIRouter(prefix="/jobs", tags=["jobs"])

# 文件名里禁止出现的字符（Windows/macOS/Linux 共同非法集合 + 控制字符）
_UNSAFE_FN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_part(name: str, max_len: int = 60) -> str:
    """清洗文件名片段：去掉文件系统非法字符 + 折叠空白 + 截断。"""
    if not name:
        return ""
    s = _UNSAFE_FN_CHARS.sub("", str(name)).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:max_len]


def _attachment_headers(filename: str, *, ascii_fallback: str) -> dict[str, str]:
    """生成 Content-Disposition：UTF-8 中文名 + 显式 ASCII fallback。

    背景：浏览器（尤其是 Chrome）即使拿到 ``filename*=UTF-8''<encoded>``，
    若同时存在 ``filename="<ascii>"``，下载文件名会优先取 ASCII 那个。
    旧实现用 ``name.encode("ascii", "ignore")`` 把中文剥光后只剩括号/下划线，
    最终用户看到 ``()_af172068.zip`` 这种乱码。

    现在调用方必须**显式**传入一个安全的英文 fallback（如 ``softcopy_af172068.zip``），
    不再依赖剥中文的危险逻辑。Modern 客户端会用 UTF-8 那份（拿到中文名），
    旧客户端会拿到清晰的英文 fallback，两全其美。
    """
    fb = ascii_fallback.strip() or "download.zip"
    encoded = quote(filename, safe="")
    return {
        "Content-Disposition": f'attachment; filename="{fb}"; filename*=UTF-8\'\'{encoded}',
    }


@router.post("", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(
    body: JobCreate,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Job:
    job = Job(
        user_id=user["id"],
        company_name=body.company_name,
        uscc=body.uscc,
        established_date=body.established_date,
        quantity=body.quantity,
        keywords=list(body.keywords or []),
        language=body.language,
        template=body.template,
        status="pending",
        progress=0,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    # 立即把任务丢给后台 worker
    worker.submit(job.id)
    return job


_GROUP_ACTIVE = ("pending", "running")
_GROUP_HISTORY = ("success", "failed", "partial")


@router.get("", response_model=JobListResp)
async def list_jobs(
    q: str = Query("", description="按公司名模糊匹配"),
    group: str = Query(
        "all", pattern=r"^(all|active|history)$",
        description="all 全部 / active 进行中(pending+running) / history 已结束(success+failed+partial)",
    ),
    status_filter: str | None = Query(
        None, alias="status",
        pattern=r"^(pending|running|success|failed|partial)$",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> JobListResp:
    where = [Job.user_id == user["id"], Job.is_deleted == False]  # noqa: E712
    if q:
        where.append(Job.company_name.ilike(f"%{q.strip()}%"))
    if status_filter:
        where.append(Job.status == status_filter)
    elif group == "active":
        where.append(Job.status.in_(_GROUP_ACTIVE))
    elif group == "history":
        where.append(Job.status.in_(_GROUP_HISTORY))

    total = (await session.execute(select(func.count()).select_from(Job).where(*where))).scalar() or 0

    rows = (await session.execute(
        select(Job).where(*where).order_by(Job.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )).scalars().all()

    return JobListResp(items=list(rows), total=int(total), page=page, page_size=page_size)


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_job(
    job_id: str,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """软删除：把 is_deleted 置为 True。

    - 任务立即从用户的列表消失（is_deleted=True 被 list_jobs 过滤掉）
    - 还在 pending 队列里的：worker 消费时检测到 is_deleted 会整体跳过，不会真的跑
    - 已经 running 的：后台继续跑完（没有 checkpoint 不好中途 kill），
      但产物不会在列表里出现，用户无感
    """
    job = await session.get(Job, job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    job.is_deleted = True
    await session.commit()


@router.get("/{job_id}", response_model=JobDetailOut)
async def get_job(
    job_id: str,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> Job:
    result = await session.execute(
        select(Job).where(Job.id == job_id).options(selectinload(Job.files))
    )
    job = result.scalar_one_or_none()
    if not job or job.user_id != user["id"] or job.is_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return job


@router.get("/{job_id}/stream")
async def stream_job(
    job_id: str,
    user: dict = Depends(current_user),
) -> StreamingResponse:
    """SSE：每秒推一次任务状态，终态后关闭连接。"""

    async def generator():
        last_payload: str | None = None
        while True:
            async with AsyncSessionLocal() as s:
                result = await s.execute(
                    select(Job).where(Job.id == job_id).options(selectinload(Job.files))
                )
                job = result.scalar_one_or_none()
                if not job or job.user_id != user["id"] or job.is_deleted:
                    yield f"event: error\ndata: {json.dumps({'error': 'not found'})}\n\n"
                    return

                payload = {
                    "id": job.id,
                    "status": job.status,
                    "progress": job.progress,
                    "error": job.error,
                    "files": [
                        {"id": f.id, "idx": f.idx, "software_name": f.software_name,
                         "status": f.status, "error": f.error}
                        for f in sorted(job.files, key=lambda x: x.idx)
                    ],
                }
                s_payload = json.dumps(payload, ensure_ascii=False)
                if s_payload != last_payload:
                    yield f"data: {s_payload}\n\n"
                    last_payload = s_payload

                if job.status in ("success", "failed", "partial"):
                    return

            await asyncio.sleep(1.0)

    return StreamingResponse(generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@router.get("/{job_id}/download")
async def download_job_all(
    job_id: str,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """下载整批 zip（all.zip，包含所有软著的子 zip）。"""
    job = await session.get(Job, job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status not in ("success", "partial"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"job status={job.status}")

    from .config import settings
    zip_path = Path(settings.DATA_DIR) / job_id / "all.zip"
    if not zip_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "zip not generated")

    # 命名：quantity=1 → "公司名_软件名_编号.zip"；
    #       quantity>1 → "公司名_软著N份_编号.zip"
    short_id = job_id[:8]
    company = _safe_part(job.company_name, max_len=40)
    if job.quantity == 1:
        first_file = (await session.execute(
            select(JobFile).where(JobFile.job_id == job_id).order_by(JobFile.idx).limit(1)
        )).scalar_one_or_none()
        soft = _safe_part(first_file.software_name if first_file else "软著", max_len=50)
        nice_name = f"{company}_{soft}_{short_id}.zip"
    else:
        nice_name = f"{company}_软著{job.quantity}份_{short_id}.zip"
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        headers=_attachment_headers(
            nice_name, ascii_fallback=f"softcopy_{short_id}.zip"
        ),
    )


@router.post("/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_whole_job(
    job_id: str,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """重做整个 Job：清空所有子文件记录 + 磁盘产物，按原参数从零再跑一遍。

    典型场景：字体 bug / 模板改动后，存量 job 批量重新生成。
    """
    job = await session.get(Job, job_id)
    if not job or job.user_id != user["id"] or job.is_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status in ("pending", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, "任务还在进行中，请等完成后再重做")
    await pipeline.reset_job_for_retry(job_id)
    worker.submit(job_id)
    return {"ok": True, "job_id": job_id, "status": "retrying"}


@router.post("/{job_id}/files/{file_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_single_file(
    job_id: str,
    file_id: int,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """重做单份失败的软著。立即返回，重做在后台进行。"""
    job = await session.get(Job, job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    jf = await session.get(JobFile, file_id)
    if not jf or jf.job_id != job_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    if jf.status == "generating":
        raise HTTPException(status.HTTP_409_CONFLICT, "该文件正在生成中")
    worker.submit_retry(job_id, file_id)
    return {"ok": True, "job_id": job_id, "file_id": file_id, "status": "retrying"}


@router.get("/{job_id}/files/{file_id}/download")
async def download_single_file(
    job_id: str,
    file_id: int,
    user: dict = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """下载单份软著的 zip。"""
    result = await session.execute(
        select(JobFile).where(JobFile.id == file_id, JobFile.job_id == job_id)
    )
    jf = result.scalar_one_or_none()
    if not jf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found")
    job = await session.get(Job, job_id)
    if not job or job.user_id != user["id"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if jf.status != "done" or not jf.zip_path:
        raise HTTPException(status.HTTP_409_CONFLICT, f"file status={jf.status}")
    if not Path(jf.zip_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "zip missing")

    # 单份命名：公司名_软件名_编号.zip（同一 Job 不同子文件靠 file_id 区分 ASCII fallback）
    short_id = job_id[:8]
    company = _safe_part(job.company_name, max_len=40)
    soft = _safe_part(jf.software_name, max_len=50)
    nice_name = f"{company}_{soft}_{short_id}.zip"
    return FileResponse(
        str(jf.zip_path),
        media_type="application/zip",
        headers=_attachment_headers(
            nice_name, ascii_fallback=f"softcopy_{short_id}_{file_id}.zip"
        ),
    )
