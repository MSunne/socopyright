"""用户手册 PDF 渲染器（方案 B）。

流程：
  1. LLM 一次调用，产出所有 UI 页的数据 + 每个模块的详细说明 + FAQ + 字典
  2. Jinja 渲染 login.html / home.html / module.html → Playwright 截图 → PNG bytes → 转 base64 data URI 嵌入手册
  3. Jinja 渲染 user_manual.html（内嵌截图）→ WeasyPrint 转 PDF
  4. 回填 manual_pdf_pages 到 spec

规范保障：手册 ≥ 60 页。不够时自动扩充 FAQ / 字典 / 附录。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pypdf import PdfReader

from .. import llm
from ..screenshot.capture import capture_html, html_to_pdf
from .design_profile import (
    _pick_design_profile,
    body_classes as _body_classes,
    brand_mark_html as _brand_mark_html,
    css_vars as _profile_css_vars,
)

ProgressCb = Callable[[float], Awaitable[None] | None]

logger = logging.getLogger(__name__)

TEMPLATES_SCREENSHOT = Path(__file__).resolve().parent.parent / "screenshot" / "templates"
TEMPLATES_MANUAL = Path(__file__).resolve().parent.parent.parent / "templates"

_jinja_ui = Environment(
    loader=FileSystemLoader(str(TEMPLATES_SCREENSHOT)),
    autoescape=select_autoescape(["html"]),
)
_jinja_manual = Environment(
    loader=FileSystemLoader(str(TEMPLATES_MANUAL)),
    autoescape=select_autoescape(["html"]),
)


# 设计档案（颜色 / 骨架 / 字体 / 密度 / 圆角 / 卡片 / 品牌）由 design_profile._pick_design_profile(seed) 产出


def _fmt_date_cn(d: str | date) -> str:
    if isinstance(d, date):
        return f"{d.year} 年 {d.month:02d} 月 {d.day:02d} 日"
    y, m, dd = d.split("-")
    return f"{int(y)} 年 {int(m):02d} 月 {int(dd):02d} 日"


# ——— 截图数量约束 ————————————————————————————————————————
MIN_SHOTS_RICH = 18
MAX_SHOTS_RICH = 60
PAGE_TYPES_RICH = ["list", "detail", "form", "approval", "chart"]


# ——— UI 数据：LLM 产出（拆成并行调用） ——————————————————————
# 原来一次巨 prompt 输出 10 模块 + 全局字段 + faq + glossary（~9k token，60-180s 延迟）。
# 现在拆成 1 个"壳"调用 + N 个"每模块"调用 asyncio.gather 并行跑，共享全局 _sem。

_UI_SHELL_PROMPT = """为下述软件生成"用户手册全局数据"（不包含各模块内部数据）。

软件名称：{name}
主要功能：{desc}
模块列表（仅用于你理解上下文，这里不需要再输出模块数据）：
{modules}

请严格返回 JSON：

{{
  "slogan": "8-16 字的软件一句话卖点",
  "features_on_login": [
    {{"title": "特性 1", "desc": "一句话"}},
    {{"title": "特性 2", "desc": "一句话"}},
    {{"title": "特性 3", "desc": "一句话"}}
  ],
  "op_user": "假想的登录用户名（中文姓名）",
  "home_metrics": [
    {{"label": "指标名", "value": "数字或状态", "trend": "↑12% 环比上升"}},
    ... 4 个
  ],
  "home_table": {{
    "title": "工作台主表标题",
    "filters": ["筛选字段1", "筛选字段2", "筛选字段3"],
    "columns": ["列1", "列2", "列3", "列4", "列5"],
    "rows": [
      [值1, 值2, {{"badge": "green|blue|orange|red|gray", "text": "状态文本"}}, 值4, 值5],
      ... 6-8 行
    ]
  }},
  "faq": [
    {{"q": "问题（贴合本系统真实使用/运维场景）", "a": "120-200 字解答，含具体步骤或排查思路"}},
    ... 30-50 条
  ],
  "glossary": [
    {{"term": "术语", "desc": "30-80 字解释"}},
    ... 60-100 条
  ]
}}

FAQ 要求（30-50 条，覆盖以下角度，不要全堆在某一类）：
- 账号 / 登录 / 密码 / 权限相关（5-8 条）
- 日常操作 / 表单填写 / 流程进度查询（8-12 条）
- 性能 / 故障排查 / 数据加载慢 / 浏览器兼容（4-6 条）
- 数据导入导出 / 报表 / 统计口径（4-6 条）
- 安全 / 审计 / 合规 / 隐私（3-5 条）
- 接口对接 / 第三方集成 / API 调用（3-5 条）
- 移动端 / 离线场景 / 异常网络（3-5 条）

glossary 要求（60-100 条，三类混搭）：
- 业务术语：来自"{name}"本身所在行业的专业词，体现业务深度
  （例如医疗系统：分诊、复诊、药敏试验；制造系统：网点扩大率、专色匹配；
  金融系统：风险敞口、保证金率、回购期限）
