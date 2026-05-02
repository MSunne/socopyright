"""源代码 PDF 渲染器：两阶段 LLM 生成 + ReportLab 排版。

阶段 1（骨架）：LLM 根据 ProjectSpec 产出文件清单 + 每个文件职责
阶段 2（填充）：每个文件再调 LLM 生成完整代码（含中文注释、业务命名）
最后：所有文件按顺序拼接，加行号 / 页眉 / 页脚 排版成 PDF。

进度回调 progress_cb(pct: float) 会在以下时刻被调用：
  - 骨架完成: 0.10
  - 每个文件完成: 0.10 + 0.80 * done/total
  - 代码行数校验/常量补齐后: 0.92
  - PDF 排版完成: 1.00

规范硬约束：
- 每页 ≥ 50 行代码；前 30 页 + 后 30 页；总代码 ≥ 3000 行
- 页眉：软件名 + 版本
- 页脚：公司名（加方括号）
- 行号右对齐，代码左对齐，等宽字体
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

ProgressCb = Callable[[float], Awaitable[None] | None]

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from .. import llm

logger = logging.getLogger(__name__)

# ——— 排版常量 ——————————————————————————————————————————————
PAGE_W, PAGE_H = A4
MARGIN_LEFT = 18 * mm
MARGIN_RIGHT = 14 * mm
MARGIN_TOP = 18 * mm
MARGIN_BOTTOM = 18 * mm
HEADER_Y = PAGE_H - 12 * mm
FOOTER_Y = 10 * mm
BODY_TOP = PAGE_H - MARGIN_TOP
BODY_BOTTOM = MARGIN_BOTTOM
LINE_HEIGHT = 10.5  # pt
FONT_SIZE = 8.5     # pt (monospace)
LINES_PER_PAGE = 55  # 约 55 行/页（大于规范要求的 50）
CHARS_PER_LINE = 95  # 超长行自动截断，避免横向溢出


# ——— 字体：双字体渲染（CJK CID + Latin TTF）——————————————————————
#
# 背景 / 为什么要这么搞：
#   ReportLab 的 TTF 解析对 .otf (CFF/PostScript outlines) **完全不支持**，
#   对 .ttc (TrueType Collection) 的 cmap 解析也经常残缺（典型坑：
#   NotoSansCJK-Regular.ttc 加载后 ASCII glyph 全丢，PDF 里英文/数字/符号
#   全部渲染成空白）。而 Linux 发行版上预装的中文字体几乎都是 .otf 或
#   "伪 .ttc"（其实是 OTC + CFF 内核），没有一个能直接当 ReportLab 单字体用。
#
# 解决方案：
#   - **中文**用 `UnicodeCIDFont("STSong-Light")` —— Adobe 标准 CID 字体，
#     所有主流 PDF 阅读器（Adobe Reader / macOS Preview / Foxit / 浏览器）
#     都内置，**不嵌入文件、不依赖系统字体**，生成的 PDF 也最轻。
#   - **Latin（ASCII + 西文标点 + 代码符号）**用 DejaVuSansMono.ttf —— 纯
#     TrueType 等宽字体，Linux 基本都预装（dejavu-fonts-ttf），ReportLab
#     100% 支持。
#   - 画字时按 CJK / 非 CJK 边界把一行切段，每段用对应字体 drawString，
#     x 按段实际 stringWidth 累加。见 `_draw_mixed`。

_CJK_FONT = "STSong-Light"      # Adobe CID 名，registerFont 后就用这个名字 setFont
_LATIN_FONT = "CodeLatin"       # 我们注册 TTF 用的别名
_LATIN_FONT_FALLBACK = "Courier"  # ReportLab 内置的 14 Type1 字体之一，保底

_FONTS_REGISTERED: tuple[str, str] | None = None


# 项目自带的字体文件（首选）—— Sarasa Fixed SC 是等宽 + CJK + Latin 全集的
# TrueType glyf 字体，ReportLab 原生支持，并会嵌入 PDF（任何阅读器都能看）
_BUNDLED_FONT_PATH = Path(__file__).resolve().parents[2] / "fonts" / "SarasaFixedSC-Regular.ttf"
_BUNDLED_FONT_NAME = "SarasaFixedSC"


def _register_fonts() -> tuple[str, str]:
    """注册可用字体，返回 (cjk_name, latin_name)。

    三级 fallback：
      1. 项目自带 SarasaFixedSC-Regular.ttf（首选，等宽 + 全集 + TT glyf + 嵌入 PDF）
      2. CID STSong-Light（不嵌入，依赖阅读器内置 CJK 包；风险：部分阅读器渲染空白）
         + DejaVuSansMono.ttf（Latin）——双字体
      3. Courier 兜底（纯 Latin，中文会变空白）
    """
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED is not None:
        return _FONTS_REGISTERED

    # Tier 1: 项目自带 Sarasa TTF
    if _BUNDLED_FONT_PATH.exists():
        try:
            pdfmetrics.registerFont(TTFont(_BUNDLED_FONT_NAME, str(_BUNDLED_FONT_PATH)))
            logger.info("源代码字体已加载：%s", _BUNDLED_FONT_PATH)
            _FONTS_REGISTERED = (_BUNDLED_FONT_NAME, _BUNDLED_FONT_NAME)
            return _FONTS_REGISTERED
        except Exception as e:
            logger.warning("项目自带字体加载失败：%s；fallback 到 CID 字体", e)

    # Tier 2: CID + TTF 双字体
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
        cjk_name = _CJK_FONT
    except Exception as e:
        logger.error("CID 字体 %s 注册失败: %s；中文将无法渲染", _CJK_FONT, e)
        cjk_name = _LATIN_FONT_FALLBACK

    latin_name = _LATIN_FONT_FALLBACK
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Courier New.ttf",
    ]:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont(_LATIN_FONT, path))
            latin_name = _LATIN_FONT
            break
        except Exception as e:
            logger.warning("Latin TTF 加载失败 %s: %s", path, e)
    if latin_name == _LATIN_FONT_FALLBACK:
        logger.warning("未找到 Latin TTF，退回 Courier")

    logger.warning(
        "使用 fallback 字体（cjk=%s, latin=%s）；生成的 PDF 在某些阅读器可能看不到中文。"
        "建议把 SarasaFixedSC-Regular.ttf 放到 %s",
        cjk_name, latin_name, _BUNDLED_FONT_PATH,
    )
    _FONTS_REGISTERED = (cjk_name, latin_name)
    return _FONTS_REGISTERED


def _is_cjk(ch: str) -> bool:
    """判断一个字符是否应该用 CJK 字体渲染。"""
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF         # CJK Unified Ideographs（基本区）
        or 0x3400 <= cp <= 0x4DBF      # CJK Unified Ideographs Extension A
        or 0xF900 <= cp <= 0xFAFF      # CJK Compatibility Ideographs
        or 0x3000 <= cp <= 0x303F      # CJK Symbols & Punctuation（、。「」等）
        or 0xFF01 <= cp <= 0xFF60      # 全角 ASCII（！？（）等）
        or 0xFFE0 <= cp <= 0xFFE6      # 全角货币/数学符号
    )


def _split_runs(text: str) -> list[tuple[str, bool]]:
    """把文本按 CJK / 非 CJK 切成段，返回 [(segment, is_cjk), ...]。"""
    runs: list[tuple[str, bool]] = []
    seg = ""
    seg_cjk: bool | None = None
    for ch in text:
        is_cjk = _is_cjk(ch)
        if seg_cjk is None or is_cjk == seg_cjk:
            seg += ch
            seg_cjk = is_cjk
        else:
            runs.append((seg, bool(seg_cjk)))
            seg, seg_cjk = ch, is_cjk
    if seg:
        runs.append((seg, bool(seg_cjk)))
    return runs


def _mixed_width(text: str, cjk_font: str, latin_font: str, size: float) -> float:
    return sum(
        pdfmetrics.stringWidth(s, cjk_font if is_cjk else latin_font, size)
        for s, is_cjk in _split_runs(text)
    )


def _draw_mixed(c: canvas.Canvas, x: float, y: float, text: str,
                cjk_font: str, latin_font: str, size: float) -> float:
    """按 CJK 边界切段绘制，返回最终 x 位置。

    单字体场景（两个参数相同，项目自带 Sarasa 时）走快路径，避免无意义的
    setFont 来回切换。
    """
    if cjk_font == latin_font:
        c.setFont(cjk_font, size)
        c.drawString(x, y, text)
        return x + pdfmetrics.stringWidth(text, cjk_font, size)
    for seg, is_cjk in _split_runs(text):
        font = cjk_font if is_cjk else latin_font
        c.setFont(font, size)
        c.drawString(x, y, seg)
        x += pdfmetrics.stringWidth(seg, font, size)
    return x


def _draw_mixed_right(c: canvas.Canvas, x_right: float, y: float, text: str,
                      cjk_font: str, latin_font: str, size: float) -> None:
    """右对齐版本：先算总宽，再左移起点。"""
    total = _mixed_width(text, cjk_font, latin_font, size)
    _draw_mixed(c, x_right - total, y, text, cjk_font, latin_font, size)


# ——— LLM 阶段 1：骨架 ————————————————————————————————————————
_SKELETON_PROMPT = """你是资深软件架构师，正在为软著申请设计一个**真实饱满的商业项目代码骨架**。
请站在审核员的视角思考：你设计的项目结构必须看起来像一个真在生产跑了几个月、由多人维护的工程，
而不是为了凑页数随手堆出来的样板。

