"""docx 通用工具：清空单元格、写纯文本、写带勾选框的文本等。

原模板里的勾选框是嵌入图片，难以程序化切换勾/未勾状态。
我们统一用 Unicode ☑/☐ 文本替换，信息等价且跨平台显示一致。
"""
from __future__ import annotations

from docx.oxml.ns import qn
from docx.shared import Pt
from docx.table import _Cell

CHECKED = "☑"
UNCHECKED = "☐"


def set_cell_text(cell: _Cell, text: str, *, font_size: int = 10, bold: bool = False) -> None:
    """清空 cell 的所有段落和图片，写入一段新文本（支持 \\n 分段）。"""
    tc = cell._tc
    # 删除现有所有 <w:p> 段落
    for p in list(tc.findall(qn("w:p"))):
        tc.remove(p)

    # 按换行分段写入
    lines = text.split("\n") if text else [""]
    for line in lines:
        p = cell.add_paragraph()
        run = p.add_run(line)
        run.font.size = Pt(font_size)
        if bold:
            run.font.bold = True


def checkbox_line(options: list[tuple[str, bool]], *, sep: str = "    ") -> str:
    """生成一行勾选框文本，如 '☑ 原创    ☐ 修改'。

    options: [(label, checked_bool), ...]
    """
    parts = [f"{CHECKED if c else UNCHECKED} {label}" for label, c in options]
    return sep.join(parts)