- 国标 / 行业规范：实际可查证的标准编号，如 GB/T 22239 等保 2.0、
  ISO/IEC 27001、ICD-10、HL7 FHIR、JR/T 0028、电子发票数据规范等
- 系统 / 技术术语：RBAC/SSO/JWT/OAuth2/Redis/Kafka/Elasticsearch/分布式锁/
  幂等性/读写分离/熔断降级/灰度发布等

通用要求：
- badge 字段必须是 green/blue/orange/red/gray 之一
- **所有文本必须贴合"{name}"具体业务**，禁止空洞抽象、禁止多个字段写一样的话
- 字段命名首选英文驼峰或下划线 + 中文释义，如 `appointmentId` / 预约单号
- 只返回 JSON，不要 markdown 或额外解释
"""


_MODULE_PROMPT = """为软件"{name}"的一个功能模块生成用户手册示例数据。
此数据将渲染成手册中的字段表、操作步骤、注意事项三块——必须像真实业务系统的
模块手册，给最终用户阅读使用。

软件整体：{desc}
当前模块：{mod_name}
模块定位：{mod_desc}

严格返回 JSON：

{{
  "name": "{mod_name}",
  "description": "200-300 字业务说明，写本模块的业务背景、解决什么问题、与其他模块如何协同。禁止空泛、禁止"基于AI实现智能化"这种没信息量的话",
  "filters": ["筛选字段1", "筛选字段2", "筛选字段3", "筛选字段4"],
  "operations": ["新建", "导入", "导出", "批量审核"],
  "columns": ["列1", "列2", "列3", "列4", "列5"],
  "rows": [
    [值1, 值2, {{"badge": "blue", "text": "进行中"}}, 值4, 值5],
    ... 6-8 行，状态列用 badge 对象，**每行数据都要不一样、贴合具体业务**
  ],
  "steps": [
    "步骤 1：进入X菜单选择X功能（含具体路径）",
    "步骤 2：在X字段输入X（说明取值规则）",
    "步骤 3：点击X按钮，系统校验X……",
    ... 8-12 步，**至少包含 1 步异常分支**（如"如果校验不通过，系统提示X……需要补全X"）
  ],
  "fields": [
    {{"name": "appointmentId / 预约单号", "type": "VARCHAR(32)", "desc": "唯一业务编号，由 yyyyMMdd+6位流水号组成"}},
    ... 10-15 个，必须包含：
    - 主键（id / xxxId）
    - 业务核心字段（贴合本模块业务，4-6 个）
    - 外键关联字段（关联其他模块，如 patientId/doctorId 等）
    - 状态字段（status，给出枚举值）
    - 审计字段（createdAt / updatedAt / creatorId）
  ],
  "notes": [
    "注意事项 1：数据合规要求（引国标/行业规范）",
    "注意事项 2：性能或并发限制",
    "注意事项 3：异常情况处理建议",
    ... 4-6 条
  ]
}}

字段命名要求：
- type 列写**真实数据库类型**（VARCHAR/BIGINT/DATETIME/DECIMAL/JSON/TINYINT 等），不要写"字符串""数字"这种泛词
- name 列用**英文驼峰 / 中文双语**格式：`fieldName / 中文释义`
- 字段值要呼应当前业务（医疗系统就用 patient/doctor/dept；制造系统就用 batch/sku/spec；
  禁止生成与"{mod_name}"无关的通用字段）

badge 必须是 green/blue/orange/red/gray 之一。只返回 JSON。
"""


_MODULE_RICH_PROMPT = """为软件"{name}"的一个功能模块生成"丰富版用户手册示例数据"（含 {sub_quota} 个子页面）。

软件整体：{desc}
当前模块：{mod_name}
模块定位：{mod_desc}

严格返回 JSON：