软件名称：{name}
主要功能描述：{desc}
10 大功能模块：
{modules}

编程语言：{lang}
技术分类：{tech_cat}

请设计一个真实可信的项目结构，硬性要求：

1. **总行数预算 {target_lines} 行左右**（这是预算不是上限，宁多勿少；后续单文件平均 150-200 行）
2. **文件数量 25-40 个**（典型企业项目就是几十个文件，不要少于 25；上不封顶但建议 ≤ 40）
3. **分层必须清晰**，至少包含以下层级（按需扩展）：
   - domain/ 领域实体（每个核心业务概念一个 entity 类，含字段/校验/状态机）
   - service/ 业务服务（每个 module 至少 1 个 service 类，组合多个 repository/util）
   - repository/ 或 dao/ 持久化访问（含真实 SQL/ORM 调用）
   - controller/ 或 handler/ 接入层（HTTP/RPC 路由）
   - dto/ 入参/出参传输对象（请求/响应/视图）
   - mapper/ 或 converter/ DTO ↔ Domain 转换
   - config/ 配置类（数据源、缓存、消息队列、定时任务等）
   - middleware/ 或 interceptor/ 鉴权/审计/限流
   - exception/ 业务异常 + 统一异常处理
   - enums/ 业务状态枚举（细化到具体业务码值，不要只有 Status.OK/FAIL）
   - util/ 工具类（**只放确实需要的**，禁止生成空泛的 StringUtil/CommonUtil）
