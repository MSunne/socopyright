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

# ——— M2/M3: 移动端模板枚举 ————————————————————————————————
# APP 15 个页面类型（带 m_app_ 前缀避免与 PC 端 list/detail 等重名）
MOBILE_APP_TYPES = [
    "app_login", "app_home", "app_list", "app_detail", "app_form",
    "app_search", "app_profile", "app_message", "app_scan", "app_chart",
    "app_camera", "app_map", "app_notify", "app_settings", "app_approval",
    "app_calendar", "app_task", "app_filter", "app_workbench", "app_chat",
]  # 共 20 个，最常用 15 个 LLM 会挑用

# 小程序 15 个页面类型
MOBILE_MINIAPP_TYPES = [
    "miniapp_login", "miniapp_home", "miniapp_list", "miniapp_detail", "miniapp_form",
    "miniapp_share", "miniapp_qrcode", "miniapp_pay", "miniapp_coupon", "miniapp_order",
    "miniapp_member", "miniapp_appointment", "miniapp_review", "miniapp_address", "miniapp_customer",
    "miniapp_grid", "miniapp_chart", "miniapp_search",
]  # 共 18 个，常用 15 个 LLM 会挑用

# 移动端视口
MOBILE_VIEWPORT_APP = (390, 844)        # iPhone 14
MOBILE_VIEWPORT_MINIAPP = (375, 667)    # 微信小程序默认


# ——— UI 数据：LLM 产出（拆成并行调用） ——————————————————————
# 原来一次巨 prompt 输出 10 模块 + 全局字段 + faq + glossary（~9k token，60-180s 延迟）。
# 现在拆成 1 个"壳"调用 + N 个"每模块"调用 asyncio.gather 并行跑，共享全局 _sem。