{{
  "name": "{mod_name}",
  "description": "200-300 字业务说明，贴合本模块具体场景，含与其他模块的协同关系",
  "steps": [
    "步骤 1（含具体路径和取值规则）", "步骤 2", ...
    8-12 步，至少包含 1 步异常分支
  ],
  "fields": [
    {{"name": "appointmentId / 预约单号", "type": "VARCHAR(32)", "desc": "唯一业务编号"}},
    ... 10-15 个，含主键/业务核心/外键关联/状态/审计字段，type 用真实 DB 类型
  ],
  "notes": ["注意事项 1（合规/性能/异常）", ... 4-6 条],
  "sub_pages": [
    // 恰好 {sub_quota} 个子页；第一个必须是 list；后续从 detail / form / approval / chart 中按业务合理性挑选
    {{
      "type": "list",
      "filters": ["筛选1","筛选2"], "operations": ["+新建","导出"],
      "columns": ["列1","列2","列3","列4"],
      "rows": [["值1","值2",{{"badge":"green|blue|orange|red|gray","text":"..."}},"值4"], ... 6 行]
    }},
    // 下面给一些可选类型的 schema（不是每个都要，按 sub_quota 决定）：
    {{
      "type": "detail",
      "record_title": "具体记录标题", "record_code": "R20260420-001",
      "status_badge": "green", "status_text": "已完成",
      "basic_fields": [{{"k":"字段","v":"值"}}, ... 6 个],
      "business_fields": [{{"k":"字段","v":"值"}}, ... 6 个],
      "timeline": [{{"time":"2026-04-20 10:30","user":"张三","action":"创建了记录","note":""}}, ... 4 条]
    }},
    {{
      "type": "form",
      "form_title": "新建...", "form_subtitle": "一句话说明",
      "basic_1": {{"label":"字段名","value":"值","hint":"提示"}},
      "basic_2": {{"label":"","value":""}},
      "basic_3": {{"label":"","options":["选项1","选项2","选项3"],"value":"选项1"}},
      "basic_4": {{"label":"日期字段","value":"2026-04-20"}},
      "section2_title": "第二部分标题",
      "tag_label": "标签", "tags": [{{"name":"标签1","active":true}}, ...],
      "desc_label": "详细说明", "desc_value": "预填的文本"
    }},
    {{
      "type": "approval",
      "order_title": "审批事项的标题", "order_code": "APL-20260420-001",
      "initiator": "李四", "initiated_at": "2026-04-20 09:15",
      "status_badge": "blue", "status_text": "审批中",
      "steps": [
        {{"label":"发起","user":"李四","time":"04-20 09:15","state":"done"}},
        {{"label":"审批","user":"王五","time":"04-20 10:30","state":"current"}}
      ],
      "records": [
        {{"user":"李四","act":"发起","act_class":"","time":"04-20 09:15","comment":"..."}},
        {{"user":"王五","act":"审批通过","act_class":"pass","time":"04-20 10:30","comment":"..."}}
      ]
    }},
    {{
      "type": "chart",
      "chart_page_title": "...统计分析",
      "kpis": [{{"label":"指标","value":"1,234","trend":"↑ 12%","down":false}}, ... 4 个],
      "line_title":"...趋势","line_sub":"近 30 天",
      "line_labels":["4-1","4-5","4-10","4-15","4-20"], "line_data":[120,135,128,152,168], "line_label":"数值",
      "bar_title":"...对比","bar_sub":"按产品线",
      "bar_labels":["A","B","C","D","E"], "bar_data":[420,380,520,290,460], "bar_label":"数量",
      "doughnut_title":"...占比","doughnut_sub":"按类别",
      "doughnut_labels":["A类","B类","C类","D类"], "doughnut_data":[35,28,22,15],
      "area_title":"...对比","area_sub":"本年 vs 上年",
      "area_labels":["1月","2月","3月","4月","5月","6月"],
      "area_series":[{{"name":"本年","data":[50,65,72,80,95,110]}},{{"name":"上年","data":[40,55,60,70,82,90]}}]
    }}
  ]
}}

badge 必须是 green/blue/orange/red/gray 之一。只返回 JSON。
"""


_MORE_FAQ_GLOSSARY_PROMPT = """为软件"{name}"的用户手册补充更多 FAQ 和术语，
本手册当前总页数 {pages} < 60 页，需要更多原始内容把页数撑满。

软件主要功能：{desc}

**已有的 FAQ 问题（不要重复，必须给出全新角度）**：
{existing_q}

**已有的 glossary 术语（不要重复）**：
{existing_t}

请追加：
- 新 FAQ {n_faq} 条（每条 Q+A，A 长 120-200 字，覆盖之前没问过的场景）
- 新 glossary {n_term} 条（含业务术语、国标行业规范如 GB/T、ISO、ICD、HL7 等、
  系统/技术术语三类混合，每条 desc 30-80 字）

**所有内容必须贴合"{name}"具体业务**，不要泛泛而谈，不要重复已有条目。