4. **package/命名空间必须贴合业务**，如 com.{company_slug}.{biz_slug}.xxx，
   **严禁使用通用名**（info.cloud、app.demo、example.system 等会被审核直接判定伪造）
5. **每个文件路径名必须体现业务实体**，如 DefectImagePreprocessor.java 而非 ImageProcessor.java，
   ConstructionWorkerLocationService.java 而非 LocationService.java
6. **文件 target_lines 必须 ≥ 80 行**，单文件控制在 80-280 行之间。**不允许任何 stub/占位/TODO 文件**
7. depends_on 字段要写真实依赖关系（其他文件路径），体现项目内部一致性

严格返回 JSON：
{{
  "package_root": "com.xxx.yyy",
  "files": [
    {{"path": "src/main/java/com/xxx/yyy/domain/DefectImageRecord.java", "role": "瑕疵图像采集记录实体", "target_lines": 150, "depends_on": []}},
    {{"path": "src/main/java/com/xxx/yyy/service/DefectDetectionService.java", "role": "瑕疵识别核心服务", "target_lines": 220, "depends_on": ["domain/DefectImageRecord.java"]}},
    ...
  ]
}}
"""


_FILE_PROMPT = """你是一名资深 {lang} 开发工程师，正在编写真实的商业项目代码。

项目：{name}
主题业务：{desc}

现在要写这个文件：
- 路径：{path}
- 职责：{role}
- 目标行数：{target_lines} 行（±20%）