_UI_SHELL_PROMPT = """为下述软件生成"用户手册全局数据"（不包含各模块内部数据）。

软件名称：{name}
主要功能：{desc}
模块列表（仅用于你理解上下文，这里不需要再输出模块数据）：
{modules}

**重要时间约束**：本系统在 {completion_date} 完成开发。所有截图里出现的"业务数据时间字段"
（如订单创建时间、最近更新、操作日志的 time 列等）**必须早于 {completion_date}**，
建议落在 {window_start} 到 {window_end} 这一个月窗口内。
绝对不能出现晚于 {completion_date} 的日期——审核员会觉得"系统都没完工怎么有这之后的数据"。

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
    {{"q": "问题（贴合本系统真实使用/运维场景）", "a": "100-160 字解答，含具体步骤或排查思路"}},
    ... 10-15 条（**精选最常见/最高频的，不要凑数**）
  ],
  "glossary": [
    {{"term": "术语", "desc": "30-80 字解释"}},
    ... 25-35 条（**只收高频/有歧义/容易误解的**，不要把所有名词都罗列）
  ]
}}

FAQ 要求（**精选 10-15 条**，宁少勿滥；选最常被问、最容易踩坑的）：
- 账号 / 登录 / 密码 / 权限（2-3 条）
- 日常核心操作（3-4 条）
- 数据导入导出 / 报表（2-3 条）
- 异常排查 / 性能 / 兼容（2-3 条）
- 安全 / 审计（1-2 条）

glossary 要求（**精选 25-35 条**，三类混搭）：
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

**重要时间约束**：本系统在 {completion_date} 完成开发。所有 rows 里的时间字段、
record_code 里的日期前缀、timeline 时间、initiated_at 等**绝对不能晚于 {completion_date}**，
建议落在 {window_start} 到 {window_end} 之间。

**截图 caption 要求**（screenshot_captions 字段）：
为本模块的每张截图配一句 15-30 字的具体业务化标题（不要泛泛说"操作界面"）。
例如"图：排产看板按工艺路线汇聚的工单视图"，体现具体看到了什么业务场景。

严格返回 JSON：

{{
  "name": "{mod_name}",
  "description": "200-300 字业务说明，写本模块的业务背景、解决什么问题、与其他模块如何协同。禁止空泛、禁止"基于AI实现智能化"这种没信息量的话",
  "screenshot_captions": [
    "图：排产看板按工艺路线汇聚的工单视图",
    "图：紧急插单后系统重算的影响范围分析",
    "图：换单清洗时间在工序切换节点上的累加示例",
    ... 与本模块对应的 1-3 个业务化截图标题，每个 15-30 字
  ],
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
  "business_rules": [
    "业务规则 1：写明触发场景、约束、配置者（一句话+举例，50-120 字）",
    ... 5-8 条贴合本模块业务，例如：
    "排产周期不能跨越法定节假日：系统读取国务院公布的节假日表，遇假期自动顺延；夜班除外",
    "工艺路线变更需经工艺组长二级审批：变更后 30 分钟内同步至所有关联设备；若设备离线则推延至上线后立即同步"
  ],
  "state_flow": {{
    "states": [
      {{"name": "草稿", "code": "DRAFT", "desc": "初始状态，可编辑"}},
      {{"name": "已下达", "code": "RELEASED", "desc": "进入排产队列"}},
      ... 4-6 个状态
    ],
    "transitions": [
      "DRAFT → RELEASED：工艺组长审批通过后由系统自动迁移",
      "RELEASED → IN_PROGRESS：首道工序首件确认后迁移",
      ... 4-8 条迁移规则
    ]
  }},
  "errors": [
    {{"code": "BIZ_30101", "scene": "工艺路线缺失", "fix": "前往工艺库补全后重新提交"}},
    ... 5-8 条业务错误码（贴合本模块），code 用 `BIZ_<5 位数字>`
  ],
  "notes": [
    "注意事项 1：数据合规要求（引国标/行业规范）",
    "注意事项 2：性能或并发限制",
    "注意事项 3：异常情况处理建议",
    ... 4-6 条
  ],

  "mobile_pages": [
    {{"type": "{mobile_type_example}", "caption": "图：业务化截图标题（15-30 字，贴合本模块业务）"}},
    ... 0-3 个移动端页面（mobile_kind={mobile_kind}）
    {mobile_pages_guide}
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

**时间约束**：本系统在 {completion_date} 完成开发。所有 sub_pages 内的时间字段
（initiated_at / timeline.time / record_code 里的日期等）**绝对不能晚于 {completion_date}**，
建议落在 {window_start} 到 {window_end} 之间。

**截图 caption 要求**（screenshot_captions 字段）：
为本模块的 {sub_quota} 张子页配业务化标题，每条 15-30 字，体现具体看到的业务场景。

严格返回 JSON：

{{
  "name": "{mod_name}",
  "description": "200-300 字业务说明，贴合本模块具体场景，含与其他模块的协同关系",
  "screenshot_captions": [
    "图：……（业务化标题，15-30 字）",
    ... {sub_quota} 条
  ],
  "steps": [
    "步骤 1（含具体路径和取值规则）", "步骤 2", ...
    8-12 步，至少包含 1 步异常分支
  ],
  "fields": [
    {{"name": "appointmentId / 预约单号", "type": "VARCHAR(32)", "desc": "唯一业务编号"}},
    ... 10-15 个，含主键/业务核心/外键关联/状态/审计字段，type 用真实 DB 类型
  ],
  "business_rules": [
    "业务规则 1：写明触发场景、约束、配置者（50-120 字，贴合本模块业务）",
    ... 5-8 条
  ],
  "state_flow": {{
    "states": [
      {{"name": "草稿", "code": "DRAFT", "desc": "初始状态"}}, ... 4-6 个
    ],
    "transitions": ["A → B：触发条件", ... 4-8 条]
  }},
  "errors": [
    {{"code": "BIZ_30101", "scene": "...", "fix": "..."}}, ... 5-8 条
  ],
  "notes": ["注意事项 1（合规/性能/异常）", ... 4-6 条],
  "mobile_pages": [
    {{"type": "{mobile_type_example}", "caption": "图：业务化截图标题（15-30 字）"}},
    ... 0-3 个移动端页面（mobile_kind={mobile_kind}）
    {mobile_pages_guide}
  ],
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


# ============================================================
# M2: 移动端壳数据 prompt（mobile_kind != none 时调用）
# ============================================================
_MOBILE_SHELL_PROMPT = """为软件"{name}"的{kind_label}生成全局数据（tabbar、登录页文案、首页 KPI）。

软件主要功能：{desc}
移动端形态：{kind_label}
{kind_hint}

严格返回 JSON：
{{
  "mobile_tabs": [
    {{"name": "首页", "icon": "🏠", "active": true}},
    {{"name": "...", "icon": "📋", "active": false}},
    ... {n_tabs} 个 tab，第一个必须 active=true（首页/工作台）
  ],
  "mobile_login_slogan": "8-16 字一句话卖点（与 PC 端 slogan 可不同，更贴近用户视角）",
  "mobile_home_metrics": [
    {{"label": "指标名（5-8 字）", "value": "数字或状态", "trend": "↑12%"}},
    ... 4 个指标
  ]
}}