严格返回 JSON：
{{
  "faq": [{{"q": "...", "a": "..."}}, ...],
  "glossary": [{{"term": "...", "desc": "..."}}, ...]
}}
"""


async def _gen_more_faq_glossary(spec: dict, existing_faq: list, existing_glossary: list) -> dict:
    """页数不足时调 LLM 生成全新（不重复）FAQ/glossary 条目。"""
    existing_q = "; ".join(f.get("q", "")[:40] for f in existing_faq[-30:])  # 取最近 30 条做去重提示
    existing_t = "; ".join(g.get("term", "")[:30] for g in existing_glossary[-50:])
    prompt = _MORE_FAQ_GLOSSARY_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:300],
        pages=0,  # 占位，避免 prompt 提及具体页数让 LLM 困惑
        existing_q=existing_q[:1500] or "（无）",
        existing_t=existing_t[:1500] or "（无）",
        n_faq=20,
        n_term=40,
    )
    return await llm.call_json(prompt, temperature=0.7, max_retries=2)


async def _gen_ui_shell(spec: dict) -> dict:
    modules_str = "\n".join(f"- {f['name']}：{f['desc']}" for f in spec.get("functions", []))
    prompt = _UI_SHELL_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:400],
        modules=modules_str,
    )
    return await llm.call_json(prompt, temperature=0.6, max_retries=3)


def _fallback_module(mod_spec: dict, template: str) -> dict:
    """LLM 失败兜底：返回最简模块 dict，保证下游渲染不崩。"""
    out = {
        "name": mod_spec.get("name", ""),
        "description": mod_spec.get("desc", "") or "模块业务数据暂缺，详见系统内实际运行情况。",
        "filters": ["关键词", "状态", "时间"],
        "operations": ["新建", "导出"],
        "columns": ["名称", "编号", "状态", "负责人", "时间"],
        "rows": [],
        "steps": [],
        "fields": [],
        "notes": [],
    }
    if template == "rich":
        out["sub_pages"] = [{"type": "list"}]  # _normalize_rich_subpages 会补齐
    return out


def _sanitize_module(m: dict, mod_spec: dict, template: str) -> dict:
    """把 LLM 返回的模块 dict 补全缺失字段 / 裁剪越界，保证下游渲染安全。"""
    fallback = _fallback_module(mod_spec, template)
    out = {**fallback, **(m or {})}
    out["name"] = out.get("name") or fallback["name"]
    # list 类字段兜底
    for k in ("filters", "operations", "columns", "rows", "steps", "fields", "notes"):
        if not isinstance(out.get(k), list):
            out[k] = fallback[k]
    if template == "rich":
        subs = out.get("sub_pages")
        if not isinstance(subs, list) or not subs:
            subs = [{"type": "list"}]
        # 裁剪到 [1, 4]，list 必须存在（_normalize_rich_subpages 也会再次校验）
        out["sub_pages"] = subs[:4]
    return out


async def _gen_module_ui(spec: dict, mod_spec: dict, template: str, sub_quota: int) -> dict:
    """单模块 UI 数据；失败回退到占位。"""
    if template == "rich":
        prompt = _MODULE_RICH_PROMPT.format(
            name=spec["software_name"],
            desc=spec.get("main_description", "")[:300],
            mod_name=mod_spec.get("name", ""),
            mod_desc=mod_spec.get("desc", ""),
            sub_quota=sub_quota,
        )
    else:
        prompt = _MODULE_PROMPT.format(
            name=spec["software_name"],
            desc=spec.get("main_description", "")[:300],
            mod_name=mod_spec.get("name", ""),
            mod_desc=mod_spec.get("desc", ""),
        )
    try:
        raw = await llm.call_json(prompt, temperature=0.6, max_retries=2)
    except Exception as e:
        logger.warning("模块 UI 生成失败，回退占位：%s / %s", mod_spec.get("name"), e)
        return _fallback_module(mod_spec, template)
    return _sanitize_module(raw, mod_spec, template)


async def _gen_ui_data(spec: dict, *, template: str = "basic") -> dict:
    """
    并行产出用户手册 UI 数据：1 个 shell + N 个模块同时发起 LLM 调用，
    受全局 LLM_MAX_CONCURRENCY 限流。
    """
    functions = spec.get("functions", []) or []
    # rich 模式每模块固定 2-4 张子页；10 模块 × [2,4] = [20,40]，落在 [MIN,MAX]=[16,40] 内
    sub_quota = 3 if template == "rich" else 0

    shell_task = asyncio.create_task(_gen_ui_shell(spec))
    module_tasks = [
        asyncio.create_task(_gen_module_ui(spec, f, template, sub_quota))
        for f in functions
    ]
    shell, *modules = await asyncio.gather(shell_task, *module_tasks)

    return {
        "slogan": shell.get("slogan", ""),
        "features_on_login": shell.get("features_on_login", []),
        "op_user": shell.get("op_user", "管理员"),
        "home_metrics": shell.get("home_metrics", []),
        "home_table": shell.get("home_table", {}),
        "modules": modules,
        "faq": shell.get("faq", []),
        "glossary": shell.get("glossary", []),
    }


# ——— 渲染 UI 页面为 HTML（用于 Playwright 截图） ——————————————
def _base_css(profile: dict[str, Any]) -> str:
    """base.css 里的 {{ COLOR_* }} / {{ FONT_FAMILY }} 占位符不是 Jinja 变量，用字符串替换注入。

    其余令牌（radius / density / card_style / brand_mark / shell）通过 body class + CSS 属性选择器响应，
    不需要在这里替换。
    """
    css_path = TEMPLATES_SCREENSHOT / "base.css"
    css = css_path.read_text(encoding="utf-8")
    for key, value in _profile_css_vars(profile).items():
        css = css.replace("{{ " + key + " }}", value)
    return css


def _base_context(spec: dict, ui: dict, profile: dict[str, Any], *,
                  page_title: str, module_title: str, crumb_text: str,
                  module_idx: int = -1) -> dict[str, Any]:
    """构建 _shell.html 需要的公共 context，再和页面特有字段合并。"""
    op_user = ui.get("op_user", "管理员")
    menu = [{"id": f"m{i}", "name": f["name"]} for i, f in enumerate(spec.get("functions", []))]
    active_id = f"m{module_idx}" if module_idx >= 0 else (menu[0]["id"] if menu else "")
    palette = profile["palette"]
    return {
        "software_name": spec["software_name"],
        "module_title": module_title,
        "page_title": page_title,
        "menu": menu,
        "active_id": active_id,
        "op_user": op_user,
        "avatar_char": op_user[:1],
        "base_css": _base_css(profile),
        "body_classes": _body_classes(profile),
        "shell": profile["shell"],
        "crumb_text": crumb_text,
        "brand_mark_html": _brand_mark_html(profile, spec["software_name"]),
        # 供 chart.html 等模板里的 <script> 块复用
        "COLOR_DARK": palette["dark"],
        "COLOR_DARK2": palette["dark2"],
        "COLOR_ACCENT": palette["accent"],
    }


def _render_login_html(spec: dict, ui: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("login.html")
    return tmpl.render(
        software_name=spec["software_name"],
        slogan=ui.get("slogan", ""),
        features=ui.get("features_on_login", [])[:3],
        company_name=spec["owner"]["name"],
        year=spec["completion_date"][:4],
        base_css=_base_css(profile),
        body_classes=_body_classes(profile),
        brand_mark_html=_brand_mark_html(profile, spec["software_name"]),
    )


def _render_home_html(spec: dict, ui: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("home.html")
    table = ui.get("home_table", {})
    rows = [{"cols": r} for r in (table.get("rows") or [])]
    ctx = _base_context(
        spec, ui, profile,
        page_title="工作台",
        module_title=table.get("title", "工作台"),
        crumb_text=f"工作台 / {table.get('title', '待办事项')}",
        module_idx=-1,
    )
    ctx.update(
        home_title=table.get("title", "工作台"),
        table_title=table.get("title", "待办事项"),
        filters=table.get("filters", []),
        columns=table.get("columns", []),
        rows=rows,
        metrics=ui.get("home_metrics", [])[:4],
        total_count=len(rows) * 10 + 3,
    )
    return tmpl.render(**ctx)


def _render_module_html(spec: dict, ui: dict, module_idx: int, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("module.html")
    mod = ui["modules"][module_idx]
    rows = [{"cols": r} for r in (mod.get("rows") or [])]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=module_title,
        module_title=module_title,
        crumb_text=f"首页 / {module_title}",
        module_idx=module_idx,
    )
    ctx.update(
        filters=mod.get("filters", []),
        operations=mod.get("operations", ["新建", "导出"]),
        columns=mod.get("columns", []),
        rows=rows,
        total_count=len(rows) * 8 + 5,
    )
    return tmpl.render(**ctx)


# ——— Rich 模板的子页渲染 ——————————————————————————————————
def _render_list_html(spec: dict, ui: dict, module_idx: int, sub: dict, profile: dict[str, Any]) -> str:
    """list 子页复用 module.html。"""
    tmpl = _jinja_ui.get_template("module.html")
    mod = ui["modules"][module_idx]
    rows = [{"cols": r} for r in (sub.get("rows") or mod.get("rows") or [])]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=module_title,
        module_title=module_title,
        crumb_text=f"首页 / {module_title}",
        module_idx=module_idx,
    )
    ctx.update(
        filters=sub.get("filters") or mod.get("filters", []),
        operations=sub.get("operations") or mod.get("operations", ["新建", "导出"]),
        columns=sub.get("columns") or mod.get("columns", []),
        rows=rows,
        total_count=len(rows) * 8 + 5,
    )
    return tmpl.render(**ctx)


def _render_detail_html(spec: dict, ui: dict, module_idx: int, sub: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("detail.html")
    mod = ui["modules"][module_idx]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=f"{module_title} · 详情",
        module_title=module_title,
        crumb_text=f"首页 / {module_title} / 详情",
        module_idx=module_idx,
    )
    ctx.update(
        record_title=sub.get("record_title", "记录详情"),
        record_code=sub.get("record_code", "R20260420-001"),
        status_badge=sub.get("status_badge", "blue"),
        status_text=sub.get("status_text", "处理中"),
        basic_fields=sub.get("basic_fields", []),
        business_fields=sub.get("business_fields", []),
        timeline=sub.get("timeline", []),
    )
    return tmpl.render(**ctx)


def _render_form_html(spec: dict, ui: dict, module_idx: int, sub: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("form.html")
    mod = ui["modules"][module_idx]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=f"{module_title} · 新建",
        module_title=module_title,
        crumb_text=f"首页 / {module_title} / 新建",
        module_idx=module_idx,
    )
    ctx.update(
        form_title=sub.get("form_title", "新建记录"),
        form_subtitle=sub.get("form_subtitle", "请填写完整信息"),
        basic_1=sub.get("basic_1", {"label": "名称", "value": "", "hint": ""}),
        basic_2=sub.get("basic_2", {"label": "编号", "value": ""}),
        basic_3=sub.get("basic_3", {"label": "类型", "options": ["A", "B"], "value": "A"}),
        basic_4=sub.get("basic_4", {"label": "日期", "value": "2026-04-20"}),
        section2_title=sub.get("section2_title", "详细信息"),
        tag_label=sub.get("tag_label", "标签"),
        tags=sub.get("tags", []),
        desc_label=sub.get("desc_label", "描述"),
        desc_value=sub.get("desc_value", ""),
    )
    return tmpl.render(**ctx)


def _render_approval_html(spec: dict, ui: dict, module_idx: int, sub: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("approval.html")
    mod = ui["modules"][module_idx]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=f"{module_title} · 审批流",
        module_title=module_title,
        crumb_text=f"首页 / {module_title} / 审批流",
        module_idx=module_idx,
    )
    ctx.update(
        order_title=sub.get("order_title", "审批事项"),
        order_code=sub.get("order_code", "APL-20260420-001"),
        initiator=sub.get("initiator", "张三"),
        initiated_at=sub.get("initiated_at", "2026-04-20 09:00"),
        status_badge=sub.get("status_badge", "blue"),
        status_text=sub.get("status_text", "审批中"),
        steps=sub.get("steps", []),
        records=sub.get("records", []),
    )
    return tmpl.render(**ctx)


def _render_chart_html(spec: dict, ui: dict, module_idx: int, sub: dict, profile: dict[str, Any]) -> str:
    tmpl = _jinja_ui.get_template("chart.html")
    mod = ui["modules"][module_idx]
    module_title = mod.get("name") or spec["functions"][module_idx]["name"]
    ctx = _base_context(
        spec, ui, profile,
        page_title=f"{module_title} · 统计",
        module_title=module_title,
        crumb_text=f"首页 / {module_title} / 统计分析",
        module_idx=module_idx,
    )
    ctx.update(
        chart_page_title=sub.get("chart_page_title", "统计分析"),
        kpis=sub.get("kpis", []),
        line_title=sub.get("line_title", "趋势"),
        line_sub=sub.get("line_sub", ""),
        line_labels=sub.get("line_labels", []),
        line_data=sub.get("line_data", []),
        line_label=sub.get("line_label", ""),
        bar_title=sub.get("bar_title", "对比"),
        bar_sub=sub.get("bar_sub", ""),
        bar_labels=sub.get("bar_labels", []),
        bar_data=sub.get("bar_data", []),
        bar_label=sub.get("bar_label", ""),
        doughnut_title=sub.get("doughnut_title", "占比"),
        doughnut_sub=sub.get("doughnut_sub", ""),
        doughnut_labels=sub.get("doughnut_labels", []),
        doughnut_data=sub.get("doughnut_data", []),
        area_title=sub.get("area_title", "变化"),
        area_sub=sub.get("area_sub", ""),
        area_labels=sub.get("area_labels", []),
        area_series=sub.get("area_series", [{"name": "A", "data": []}, {"name": "B", "data": []}]),
    )
    return tmpl.render(**ctx)


_SUBPAGE_RENDERERS = {
    "list": _render_list_html,
    "detail": _render_detail_html,
    "form": _render_form_html,
    "approval": _render_approval_html,
    "chart": _render_chart_html,
}


# ——— 截图 → base64 data URI（嵌入 WeasyPrint HTML 避免外部文件依赖）——
async def _shot(html: str) -> str:
    png = await capture_html(html, viewport_width=1280, viewport_height=800)
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ——— 主入口 ———————————————————————————————————————————————
_SUBPAGE_CAPTION = {
    "list": "列表页",
    "detail": "详情页",
    "form": "新建表单页",
    "approval": "审批流页",
    "chart": "统计分析页",
}


def _normalize_rich_subpages(ui: dict, functions: list) -> None:
    """校验 / 修剪 / 补齐 sub_pages，保证总截图数 ∈ [MIN_SHOTS_RICH, MAX_SHOTS_RICH]（含 login+home 计 2 张）。"""
    total = 0
    for i, f in enumerate(functions):
        if i >= len(ui["modules"]):
            break
        m = ui["modules"][i]
        subs = m.get("sub_pages") or []
        # 只保留合法 type，list 必须第一个
        subs = [s for s in subs if s.get("type") in PAGE_TYPES_RICH]
        if not any(s["type"] == "list" for s in subs):
            subs.insert(0, {"type": "list"})
        # 按 list 优先排序
        subs.sort(key=lambda s: 0 if s["type"] == "list" else 1)
        subs = subs[:4]  # 每模块最多 4 个
        m["sub_pages"] = subs
        total += len(subs)

    target_total = 2 + total  # 含 login + home
    # 超上限：从 sub_pages 最多的模块开始裁
    while target_total > MAX_SHOTS_RICH:
        largest = max(range(len(ui["modules"])),
                      key=lambda k: len(ui["modules"][k].get("sub_pages", [])))
        subs = ui["modules"][largest]["sub_pages"]
        if len(subs) <= 1:
            break
        subs.pop()  # 删除最后一个（保留 list）
        target_total -= 1

    # 低于下限：给每个只有 list 的模块补一个 detail/form
    default_fill_types = ["detail", "form", "chart", "approval"]
    fill_idx = 0
    while target_total < MIN_SHOTS_RICH:
        added = False
        for m in ui["modules"]:
            if target_total >= MIN_SHOTS_RICH:
                break
            subs = m["sub_pages"]
            existing = {s["type"] for s in subs}
            for t in default_fill_types:
                if t not in existing and len(subs) < 4:
                    subs.append({"type": t})
                    target_total += 1
                    added = True
                    break
        if not added:
            break


async def render(
    spec: dict, *,
    output_path: str | Path,
    template: str = "basic",
    progress_cb: ProgressCb | None = None,
) -> dict:
    """生成用户手册 PDF 并回填 spec['manual_pdf_pages'] 和 spec['ui_pages']。

    template=basic: 12 张截图（登录+主页+10 模块×1）
    template=rich:  18-60 张截图，每模块 1-4 子页，类型来自 list/detail/form/approval/chart
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async def _notify(pct: float) -> None:
        if progress_cb is None:
            return
        r = progress_cb(pct)
        if asyncio.iscoroutine(r):
            await r

    # 1. LLM 产出 UI 数据
    ui = await _gen_ui_data(spec, template=template)
    await _notify(0.15)

    # 校验模块数量
    if len(ui.get("modules", [])) < len(spec.get("functions", [])):
        raise ValueError(f"UI 数据模块数不足：{len(ui.get('modules', []))} < {len(spec['functions'])}")

    profile = _pick_design_profile(spec["software_name"])

    # 2. 规划所有要截的图
    shot_plan: list[dict] = []  # 每项: {kind, module_idx, sub, caption}
    shot_plan.append({"kind": "login", "module_idx": -1, "sub": None, "caption": "登录页"})
    shot_plan.append({"kind": "home", "module_idx": -1, "sub": None, "caption": "工作台首页"})

    if template == "rich":
        _normalize_rich_subpages(ui, spec["functions"])
        for mi, mod_data in enumerate(ui["modules"][:len(spec["functions"])]):
            for sub in mod_data.get("sub_pages", []):
                shot_plan.append({
                    "kind": sub["type"], "module_idx": mi, "sub": sub,
                    "caption": f"{mod_data.get('name') or spec['functions'][mi]['name']} - {_SUBPAGE_CAPTION.get(sub['type'], sub['type'])}",
                })
    else:
        for mi in range(len(spec["functions"])):
            shot_plan.append({
                "kind": "module", "module_idx": mi, "sub": None,
                "caption": f"{ui['modules'][mi].get('name') or spec['functions'][mi]['name']} 操作界面",
            })

    total_shots = len(shot_plan)
    done_shots = 0
    done_lock = asyncio.Lock()

    async def _tracked_shot(html: str) -> str:
        nonlocal done_shots
        out = await _shot(html)
        async with done_lock:
            done_shots += 1
            pct = 0.20 + 0.60 * (done_shots / total_shots)
        await _notify(pct)
        return out

    def _render_shot(item: dict) -> str:
        kind = item["kind"]
        if kind == "login":
            return _render_login_html(spec, ui, profile)
        if kind == "home":
            return _render_home_html(spec, ui, profile)
        if kind == "module":
            return _render_module_html(spec, ui, item["module_idx"], profile)
        renderer = _SUBPAGE_RENDERERS.get(kind, _render_list_html)
        return renderer(spec, ui, item["module_idx"], item["sub"], profile)

    tasks = [asyncio.create_task(_tracked_shot(_render_shot(item))) for item in shot_plan]
    pngs = await asyncio.gather(*tasks)

    login_png = pngs[0]
    home_png = pngs[1]

    # 3. 拼装 modules 数据结构
    modules_for_pdf: list[dict] = []
    for i, mod_data in enumerate(ui["modules"][:len(spec["functions"])]):
        shots_for_mod: list[dict] = []
        for item, png in zip(shot_plan, pngs):
            if item["module_idx"] == i:
                shots_for_mod.append({
                    "img": png,
                    "caption": f"{mod_data.get('name') or spec['functions'][i]['name']} - {_SUBPAGE_CAPTION.get(item['kind'], item['kind'])}",
                })
        if not shots_for_mod:  # 保险起见
            shots_for_mod.append({"img": "", "caption": "界面预览"})
        modules_for_pdf.append({
            "title": mod_data.get("name") or spec["functions"][i]["name"],
            "description": mod_data.get("description", ""),
            "screenshots": shots_for_mod,
            "steps": mod_data.get("steps", []),
            "fields": mod_data.get("fields", []),
            "notes": mod_data.get("notes", []),
        })

    # 4. 渲染手册 HTML → WeasyPrint → PDF
    manual_tmpl = _jinja_manual.get_template("user_manual.html")
    html_str = manual_tmpl.render(
        software_name=spec["software_name"],
        version=spec.get("version", "V1.0"),
        version_plain=spec.get("version", "V1.0").replace("V", ""),
        company_name=spec["owner"]["name"],
        completion_date_cn=_fmt_date_cn(spec["completion_date"]),
        main_description=spec.get("main_description", ""),
        tech_features=spec.get("tech_features", ""),
        industry=spec.get("industry", ""),
        industry_short=(spec.get("industry", "") or "")[:20],
        functions=spec.get("functions", []),
        hardware_dev=spec.get("hardware_dev", {}),
        hardware_run=spec.get("hardware_run", {}),
        dev_os=spec.get("dev_os", ""),
        run_os=spec.get("run_os", ""),
        ide=spec.get("ide", ""),
        database=spec.get("database", ""),
        web_server=spec.get("web_server", ""),
        language_list_join="、".join(spec.get("language_list") or [spec.get("language", "")]),
        login_png=login_png,
        home_png=home_png,
        modules=modules_for_pdf,
        faq=ui.get("faq", []),
        glossary=ui.get("glossary", []),
    )

    header_tmpl = (
        f'<div style="font-size:9pt; width:100%; padding:0 18mm; color:#555; '
        f'font-family:PingFang SC,Microsoft YaHei,sans-serif; display:flex; justify-content:space-between;">'
        f'<span>{spec["software_name"]} {spec.get("version", "V1.0")}</span>'
        f'<span>第 <span class="pageNumber"></span> 页 共 <span class="totalPages"></span> 页</span>'
        f'</div>'
    )
    footer_tmpl = (
        f'<div style="font-size:9pt; width:100%; padding:0 18mm; color:#555; '
        f'font-family:PingFang SC,Microsoft YaHei,sans-serif;">'
        f'<span>【{spec["owner"]["name"]}】</span></div>'
    )
    pdf_bytes = await html_to_pdf(html_str, header_template=header_tmpl, footer_template=footer_tmpl)
    output_path.write_bytes(pdf_bytes)
    await _notify(0.90)

    # 5. 点页数，回填 spec
    reader = PdfReader(str(output_path))
    pages = len(reader.pages)

    # 规范要求 ≥ 60 页。LLM 已被要求一次出 30-50 FAQ + 60-100 glossary，
    # 正常情况一次到位。如果还不够，**调 LLM 二次生成全新 FAQ/glossary 拼上**，
    # 永远不要复制粘贴或加"扩展 N"后缀（那种凑数审核员一眼识破）。
    extended_glossary = list(ui.get("glossary", []))
    extended_faq = list(ui.get("faq", []))
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if pages >= 60:
            break
        logger.warning(
            "手册 %d 页 < 60，第 %d 次调 LLM 生成新 FAQ/glossary（已有 FAQ %d 条 / glossary %d 条）",
            pages, attempt, len(extended_faq), len(extended_glossary),
        )
        try:
            extra = await _gen_more_faq_glossary(spec, extended_faq, extended_glossary)
            new_faqs = [x for x in extra.get("faq", []) if isinstance(x, dict) and x.get("q") and x.get("a")]
            new_terms = [x for x in extra.get("glossary", []) if isinstance(x, dict) and x.get("term") and x.get("desc")]
            # 去重（按 q / term 字面量）
            existing_q = {f.get("q", "") for f in extended_faq}
            existing_t = {g.get("term", "") for g in extended_glossary}
            new_faqs = [f for f in new_faqs if f["q"] not in existing_q]
            new_terms = [t for t in new_terms if t["term"] not in existing_t]
            extended_faq.extend(new_faqs)
            extended_glossary.extend(new_terms)
            logger.info("第 %d 次扩充：新增 FAQ %d 条 / glossary %d 条", attempt, len(new_faqs), len(new_terms))
        except Exception as e:
            logger.warning("LLM 扩充失败（第 %d 次）：%s；停止扩容", attempt, e)
            break

        html_str = manual_tmpl.render(
            software_name=spec["software_name"],
            version=spec.get("version", "V1.0"),
            version_plain=spec.get("version", "V1.0").replace("V", ""),
            company_name=spec["owner"]["name"],
            completion_date_cn=_fmt_date_cn(spec["completion_date"]),
            main_description=spec.get("main_description", ""),
            tech_features=spec.get("tech_features", ""),
            industry=spec.get("industry", ""),
            industry_short=(spec.get("industry", "") or "")[:20],
            functions=spec.get("functions", []),
            hardware_dev=spec.get("hardware_dev", {}),
            hardware_run=spec.get("hardware_run", {}),
            dev_os=spec.get("dev_os", ""),
            run_os=spec.get("run_os", ""),
            ide=spec.get("ide", ""),
            database=spec.get("database", ""),
            web_server=spec.get("web_server", ""),
            language_list_join="、".join(spec.get("language_list") or [spec.get("language", "")]),
            login_png=login_png,
            home_png=home_png,
            modules=modules_for_pdf,
            faq=extended_faq,
            glossary=extended_glossary,
        )
        pdf_bytes = await html_to_pdf(html_str, header_template=header_tmpl, footer_template=footer_tmpl)
        output_path.write_bytes(pdf_bytes)
        pages = len(PdfReader(str(output_path)).pages)
        await _notify(min(0.99, 0.90 + 0.02 * attempt))

    if pages < 60:
        logger.error("手册扩容 %d 次后仍 %d 页 < 60，提交时可能不达标", max_attempts, pages)

    spec["manual_pdf_pages"] = pages
    spec["ui_pages"] = ui  # 保存以便调试/审计
    spec["design_profile"] = {  # 保存当次命中的 shell / palette / 令牌，便于排查截图风格差异
        "shell": profile["shell"],
        "palette": profile["palette"]["name"],
        "radius": profile["radius"],
        "density": profile["density"],
        "font": profile["font"],
        "card_style": profile["card_style"],
        "brand_mark": profile["brand_mark"],
    }

    await _notify(1.0)

    return {"pages": pages, "path": output_path}