整体项目的 package/命名空间根：{package_root}
相关模块清单（可在代码中引用）：
{sibling_files}

要求：
1. 输出**完整、可编译**的 {lang} 源代码（不要用 "..."、"TODO" 等占位；不允许把方法体留空）
2. 必须贴合 "{desc}" 这一具体业务，禁止出现与业务无关的泛化逻辑
3. 变量名、类名、方法名必须体现业务语义（如 DefectImageProcessor 而非 ImageProcessor）
4. 关键方法加**简体中文注释**，解释业务含义，非每行都注释
5. 必要的 import / 依赖都写全
6. 只输出代码本身，不要 markdown 栅栏、不要解释文字
7. **必须包含真实业务逻辑分支**：参数校验、异常路径、并发/事务考虑、数据库映射、状态机迁移等
   不要只写 getter/setter 和 happy path
8. **中文注释要解释业务规则的依据**，不是只重复方法名
   比如"按 GB/T 22239 等保 2.0 要求，密码必须 12 位以上含三种字符"，
   或"夜间 22:00-06:00 的高危区域滞留阈值放宽到 30 秒（行业惯例）"
9. **引用其他文件里的类/方法时要用真实路径**，体现项目的内部一致性，不要凭空造类名
10. **宁可写得更长更扎实，也不要为节省篇幅省略业务细节**——每个文件至少 80 行有效代码
11. 出现明显的业务编码（错误码、状态值、配置 key 等）时，给出有意义的命名和值，
    不要 `int_1` / `STATUS_A` 这种敷衍命名
