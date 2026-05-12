"""用户手册 UI 的"设计档案"生成。

基于软件名哈希（稳定可复现），为每份手册产出一个 `profile` dict，
覆盖 7 个维度：shell / palette / radius / density / font / card_style / brand_mark。

渲染端把 profile 翻译为：
  - 一组 CSS 变量（颜色、圆角、字号、行高、阴影、字体）
  - 一串 body class（shell-* / density-* / radius-* / font-* / card-* / brand-*）

base.css 用 CSS 变量和属性选择器响应，partials/_shell.html 根据 shell 渲染不同骨架。
"""
from __future__ import annotations

import hashlib
from typing import Any


# ——— 调色板：12 组 ———————————————————————————————————————————————
PALETTES: list[dict[str, str]] = [
    {"name": "royal-blue",  "dark": "#1e3a8a", "dark2": "#1e40af", "accent": "#2563eb"},
    {"name": "forest",      "dark": "#064e3b", "dark2": "#065f46", "accent": "#059669"},
    {"name": "violet",      "dark": "#4c1d95", "dark2": "#5b21b6", "accent": "#7c3aed"},
    {"name": "amber",       "dark": "#7c2d12", "dark2": "#9a3412", "accent": "#ea580c"},
    {"name": "slate",       "dark": "#1f2937", "dark2": "#111827", "accent": "#3b82f6"},
    {"name": "teal",        "dark": "#134e4a", "dark2": "#115e59", "accent": "#0d9488"},
    {"name": "crimson",     "dark": "#7f1d1d", "dark2": "#991b1b", "accent": "#dc2626"},
    {"name": "indigo",      "dark": "#312e81", "dark2": "#3730a3", "accent": "#4f46e5"},
    {"name": "graphite",    "dark": "#0f172a", "dark2": "#1e293b", "accent": "#475569"},
    {"name": "rose",        "dark": "#881337", "dark2": "#9f1239", "accent": "#e11d48"},
    {"name": "lime-pro",    "dark": "#365314", "dark2": "#3f6212", "accent": "#65a30d"},
    {"name": "ocean",       "dark": "#0c4a6e", "dark2": "#075985", "accent": "#0284c7"},
]

SHELLS: list[str] = [
    "classic-left",      # 220 左竖边栏 + 56 顶栏
    "top-ribbon",        # 112 双层顶栏，无左栏
    "app-rail",          # 64 深色窄图标条，无顶栏，内容内嵌页头
    "split-panel",       # 56 品牌顶栏 + 280 功能面板（搜索+快捷+树）
    "stacked-rail",      # 64 深图标 + 220 浅二级菜单（三段式，双左栏）
    "sidebar-right",     # 顶栏 + content 在左 + 220 右侧栏（镜像）
    "minimal-topbar",    # 40 超薄顶栏 + content 居中 1080px，两侧留白
    "hero-dashboard",    # 160 渐变 banner + 200 左栏 + content
    "dark-console",      # 全暗黑主题：深色顶 + 深色左栏 + 深色 content，浅色卡片
    "split-horizontal",  # 上下分区：顶部 240 大 banner（品牌+筛选+KPI）+ 下方全宽内容
    "floating-card",     # 灰色大背景 + 中央漂浮 1180×760 大白卡（自带顶栏+左栏+content）
    "window-chrome",     # 48 桌面窗口标题栏（红黄绿圆点）+ 200 左栏 + content
]

# ——— 移动端 APP shell 主题（15 套）——————————————————————————————
# 同一份 HTML 模板（如 app_home.html）在不同 mobile_app_shell 下，
# 通过 _app_shell.html 的 {% if app_shell == 'xxx' %} 控制结构差异，
# _mobile.css 用 body.app-shell-xxx 选择器覆盖颜色 / 卡片 / 圆角 / 阴影。
MOBILE_APP_SHELLS: list[str] = [
    "ios-bottom",         # iOS 经典底 5tab，大圆角，软阴影，白底
    "android-material",   # Material 风，直角，FAB 浮动按钮，主色调强
    "fluent-glass",       # Fluent 玻璃质感，半透明卡片，模糊背景
    "flat-mono",          # 扁平极简，无阴影，单色线条，灰底白卡
    "dark-pro",           # 暗黑高级，深蓝深紫渐变，高对比
    "drawer-left",        # 左侧抽屉，顶部汉堡按钮 + 用户头像
    "drawer-right",       # 右侧抽屉，简洁顶栏
    "top-tabs",           # 顶部 5tab 切换（无底 tabbar）
    "segment-control",    # iOS SegmentControl 风分段控件
    "rail-left",          # 左侧 64px 窄竖栏 + 主区
    "bubble-fab-center",  # 中央大气泡按钮（外卖/打车 APP 风）
    "card-stack",         # 卡片堆叠（信息流 APP 风）
    "pull-tab",           # 底部可上下拉抽屉（地图 APP 风）
    "floating-min",       # 悬浮主操作按钮 + 极简空白
    "warm-friendly",      # 暖色系（橙黄），亲和力强，大圆角
]

