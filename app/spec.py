"""ProjectSpec 生成器：从公司信息 + 数量 + 关键词 → N 份 ProjectSpec JSON。

本阶段只产出业务描述/环境/功能列表等元数据，**不产出源代码和 UI 页**（那些在 Phase 3/4 由各渲染器基于 Spec 生成时回填）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import date, timedelta
from typing import Any

from . import llm
from .region import parse_uscc

logger = logging.getLogger(__name__)

# ——— 软著合规白名单（PROJECT_SPEC 第 9 节 + 国家版权登记规范） ————
TECH_CATEGORIES = {
    "人工智能软件", "物联网软件", "大数据软件", "系统管理软件",
    "行业应用软件", "图形图像软件", "信息安全软件",
}
SOFTWARE_CATEGORIES = {"系统软件", "应用软件", "支撑软件"}
PUBLISH_STATUSES = {"未发表", "已发表"}
DEV_MODES = {"单独开发", "合作开发", "委托开发", "下达任务开发"}
_VERSION_RE = re.compile(r"^V\d+\.\d+(\.\d+)?$")

# 第三轮 M1：移动端形态白名单（none / miniapp / app / both）
# none    = 纯后端管理/工控/中台，没有手机端
# miniapp = C 端轻应用、扫码、会员、点单（微信小程序）
# app     = B 端便携工具（巡检/外勤）或 C 端深度应用（健身/教育）
# both    = 大型 C 端综合服务（电商/银行/出行）—— 极少
MOBILE_KINDS = {"none", "miniapp", "app", "both"}

# USCC 第 1 位 → 著作权人类型 / 证件类型
# 参考 GB 32100 登记管理部门代码：1 机构编制、5 民政、9 工商、Y 其他
USCC_OWNER_KIND = {
    "9": ("企业法人", "统一社会信用代码证书"),
    "5": ("社会团体法人", "社会团体法人登记证书"),
    "1": ("机关法人", "事业单位法人证书"),
    "Y": ("其他", "营业执照"),
}

# ——— 编程语言与合理的技术栈"候选池"——————————————————————————
# 每项给 2-3 个合理候选，spec 生成时按 (software_name + completion_date) 哈希采样，
# 让同一公司多份软著的"运行环境"栏不至于完全雷同（N1）。
import hashlib  # noqa: E402

LANG_STACKS: dict[str, dict[str, list[str] | list]] = {
    "Java": {
        "ide": ["IntelliJ IDEA 2023.3", "IntelliJ IDEA 2024.1", "Eclipse 2023-09"],
        "web_server": ["nginx 1.24.0", "nginx 1.22.1", "Apache HTTP Server 2.4.58"],
        "db": ["MySQL 8.0.34", "MySQL 5.7.42", "PostgreSQL 15.4"],
        "lang_list": ["Java", "HTML", "JavaScript", "SQL"],
    },
    "Go": {
        "ide": ["GoLand 2023.3", "VS Code 1.85", "Vim 9.0"],
        "web_server": ["nginx 1.24.0", "Caddy 2.7.5", "Traefik 2.10"],
        "db": ["MySQL 8.0.34", "PostgreSQL 15.4", "TiDB 7.1"],
        "lang_list": ["Go", "HTML", "JavaScript", "SQL"],
    },
    "Python": {
        "ide": ["PyCharm Professional 2023.3", "VS Code 1.85", "PyCharm Community 2024.1"],
        "web_server": ["nginx 1.22.1", "uWSGI 2.0.23 + nginx", "Gunicorn 21.2 + nginx"],
        "db": ["PostgreSQL 15.4", "PostgreSQL 14.10", "MySQL 8.0.34"],
        "lang_list": ["Python", "HTML", "JavaScript", "SQL"],
    },
    "C++": {
        "ide": ["Visual Studio 2022", "CLion 2023.3", "Visual Studio 2019"],
        "web_server": ["nginx 1.22.1", "nginx 1.24.0", "Apache HTTP Server 2.4.58"],
        "db": ["MySQL 5.7.42", "MySQL 8.0.34", "PostgreSQL 14.10"],
        "lang_list": ["C++", "HTML", "JavaScript", "SQL"],
    },
    "C#": {
        "ide": ["Visual Studio 2022", "Visual Studio 2019", "JetBrains Rider 2023.3"],
        "web_server": ["IIS 10.0", "Kestrel + nginx 1.24", "IIS 8.5"],
        "db": ["SQL Server 2019", "SQL Server 2022", "PostgreSQL 15.4"],
        "lang_list": ["C#", "HTML", "JavaScript", "SQL"],
    },
    "JavaScript": {
        "ide": ["VS Code 1.85", "WebStorm 2023.3", "Sublime Text 4"],
        "web_server": ["nginx 1.24.0", "PM2 5.3 + nginx", "Caddy 2.7.5"],
        "db": ["MongoDB 6.0.12", "MongoDB 7.0.4", "MySQL 8.0.34"],
        "lang_list": ["JavaScript", "HTML", "CSS"],
    },
    "TypeScript": {
        "ide": ["VS Code 1.85", "WebStorm 2023.3", "VS Code 1.86 Insiders"],
        "web_server": ["nginx 1.24.0", "nginx 1.22.1", "Caddy 2.7.5"],
        "db": ["PostgreSQL 15.4", "PostgreSQL 14.10", "MongoDB 7.0.4"],
        "lang_list": ["TypeScript", "HTML", "CSS"],
    },
}

_DEFAULT_LANG_POOL = ["Java", "Go", "Python", "C++"]


# CPU 主频 / 内存 / 硬盘 候选，用于打散多份软著的硬件清单（N1）
_CPU_DEV_POOL = ["2.6 GHz", "2.8 GHz", "3.0 GHz", "3.2 GHz", "3.6 GHz"]
_RAM_DEV_HEAVY = ["16 GB", "16 GB", "32 GB", "32 GB"]   # heavy 语言（C++/Java/C#）
_RAM_DEV_LIGHT = ["8 GB", "16 GB", "16 GB", "32 GB"]
_DISK_DEV_POOL = ["256 GB SSD", "512 GB SSD", "512 GB NVMe", "1 TB NVMe"]
_CPU_RUN_POOL = ["双核 2.0 GHz", "双核 2.4 GHz", "四核 2.0 GHz", "四核 2.4 GHz"]
_RAM_RUN_POOL = ["4 GB", "8 GB", "8 GB", "16 GB"]
_DISK_RUN_POOL = ["200 GB", "500 GB", "1 TB"]
_DEV_OS_POOL = [
    "Windows 11 23H2",
    "Windows 10 22H2",
    "macOS Sonoma 14.4",
    "Ubuntu 22.04 LTS",
]
_RUN_OS_POOL = [
    "Windows Server 2019",
    "Windows Server 2022",
    "Ubuntu 22.04 LTS Server",
    "CentOS 7.9",
]


def _seeded_rng(*parts: str) -> random.Random:
    """根据传入字符串拼接做 MD5 → 稳定的 Random，保证同输入每次结果一致。"""
    seed = int(hashlib.md5("|".join(parts).encode("utf-8")).hexdigest(), 16)
    return random.Random(seed)


def _pick_stack(language: str, software_name: str, completion_date: str) -> dict[str, Any]:
    """在 LANG_STACKS 候选池里采样，多份同语言软著之间至少 1 个组件不同。"""
    pool = LANG_STACKS.get(language, LANG_STACKS["Go"])
    rng = _seeded_rng(software_name, completion_date, language)
    return {
        "ide": rng.choice(pool["ide"]) if isinstance(pool["ide"], list) else pool["ide"],
        "web_server": rng.choice(pool["web_server"]) if isinstance(pool["web_server"], list) else pool["web_server"],
        "db": rng.choice(pool["db"]) if isinstance(pool["db"], list) else pool["db"],
        "lang_list": list(pool["lang_list"]),
    }


def _pick_hardware(language: str, software_name: str, completion_date: str) -> tuple[dict, dict, str, str]:
    """采样开发/运行硬件 + dev_os / run_os。同一 software_name + completion_date 稳定。"""
    rng = _seeded_rng(software_name, completion_date, "hw")
    heavy = language in ("C++", "Java", "C#")
    hw_dev = {
        "cpu": rng.choice(_CPU_DEV_POOL),
        "ram": rng.choice(_RAM_DEV_HEAVY if heavy else _RAM_DEV_LIGHT),
        "disk": rng.choice(_DISK_DEV_POOL),
    }
    hw_run = {
        "cpu": rng.choice(_CPU_RUN_POOL),
        "ram": rng.choice(_RAM_RUN_POOL),
        "disk": rng.choice(_DISK_RUN_POOL),
    }
    dev_os = rng.choice(_DEV_OS_POOL)
    run_os = rng.choice(_RUN_OS_POOL)
    return hw_dev, hw_run, dev_os, run_os


def _random_completion_date(established: date | None = None,
                            days_min: int = 30, days_max: int = 180) -> str:
    """完成日期：当前往前推 1-6 个月随机，并尽量保证 ≥ established+30 且 ≤ today-1。

    边缘情况——公司刚成立 < 60 天：
      floor (established+30) 可能 > ceiling (today-1)。此时无论怎么选都至少
      违反一个约束。优先保护 ceiling（不晚于昨天），返回 today-1，但**记 warning**，
      由调用方酌情拦截或在前端提示"新成立公司不建议申请软著"。
    """
    today = date.today()
    ceiling = today - timedelta(days=1)
    delta = random.randint(days_min, days_max)
    candidate = today - timedelta(days=delta)

    if established is not None:
        floor = established + timedelta(days=30)
        if floor > ceiling:
            logger.warning(
                "公司成立时间过短（%s，距今 %d 天），无法生成合规 completion_date，回落到 %s",
                established.isoformat(), (today - established).days, ceiling.isoformat(),
            )
            return ceiling.isoformat()
        if candidate < floor:
            candidate = floor

    if candidate > ceiling:
        candidate = ceiling
    return candidate.isoformat()


def _normalize_enums(spec: dict) -> dict:
    """对会被审核员检查的枚举字段做白名单兜底。LLM 抖出非法值时强制替换。"""
    tc = (spec.get("tech_category") or "").strip()
    if tc not in TECH_CATEGORIES:
        logger.warning("tech_category=%r 不在白名单，兜底为 行业应用软件", tc)
        spec["tech_category"] = "行业应用软件"

    sc = (spec.get("software_category") or "").strip()
    if sc not in SOFTWARE_CATEGORIES:
        spec["software_category"] = "应用软件"

    ver = (spec.get("version") or "").strip()
    if not _VERSION_RE.match(ver):
        spec["version"] = "V1.0"

    ps = (spec.get("publish_status") or "").strip()
    if ps not in PUBLISH_STATUSES:
        spec["publish_status"] = "未发表"

    # 申请表勾选项默认值（O7 申请表渲染时直接读这些字段）
    spec.setdefault("is_original", True)        # 原创/修改：默认原创
    spec.setdefault("dev_mode", "单独开发")
    if spec["dev_mode"] not in DEV_MODES:
        spec["dev_mode"] = "单独开发"

    # M1: 移动端形态白名单兜底
    mk = (spec.get("mobile_kind") or "").strip().lower()
    if mk not in MOBILE_KINDS:
        logger.warning("mobile_kind=%r 不在白名单，兜底为 none", mk)
        spec["mobile_kind"] = "none"
    else:
        spec["mobile_kind"] = mk
    spec.setdefault("mobile_kind_reason", "")
    return spec


def _owner_kind_by_uscc(uscc: str) -> tuple[str, str]:
    """USCC 第 1 位 → (owner.type, cert_type)。未知则按企业法人兜底。"""
    if not uscc:
        return ("企业法人", "统一社会信用代码证书")
    return USCC_OWNER_KIND.get(uscc[0].upper(), ("企业法人", "统一社会信用代码证书"))


def _hardware_by_lang(language: str) -> tuple[dict, dict]:
    """按语言给出合理的开发/运行硬件要求。"""
    heavy = language in ("C++", "Java", "C#")
    hw_dev = {"cpu": "3GHz+", "ram": "16G+" if heavy else "8G+", "disk": "200G+"}
    hw_run = {"cpu": "双核2GHz+", "ram": "8G+", "disk": "200G+"}
    return hw_dev, hw_run


# ——— Prompt 片段 ——————————————————————————————————————————
_TOPICS_PROMPT = """你是软著选题顾问。根据企业基本信息，生成 {need} 个具体的软件项目主题。