要求：
- tab 名称必须贴合"{name}"具体业务（不要"我的/首页/工作"这种通用名）
- icon 用 1 个 emoji 字符
- 只返回 JSON
"""


# ============================================================
# M3: 单模块移动端页面 prompt（追加到 _MODULE_PROMPT 输出里的 mobile_pages 字段）
# 不单独调 LLM，而是把"该模块需要哪几个移动端页面"作为 _MODULE_PROMPT 的一个字段
# ============================================================


_MORE_BUSINESS_RULES_PROMPT = """为软件"{name}"的用户手册的"业务模块详解"章节追加更多业务规则。
本手册当前业务部分页数偏少，需要为各模块补充 **业务规则、数据流转、异常处理** 三类内容
来增加业务深度。**严禁追加 FAQ 或字典**——那种格式会显得生硬。

软件主要功能：{desc}

模块清单及定位：
{modules_str}

为每个模块各产出一段补充内容，要求贴合该模块业务、不重复、有真实业务温度：

严格返回 JSON：
{{
  "supplements": [
    {{
      "module_idx": 0,                    // 对应上面模块清单的序号（0 开始）
      "module_name": "模块名（与上面一致）",
      "extra_rules": [
        "业务规则补充 1：60-150 字，贴合该模块、写明触发场景/约束/责任人/异常分支",
        ... 4-8 条新规则（与原有 business_rules 不重复）
      ],
      "scenarios": [
        {{"title": "场景 1：业务场景标题", "narrative": "100-180 字描述：什么情况下用、谁来操作、操作什么、出什么、对接哪个模块"}},
        ... 2-4 个典型业务场景
      ]
    }},
    ... 一共 {n_modules} 个 module 都要补
  ]
}}

