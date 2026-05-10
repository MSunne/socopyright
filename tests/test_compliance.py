"""软著合规层单元测试。

覆盖 PROJECT_SPEC 第 2 节硬指标的入参/兜底逻辑：
- USCC GB 32100 校验位（O3）
- 入参黑名单（O3 + O4）
- completion_date 边界（O2）
- 枚举白名单兜底（O11）
- owner.type / cert_type 与 USCC 联动（O9）
- 软件名 dedup（O8）

不依赖 pytest，用 stdlib unittest 直接跑：
    .venv/bin/python -m unittest tests.test_compliance -v
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.region import validate_uscc
from app.schemas import JobCreate
from app.spec import (
    _normalize_enums,
    _owner_kind_by_uscc,
    _random_completion_date,
)
from app.pipeline import _is_duplicate_name, find_duplicate_indexes


# 已知合法 USCC：项目 spec 示例（武汉兰亭印务）
LEGAL_USCC = "91420112MA7DX2ME10"


class TestValidateUscc(unittest.TestCase):
    def test_legal(self):
        ok, reason = validate_uscc(LEGAL_USCC)
        self.assertTrue(ok, msg=reason)

    def test_wrong_check_digit(self):
        # 把校验位改错
        bad = LEGAL_USCC[:-1] + "1"
        ok, reason = validate_uscc(bad)
        self.assertFalse(ok)
        self.assertIn("校验码", reason)

    def test_illegal_chars(self):
        # 含 O（非法字符）
        ok, reason = validate_uscc(LEGAL_USCC[:-2] + "O0")
        self.assertFalse(ok)

    def test_wrong_length(self):
        ok, reason = validate_uscc("ABC")
        self.assertFalse(ok)
        self.assertIn("18", reason)

    def test_lowercase_normalized(self):
        ok, _ = validate_uscc(LEGAL_USCC.lower())
        self.assertTrue(ok)


class TestJobCreateValidator(unittest.TestCase):
    def _make(self, **overrides):
        body = dict(
            company_name="武汉兰亭印务有限公司",
            uscc=LEGAL_USCC,
            established_date=date(2020, 6, 15),
            quantity=3,
        )
        body.update(overrides)
        return JobCreate(**body)

    def test_legal_passes(self):
        m = self._make()
        self.assertEqual(m.uscc, LEGAL_USCC)

    def test_company_name_normalized(self):
        m = self._make(company_name="  武汉（兰亭）印务有限公司   ")
        self.assertEqual(m.company_name, "武汉(兰亭)印务有限公司")

    def test_company_name_too_short(self):
        with self.assertRaises(ValueError):
            self._make(company_name="abc")

    def test_uscc_check_digit_rejected(self):
        with self.assertRaises(ValueError):
            self._make(uscc=LEGAL_USCC[:-1] + "1")

    def test_uscc_illegal_char_rejected(self):
        with self.assertRaises(ValueError):
            self._make(uscc=LEGAL_USCC[:-2] + "O0")

    def test_established_today_rejected(self):
        with self.assertRaises(ValueError):
            self._make(established_date=date.today())

    def test_established_future_rejected(self):
        with self.assertRaises(ValueError):
            self._make(established_date=date.today() + timedelta(days=1))

    def test_established_too_early_rejected(self):
        with self.assertRaises(ValueError):
            self._make(established_date=date(1985, 1, 1))


class TestCompletionDate(unittest.TestCase):
    def test_normal_old_company(self):
        # 5 年前成立的公司
        d = _random_completion_date(date(2021, 1, 1))
        result = date.fromisoformat(d)
        self.assertLess(result, date.today())
        # 应在 30-180 天前
        delta = (date.today() - result).days
        self.assertLessEqual(delta, 180)
        self.assertGreaterEqual(delta, 1)

    def test_brand_new_company_falls_back(self):
        # 公司刚成立 5 天：floor > ceiling，函数会 warning + 回落 today-1
        # （这是已知边缘情况：业务侧应在前端拦截"新成立公司不建议申软著"）
        established = date.today() - timedelta(days=5)
        d = _random_completion_date(established)
        result = date.fromisoformat(d)
        self.assertEqual(result, date.today() - timedelta(days=1))

    def test_company_60d_old_at_least_30d_after(self):
        # 公司成立 60 天 → completion_date ≥ established + 30（即 30 天前）
        established = date.today() - timedelta(days=60)
        d = _random_completion_date(established)
        result = date.fromisoformat(d)
        self.assertGreaterEqual(result, established + timedelta(days=30))
        self.assertLess(result, date.today())

    def test_no_established_works(self):
        # 不传 established 时按老逻辑工作
        d = _random_completion_date()
        result = date.fromisoformat(d)
        self.assertLess(result, date.today())


class TestNormalizeEnums(unittest.TestCase):
    def test_invalid_tech_category_fallback(self):
        spec = {"tech_category": "AI 智能软件"}
        out = _normalize_enums(spec)
        self.assertEqual(out["tech_category"], "行业应用软件")

    def test_valid_tech_category_preserved(self):
        spec = {"tech_category": "人工智能软件"}
        out = _normalize_enums(spec)
        self.assertEqual(out["tech_category"], "人工智能软件")

    def test_invalid_software_category_fallback(self):
        spec = {"software_category": "应用"}
        out = _normalize_enums(spec)
        self.assertEqual(out["software_category"], "应用软件")

    def test_invalid_version_fallback(self):
        for bad in ("v1.0", "1.0", "Version 1", ""):
            with self.subTest(version=bad):
                out = _normalize_enums({"version": bad})
                self.assertEqual(out["version"], "V1.0")

    def test_valid_version_preserved(self):
        for ok in ("V1.0", "V2.5", "V10.20.30"):
            with self.subTest(version=ok):
                out = _normalize_enums({"version": ok})
                self.assertEqual(out["version"], ok)

    def test_publish_status_fallback(self):
        out = _normalize_enums({"publish_status": "待发布"})
        self.assertEqual(out["publish_status"], "未发表")

    def test_default_dev_mode_and_is_original(self):
        out = _normalize_enums({})
        self.assertTrue(out["is_original"])
        self.assertEqual(out["dev_mode"], "单独开发")


class TestOwnerKind(unittest.TestCase):
    def test_enterprise_9(self):
        t, c = _owner_kind_by_uscc("91420112MA7DX2ME10")
        self.assertEqual(t, "企业法人")
        self.assertEqual(c, "统一社会信用代码证书")

    def test_social_5(self):
        t, c = _owner_kind_by_uscc("51100000XXXXXXXXXX")
        self.assertEqual(t, "社会团体法人")

    def test_government_1(self):
        t, c = _owner_kind_by_uscc("11000000XXXXXXXXXX")
        self.assertEqual(t, "机关法人")

    def test_other_y(self):
        t, c = _owner_kind_by_uscc("Y2000000XXXXXXXXXX")
        self.assertEqual(t, "其他")

    def test_empty_falls_back_to_enterprise(self):
        t, c = _owner_kind_by_uscc("")
        self.assertEqual(t, "企业法人")


class TestSoftwareNameDedup(unittest.TestCase):
    def test_pure_number_suffix_blacklisted(self):
        names = ["行业应用 1", "行业应用 2", "正常名称系统"]
        bad = find_duplicate_indexes(names)
        self.assertIn(0, bad)
        self.assertIn(1, bad)
        self.assertNotIn(2, bad)

    def test_chinese_number_suffix_blacklisted(self):
        names = ["行业应用一", "行业应用二"]
        bad = find_duplicate_indexes(names)
        self.assertEqual(set(bad), {0, 1})

    def test_high_similarity_dedup(self):
        # 共享几乎所有字，仅一字之差 → 视为重复
        names = ["印刷品质量检测平台", "印刷品缺陷检测平台", "完全无关的车间排程"]
        bad = find_duplicate_indexes(names)
        # 第 1 个会保留，第 2 个被识别为重复
        self.assertIn(1, bad)
        self.assertNotIn(2, bad)

    def test_distinct_names_kept(self):
        names = ["印刷缺陷智能检测", "生产排程优化系统", "供应链协同平台"]
        bad = find_duplicate_indexes(names)
        self.assertEqual(bad, [])

    def test_exact_duplicate(self):
        names = ["ABC 系统", "ABC 系统"]
        bad = find_duplicate_indexes(names)
        self.assertEqual(bad, [1])

    def test_is_duplicate_helper(self):
        self.assertTrue(_is_duplicate_name("印刷品质量检测平台", "印刷品质量检测系统"))
        self.assertFalse(_is_duplicate_name("印刷质量分析", "排程优化"))


class TestPdfClipping(unittest.TestCase):
    """O0：_clip_general_deposit 截取行为（不依赖真实 PDF，仅冒烟生成 70 页 PDF 后截取）"""

    def test_clip_to_60_pages(self):
        from io import BytesIO
        from pathlib import Path
        from tempfile import TemporaryDirectory

        try:
            from reportlab.pdfgen.canvas import Canvas
        except ImportError:
            self.skipTest("reportlab 未安装")

        from pypdf import PdfReader

        from app.pipeline import _clip_general_deposit, GENERAL_DEPOSIT_HEAD, GENERAL_DEPOSIT_TAIL

        with TemporaryDirectory() as tmp:
            pdf_path = Path(tmp) / "src.pdf"
            full_dir = Path(tmp) / "_full"

            # 生成 70 页 PDF
            buf = BytesIO()
            c = Canvas(str(pdf_path))
            for i in range(70):
                c.drawString(72, 720, f"page {i+1}")
                c.showPage()
            c.save()

            self.assertEqual(len(PdfReader(str(pdf_path)).pages), 70)

            kept = _clip_general_deposit(pdf_path, full_dir)
            self.assertEqual(kept, GENERAL_DEPOSIT_HEAD + GENERAL_DEPOSIT_TAIL)
            self.assertEqual(len(PdfReader(str(pdf_path)).pages), 60)
            # 完整版备份
            self.assertTrue((full_dir / "src.pdf").exists())
            self.assertEqual(len(PdfReader(str(full_dir / "src.pdf")).pages), 70)


if __name__ == "__main__":
    unittest.main()