企业名称：{company}
所在地：{province} {city}
已指定主题：{given}

要求：
1. 每个主题必须贴合该企业的核心业务领域（从企业名称判断主营行业）
2. 不同主题之间要覆盖不同子方向，避免雷同
3. 主题要具体可落地（如"XX缺陷智能检测平台"而非"管理系统"）
4. 主题命名格式参考："[企业简称/行业][具体业务][系统/平台/软件]"
5. 与已指定主题避免重复

严格返回 JSON，形如：
{{"topics": ["主题1", "主题2", "主题3"]}}
"""


_SPEC_PROMPT = """为下述软件生成完整的软著登记元数据。

软件名称：{topic}
所属企业：{company}
企业所在地：{province} {city}
主编程语言：{language}

你需要严格返回以下结构的 JSON（所有字段都要填，不得省略）：

{{
  "software_name": "{topic}",
  "software_abbr": "",
  "version": "V1.0",
  "software_category": "应用软件",
  "tech_category": "选一个最贴合的：人工智能软件 / 物联网软件 / 大数据软件 / 系统管理软件 / 行业应用软件 / 图形图像软件 / 信息安全软件",

  "purpose": "1-2 句话，说明软件要解决的核心问题",
  "industry": "具体面向的行业和领域，1 句话",
  "main_description": "300-450 字，**严格分 3 段，段间用单个换行符 \\n\\n 分隔**：\\n\\n第 1 段（80-120 字）：业务背景，写所在行业当前的现实痛点 / 业务诉求 / 监管或客户压力。**禁止**以'该平台/该系统/该软件/本系统'开头，要从业务场景说起。\\n\\n第 2 段（120-180 字）：核心方法，写本软件用了什么具体技术、解决了什么具体问题、与现有方案的差异点。要带 2-3 个具体技术名词或算法。\\n\\n第 3 段（80-120 字）：实际效果与价值，**带 2-3 个具体数字**（效率提升百分比、节省工时、缩短周期等），可以是合理的估算。",
  "tech_features": "8-12 个技术关键词，逗号分隔",

  "functions": [
    {{"name": "功能模块 1 名", "desc": "1-2 句模块职责描述"}},
    ...
    共 10 个功能模块，必须全部紧扣'{topic}'业务主线
  ],

  "mobile_kind": "判断该软件最合理的移动端形态，4 选 1：none / miniapp / app / both",
  "mobile_kind_reason": "20-50 字说明为什么选这种"
}}

