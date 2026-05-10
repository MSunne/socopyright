"""一份 Job 的完整生成流水线。

输入：job_id
流程：
  1. 根据 Job 信息 → generate_specs → N 份 ProjectSpec（Phase 2）
  2. 对每份 spec 并发执行：
     a) 生成 源代码.pdf（会回填 spec.source_lines / source_pdf_pages）
     b) 生成 用户手册.pdf（会回填 spec.manual_pdf_pages）
     c) 上两者完成后 → 生成 申请表.docx / 功能特点.docx（依赖回填后的 spec）
  3. 每份打包成 {软件名}.zip
  4. 整体更新 Job.status / progress

进度粒度：一份软著分 4 阶段（source=35 / manual=45 / app_form=10 / features=10 百分点），
总进度 = 所有软著的加权平均。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
import zipfile
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import settings
from .db import AsyncSessionLocal
from .models import Job, JobFile
from .renderers import application_form, features, source_code, user_manual
from .spec import generate_specs

logger = logging.getLogger(__name__)

# 一般交存：前 30 页 + 后 30 页 = 60 页
GENERAL_DEPOSIT_HEAD = 30
GENERAL_DEPOSIT_TAIL = 30

# 软件名相似度阈值（避免审核员一眼看出"批量水货"）
NAME_DEDUP_RATIO = 0.75
NAME_DEDUP_JACCARD = 0.75
# 通用词剥离：判断"业务名词集合"重合度时把这些词去掉
_GENERIC_WORDS = {
    "系统", "平台", "软件", "工具", "管理", "应用",
    "产品", "解决方案", "服务", "应用程序", "网站",
}
# 纯序号黑名单：以"1/2/3"或"一/二/三"等结尾视为序号化软件名
_NUMERIC_TAIL_RE = re.compile(r".*\s*(\d+|[一二三四五六七八九十百]+)\s*$")


def _name_tokens(name: str) -> set[str]:
    """把软件名拆成 2-3 字业务词（粗粒度，不依赖外部分词库）。"""
    # 去除括号、空格、版本号等标记，保留中文/英文/数字
    name = re.sub(r"[\s（）()\[\]【】《》<>“”\"']+", "", name)
    if not name:
        return set()
    tokens: set[str] = set()
    # 2-gram：滑窗 2 字
    for i in range(len(name) - 1):
        t = name[i : i + 2]
        if t not in _GENERIC_WORDS:
            tokens.add(t)
    # 同时保留通用词剥离后的整名片段（粗粒度业务感）
    bare = name
    for w in _GENERIC_WORDS:
        bare = bare.replace(w, "")
    if bare:
        tokens.add(bare)
    return tokens


def _is_duplicate_name(a: str, b: str) -> bool:
    """两个软件名是否会让审核员视作"同一软件水货"。"""
    if not a or not b or a == b:
        return a == b
    # 1) Ratcliff-Obershelp ratio
    if SequenceMatcher(None, a, b).ratio() >= NAME_DEDUP_RATIO:
        return True
    # 2) 业务名词集合 Jaccard
    ta, tb = _name_tokens(a), _name_tokens(b)
    if ta and tb:
        inter = len(ta & tb)
        union = len(ta | tb)
        if union and inter / union >= NAME_DEDUP_JACCARD:
            return True
    return False


def find_duplicate_indexes(names: list[str]) -> list[int]:
    """返回需要重生的下标（保留首次出现，丢后面的）+ 命中纯序号黑名单的下标。"""
    bad: set[int] = set()
    for i, n in enumerate(names):
        if _NUMERIC_TAIL_RE.match(n or ""):
            bad.add(i)
    for i in range(len(names)):
        if i in bad:
            continue
        for j in range(i + 1, len(names)):
            if j in bad:
                continue
            if _is_duplicate_name(names[i], names[j]):
                bad.add(j)
    return sorted(bad)


def _clip_general_deposit(pdf_path: Path, full_dir: Path) -> int:
    """把 pdf_path 改成"一般交存"格式：前 30 页 + 后 30 页 = 60 页。

    完整 PDF 移到 full_dir 留档（不进 zip）。
    返回截取后的页数（≤ 60）。若原 PDF ≤ 60 页则原地保留，不动。
    """
    if not pdf_path.exists():
        return 0
    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)
    if total <= GENERAL_DEPOSIT_HEAD + GENERAL_DEPOSIT_TAIL:
        return total

    full_dir.mkdir(parents=True, exist_ok=True)
    backup = full_dir / pdf_path.name
    # 先备份完整版
    pdf_path.replace(backup)

    reader = PdfReader(str(backup))
    writer = PdfWriter()
    # 前 30 页
    for i in range(GENERAL_DEPOSIT_HEAD):
        writer.add_page(reader.pages[i])
    # 后 30 页
    for i in range(total - GENERAL_DEPOSIT_TAIL, total):
        writer.add_page(reader.pages[i])
    with open(pdf_path, "wb") as f:
        writer.write(f)
    return GENERAL_DEPOSIT_HEAD + GENERAL_DEPOSIT_TAIL

# 每份软著 4 阶段权重（加起来 100）
STAGE_WEIGHTS = {"source": 35, "manual": 45, "app_form": 10, "features": 10}


async def _update_job(job_id: str, **fields) -> None:
    async with AsyncSessionLocal() as s:
        job = await s.get(Job, job_id)
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await s.commit()


async def _update_job_file(file_id: int, **fields) -> None:
    async with AsyncSessionLocal() as s:
        jf = await s.get(JobFile, file_id)
        if not jf:
            return
        for k, v in fields.items():
            setattr(jf, k, v)
        await s.commit()


class _ProgressTracker:
    """把"每份软著每阶段完成度"映射为 Job 级 0-100 整数 + JobFile 级独立进度，带节流写库。"""

    def __init__(self, total_softwares: int, job_id: str, file_ids: list[int]):
        self.n = total_softwares
        self.job_id = job_id
        self.file_ids = file_ids  # idx → JobFile.id
        self.stages: list[dict[str, float]] = [
            {"source": 0.0, "manual": 0.0, "app_form": 0.0, "features": 0.0}
            for _ in range(total_softwares)
        ]
        self._last_job_pct = -1
        self._last_file_pct = [-1] * total_softwares
        self._last_db_ts = 0.0
        self._lock = asyncio.Lock()
        # 文件 done/failed 后置 True，sync 不再覆写它的 JobFile.progress，
        # 避免把终态写入的 progress=100 重新算回 99
        self._locked = [False] * total_softwares

    def set(self, idx: int, stage: str, value: float) -> None:
        self.stages[idx][stage] = max(0.0, min(1.0, value))

    def lock_file(self, idx: int) -> None:
        """文件已经写入终态（done/failed），告知 tracker 停止覆盖它的 progress。"""
        self._locked[idx] = True

    def _file_pct(self, idx: int) -> int:
        """单份软著 0-100 进度。"""
        st = self.stages[idx]
        total = sum(st[s] * w for s, w in STAGE_WEIGHTS.items())  # 0-100
        return int(min(99, total * 0.99))  # 留 1 点给打 zip

    def overall(self) -> int:
        total = 0.0
        per_software_max = sum(STAGE_WEIGHTS.values())  # 100
        for st in self.stages:
            for stage, w in STAGE_WEIGHTS.items():
                total += st[stage] * w
        pct = total / (self.n * per_software_max) * 100.0
        return int(min(95, pct * 0.95))

    async def sync(self, force: bool = False) -> None:
        """节流后写库：同时更新 Job.progress + 每个 JobFile.progress。"""
        async with self._lock:
            job_pct = self.overall()
            now = time.monotonic()

            # 节流：距离上次 < 1.5s 且 job 进度变化 < 2% 就跳过
            if not force and job_pct == self._last_job_pct and now - self._last_db_ts < 1.5:
                return

            # 批量更新：Job.progress + 所有进度变化的 JobFile
            async with AsyncSessionLocal() as s:
                if job_pct != self._last_job_pct or force:
                    job = await s.get(Job, self.job_id)
                    if job is not None:
                        job.progress = job_pct
                    self._last_job_pct = job_pct
                for i, fid in enumerate(self.file_ids):
                    if self._locked[i]:
                        continue
                    fpct = self._file_pct(i)
                    if fpct != self._last_file_pct[i] or force:
                        jf = await s.get(JobFile, fid)
                        if jf is not None:
                            jf.progress = fpct
                        self._last_file_pct[i] = fpct
                await s.commit()
            self._last_db_ts = now


async def _gen_one_software(
    *,
    job_id: str,
    spec: dict,
    file_id: int,
    output_dir: Path,
    tracker: _ProgressTracker,
    idx: int,
    template: str = "basic",
) -> Path | None:
    """为一份 software 完整生成 4 个文件 + 打 zip。返回 zip 路径（失败返 None）。"""
    name = spec["software_name"]
    soft_dir = output_dir / name
    soft_dir.mkdir(parents=True, exist_ok=True)

    try:
        await _update_job_file(file_id, status="generating")

        async def on_source(p: float) -> None:
            tracker.set(idx, "source", p)
            await tracker.sync()

        async def on_manual(p: float) -> None:
            tracker.set(idx, "manual", p)
            await tracker.sync()

        # 1) 源代码 + 2) 手册 并行（互不依赖）
        async def _source_step():
            await source_code.render(spec, output_path=soft_dir / "源代码.pdf", progress_cb=on_source)
            tracker.set(idx, "source", 1.0)

        async def _manual_step():
            await user_manual.render(
                spec, output_path=soft_dir / "用户手册.pdf",
                template=template, progress_cb=on_manual,
            )
            tracker.set(idx, "manual", 1.0)

        await asyncio.gather(_source_step(), _manual_step())
        await tracker.sync(force=True)

        # 3) 申请表（依赖回填后的 spec）
        await asyncio.to_thread(application_form.render, spec, output_path=soft_dir / "申请表.docx")
        tracker.set(idx, "app_form", 1.0)
        await tracker.sync(force=True)

        # 4) 功能特点
        await asyncio.to_thread(features.render, spec, output_path=soft_dir / "功能特点.docx")
        tracker.set(idx, "features", 1.0)
        await tracker.sync(force=True)

        # 5) 一般交存：把 源代码.pdf / 用户手册.pdf 截取为前 30 + 后 30 = 60 页
        #    申请表 t3[7,2] 已固定写"60页/60页"，此处截取后 zip 内 PDF = 申请表声明 = 60 页
        #    完整 PDF 仍保留在 _full/ 留档，不进 zip
        full_dir = soft_dir / "_full"
        await asyncio.to_thread(_clip_general_deposit, soft_dir / "源代码.pdf", full_dir)
        await asyncio.to_thread(_clip_general_deposit, soft_dir / "用户手册.pdf", full_dir)

        # 打 zip：{软件名}.zip 放在 job 目录里（排除 _full/ 留档目录）
        zip_path = output_dir / f"{name}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(soft_dir.iterdir()):
                if p.is_dir():
                    continue  # 跳过 _full/
                zf.write(p, arcname=f"{name}/{p.name}")

        await _update_job_file(
            file_id, status="done", zip_path=str(zip_path), spec=spec, software_name=name, progress=100
        )
        tracker.lock_file(idx)
        return zip_path

    except Exception as e:
        logger.exception("软著生成失败: %s", name)
        await _update_job_file(file_id, status="failed", error=str(e)[:2000])
        tracker.lock_file(idx)
        return None


async def run_job(job_id: str) -> None:
    """Job 入口。异常不抛出，全部记录到 Job.error/status。"""
    async with AsyncSessionLocal() as s:
        job = (await s.execute(select(Job).where(Job.id == job_id))).scalar_one_or_none()
        if not job:
            logger.error("Job 不存在: %s", job_id)
            return
        job_snapshot = {
            "company_name": job.company_name,
            "uscc": job.uscc,
            "established_date": job.established_date,
            "quantity": job.quantity,
            "keywords": list(job.keywords or []),
            "language": job.language,
            "template": job.template,
        }

    output_dir = Path(settings.DATA_DIR) / job_id
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        await _update_job(job_id, status="running", progress=1, error=None, started_at=datetime.utcnow())

        # 1. 生成 N 份 Spec
        specs = await generate_specs(
            company_name=job_snapshot["company_name"],
            uscc=job_snapshot["uscc"],
            established_date=job_snapshot["established_date"],
            quantity=job_snapshot["quantity"],
            keywords=job_snapshot["keywords"],
            language=job_snapshot["language"],
        )
        if len(specs) != job_snapshot["quantity"]:
            logger.warning("Spec 数量异常：请求 %d 生成 %d", job_snapshot["quantity"], len(specs))

        # 1.5 软件名后置 dedup（O8）：审核员一眼看出"批量水货"必驳，要主动避免
        for attempt in range(3):
            names = [s.get("software_name", "") for s in specs]
            bad_idx = find_duplicate_indexes(names)
            if not bad_idx:
                break
            logger.warning(
                "第 %d 轮检测到雷同/序号化软件名，索引=%s，名称=%s。重生中...",
                attempt + 1, bad_idx, [names[i] for i in bad_idx],
            )
            # 局部重生：把这些 idx 的 spec 换掉。生成新主题时把现有名作为 already_used
            used = [n for i, n in enumerate(names) if i not in bad_idx]
            try:
                new_specs = await generate_specs(
                    company_name=job_snapshot["company_name"],
                    uscc=job_snapshot["uscc"],
                    established_date=job_snapshot["established_date"],
                    quantity=len(bad_idx),
                    keywords=used,  # 把已有名传进去当主题，避免再产出一样的
                    language=job_snapshot["language"],
                )
            except Exception as e:
                logger.warning("dedup 重生 specs 失败：%s", e)
                break
            for slot, ns in zip(bad_idx, new_specs):
                # 保留原 _idx 编号
                ns["_idx"] = specs[slot].get("_idx", slot)
                specs[slot] = ns
        else:
            logger.warning("dedup 经 3 轮仍有雷同名，继续生产但记录 partial 风险")

        # 2. 为每份 spec 建 JobFile 记录
        async with AsyncSessionLocal() as s:
            file_ids: list[int] = []
            for idx, spec in enumerate(specs):
                jf = JobFile(
                    job_id=job_id,
                    idx=idx,
                    software_name=spec["software_name"],
                    spec=spec,
                    status="pending",
                )
                s.add(jf)
            await s.commit()
            # 再查回 ID（按 idx 排序）
            rows = (await s.execute(
                select(JobFile).where(JobFile.job_id == job_id).order_by(JobFile.idx)
            )).scalars().all()
            file_ids = [r.id for r in rows]

        tracker = _ProgressTracker(len(specs), job_id, file_ids)
        await _update_job(job_id, progress=5)

        # 3. 并发生成所有软著（受 llm/browser 全局 Semaphore 限流）
        results = await asyncio.gather(*[
            _gen_one_software(
                job_id=job_id, spec=spec, file_id=fid,
                output_dir=output_dir, tracker=tracker, idx=i,
                template=job_snapshot["template"],
            )
            for i, (spec, fid) in enumerate(zip(specs, file_ids))
        ])
        success_count = sum(1 for r in results if r is not None)

        # 4. 汇总 zip（all.zip，批量下载用）
        all_zip = output_dir / "all.zip"
        with zipfile.ZipFile(all_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for zp in results:
                if zp is None:
                    continue
                zf.write(zp, arcname=zp.name)

        # 5. 终态
        if success_count == len(specs):
            final_status = "success"
        elif success_count == 0:
            final_status = "failed"
        else:
            final_status = "partial"

        await _update_job(
            job_id, status=final_status, progress=100, finished_at=datetime.utcnow()
        )

    except Exception as e:
        logger.exception("Job 整体失败: %s", job_id)
        await _update_job(
            job_id, status="failed", error=str(e)[:4000], finished_at=datetime.utcnow()
        )


async def reset_job_for_retry(job_id: str) -> None:
    """把一个已完成 / 失败的 Job 彻底重置为 pending，清掉所有 JobFile 记录和磁盘产物。

    调用路径：/jobs/{id}/retry API → 本函数清理 → worker.submit(job_id) 重新入队走 run_job。
    """
    async with AsyncSessionLocal() as s:
        job = (await s.execute(
            select(Job).where(Job.id == job_id).options(selectinload(Job.files))
        )).scalar_one_or_none()
        if not job:
            raise ValueError(f"Job {job_id} not found")
        # 删光子文件记录（run_job 会重新创建）
        for f in list(job.files):
            await s.delete(f)
        job.status = "pending"
        job.progress = 0
        job.error = None
        job.started_at = None
        job.finished_at = None
        await s.commit()
    # 清磁盘产物
    out_dir = Path(settings.DATA_DIR) / job_id
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)


async def retry_file(job_id: str, file_id: int) -> None:
    """重做指定的 JobFile：删除旧文件 / 重置状态 → 重新生成 → 重建 all.zip。

    期间 Job 状态临时改回 running；完成后根据所有 JobFile 终态综合判定。
    """
    async with AsyncSessionLocal() as s:
        job = (await s.execute(
            select(Job).where(Job.id == job_id).options(selectinload(Job.files))
        )).scalar_one_or_none()
        if not job:
            raise ValueError(f"Job {job_id} not found")
        target = next((f for f in job.files if f.id == file_id), None)
        if not target:
            raise ValueError(f"JobFile {file_id} not found under Job {job_id}")
        spec = dict(target.spec or {})
        idx = target.idx
        file_ids_sorted = [f.id for f in sorted(job.files, key=lambda x: x.idx)]
        template = job.template
        company_name = job.company_name

    output_dir = Path(settings.DATA_DIR) / job_id
    soft_dir = output_dir / target.software_name
    old_zip = output_dir / f"{target.software_name}.zip"

    # 清理残留
    if soft_dir.exists():
        shutil.rmtree(soft_dir, ignore_errors=True)
    if old_zip.exists():
        old_zip.unlink(missing_ok=True)

    # 重置 JobFile
    await _update_job_file(file_id, status="pending", progress=0, zip_path=None, error=None)
    await _update_job(job_id, status="running", error=None, finished_at=None, started_at=datetime.utcnow())

    # 只要重跑这一份。tracker 按当前所有 files 初始化（其它已完成的标 100%）
    tracker = _ProgressTracker(len(file_ids_sorted), job_id, file_ids_sorted)
    # 预填其它已完成文件的 stages 为 1.0
    async with AsyncSessionLocal() as s:
        files = (await s.execute(
            select(JobFile).where(JobFile.job_id == job_id).order_by(JobFile.idx)
        )).scalars().all()
        for i, f in enumerate(files):
            if f.id == file_id:
                continue
            if f.status == "done":
                for stage in STAGE_WEIGHTS:
                    tracker.set(i, stage, 1.0)
            # 其它状态保持 0

    result = await _gen_one_software(
        job_id=job_id, spec=spec, file_id=file_id,
        output_dir=output_dir, tracker=tracker, idx=idx,
        template=template,
    )

    # 重建 all.zip
    async with AsyncSessionLocal() as s:
        files = (await s.execute(
            select(JobFile).where(JobFile.job_id == job_id).order_by(JobFile.idx)
        )).scalars().all()
        all_zip = output_dir / "all.zip"
        with zipfile.ZipFile(all_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if f.zip_path and Path(f.zip_path).exists():
                    zf.write(f.zip_path, arcname=Path(f.zip_path).name)

        # 综合状态
        statuses = [f.status for f in files]
        if all(st == "done" for st in statuses):
            final_status = "success"
        elif all(st == "failed" for st in statuses):
            final_status = "failed"
        else:
            final_status = "partial"

    await _update_job(
        job_id, status=final_status, progress=100 if result else tracker.overall(),
        finished_at=datetime.utcnow(),
    )
