"""图纸解析层回归测试：几何精度、标注抽取、一致性告警、安全降级、端到端准入。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cncagent.agent import CNCAgent                      # noqa: E402
from cncagent.drawing import (DrawingError, _discretize_bulge, _pairs,  # noqa: E402
                              analyze_bytes, analyze_file, decode_dxf,
                              dxf_unit_scale, parse_dxf_entities, part_to_json)
from cncagent.inputs import parse_json                  # noqa: E402
from cncagent.models import FeatureKind                # noqa: E402

DXF_PATH = ROOT / "examples" / "drawing_plate.dxf"
DXF_TEXT = DXF_PATH.read_text(encoding="utf-8")


class TestDxfParsing(unittest.TestCase):
    def setUp(self):
        self.a = analyze_file(DXF_PATH)

    def test_entities_and_notes(self):
        ents, notes = parse_dxf_entities(DXF_TEXT)
        kinds = [e.kind for e in ents]
        self.assertEqual(kinds.count("circle"), 3)
        self.assertEqual(kinds.count("polyline"), 2)   # 外框 + 矩形槽
        self.assertEqual(kinds.count("line"), 9)       # 3 中心线 + 6 尺寸线
        self.assertEqual(len(notes), 6)
        self.assertTrue(any("毛坯" in n["text"] for n in notes))
        self.assertTrue(any("Ra3.2" in n["text"] for n in notes))

    def test_dxf_control_codes(self):
        from cncagent.drawing import _clean_dxf_text
        self.assertEqual(_clean_dxf_text("%%c50 %%p0.05 %%d"), "Φ50 ±0.05 °")

    def test_stock_and_material(self):
        p = self.a.part
        self.assertEqual((p.stock_x, p.stock_y, p.stock_z), (100.0, 100.0, 20.0))
        self.assertEqual(p.material.key, "45#")
        self.assertEqual(p.source, "dxf_drawing")

    def test_four_features_geometry(self):
        fs = self.a.part.features
        self.assertEqual(len(fs), 4)
        by_kind = {}
        for f in fs:
            by_kind.setdefault(f.kind, []).append(f)
        cp = by_kind[FeatureKind.CIRCLE_POCKET]
        self.assertEqual(len(cp), 1)
        self.assertEqual(cp[0].diameter, 40.0)
        self.assertEqual((cp[0].cx, cp[0].cy), (-22.0, 5.0))
        self.assertEqual(cp[0].depth, 3.0)
        self.assertEqual(cp[0].tolerance, 0.05)
        holes = by_kind[FeatureKind.HOLE]
        self.assertEqual(len(holes), 2)
        self.assertEqual({h.diameter for h in holes}, {8.0})
        self.assertEqual({h.depth for h in holes}, {10.0})
        rp = by_kind[FeatureKind.RECT_POCKET][0]
        self.assertEqual((rp.width, rp.height), (36.0, 18.0))
        self.assertEqual((rp.cx, rp.cy), (25.0, 28.0))
        self.assertEqual(rp.depth, 4.0)
        self.assertEqual(rp.corner_radius, 3.0)
        self.assertEqual(rp.surface_roughness, 3.2)

    def test_general_tolerance_not_leaked_from_local_note(self):
        """两个孔都未标公差，应取「未注公差 ±0.10」，不能串到圆槽的 ±0.05。"""
        holes = [f for f in self.a.part.features if f.kind == FeatureKind.HOLE]
        self.assertEqual([h.tolerance for h in holes], [0.1, 0.1])

    def test_svg_preview(self):
        self.assertIn("<svg", self.a.svg)
        self.assertIn("F1", self.a.svg)
        self.assertIn("F4", self.a.svg)
        self.assertIn("OUTLINE 100", self.a.svg)
        self.assertIn("Φ40", self.a.svg)

    def test_part_json_roundtrip(self):
        p = parse_json(self.a.part_json)
        self.assertEqual(len(p.features), len(self.a.part.features))
        self.assertEqual(p.stock_x, self.a.part.stock_x)
        self.assertEqual([f.kind for f in p.features],
                         [f.kind for f in self.a.part.features])
        self.assertEqual(part_to_json(p)[:1], "{")

    def test_confidence_and_no_warnings_for_sample(self):
        self.assertGreaterEqual(self.a.confidence, 0.85)
        self.assertEqual(self.a.warnings, [])


class TestRobustness(unittest.TestCase):
    def test_closed_polyline_with_duplicated_first_point(self):
        from cncagent.drawing import _axis_rect
        # 部分 CAM 导出的闭合多段线会重复首点，不能因此漏识别矩形槽
        self.assertEqual(
            _axis_rect([(0, 0), (10, 0), (10, 5), (0, 5), (0, 0)]),
            (5.0, 2.5, 10.0, 5.0))
        self.assertIsNone(_axis_rect([(0, 0), (10, 0), (5, 8)]))

    def test_diameter_mismatch_warns(self):
        bad = DXF_TEXT.replace("Φ40 圆槽 深3", "Φ60 圆槽 深3")
        a = analyze_bytes("mismatch.dxf", bad.encode("utf-8"))
        self.assertTrue(any("不一致" in w for w in a.warnings))
        # 仍以几何为准，不被错误标注带偏
        cp = [f for f in a.part.features
              if f.kind == FeatureKind.CIRCLE_POCKET][0]
        self.assertEqual(cp.diameter, 40.0)

    def test_missing_depth_uses_default_and_warns(self):
        bad = DXF_TEXT.replace("矩形槽 36x18 深4", "矩形槽 36x18")
        a = analyze_bytes("nodepth.dxf", bad.encode("utf-8"))
        rp = [f for f in a.part.features
              if f.kind == FeatureKind.RECT_POCKET][0]
        self.assertEqual(rp.depth, 2.0)
        self.assertTrue(any("未读到深度标注" in w for w in a.warnings))

    def test_raster_without_key_fails_loudly(self):
        from unittest.mock import patch
        from cncagent.config import Settings
        with patch("cncagent.drawing.get_settings",
                   return_value=Settings(llm_api_key="")):
            with self.assertRaises(DrawingError) as ctx:
                analyze_bytes("photo.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.assertIn("LLM_API_KEY", str(ctx.exception))
        self.assertIn("DXF", str(ctx.exception))

    def test_unsupported_suffix_and_oversize(self):
        with self.assertRaises(DrawingError):
            analyze_bytes("part.exe", b"MZ")
        with self.assertRaises(DrawingError):
            analyze_bytes("huge.json", b" " * (6 * 1024 * 1024 + 1))

    def test_empty_dxf_rejected(self):
        with self.assertRaises(DrawingError):
            analyze_bytes("empty.dxf", b"0\nEOF\n")

    def test_structured_json_backend(self):
        payload = json.dumps({
            "name": "手工填写", "material": "6061",
            "stock": {"x": 80, "y": 60, "z": 15},
            "features": [{"kind": "circle_pocket", "diameter": 20,
                          "depth": 2, "tolerance": 0.05}],
        })
        a = analyze_bytes("p.json", payload.encode("utf-8"))
        self.assertEqual(a.backend, "json")
        self.assertEqual(a.part.stock_x, 80)
        self.assertEqual(len(a.part.features), 1)
        self.assertTrue(any("人工确认" in w for w in a.warnings))


class TestEndToEnd(unittest.TestCase):
    def test_drawing_part_passes_validation_and_simulation(self):
        """图纸识别结果直接喂进既有链路，必须双关卡通过且允许下发准入。"""
        a = analyze_file(DXF_PATH)
        r = CNCAgent().run(a.part_json, source="json", write_files=False)
        self.assertTrue(r.approved, r.blocked_reason)
        self.assertIsNotNone(r.validation)
        self.assertTrue(r.validation.ok,
                        [i.message for i in r.validation.errors])
        self.assertIsNotNone(r.simulation)
        self.assertTrue(r.simulation.ok)
        self.assertEqual(r.simulation.stats.get("collision_points", 0), 0)
        self.assertGreater(r.program.block_count, 50)


class TestDxfCompatibility(unittest.TestCase):
    """真实 CAD 兼容性：GBK 编码、图纸单位、凸度、块参照、词法容错。"""

    def test_gbk_dxf_keeps_chinese_notes(self):
        raw = DXF_TEXT.encode("gbk")
        self.assertTrue(any("\u4e00" <= c <= "\u9fff" for c in decode_dxf(raw)))
        a = analyze_bytes("gbk.dxf", raw)
        self.assertEqual(len(a.part.features), 4)
        self.assertEqual(a.confidence, 0.9)

    def test_dwgcodepage_header_is_honoured(self):
        raw = DXF_TEXT.replace(
            "9\n$ACADVER",
            "9\n$DWGCODEPAGE\n  3\nANSI_936\n  9\n$ACADVER", 1)
        self.assertIn("毛坯", decode_dxf(raw.encode("gbk")))

    def test_insunits_inch_scales_geometry(self):
        raw = DXF_TEXT.replace(
            "9\n$ACADVER", "9\n$INSUNITS\n 70\n1\n  9\n$ACADVER", 1)
        self.assertEqual(dxf_unit_scale(raw), 25.4)
        ents, _ = parse_dxf_entities(raw)
        circles = [e for e in ents if e.kind == "circle"]
        self.assertTrue(circles)
        self.assertAlmostEqual(circles[0].data["r"], 20.0 * 25.4, places=2)
        a = analyze_bytes("inch.dxf", raw.encode("utf-8"))
        self.assertTrue(any("单位" in w for w in a.warnings))

    def test_blank_lines_do_not_shift_group_pairs(self):
        pairs = _pairs("0\nSECTION\n\n  2\nENTITIES\n0\nENDSEC\n")
        self.assertEqual(pairs[0], (0, "SECTION"))
        self.assertEqual(pairs[1], (2, "ENTITIES"))

    def test_bulge_discretized_but_plain_polyline_untouched(self):
        plain = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
                 (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
        pts, r, hull = _discretize_bulge(plain, True)
        self.assertEqual(pts, [(x, y) for x, y, _ in plain])
        self.assertEqual(r, 0.0)
        rounded = [(x, y, 0.41421356) for x, y, _ in plain]
        pts2, r2, hull2 = _discretize_bulge(rounded, True)
        self.assertGreater(len(pts2), len(rounded))
        self.assertGreater(r2, 0)
        self.assertEqual(len(hull2), 4)

    def test_insert_block_array_is_expanded(self):
        dxf = """0
SECTION
  2
BLOCKS
  0
BLOCK
  8
0
  2
HOLE8
 70
0
 10
0.0
 20
0.0
  0
CIRCLE
  8
FEATURE
 10
0.0
 20
0.0
 40
4.0
  0
ENDBLK
  0
ENDSEC
  0
SECTION
  2
ENTITIES
  0
LWPOLYLINE
  8
OUTLINE
 90
4
 70
1
 10
-50.0
 20
-50.0
 10
50.0
 20
-50.0
 10
50.0
 20
50.0
 10
-50.0
 20
50.0
  0
INSERT
  8
FEATURE
  2
HOLE8
 10
-15.0
 20
-15.0
 70
2
 71
2
 44
30.0
 45
30.0
  0
ENDSEC
  0
EOF
"""
        a = analyze_bytes("blocks.dxf", dxf.encode("utf-8"))
        self.assertEqual(len(a.part.features), 4)
        xs = sorted(f.cx for f in a.part.features)
        self.assertEqual(xs, [-15.0, -15.0, 15.0, 15.0])


if __name__ == "__main__":
    unittest.main()
