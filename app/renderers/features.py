"""功能特点.docx 渲染器。

结构比申请表简单，只是一组 label / value 单元格。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document

from .docx_utils import set_cell_text

TEMPLATE = Path(__file__).resolve().parent.parent.parent / "templates" / "features_template.docx"


def _fmt_date_cn(d: str | date) -> str:
    if isinstance(d, date):
        return f"{d.year}年{d.month:02d}月{d.day:02d}日"
    y, m, dd = d.split("-")
    return f"{int(y)}年{int(m):02d}月{int(dd):02d}日"


def _hw_line(hw: dict) -> str:
    return f"CPU：{hw.get('cpu', '')}；内存：{hw.get('ram', '')}；硬盘：{hw.get('disk', '')}"


def render(spec: dict, *, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    doc = Document(str(TEMPLATE))
    owner = spec.get("owner", {})

    # ==== Table 0 ====
    t0 = doc.tables[0]
    set_cell_text(t0.cell(1, 1), spec["software_name"])
    set_cell_text(t0.cell(1, 3), spec.get("version", "V1.0"))
    set_cell_text(t0.cell(2, 1), spec.get("software_abbr", ""))
    set_cell_text(t0.cell(2, 3), spec.get("software_category", "应用软件"))
    set_cell_text(t0.cell(3, 1), "原创")
    set_cell_text(t0.cell(3, 3), "单独开发")
    set_cell_text(t0.cell(4, 1), _fmt_date_cn(spec["completion_date"]))
    set_cell_text(t0.cell(4, 3), "未发表")
    set_cell_text(t0.cell(5, 1), _hw_line(spec["hardware_dev"]))
    set_cell_text(t0.cell(6, 1), _hw_line(spec["hardware_run"]))
    set_cell_text(t0.cell(7, 1), spec.get("dev_os", ""))
    set_cell_text(t0.cell(8, 1), f"IDE：{spec.get('ide', '')}；数据库：{spec.get('database', '')}")
    set_cell_text(t0.cell(9, 1), spec.get("run_os", ""))
    set_cell_text(t0.cell(10, 1), f"WEB容器：{spec.get('web_server', '')}；数据库：{spec.get('database', '')}")
    set_cell_text(t0.cell(11, 1), "、".join(spec.get("language_list") or [spec.get("language", "")]))
    set_cell_text(t0.cell(12, 1), str(spec.get("source_lines", 0)))
    set_cell_text(t0.cell(13, 1), spec.get("purpose", ""))
    set_cell_text(t0.cell(14, 1), spec.get("industry", ""))

    # 主要功能：拼合"整段描述 + 10 个模块的清单"
    func_lines = "\n".join(f"{i+1}. {f['name']}：{f['desc']}" for i, f in enumerate(spec.get("functions", [])))
    main_text = spec.get("main_description", "") + "\n\n主要功能模块：\n" + func_lines
    set_cell_text(t0.cell(15, 1), main_text)

    set_cell_text(t0.cell(16, 1), spec.get("tech_category", ""))
    set_cell_text(t0.cell(17, 1), spec.get("tech_features", ""))

    # ==== Table 1: 著作权人 ====
    t1 = doc.tables[1]
    set_cell_text(t1.cell(0, 1), owner.get("name", ""))
    set_cell_text(t1.cell(1, 1), "企业法人")
    set_cell_text(t1.cell(2, 1), "统一社会信用代码证书")
    set_cell_text(t1.cell(3, 1), owner.get("uscc", ""))
    set_cell_text(t1.cell(4, 1), f"{owner.get('province', '')} {owner.get('city', '')}".strip())

    doc.save(str(output_path))
    return output_path