mobile_kind 选型 rubric：
- "none"：纯后端管理 / 工控 / 数据中台 / SCADA / 内部 BI 等——不需要任何手机端
- "miniapp"：C 端轻应用，低频使用，扫码/支付/会员/点单/预约场景（如餐饮、商超、活动报名）—— 优先选微信小程序
- "app"：B 端便携工具（巡检、外勤打卡、设备点检）或 C 端深度高频应用（健身、教育、社交）—— 选原生 APP
- "both"：大型 C 端综合服务（电商、银行、出行）—— **极少**，只有功能复杂到 APP 必备、同时引流又靠小程序时选

禁止：
- 不要写与"{topic}"无关的通用功能（如"用户管理""权限管理"这种太泛的不计入 10 个模块）
- main_description **绝对不能**以"该平台 / 该系统 / 该软件 / 本系统 / 本平台"开头（这种开头是 AI 写作的标志）
- mobile_kind 必须严格 4 选 1，**绝对不要写其他值**
- 不要在 JSON 外输出任何文字
- 不要用 markdown 包裹
"""


async def _gen_topics(company: str, province: str, city: str, given: list[str],
                      need: int, *, temperature: float = 0.8) -> list[str]:
    if need <= 0:
        return []
    prompt = _TOPICS_PROMPT.format(
        need=need,
        company=company,
        province=province or "(未知)",
        city=city or "(未知)",
        given=json.dumps(given, ensure_ascii=False) if given else "(无)",
    )
    data = await llm.call_json(prompt, temperature=temperature)
    topics = data.get("topics") or []
    if not isinstance(topics, list):
        raise ValueError(f"LLM 返回 topics 非列表: {data}")
    return [str(t).strip() for t in topics if str(t).strip()][:need]


_BANNED_DESC_OPENINGS = ("该平台", "该系统", "该软件", "本系统", "本平台", "本软件")


def _strip_banned_opening(text: str, topic: str) -> str:
    """如果 main_description 第 1 段以 banned 词开头，前缀替换为业务化引子。

    LLM 即使被 prompt 禁止仍可能落回老套——这里加最后一道兜底。
    """
    if not text:
        return text
    head = text.lstrip()
    for w in _BANNED_DESC_OPENINGS:
        if head.startswith(w):
            # 直接换成"针对…的实际需求，"开头，让句子自然过渡
            return "针对" + topic + "的实际业务需求，" + head[len(w):]
    return text


async def _gen_one_spec(
    *, topic: str, company: str, province: str, city: str, language: str,
    established: date | None = None,
) -> dict:
    completion_date = _random_completion_date(established)

    # N1: 候选池采样，多份同语言软著之间至少 1 个组件不同
    stack = _pick_stack(language, topic, completion_date)
    hw_dev, hw_run, dev_os, run_os = _pick_hardware(language, topic, completion_date)

    prompt = _SPEC_PROMPT.format(
        topic=topic, company=company,
        province=province or "", city=city or "",
        language=language,
    )
    body = await llm.call_json(prompt, temperature=0.6)

    # 兜底校验关键字段
    functions = body.get("functions") or []
    if not isinstance(functions, list) or len(functions) < 8:
        raise ValueError(f"LLM 生成的功能模块数量不足（{len(functions)}），topic={topic}")

    main_desc = _strip_banned_opening(body.get("main_description", "").strip(), topic)

    spec = {
        "software_name": body.get("software_name") or topic,
        "software_abbr": body.get("software_abbr", ""),
        "version": body.get("version", "V1.0"),
        "software_category": body.get("software_category", "应用软件"),
        "tech_category": body.get("tech_category", "行业应用软件"),
        "completion_date": completion_date,
        "publish_status": "未发表",

        "language": language,
        "language_list": stack["lang_list"],
        "ide": stack["ide"],
        "web_server": stack["web_server"],
        "database": stack["db"],
        "dev_os": dev_os,
        "run_os": run_os,
        "hardware_dev": hw_dev,
        "hardware_run": hw_run,

        "purpose": body.get("purpose", "").strip(),
        "industry": body.get("industry", "").strip(),
        "main_description": main_desc,
        "tech_features": body.get("tech_features", "").strip(),
        "functions": [
            {"name": str(f.get("name", "")).strip(), "desc": str(f.get("desc", "")).strip()}
            for f in functions[:10]
        ],

        # M1: 移动端形态（_normalize_enums 兜底为 none）
        "mobile_kind": (body.get("mobile_kind") or "none").strip().lower(),
        "mobile_kind_reason": str(body.get("mobile_kind_reason", "")).strip(),

        # Phase 3/4 回填
        "source_lines": 0,
        "source_files": [],
        "ui_pages": [],
        "source_pdf_pages": 0,
        "manual_pdf_pages": 0,
    }
    return _normalize_enums(spec)


async def generate_specs(
    *,
    company_name: str,
    uscc: str,
    established_date: date,
    quantity: int,
    keywords: list[str] | None = None,
    language: str | None = None,
) -> list[dict]:
    """给定企业信息和数量，生成 N 份 ProjectSpec。

    - 用户提供的 keywords 直接作为主题；不足 quantity 时调 LLM 补齐
    - language 未指定时从 _DEFAULT_LANG_POOL 为每份项目随机选
    """
    keywords = [k.strip() for k in (keywords or []) if k and k.strip()]
    given = keywords[:quantity]
    need = max(0, quantity - len(given))

    region = parse_uscc(uscc)
    province, city = region["province"], region["city"]

    # 1. 补齐主题：LLM retry 最多 3 次，把已用 topic 列表传回去避免雷同。
    #    严禁出现"行业应用 1 / 行业应用 2"这种序号化兜底（一眼批量水货 → 必驳）
    topics: list[str] = list(given)
    temperatures = [0.8, 0.9, 1.0]
    for attempt, t in enumerate(temperatures):
        if len(topics) >= quantity:
            break
        remaining = quantity - len(topics)
        try:
            extra = await _gen_topics(
                company_name, province, city, topics, remaining, temperature=t,
            )
        except Exception as e:
            logger.warning("_gen_topics 第 %d 次失败：%s", attempt + 1, e)
            extra = []
        # 去重（与已有 topic 不重叠）
        for tp in extra:
            if tp and tp not in topics:
                topics.append(tp)

    if len(topics) < quantity:
        raise ValueError(
            f"LLM 经 3 次重试仍无法生成 {quantity} 个不重复的项目主题（已有 {len(topics)} 个）。"
            f"请减少数量或补充关键词。"
        )
    topics = topics[:quantity]

    # 2. owner 类型 + 证件类型 由 USCC 第 1 位决定（O9）
    owner_type, cert_type = _owner_kind_by_uscc(uscc)

    # 3. 每份 Spec 并行生成（受 llm._sem 全局限流）
    async def _one(idx: int, topic: str) -> dict:
        lang = language or random.choice(_DEFAULT_LANG_POOL)
        spec = await _gen_one_spec(
            topic=topic, company=company_name,
            province=province, city=city, language=lang,
            established=established_date,
        )
        # 附著作权人信息
        spec["owner"] = {
            "name": company_name,
            "uscc": uscc,
            "type": owner_type,
            "cert_type": cert_type,
            "nationality": "中国",
            "province": province,
            "city": city,
            "established_date": established_date.isoformat(),
        }
        spec["_idx"] = idx
        return spec

    specs = await asyncio.gather(*(_one(i, t) for i, t in enumerate(topics)))
    return list(specs)