**所有内容必须贴合"{name}"具体业务**，不要泛泛而谈、不要"基于AI智能化"这种空话。
"""


async def _gen_more_business_rules(spec: dict) -> dict:
    """页数不足时调 LLM 生成"业务规则 + 业务场景"补充内容。

    替代旧的 _gen_more_faq_glossary：
      - FAQ 的 Q&A 模板感强，30+ 条堆在一起一眼"AI 写的"
      - 业务规则 + 场景叙述 散落在每个模块内，更像真实手册的样子
    """
    modules = spec.get("functions", [])
    modules_str = "\n".join(f"  {i}. {f['name']}：{f['desc']}" for i, f in enumerate(modules))
    prompt = _MORE_BUSINESS_RULES_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:300],
        modules_str=modules_str,
        n_modules=len(modules),
    )
    return await llm.call_json(prompt, temperature=0.6, max_retries=2)


async def _gen_ui_shell(spec: dict) -> dict:
    from datetime import date, timedelta as _td

    modules_str = "\n".join(f"- {f['name']}：{f['desc']}" for f in spec.get("functions", []))
    # N3：给 LLM 一个明确的时间窗口（completion_date 前 30 天到前 1 天），让截图里的
    # 时间字段不会晚于系统完成日
    cd = spec.get("completion_date", "")
    try:
        end = date.fromisoformat(cd) - _td(days=1)
    except Exception:
        end = date.today() - _td(days=30)
    start = end - _td(days=30)

    prompt = _UI_SHELL_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:400],
        modules=modules_str,
        completion_date=cd or end.isoformat(),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
    )
    return await llm.call_json(prompt, temperature=0.6, max_retries=3)


async def _gen_mobile_shell(spec: dict) -> dict:
    """M2: 移动端壳数据（tabbar + 登录页文案 + 首页 KPI）。mobile_kind=none 时返回空 dict。"""
    mk = spec.get("mobile_kind", "none")
    if mk == "none":
        return {}
    kind_label_map = {"app": "原生 APP", "miniapp": "微信小程序", "both": "APP 与小程序"}
    kind_label = kind_label_map.get(mk, "移动端")
    kind_hint_map = {
        "app": "APP 拥有 5 个底部 tab 是常见结构。tabbar 第一个一般是「首页/工作台」。",
        "miniapp": "小程序固定 4 个 tabbar（受微信平台限制）。第一个一般是「首页」。",
        "both": "如果是 APP 提供 5 个 tab，小程序 4 个；统一给 4 个，前 4 个都能用。",
    }
    n_tabs = 4 if mk in ("miniapp", "both") else 5
    prompt = _MOBILE_SHELL_PROMPT.format(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:400],
        kind_label=kind_label,
        kind_hint=kind_hint_map.get(mk, ""),
        n_tabs=n_tabs,
    )
    try:
        data = await llm.call_json(prompt, temperature=0.6, max_retries=2)
    except Exception as e:
        logger.warning("移动端壳数据生成失败：%s；用兜底", e)
        data = {}
    # 兜底
    if not isinstance(data.get("mobile_tabs"), list) or len(data["mobile_tabs"]) < 2:
        data["mobile_tabs"] = [
            {"name": "首页", "icon": "🏠", "active": True},
            {"name": "订单", "icon": "📋", "active": False},
            {"name": "消息", "icon": "💬", "active": False},
            {"name": "我的", "icon": "👤", "active": False},
        ]
    if not data.get("mobile_login_slogan"):
        data["mobile_login_slogan"] = "让业务办理变得简单"
    if not isinstance(data.get("mobile_home_metrics"), list):
        data["mobile_home_metrics"] = []
    return data


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
        # 新增三类业务字段（让"业务模块详解"章节比 FAQ/字典更厚实）
        "business_rules": [],
        "state_flow": {"states": [], "transitions": []},
        "errors": [],
        # N8: 业务化截图标题
        "screenshot_captions": [],
        # M3: 移动端页面（LLM 在 mobile_kind != none 时输出，否则空）
        "mobile_pages": [],
    }
    if template == "rich":
        out["sub_pages"] = [{"type": "list"}]  # _normalize_rich_subpages 会补齐
    return out


def _sanitize_module(m: dict, mod_spec: dict, template: str, *, mobile_kind: str = "none") -> dict:
    """把 LLM 返回的模块 dict 补全缺失字段 / 裁剪越界，保证下游渲染安全。"""
    fallback = _fallback_module(mod_spec, template)
    out = {**fallback, **(m or {})}
    out["name"] = out.get("name") or fallback["name"]
    # list 类字段兜底
    for k in ("filters", "operations", "columns", "rows", "steps", "fields", "notes",
              "business_rules", "errors", "screenshot_captions", "mobile_pages"):
        if not isinstance(out.get(k), list):
            out[k] = fallback[k]
    # state_flow 必须是 {states:list, transitions:list}
    sf = out.get("state_flow") or {}
    if not isinstance(sf, dict):
        sf = {}
    if not isinstance(sf.get("states"), list):
        sf["states"] = []
    if not isinstance(sf.get("transitions"), list):
        sf["transitions"] = []
    out["state_flow"] = sf
    if template == "rich":
        subs = out.get("sub_pages")
        if not isinstance(subs, list) or not subs:
            subs = [{"type": "list"}]
        # 裁剪到 [1, 4]，list 必须存在（_normalize_rich_subpages 也会再次校验）
        out["sub_pages"] = subs[:4]
    # M3: 校验 mobile_pages（先过滤，再切片到 3）
    all_mobile_types = set(MOBILE_APP_TYPES) | set(MOBILE_MINIAPP_TYPES)
    cleaned_mobile = []
    if mobile_kind != "none":
        for p in out.get("mobile_pages", []):
            if not isinstance(p, dict):
                continue
            t = p.get("type", "")
            if t not in all_mobile_types:
                continue
            # mobile_kind=miniapp 时拒绝 app_*；反之亦然；both 全收
            if mobile_kind == "miniapp" and not t.startswith("miniapp_"):
                continue
            if mobile_kind == "app" and not t.startswith("app_"):
                continue
            cleaned_mobile.append({"type": t, "caption": str(p.get("caption", "") or "").strip()})
    out["mobile_pages"] = cleaned_mobile[:3]  # 单模块最多 3 个
    return out


def _mobile_pages_guide(mobile_kind: str) -> tuple[str, str]:
    """根据 mobile_kind 返回 (例子类型, prompt 引导文本)。"""
    if mobile_kind == "miniapp":
        example = "miniapp_list"
        guide = (
            "type 必须从以下小程序模板选：miniapp_list / miniapp_detail / miniapp_form / "
            "miniapp_qrcode / miniapp_pay / miniapp_coupon / miniapp_order / miniapp_member / "
            "miniapp_appointment / miniapp_review / miniapp_address / miniapp_customer / "
            "miniapp_grid / miniapp_chart / miniapp_search / miniapp_share。"
            "为本模块挑 1-3 个最贴合该业务功能的小程序页面。"
        )
    elif mobile_kind == "app":
        example = "app_list"
        guide = (
            "type 必须从以下 APP 模板选：app_list / app_detail / app_form / app_search / "
            "app_profile / app_message / app_scan / app_chart / app_camera / app_map / "
            "app_notify / app_settings / app_approval / app_calendar / app_task / app_filter / "
            "app_workbench / app_chat。"
            "为本模块挑 1-3 个最贴合该业务功能的 APP 页面。"
        )
    elif mobile_kind == "both":
        example = "app_list"
        guide = (
            "**both 模式**：可以混选 app_* 和 miniapp_* 类型。1-3 个，挑最适合该业务的。"
            "APP 类型见 app_*，小程序类型见 miniapp_*。"
        )
    else:  # none
        example = ""
        guide = "**mobile_kind=none，本字段必须是空数组 []**。"
    return example, guide


async def _gen_module_ui(spec: dict, mod_spec: dict, template: str, sub_quota: int) -> dict:
    """单模块 UI 数据；失败回退到占位。"""
    from datetime import date as _date, timedelta as _td

    cd = spec.get("completion_date", "")
    try:
        end_d = _date.fromisoformat(cd) - _td(days=1)
    except Exception:
        end_d = _date.today() - _td(days=30)
    start_d = end_d - _td(days=30)

    mobile_kind = spec.get("mobile_kind", "none")
    mobile_type_example, mobile_pages_guide = _mobile_pages_guide(mobile_kind)

    common = dict(
        name=spec["software_name"],
        desc=spec.get("main_description", "")[:300],
        mod_name=mod_spec.get("name", ""),
        mod_desc=mod_spec.get("desc", ""),
        completion_date=cd or end_d.isoformat(),
        window_start=start_d.isoformat(),
        window_end=end_d.isoformat(),
        mobile_kind=mobile_kind,
        mobile_type_example=mobile_type_example or "miniapp_list",
        mobile_pages_guide=mobile_pages_guide,
    )
    if template == "rich":
        prompt = _MODULE_RICH_PROMPT.format(sub_quota=sub_quota, **common)
    else:
        prompt = _MODULE_PROMPT.format(**common)
    try:
        raw = await llm.call_json(prompt, temperature=0.6, max_retries=2)
    except Exception as e:
        logger.warning("模块 UI 生成失败，回退占位：%s / %s", mod_spec.get("name"), e)
        return _fallback_module(mod_spec, template)
    return _sanitize_module(raw, mod_spec, template, mobile_kind=mobile_kind)


async def _gen_ui_data(spec: dict, *, template: str = "basic") -> dict:
    """
    并行产出用户手册 UI 数据：1 个 PC shell + 1 个 mobile shell（可选）+ N 个模块同时发起 LLM 调用，
    受全局 LLM_MAX_CONCURRENCY 限流。
    """
    functions = spec.get("functions", []) or []
    # rich 模式每模块固定 2-4 张子页；10 模块 × [2,4] = [20,40]，落在 [MIN,MAX]=[16,40] 内
    sub_quota = 3 if template == "rich" else 0

    shell_task = asyncio.create_task(_gen_ui_shell(spec))
    mobile_shell_task = asyncio.create_task(_gen_mobile_shell(spec))  # M2
    module_tasks = [
        asyncio.create_task(_gen_module_ui(spec, f, template, sub_quota))
        for f in functions
    ]
    shell, mobile_shell, *modules = await asyncio.gather(
        shell_task, mobile_shell_task, *module_tasks
    )

    return {
        "slogan": shell.get("slogan", ""),
        "features_on_login": shell.get("features_on_login", []),
        "op_user": shell.get("op_user", "管理员"),
        "home_metrics": shell.get("home_metrics", []),
        "home_table": shell.get("home_table", {}),
        "modules": modules,
        "faq": shell.get("faq", []),
        "glossary": shell.get("glossary", []),
        # M2: 移动端壳数据
        "mobile_shell": mobile_shell,
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
        # O12: 全局水印（所有 shell 共用），审核员一眼能看到截图右下角公司名
        "company_name": spec["owner"]["name"],
        "year": spec.get("completion_date", "2025-01-01")[:4],
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


# ===========================================================================
# M3: 移动端 mobile_css 注入 + 通用 mobile renderer
# ===========================================================================

_MOBILE_CSS_CACHE: dict[str, str] = {}


def _mobile_css(profile: dict[str, Any]) -> str:
    """读 _mobile.css 并注入 COLOR_* / FONT_FAMILY，同 base.css 的处理方式。"""
    css_path = TEMPLATES_SCREENSHOT / "mobile" / "_mobile.css"
    css = css_path.read_text(encoding="utf-8")
    for k, v in _profile_css_vars(profile).items():
        css = css.replace("{{ " + k + " }}", v)
    return css


def _mobile_context(spec: dict, ui: dict, profile: dict, *, page_type: str,
                    page_title: str = "", module_title: str = "",
                    show_back: bool = True) -> dict[str, Any]:
    """所有 mobile 模板共用的 context 基底。"""
    mshell = ui.get("mobile_shell", {}) or {}
    op_user = ui.get("op_user", "管理员")
    return {
        "software_name": spec["software_name"],
        "page_title": page_title,
        "module_title": module_title,
        "op_user": op_user,
        "avatar_char": op_user[:1] if op_user else "U",
        "mobile_css": _mobile_css(profile),
        "body_classes": _body_classes(profile).replace("shell-", "_unused-"),  # mobile 不用 shell-* class
        "mobile_tabs": mshell.get("mobile_tabs", []),
        "slogan": mshell.get("mobile_login_slogan", ui.get("slogan", "")),
        "home_metrics": mshell.get("mobile_home_metrics", []) or ui.get("home_metrics", []),
        "company_name": spec["owner"]["name"],
        "year": spec.get("completion_date", "2025-01-01")[:4],
        # APP 模板里的 is_login 标志：仅 app_login/miniapp_login 时为 True
        "is_login": page_type in ("app_login", "miniapp_login"),
        "show_back": show_back,
        # 给 chart/share 等用到的 COLOR_* 注入到 Jinja context
        **{k: v for k, v in _profile_css_vars(profile).items()},
    }


def _render_mobile_html(spec: dict, ui: dict, profile: dict, *, page_type: str,
                       module_idx: int = -1, caption: str = "") -> str:
    """通用 mobile renderer：拿到 page_type 直接渲染对应的 mobile/<page_type>.html。

    模块上下文（如果有 module_idx）会注入 mod_data，让模板可访问该模块的业务数据。
    """
    tmpl = _jinja_ui.get_template(f"mobile/{page_type}.html")
    # 默认 page_title 与 module_title
    if module_idx >= 0 and module_idx < len(ui.get("modules", [])):
        mod_data = ui["modules"][module_idx]
        module_title = mod_data.get("name") or spec["functions"][module_idx]["name"]
        page_title = caption.strip() or module_title
    else:
        mod_data = {}
        module_title = ""
        page_title = caption.strip()
    ctx = _mobile_context(spec, ui, profile,
                          page_type=page_type,
                          page_title=page_title,
                          module_title=module_title)
    # 把 mod_data 里的几个常用字段也透传到 context（供 list/detail 等模板若需要访问）
    ctx["mod_data"] = mod_data
    return tmpl.render(**ctx)


# ——— 截图 → base64 data URI（嵌入 WeasyPrint HTML 避免外部文件依赖）——
async def _shot(html: str, *, viewport: tuple[int, int] = (1280, 800)) -> str:
    png = await capture_html(html, viewport_width=viewport[0], viewport_height=viewport[1])
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# ——— 主入口 ———————————————————————————————————————————————
_SUBPAGE_CAPTION = {
    "list": "列表页",
    "detail": "详情页",
    "form": "新建表单页",
    "approval": "审批流页",
    "chart": "统计分析页",
    # APP
    "app_login": "APP 登录", "app_home": "APP 首页", "app_list": "APP 列表",
    "app_detail": "APP 详情", "app_form": "APP 表单", "app_search": "APP 搜索",
    "app_profile": "APP 个人中心", "app_message": "APP 消息", "app_scan": "APP 扫码",
    "app_chart": "APP 数据看板", "app_camera": "APP 拍照", "app_map": "APP 地图打卡",
    "app_notify": "APP 告警", "app_settings": "APP 设置", "app_approval": "APP 审批",
    "app_calendar": "APP 日历", "app_task": "APP 任务", "app_filter": "APP 筛选",
    "app_workbench": "APP 工作台", "app_chat": "APP IM 沟通",
    # 小程序
    "miniapp_login": "小程序授权", "miniapp_home": "小程序首页", "miniapp_list": "小程序列表",
    "miniapp_detail": "小程序详情", "miniapp_form": "小程序表单", "miniapp_share": "小程序分享卡",
    "miniapp_qrcode": "小程序扫码", "miniapp_pay": "小程序支付", "miniapp_coupon": "小程序卡券",
    "miniapp_order": "小程序订单", "miniapp_member": "小程序会员", "miniapp_appointment": "小程序预约",
    "miniapp_review": "小程序评价", "miniapp_address": "小程序地址", "miniapp_customer": "小程序客服",
    "miniapp_grid": "小程序服务方阵", "miniapp_chart": "小程序统计", "miniapp_search": "小程序搜索",
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

    # 2. 规划所有要截的图（移动端在前 + 模块内 mobile→PC 混合）
    mobile_kind = spec.get("mobile_kind", "none")
    shot_plan: list[dict] = []  # 每项: {kind, module_idx, sub, caption, platform, viewport}

    def _platform_of(kind: str) -> str:
        if kind.startswith("app_"):
            return "app"
        if kind.startswith("miniapp_"):
            return "miniapp"
        return "pc"

    def _viewport_of(platform: str) -> tuple[int, int]:
        if platform == "app":
            return MOBILE_VIEWPORT_APP
        if platform == "miniapp":
            return MOBILE_VIEWPORT_MINIAPP
        return (1280, 800)

    def _add_shot(kind: str, mi: int, sub: dict | None, caption: str):
        platform = _platform_of(kind)
        shot_plan.append({
            "kind": kind, "module_idx": mi, "sub": sub,
            "caption": caption, "platform": platform,
            "viewport": _viewport_of(platform),
        })

    # —— 全局 mobile（如果有）—— login + home
    if mobile_kind in ("miniapp", "both"):
        _add_shot("miniapp_login", -1, None, "小程序授权页")
        _add_shot("miniapp_home", -1, None, "小程序首页")
    if mobile_kind in ("app", "both"):
        _add_shot("app_login", -1, None, "APP 登录页")
        _add_shot("app_home", -1, None, "APP 首页")

    # —— 全局 PC —— login + home
    _add_shot("login", -1, None, "PC 登录页")
    _add_shot("home", -1, None, "PC 工作台首页")

    if template == "rich":
        _normalize_rich_subpages(ui, spec["functions"])

    # —— 模块内：mobile → PC ——
    for mi, mod_data in enumerate(ui["modules"][:len(spec["functions"])]):
        mod_title = mod_data.get("name") or spec["functions"][mi]["name"]
        # 移动端
        for mp in (mod_data.get("mobile_pages") or []):
            mp_type = mp.get("type")
            if not mp_type:
                continue
            cap = (mp.get("caption") or f"{mod_title} - {_SUBPAGE_CAPTION.get(mp_type, mp_type)}").strip()
            _add_shot(mp_type, mi, None, cap)
        # PC
        if template == "rich":
            for sub in mod_data.get("sub_pages", []):
                cap = f"{mod_title} - {_SUBPAGE_CAPTION.get(sub['type'], sub['type'])}"
                _add_shot(sub["type"], mi, sub, cap)
        else:
            _add_shot("module", mi, None, f"{mod_title} 操作界面")

    total_shots = len(shot_plan)
    done_shots = 0
    done_lock = asyncio.Lock()

    async def _tracked_shot(html: str, viewport: tuple[int, int]) -> str:
        nonlocal done_shots
        out = await _shot(html, viewport=viewport)
        async with done_lock:
            done_shots += 1
            pct = 0.20 + 0.60 * (done_shots / total_shots)
        await _notify(pct)
        return out

    def _render_shot(item: dict) -> str:
        kind = item["kind"]
        # PC 端
        if kind == "login":
            return _render_login_html(spec, ui, profile)
        if kind == "home":
            return _render_home_html(spec, ui, profile)
        if kind == "module":
            return _render_module_html(spec, ui, item["module_idx"], profile)
        if kind in _SUBPAGE_RENDERERS:
            renderer = _SUBPAGE_RENDERERS[kind]
            return renderer(spec, ui, item["module_idx"], item["sub"], profile)
        # 移动端（app_* / miniapp_*）— 走通用 mobile renderer
        if kind.startswith("app_") or kind.startswith("miniapp_"):
            return _render_mobile_html(spec, ui, profile,
                                        page_type=kind,
                                        module_idx=item["module_idx"],
                                        caption=item["caption"])
        # 兜底
        return _render_list_html(spec, ui, item["module_idx"], item.get("sub"), profile)

    tasks = [asyncio.create_task(_tracked_shot(_render_shot(item), item["viewport"]))
             for item in shot_plan]
    pngs = await asyncio.gather(*tasks)

    # 找出第一个 login / home 的 PNG（用户手册章节 4 和 5 渲染用，优先选移动端版）
    def _first_png(predicate) -> str:
        for item, png in zip(shot_plan, pngs):
            if predicate(item):
                return png
        return ""
    login_png = _first_png(lambda it: it["kind"] in ("app_login", "miniapp_login")) \
                or _first_png(lambda it: it["kind"] == "login")
    home_png = _first_png(lambda it: it["kind"] in ("app_home", "miniapp_home")) \
                or _first_png(lambda it: it["kind"] == "home")

    # 3. 拼装 modules 数据结构（模块内 mobile → PC 顺序）
    modules_for_pdf: list[dict] = []
    for i, mod_data in enumerate(ui["modules"][:len(spec["functions"])]):
        llm_captions = list(mod_data.get("screenshot_captions") or [])
        mod_title = mod_data.get("name") or spec["functions"][i]["name"]
        shots_for_mod: list[dict] = []
        # 先 mobile（按 shot_plan 顺序）
        mobile_local_idx = 0
        for item, png in zip(shot_plan, pngs):
            if item["module_idx"] == i and item["platform"] != "pc":
                cap = item["caption"].strip() or f"{mod_title} - {_SUBPAGE_CAPTION.get(item['kind'], item['kind'])}"
                shots_for_mod.append({"img": png, "caption": cap, "platform": item["platform"]})
                mobile_local_idx += 1
        # 后 PC
        pc_local_idx = 0
        for item, png in zip(shot_plan, pngs):
            if item["module_idx"] == i and item["platform"] == "pc":
                if pc_local_idx < len(llm_captions) and isinstance(llm_captions[pc_local_idx], str) and llm_captions[pc_local_idx].strip():
                    cap = llm_captions[pc_local_idx].strip()
                else:
                    cap = f"{mod_title} - {_SUBPAGE_CAPTION.get(item['kind'], item['kind'])}"
                shots_for_mod.append({"img": png, "caption": cap, "platform": "pc"})
                pc_local_idx += 1
        if not shots_for_mod:
            shots_for_mod.append({"img": "", "caption": "界面预览", "platform": "pc"})
        modules_for_pdf.append({
            "title": mod_title,
            "description": mod_data.get("description", ""),
            "screenshots": shots_for_mod,
            "steps": mod_data.get("steps", []),
            "fields": mod_data.get("fields", []),
            "business_rules": mod_data.get("business_rules", []),
            "state_flow": mod_data.get("state_flow", {"states": [], "transitions": []}),
            "errors": mod_data.get("errors", []),
            "notes": mod_data.get("notes", []),
        })

    # M4: 找移动端 login / home PNG（章节 4 / 5 渲染时分开展示，优先 mobile）
    def _find_shot(predicate):
        for item, png in zip(shot_plan, pngs):
            if predicate(item):
                return png, item.get("platform", "pc")
        return None, None
    mobile_login_png, mobile_login_platform = _find_shot(
        lambda it: it["kind"] in ("app_login", "miniapp_login"))
    mobile_home_png, mobile_home_platform = _find_shot(
        lambda it: it["kind"] in ("app_home", "miniapp_home"))
    # PC login/home 单独取（不带 mobile fallback）
    pc_login_png, _ = _find_shot(lambda it: it["kind"] == "login")
    pc_home_png, _ = _find_shot(lambda it: it["kind"] == "home")

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
        login_png=pc_login_png or login_png,
        home_png=pc_home_png or home_png,
        # M4: 移动端 login/home（仅 mobile_kind != none 时才有值）
        mobile_login_png=mobile_login_png,
        mobile_login_platform=mobile_login_platform,
        mobile_home_png=mobile_home_png,
        mobile_home_platform=mobile_home_platform,
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

    # 规范要求 ≥ 60 页。FAQ/字典已经精简（10-15+25-35 条），如果不够，
    # **追加业务规则与业务场景**到每个模块（章节 6.x.9 / 6.x.10），
    # 而不是再堆 FAQ/字典——那种格式 30+ 条堆一起一眼"AI 写的"。
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        if pages >= 60:
            break
        logger.warning(
            "手册 %d 页 < 60，第 %d 次调 LLM 追加业务规则与场景（不再堆 FAQ/字典）",
            pages, attempt,
        )
        try:
            extra = await _gen_more_business_rules(spec)
            sups = extra.get("supplements", []) if isinstance(extra, dict) else []
            applied = 0
            for sup in sups:
                if not isinstance(sup, dict):
                    continue
                idx = sup.get("module_idx")
                if not isinstance(idx, int) or idx < 0 or idx >= len(modules_for_pdf):
                    # 兜底：按 module_name 模糊匹配
                    name = (sup.get("module_name") or "").strip()
                    idx = next((i for i, m in enumerate(modules_for_pdf) if m["title"].strip() == name), None)
                if idx is None:
                    continue
                m = modules_for_pdf[idx]
                new_rules = [r for r in (sup.get("extra_rules") or []) if isinstance(r, str) and r.strip()]
                new_scs = [
                    s for s in (sup.get("scenarios") or [])
                    if isinstance(s, dict) and s.get("title") and s.get("narrative")
                ]
                # 累加（去重：与已有 extra_rules 字面不重叠）
                existing_rules = set(m.get("extra_rules") or [])
                m["extra_rules"] = list(m.get("extra_rules") or []) + [
                    r for r in new_rules if r not in existing_rules
                ]
                existing_titles = {sc.get("title") for sc in (m.get("scenarios") or [])}
                m["scenarios"] = list(m.get("scenarios") or []) + [
                    s for s in new_scs if s.get("title") not in existing_titles
                ]
                applied += 1
            logger.info("第 %d 次扩充：%d 个模块得到了业务规则/场景补充", attempt, applied)
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
            login_png=pc_login_png or login_png,
            home_png=pc_home_png or home_png,
            mobile_login_png=mobile_login_png,
            mobile_login_platform=mobile_login_platform,
            mobile_home_png=mobile_home_png,
            mobile_home_platform=mobile_home_platform,
            modules=modules_for_pdf,
            faq=ui.get("faq", []),
            glossary=ui.get("glossary", []),
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
