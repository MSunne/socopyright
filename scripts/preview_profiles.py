"""本地枚举 design profile 预览脚本。

- 为几组软件名产出 profile
- 对每组 profile 跑一次全量 UI 渲染（login + home + 列表 + 详情 + 表单 + 审批 + 图表）
- 输出到 tmp/previews/{software_slug}/{page}.png

供人工抽检：确认所有骨架 × 令牌组合在 1280×800 视口下都不溢出、不难看。

用法：
    python scripts/preview_profiles.py                # 用内置默认软件名
    python scripts/preview_profiles.py 智慧医疗 CRM销售   # 用自定义软件名
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# 允许直接运行脚本（不用 -m）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.renderers import user_manual as um
from app.renderers.design_profile import _pick_design_profile
from app.screenshot.capture import capture_html


DEFAULT_NAMES = [
    "协同办公管理系统",
    "智慧医疗预约平台",
    "财务共享服务系统",
    "CRM销售管理平台",
    "供应链协同系统",
    "政务审批综合平台",
    "教育教务管理系统",
    "人力资源 SaaS 平台",
]


def _slug(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fa5]+", "_", name)
    return s.strip("_")[:40]


def _fake_ui_data(spec: dict) -> dict:
    """不跑 LLM，手填一份"够用"的 UI 数据用于预览。"""
    modules = []
    for i, f in enumerate(spec["functions"]):
        modules.append({
            "name": f["name"],
            "description": f["desc"],
            "filters": ["关键词", "状态", "负责人"],
            "operations": ["新建", "批量导入", "导出"],
            "columns": ["名称", "编号", "状态", "负责人", "更新时间"],
            "rows": [
                [f"{f['name']}-{j:03d}", f"NO-{j:04d}",
                 {"badge": ["blue", "green", "orange", "gray"][j % 4], "text": ["进行中", "已完成", "待处理", "草稿"][j % 4]},
                 ["张三", "李四", "王五"][j % 3], "2026-04-24 10:30"]
                for j in range(1, 7)
            ],
            "steps": ["步骤 1", "步骤 2", "步骤 3"],
            "fields": [{"name": "字段A", "type": "string", "desc": "示例"}],
            "notes": ["注意事项 1"],
        })
    return {
        "slogan": "一体化企业协同管理平台",
        "features_on_login": [
            {"title": "高效协同", "desc": "跨部门流程通畅"},
            {"title": "数据洞察", "desc": "实时业务看板"},
            {"title": "安全合规", "desc": "权限与审计双重保障"},
        ],
        "op_user": "张三",
        "home_metrics": [
            {"label": "待办事项", "value": "128", "trend": "↑ 12% 环比上升"},
            {"label": "本周新增", "value": "36", "trend": "↑ 8%"},
            {"label": "处理中", "value": "52", "trend": "↓ 3%"},
            {"label": "已完成", "value": "240", "trend": "↑ 24%"},
        ],
        "home_table": {
            "title": "我的待办",
            "filters": ["事项名称", "申请人", "类型"],
            "columns": ["事项", "申请人", "状态", "优先级", "提交时间"],
            "rows": [
                [f"事项 {i}", ["张三", "李四"][i % 2],
                 {"badge": ["blue", "green", "orange"][i % 3], "text": ["进行中", "已完成", "待处理"][i % 3]},
                 ["P0", "P1", "P2"][i % 3], "2026-04-24"]
                for i in range(1, 7)
            ],
        },
        "modules": modules,
        "faq": [], "glossary": [],
    }


def _fake_spec(name: str) -> dict:
    return {
        "software_name": name,
        "main_description": f"{name}是一款面向企业的综合管理软件。",
        "owner": {"name": f"{name}有限公司"},
        "completion_date": "2026-04-24",
        "version": "V1.0",
        "functions": [{"name": f"模块{i+1}", "desc": f"{name}的功能模块 {i+1}"} for i in range(8)],
    }


_SUB_SAMPLE = {
    "detail": {
        "record_title": "示例记录 · 预览",
        "record_code": "R20260424-001",
        "status_badge": "blue", "status_text": "处理中",
        "basic_fields": [{"k": f"字段{i}", "v": f"值{i}"} for i in range(1, 7)],
        "business_fields": [{"k": f"业务{i}", "v": f"数据{i}"} for i in range(1, 7)],
        "timeline": [
            {"time": "2026-04-24 10:30", "user": "张三", "action": "创建了记录", "note": ""},
            {"time": "2026-04-24 11:15", "user": "李四", "action": "审批通过", "note": "信息完整"},
        ],
    },
    "form": {
        "form_title": "新建示例记录", "form_subtitle": "请完整填写必填项",
        "basic_1": {"label": "名称", "value": "示例A", "hint": "2-20 字"},
        "basic_2": {"label": "编号", "value": "NO-0001"},
        "basic_3": {"label": "类型", "options": ["普通", "紧急", "特殊"], "value": "紧急"},
        "basic_4": {"label": "日期", "value": "2026-04-24"},
        "section2_title": "附加信息",
        "tag_label": "标签",
        "tags": [{"name": "优先", "active": True}, {"name": "复核", "active": False}],
        "desc_label": "详细说明", "desc_value": "此处为预填的描述文字。",
    },
    "approval": {
        "order_title": "示例审批事项 · 预览",
        "order_code": "APL-20260424-001",
        "initiator": "李四", "initiated_at": "2026-04-24 09:15",
        "status_badge": "blue", "status_text": "审批中",
        "steps": [
            {"label": "发起", "user": "李四", "time": "04-24 09:15", "state": "done"},
            {"label": "初审", "user": "王五", "time": "04-24 10:30", "state": "current"},
            {"label": "复审", "user": "赵六", "time": "-", "state": ""},
            {"label": "归档", "user": "系统", "time": "-", "state": ""},
        ],
        "records": [
            {"user": "李四", "act": "发起", "act_class": "", "time": "04-24 09:15", "comment": "提请审批"},
            {"user": "王五", "act": "审批通过", "act_class": "pass", "time": "04-24 10:30", "comment": "信息完整"},
        ],
    },
    "chart": {
        "chart_page_title": "月度运营分析",
        "kpis": [
            {"label": "总量", "value": "2,480", "trend": "↑ 18%", "down": False},
            {"label": "活跃", "value": "680", "trend": "↑ 9%", "down": False},
            {"label": "转化", "value": "12.4%", "trend": "↓ 1.2%", "down": True},
            {"label": "留存", "value": "88%", "trend": "↑ 4%", "down": False},
        ],
        "line_title": "日活趋势", "line_sub": "近 30 天",
        "line_labels": ["4-1", "4-8", "4-15", "4-22", "4-28"],
        "line_data": [320, 380, 420, 480, 520], "line_label": "人数",
        "bar_title": "部门对比", "bar_sub": "订单数",
        "bar_labels": ["销售", "客服", "财务", "技术", "运营"],
        "bar_data": [420, 380, 220, 160, 340], "bar_label": "订单",
        "doughnut_title": "类型占比", "doughnut_sub": "",
        "doughnut_labels": ["A类", "B类", "C类", "D类"],
        "doughnut_data": [35, 28, 22, 15],
        "area_title": "本年 vs 上年", "area_sub": "",
        "area_labels": ["1月", "2月", "3月", "4月", "5月"],
        "area_series": [
            {"name": "本年", "data": [120, 160, 180, 220, 250]},
            {"name": "上年", "data": [100, 130, 150, 170, 190]},
        ],
    },
}


async def _preview_one(name: str, out_root: Path) -> None:
    spec = _fake_spec(name)
    ui = _fake_ui_data(spec)
    profile = _pick_design_profile(spec["software_name"])

    out_dir = out_root / _slug(name)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "profile.txt").write_text(
        f"shell: {profile['shell']}\n"
        f"palette: {profile['palette']['name']}\n"
        f"radius: {profile['radius']}\n"
        f"density: {profile['density']}\n"
        f"font: {profile['font']}\n"
        f"card: {profile['card_style']}\n"
        f"brand: {profile['brand_mark']}\n",
        encoding="utf-8",
    )

    pages = [
        ("login",    um._render_login_html(spec, ui, profile)),
        ("home",     um._render_home_html(spec, ui, profile)),
        ("module",   um._render_module_html(spec, ui, 0, profile)),
        ("detail",   um._render_detail_html(spec, ui, 0, _SUB_SAMPLE["detail"], profile)),
        ("form",     um._render_form_html(spec, ui, 0, _SUB_SAMPLE["form"], profile)),
        ("approval", um._render_approval_html(spec, ui, 0, _SUB_SAMPLE["approval"], profile)),
        ("chart",    um._render_chart_html(spec, ui, 0, _SUB_SAMPLE["chart"], profile)),
    ]
    for page_name, html in pages:
        png = await capture_html(html, viewport_width=1280, viewport_height=800)
        (out_dir / f"{page_name}.png").write_bytes(png)
        print(f"  {page_name}.png ({len(png)//1024} KB)")
    print(f"✓ {name} [{profile['shell']} / {profile['palette']['name']}] → {out_dir}")


async def main() -> None:
    names = sys.argv[1:] or DEFAULT_NAMES
    out_root = Path(__file__).resolve().parent.parent / "tmp" / "previews"
    out_root.mkdir(parents=True, exist_ok=True)
    for name in names:
        print(f"\n→ {name}")
        await _preview_one(name, out_root)
    print(f"\n完成。预览目录：{out_root}")


if __name__ == "__main__":
    asyncio.run(main())