# ——— 微信小程序 shell 主题（5 套）——————————————————————————————
# 受微信限制：胶囊必须在右上、tabbar 4 个固定。可变维度：配色、卡片、顶部样式、密度。
MOBILE_MINIAPP_SHELLS: list[str] = [
    "wx-classic",      # 经典微信绿 + 白卡 + 灰底（默认）
    "vivid-brand",     # 品牌强色 banner（顶部全色），白色圆角卡片
    "monochrome",      # 黑白极简（高级感），细线分隔，无阴影
    "warm-card",       # 暖色调（橙黄/玫红），大圆角卡片，柔和阴影
    "cool-flat",       # 冷色调（蓝灰），扁平，强对比，企业 SaaS 感
]

RADII: list[int] = [0, 4, 8, 12]
DENSITIES: list[str] = ["comfortable", "compact"]
FONTS: list[str] = ["system", "pingfang", "source-han", "noto-serif"]
CARD_STYLES: list[str] = ["flat-bordered", "subtle-shadow", "strong-shadow"]
BRAND_MARKS: list[str] = ["dot", "square", "circle-letter"]


FONT_STACKS: dict[str, str] = {
    "system":      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    "pingfang":    '"PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans CN", sans-serif',
    "source-han":  '"Source Han Sans CN", "PingFang SC", "Microsoft YaHei", sans-serif',
    "noto-serif":  '"Noto Serif SC", "Source Han Serif SC", "Songti SC", "SimSun", serif',
}


def _pick_design_profile(seed: str) -> dict[str, Any]:
    """从软件名稳定哈希出完整 design profile。

    使用 MD5（32 hex chars = 16 bytes），不同字节段独立映射到各维度，
    同一 seed 永远返回同一 profile；不同 seed 在 ~1.4 万组合空间里均匀分布。
    """
    h = hashlib.md5(seed.encode("utf-8")).digest()  # 16 bytes

    palette          = PALETTES[h[0] % len(PALETTES)]
    shell            = SHELLS[h[2] % len(SHELLS)]
    radius           = RADII[h[4] % len(RADII)]
    density          = DENSITIES[h[6] % len(DENSITIES)]
    font             = FONTS[h[8] % len(FONTS)]
    card_style       = CARD_STYLES[h[10] % len(CARD_STYLES)]
    brand_mark       = BRAND_MARKS[h[12] % len(BRAND_MARKS)]
    # 移动端主题：用不同字节避免与 PC shell 相关
    mobile_app_shell     = MOBILE_APP_SHELLS[h[1] % len(MOBILE_APP_SHELLS)]
    mobile_miniapp_shell = MOBILE_MINIAPP_SHELLS[h[3] % len(MOBILE_MINIAPP_SHELLS)]

    return {
        "palette": palette,
        "shell": shell,
        "radius": radius,
        "density": density,
        "font": font,
        "card_style": card_style,
        "brand_mark": brand_mark,
        "mobile_app_shell": mobile_app_shell,
        "mobile_miniapp_shell": mobile_miniapp_shell,
    }


def body_classes(profile: dict[str, Any]) -> str:
    """拼接 <body> 的 class 串，供模板里 `<body class="{{ body_classes }}">` 使用。"""
    return " ".join([
        f"shell-{profile['shell']}",
        f"density-{profile['density']}",
        f"radius-{profile['radius']}",
        f"font-{profile['font']}",
        f"card-{profile['card_style']}",
        f"brand-{profile['brand_mark']}",
    ])


def css_vars(profile: dict[str, Any]) -> dict[str, str]:
    """把 profile 翻译成一组 CSS 自定义属性值（供 base.css 里 {{ ... }} 占位符替换）。

    只包括「颜色」和「字体」这两个 palette/font 决定的值；
    圆角、密度、卡片阴影走 body class 驱动的选择器覆盖，不在此输出。
    """
    p = profile["palette"]
    return {
        "COLOR_DARK":    p["dark"],
        "COLOR_DARK2":   p["dark2"],
        "COLOR_ACCENT":  p["accent"],
        "FONT_FAMILY":   FONT_STACKS[profile["font"]],
    }


def brand_mark_html(profile: dict[str, Any], label: str = "") -> str:
    """返回用作品牌图标的小块 HTML。"""
    mark = profile["brand_mark"]
    if mark == "dot":
        return '<span class="brand-mark brand-dot"></span>'
    if mark == "square":
        return '<span class="brand-mark brand-square"></span>'
    # circle-letter：取 label 首字
    ch = (label[:1] if label else "") or "S"
    return f'<span class="brand-mark brand-circle">{ch}</span>'