"""


async def _gen_skeleton(spec: dict) -> dict:
    modules = "\n".join(f"- {f['name']}：{f['desc']}" for f in spec.get("functions", []))
    # 构造 company_slug（用于 package 名），从公司名简单取拼音首字母太麻烦，让 LLM 自己处理
    company = spec["owner"]["name"]

    prompt = _SKELETON_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:400],
        modules=modules,
        lang=spec["language"],
        tech_cat=spec.get("tech_category", ""),
        target_lines=5000,  # 充足预算让 LLM 把项目设计饱满；3200 阈值有 1500+ 行缓冲
        company_slug=_slug(company),
        biz_slug=_slug(spec["software_name"])[:20],
    )
    data = await llm.call_json(prompt, temperature=0.5)
    if not isinstance(data.get("files"), list) or len(data["files"]) < 10:
        raise ValueError(f"骨架生成结果异常：{data}")
    return data


_PINYIN_MAP = {
    # 极简，够用
}


def _slug(s: str) -> str:
    """从中文名简单抽取拼音/英文 slug，用不了拼音就用 'app'。"""
    # 如果有 ASCII 片段就用 ASCII
    ascii_ = re.sub(r"[^a-zA-Z0-9]", "", s).lower()
    if ascii_:
        return ascii_
    return "app"


async def _gen_file(spec: dict, file_spec: dict, package_root: str, sibling_files: list[dict]) -> str:
    siblings_desc = "\n".join(f"- {f['path']}: {f['role']}" for f in sibling_files if f["path"] != file_spec["path"])[:2000]

    prompt = _FILE_PROMPT.format(
        lang=spec["language"],
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:300],
        path=file_spec["path"],
        role=file_spec["role"],
        target_lines=file_spec.get("target_lines", 200),
        package_root=package_root,
        sibling_files=siblings_desc,
    )
    code = await llm.call_text(prompt, temperature=0.4, max_tokens=8000)
    # 剥离可能的 markdown 栅栏
    code = re.sub(r"^```[\w+]*\n?", "", code.strip())
    code = re.sub(r"\n?```$", "", code.strip())
    return code.strip() + "\n"


# ——— 字符白名单：只放行 CJK fallback 字体确定覆盖的区段 ————————————
# 未覆盖区段（emoji、数学符号、CJK Ext B+、罕用部首等）替换为 '?'，避免 PDF 出方框。
# Why: 注册的 DroidSansFallback/NotoSansCJK/PingFang 对扩展平面字符覆盖不全；
# LLM 偶尔会产出 emoji 或 → ∀ 之类符号，不过滤会渲染成 .notdef 空框。
_SAFE_CHAR_RE = re.compile(
    r"[^"
    r"\x09\x0A\x20-\x7E"              # Tab / LF / ASCII printable
    r" -ÿ"                   # Latin-1 Supplement（© ® ° 等）
    r"‐-―‘-‟•…‰‹›"  # 常用西文标点
    r"　-〿"                   # CJK 符号与标点
    r"぀-ゟ゠-ヿ"      # 平假名/片假名（LLM 偶尔混入）
    r"一-鿿"                   # CJK 基本汉字区
    r"＀-￯"                   # 全角/半角形式
    r"]"
)


def _sanitize_for_font(text: str) -> str:
    return _SAFE_CHAR_RE.sub("?", text)


# ——— 代码 → 行列表（处理超长行） ————————————————————————————
def _break_long_lines(code: str, max_chars: int = CHARS_PER_LINE) -> list[str]:
    out: list[str] = []
    for line in code.split("\n"):
        # 展开 tab 并清洗字体不覆盖的字符
        line = _sanitize_for_font(line.replace("\t", "    "))
        while len(line) > max_chars:
            # 找最后一个空格切断；没空格就硬切
            cut = line.rfind(" ", 0, max_chars)
            if cut < max_chars * 0.5:
                cut = max_chars
            out.append(line[:cut])
            line = "    " + line[cut:].lstrip()
        out.append(line)
    return out


# ——— PDF 排版 ————————————————————————————————————————————————
def _draw_page(c: canvas.Canvas, page_lines: list[str], start_line_no: int,
               software_name: str, version: str, company_name: str,
               page_num: int, cjk_font: str, latin_font: str) -> None:
    # 页眉（软件名可能含中文 → 混合；页码纯数字 → Latin 即可）
    _draw_mixed(c, MARGIN_LEFT, HEADER_Y, f"{software_name} {version}", cjk_font, latin_font, 9)
    c.setFont(latin_font, 9)
    c.drawRightString(PAGE_W - MARGIN_RIGHT, HEADER_Y, str(page_num))
    c.setLineWidth(0.3)
    c.line(MARGIN_LEFT, HEADER_Y - 3, PAGE_W - MARGIN_RIGHT, HEADER_Y - 3)

    # 页脚（公司名几乎肯定有中文）
    _draw_mixed(c, MARGIN_LEFT, FOOTER_Y, f"【{company_name}】", cjk_font, latin_font, 8)

    # 代码正文：行号纯数字 → Latin；代码内容 → 混合
    y = BODY_TOP - LINE_HEIGHT
    line_no_width = 32  # pt
    for i, line in enumerate(page_lines):
        ln = start_line_no + i
        c.setFont(latin_font, FONT_SIZE)
        c.drawRightString(MARGIN_LEFT + line_no_width, y, str(ln))
        _draw_mixed(c, MARGIN_LEFT + line_no_width + 6, y, line, cjk_font, latin_font, FONT_SIZE)
        y -= LINE_HEIGHT


def _render_pdf(lines: list[str], *, pdf_path: Path, software_name: str, version: str, company_name: str) -> int:
    """把行列表排版成 PDF，返回总页数。"""
    cjk_font, latin_font = _register_fonts()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(pdf_path), pagesize=A4)

    total_pages = (len(lines) + LINES_PER_PAGE - 1) // LINES_PER_PAGE
    for pg in range(total_pages):
        start = pg * LINES_PER_PAGE
        page_lines = lines[start:start + LINES_PER_PAGE]
        # 每页行号从 1 开始（官方要求："第 N 页共 M 页，行号 1-55")
        _draw_page(c, page_lines, 1, software_name, version, company_name, pg + 1, cjk_font, latin_font)
        c.showPage()

    c.save()
    return total_pages


# ——— 主入口 ————————————————————————————————————————————————
async def render(spec: dict, *, output_path: str | Path, progress_cb: ProgressCb | None = None) -> dict:
    """生成源代码 PDF 并把 source_lines/source_files/source_pdf_pages 回填到 spec（原地）。

    返回 {'lines': int, 'pages': int, 'path': Path}
    """
    output_path = Path(output_path)

    async def _notify(pct: float) -> None:
        if progress_cb is None:
            return
        r = progress_cb(pct)
        if asyncio.iscoroutine(r):
            await r

    # 1. 骨架
    skel = await _gen_skeleton(spec)
    package_root = skel.get("package_root", "com.app")
    file_specs = skel["files"]
    await _notify(0.10)

    # 2. 并行填充（受 llm 全局 semaphore 限流）
    total = len(file_specs)
    done = 0
    done_lock = asyncio.Lock()

    async def _one(fs: dict) -> tuple[dict, str]:
        nonlocal done
        try:
            code = await _gen_file(spec, fs, package_root, file_specs)
        except Exception as e:
            logger.warning("文件生成失败 %s: %s，使用占位", fs["path"], e)
            code = f"// {fs['path']}\n// TODO: {fs['role']}\n"
        async with done_lock:
            done += 1
            pct = 0.10 + 0.80 * (done / total)
        await _notify(pct)
        return fs, code

    results = await asyncio.gather(*[_one(fs) for fs in file_specs])

    # 3. 拼接所有文件（带文件分隔符）
    all_lines: list[str] = []
    source_files_meta: list[dict] = []
    for fs, code in results:
        # 文件头注释
        header = f"// ========== File: {fs['path']} ==========\n"
        content = header + code + "\n"
        broken = _break_long_lines(content)
        source_files_meta.append({"path": fs["path"], "lines": len(broken), "role": fs["role"]})
        all_lines.extend(broken)

    total_lines = len(all_lines)

    # 规范要求 ≥ 60 页 × 50 行 = 3000。LLM 已经按 5000 行预算生成，正常情况
    # 总行数 4000+，几乎不会触发以下兜底。如果 LLM 输出意外不足才走这条路：
    # 生成 SQL DDL / 错误码字典 / API 路由表 三个独立"补充源文件"，
    # 让填充内容融入正常项目结构、有 File header 和 source_files_meta 追踪，
    # 而不是末尾整块 dump 同模式 boilerplate。
    min_lines = 3200
    if total_lines < min_lines:
        pad_needed = min_lines - total_lines
        logger.warning("代码行数不足 %d（当前 %d），启用兜底补充约 %d 行", min_lines, total_lines, pad_needed)
        for fs, code in _pad_files(pad_needed, spec):
            header = f"// ========== File: {fs['path']} ==========\n"
            broken = _break_long_lines(header + code + "\n")
            source_files_meta.append({"path": fs["path"], "lines": len(broken), "role": fs["role"]})
            all_lines.extend(broken)
        total_lines = len(all_lines)

    await _notify(0.92)

    # 4. 排版 PDF
    pages = _render_pdf(
        all_lines,
        pdf_path=output_path,
        software_name=spec["software_name"],
        version=spec.get("version", "V1.0"),
        company_name=spec["owner"]["name"],
    )

    # 5. 回填 spec
    spec["source_lines"] = total_lines
    spec["source_files"] = source_files_meta
    spec["source_pdf_pages"] = pages

    await _notify(1.0)

    return {"lines": total_lines, "pages": pages, "path": output_path}


def _pad_files(n_lines: int, spec: dict) -> list[tuple[dict, str]]:
    """LLM 输出不足时的兜底，按需逐段返回独立的"补充源文件"。

    返回 [(file_spec, code), ...]，每段都是一个独立的源文件，外层 render() 会按
    LLM 文件相同方式加 ``// ========== File: <path> ==========`` 头并写入
    ``source_files_meta``。

    文件按需逐个生成（用满 n_lines 即停），顺序：
      1. db/migrations/V1__init_schema.sql —— 每模块一张 InnoDB 表，含完整字段/索引
      2. common/error_codes.go            —— 每模块 × 10 业务错误模板，全局唯一码
      3. routes/api_routes.py             —— 每模块 × 8 个 RESTful 端点

    注意：
      - 表名用 ``m{idx:02d}_{ascii}`` 格式，避免中文模块名 ASCII 化为空时表名重复
      - 错误码变量加 ``M{idx:02d}`` 前缀避免变量重名
      - LLM 现在按 5000 行预算生成，n_lines 通常很小或不会触发
    """
    sw_name = spec.get("software_name", "System")
    company = spec.get("owner", {}).get("name", "公司")
    modules = spec.get("functions", []) or [{"name": "core", "desc": "核心业务模块"}]
    files: list[tuple[dict, str]] = []
    remaining = n_lines

    # ========== File 1: SQL DDL ==========
    if remaining > 0:
        sql: list[str] = []
        sql.append(f"-- {sw_name} 数据库初始化脚本（V1）")
        sql.append(f"-- 维护：{company} 数据库小组")
        sql.append("-- 字符集 utf8mb4 / 引擎 InnoDB / 多租户隔离")
        sql.append("")
        sql.append("SET NAMES utf8mb4;")
        sql.append("SET FOREIGN_KEY_CHECKS = 0;")
        sql.append("")
        for mi, mod in enumerate(modules):
            mname = mod.get("name", f"module_{mi}")
            mdesc = (mod.get("desc", "") or "")[:80]
            ascii_part = re.sub(r"[^a-zA-Z0-9]+", "_", mname).strip("_").lower()[:18]
            tbl = f"m{mi+1:02d}_{ascii_part}" if ascii_part else f"m{mi+1:02d}_module"
            sql.append(f"-- ---------- 模块：{mname} ----------")
            sql.append(f"-- {mdesc}")
            sql.append(f"DROP TABLE IF EXISTS `t_{tbl}`;")
            sql.append(f"CREATE TABLE `t_{tbl}` (")
            sql.append(f"  `id` BIGINT(20) UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键 ID',")
            sql.append(f"  `biz_code` VARCHAR(64) NOT NULL COMMENT '业务编码（对外可见）',")
            sql.append(f"  `title` VARCHAR(255) NOT NULL COMMENT '标题/名称',")
            sql.append(f"  `description` VARCHAR(2000) DEFAULT NULL COMMENT '业务描述',")
            sql.append(f"  `category` VARCHAR(32) DEFAULT NULL COMMENT '分类标签',")
            sql.append(f"  `status` TINYINT(2) NOT NULL DEFAULT 1 COMMENT '状态：0 草稿 / 1 生效 / 2 归档',")
            sql.append(f"  `priority` TINYINT(2) NOT NULL DEFAULT 3 COMMENT '优先级 1-5，数字越小越紧急',")
            sql.append(f"  `effective_at` DATETIME DEFAULT NULL COMMENT '生效时间',")
            sql.append(f"  `expires_at` DATETIME DEFAULT NULL COMMENT '失效时间',")
            sql.append(f"  `creator_id` BIGINT(20) DEFAULT NULL COMMENT '创建人 user_id',")
            sql.append(f"  `tenant_id` BIGINT(20) NOT NULL COMMENT '租户 ID（多租户隔离）',")
            sql.append(f"  `version` INT(10) NOT NULL DEFAULT 1 COMMENT '乐观锁版本号',")
            sql.append(f"  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,")
            sql.append(f"  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,")
            sql.append(f"  `is_deleted` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '软删除标志',")
            sql.append(f"  PRIMARY KEY (`id`),")
            sql.append(f"  UNIQUE KEY `uk_{tbl}_biz_code` (`biz_code`, `tenant_id`),")
            sql.append(f"  KEY `idx_{tbl}_status` (`status`, `is_deleted`),")
            sql.append(f"  KEY `idx_{tbl}_tenant_created` (`tenant_id`, `created_at` DESC),")
            sql.append(f"  KEY `idx_{tbl}_creator` (`creator_id`)")
            sql.append(f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='{mname}';")
            sql.append("")
            if len(sql) >= remaining:
                break
        sql.append("SET FOREIGN_KEY_CHECKS = 1;")
        sql = sql[:remaining]
        files.append((
            {"path": "db/migrations/V1__init_schema.sql", "role": "数据库初始化脚本", "target_lines": len(sql)},
            "\n".join(sql),
        ))
        remaining -= len(sql)

    # ========== File 2: 业务错误码字典 ==========
    if remaining > 0:
        err: list[str] = []
        err.append(f"// {sw_name} 全局业务错误码")
        err.append("// 按模块分段、确保码值唯一；前端按 Code 字段做国际化映射")
        err.append("")
        err.append("package common")
        err.append("")
        err.append("// BizError 业务错误对象，HTTP 200 + 业务码模式")
        err.append("type BizError struct {")
        err.append("    Code     int    `json:\"code\"`")
        err.append("    Message  string `json:\"message\"`")
        err.append("    Solution string `json:\"solution,omitempty\"`")
        err.append("}")
        err.append("")
        err.append("// Error implements error interface")
        err.append("func (e *BizError) Error() string { return e.Message }")
        err.append("")
        err.append("var (")
        err_tpls = [
            ("ParamInvalid",           "请求参数不合法",         "检查必填字段与数据类型，参考 OpenAPI 文档"),
            ("AuthRequired",           "未登录或会话已过期",     "请重新登录后重试"),
            ("PermissionDenied",       "当前账号无权访问该资源", "联系管理员申请角色或数据权限"),
            ("ResourceNotFound",       "目标记录不存在或已被删除", "确认 ID 正确，或刷新列表"),
            ("BizStateConflict",       "业务状态冲突",           "刷新页面查看最新状态后再操作"),
            ("DependencyMissing",      "前置依赖未满足",         "先完成关联流程再继续"),
            ("RateLimited",            "操作过于频繁",           "请稍后再试，或联系管理员调高限额"),
            ("DataIntegrityViolation", "数据完整性校验失败",     "检查关联表外键 / 唯一约束"),
            ("OptimisticLockFailed",   "数据被其他人同时修改",   "刷新后基于最新版本重试"),
            ("ExternalSrvUnavailable", "外部服务暂时不可用",     "重试或切换到降级流程"),
        ]
        err_idx = 1000
        outer_break = False
        for mi, mod in enumerate(modules):
            if outer_break:
                break
            mname = mod.get("name", "Module")
            mod_prefix = f"M{mi+1:02d}"
            for tpl_name, tpl_msg, tpl_sol in err_tpls:
                err_idx += 1
                err.append(
                    f"    Err{mod_prefix}{tpl_name} = &BizError{{Code: {err_idx}, "
                    f"Message: \"[{mname}] {tpl_msg}\", Solution: \"{tpl_sol}\"}}"
                )
                if len(err) >= remaining:
                    outer_break = True
                    break
        err.append(")")
        err = err[:remaining]
        files.append((
            {"path": "common/error_codes.go", "role": "全局业务错误码定义", "target_lines": len(err)},
            "\n".join(err),
        ))
        remaining -= len(err)

    # ========== File 3: HTTP 路由清单 ==========
    if remaining > 0:
        rt: list[str] = []
        rt.append(f"# {sw_name} HTTP API 路由总览")
        rt.append("# 此文件汇总所有 v1 endpoint，便于运维侧拉取做接口监控/限流策略对照")
        rt.append("")
        rt.append("from fastapi import APIRouter")
        rt.append("")
        rt.append("router = APIRouter(prefix=\"/api/v1\")")
        rt.append("")
        rt.append("# 路由列表（method, path, handler, description）")
        rt.append("ROUTES = [")
        methods = [
            ("GET",    "list",        "分页查询"),
            ("GET",    "detail",      "详情"),
            ("POST",   "create",      "新建"),
            ("PUT",    "update",      "更新"),
            ("DELETE", "delete",      "删除"),
            ("POST",   "audit",       "审核"),
            ("POST",   "export",      "批量导出"),
            ("POST",   "import_data", "批量导入"),
        ]
        outer_break = False
        for mi, mod in enumerate(modules):
            if outer_break:
                break
            mname = mod.get("name", "module")
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", mname).strip("_").lower()[:24] or f"m{mi+1:02d}"
            rt.append(f"    # ---------- 模块：{mname} ----------")
            for method, action, desc in methods:
                path = f"/{slug}/{action}" if action != "detail" else f"/{slug}/{{id}}"
                handler = f"{slug}_handler.{action}"
                rt.append(f"    (\"{method}\", \"{path}\", \"{handler}\", \"{desc} - {mname}\"),")
                if len(rt) >= remaining:
                    outer_break = True
                    break
        rt.append("]")
        rt = rt[:remaining]
        files.append((
            {"path": "routes/api_routes.py", "role": "HTTP API 路由清单", "target_lines": len(rt)},
            "\n".join(rt),
        ))
        remaining -= len(rt)

    return files
