"""ProjectSpec 生成器：从公司信息 + 数量 + 关键词 → N 份 ProjectSpec JSON。

本阶段只产出业务描述/环境/功能列表等元数据，**不产出源代码和 UI 页**（那些在 Phase 3/4 由各渲染器基于 Spec 生成时回填）。
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import date, timedelta
from typing import Any

from . import llm
from .region import parse_uscc

# ——— 编程语言与合理的技术栈搭配 ——————————————————————————
LANG_STACKS: dict[str, dict[str, Any]] = {
    "Java": {"ide": "IntelliJ IDEA", "web_server": "nginx1.24", "db": "MySQL8.0",
             "lang_list": ["Java", "HTML", "JavaScript", "SQL"]},
    "Go": {"ide": "VsCode", "web_server": "nginx1.24", "db": "MySQL5.7",
           "lang_list": ["Go", "HTML", "JavaScript", "SQL"]},
    "Python": {"ide": "PyCharm", "web_server": "nginx1.22", "db": "PostgreSQL14",
               "lang_list": ["Python", "HTML", "JavaScript", "SQL"]},
    "C++": {"ide": "Visual Studio 2022", "web_server": "nginx1.22", "db": "MySQL5.7",
            "lang_list": ["C++", "HTML", "JavaScript", "SQL"]},
    "C#": {"ide": "Visual Studio 2022", "web_server": "IIS10", "db": "SQL Server 2019",
           "lang_list": ["C#", "HTML", "JavaScript", "SQL"]},
    "JavaScript": {"ide": "VsCode", "web_server": "nginx1.24", "db": "MongoDB6.0",
                   "lang_list": ["JavaScript", "HTML", "CSS"]},
    "TypeScript": {"ide": "VsCode", "web_server": "nginx1.24", "db": "PostgreSQL14",
                   "lang_list": ["TypeScript", "HTML", "CSS"]},
}

_DEFAULT_LANG_POOL = ["Java", "Go", "Python", "C++"]


def _random_completion_date(days_min: int = 30, days_max: int = 180) -> str:
    """默认完成日期：当前往前推 1-6 个月随机。"""
    delta = random.randint(days_min, days_max)
    return (date.today() - timedelta(days=delta)).isoformat()


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
  "main_description": "150-300 字整段描述，说明软件的定位、目标用户、核心能力、技术亮点、价值。要紧扣'{topic}'，不要出现与主题无关的功能",
  "tech_features": "8-12 个技术关键词，逗号分隔",

  "functions": [
    {{"name": "功能模块 1 名", "desc": "1-2 句模块职责描述"}},
    ...
    共 10 个功能模块，必须全部紧扣'{topic}'业务主线
  ]
}}

禁止：
- 不要写与"{topic}"无关的通用功能（如"用户管理""权限管理"这种太泛的不计入 10 个模块）
- 不要在 JSON 外输出任何文字
- 不要用 markdown 包裹
"""


async def _gen_topics(company: str, province: str, city: str, given: list[str], need: int) -> list[str]:
    if need <= 0:
        return []
    prompt = _TOPICS_PROMPT.format(
        need=need,
        company=company,
        province=province or "(未知)",
        city=city or "(未知)",
        given=json.dumps(given, ensure_ascii=False) if given else "(无)",
    )
    data = await llm.call_json(prompt, temperature=0.8)
    topics = data.get("topics") or []
    if not isinstance(topics, list):
        raise ValueError(f"LLM 返回 topics 非列表: {data}")
    return [str(t).strip() for t in topics if str(t).strip()][:need]


async def _gen_one_spec(
    *, topic: str, company: str, province: str, city: str, language: str,
) -> dict:
    stack = LANG_STACKS.get(language, LANG_STACKS["Go"])
    hw_dev, hw_run = _hardware_by_lang(language)

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

    spec = {
        "software_name": body.get("software_name") or topic,
        "software_abbr": body.get("software_abbr", ""),
        "version": body.get("version", "V1.0"),
        "software_category": body.get("software_category", "应用软件"),
        "tech_category": body.get("tech_category", "行业应用软件"),
        "completion_date": _random_completion_date(),
        "publish_status": "未发表",

        "language": language,
        "language_list": stack["lang_list"],
        "ide": stack["ide"],
        "web_server": stack["web_server"],
        "database": stack["db"],
        "dev_os": "windows11, macos13+",
        "run_os": "winserver2019+",
        "hardware_dev": hw_dev,
        "hardware_run": hw_run,

        "purpose": body.get("purpose", "").strip(),
        "industry": body.get("industry", "").strip(),
        "main_description": body.get("main_description", "").strip(),
        "tech_features": body.get("tech_features", "").strip(),
        "functions": [
            {"name": str(f.get("name", "")).strip(), "desc": str(f.get("desc", "")).strip()}
            for f in functions[:10]
        ],

        # Phase 3/4 回填
        "source_lines": 0,
        "source_files": [],
        "ui_pages": [],
        "source_pdf_pages": 0,
        "manual_pdf_pages": 0,
    }
    return spec


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

    # 1. 补齐主题
    extra = await _gen_topics(company_name, province, city, given, need) if need > 0 else []
    topics = given + extra
    if len(topics) < quantity:
        # 极端情况：LLM 返回不够，复用已有 + 加序号
        while len(topics) < quantity:
            topics.append(f"{given[0] if given else '行业应用'} {len(topics) + 1}")
    topics = topics[:quantity]

    # 2. 每份 Spec 并行生成（受 llm._sem 全局限流）
    async def _one(idx: int, topic: str) -> dict:
        lang = language or random.choice(_DEFAULT_LANG_POOL)
        spec = await _gen_one_spec(
            topic=topic, company=company_name,
            province=province, city=city, language=lang,
        )
        # 附著作权人信息
        spec["owner"] = {
            "name": company_name,
            "uscc": uscc,
            "type": "企业法人",
            "cert_type": "统一社会信用代码证书",
            "nationality": "中国",
            "province": province,
            "city": city,
            "established_date": established_date.isoformat(),
        }
        spec["_idx"] = idx
        return spec

    specs = await asyncio.gather(*(_one(i, t) for i, t in enumerate(topics)))
    return list(specs)
