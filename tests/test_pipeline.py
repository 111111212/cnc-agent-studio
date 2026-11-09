"""端到端回归测试（标准库 unittest，无需 pytest）。

运行：python -m unittest discover -s tests -v
覆盖：输入解析、参数夹紧、三种方言、校验/仿真拦截、自修复闭环、
      机型能力派生、视觉检测零误报、安全门控、完整闭环重编程。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cncagent.agent import CNCAgent, diagnose_legacy
from cncagent.config import OUTPUT_DIR, get_settings
from cncagent.cutting import clamp_plan
from cncagent.generator import (FAULT_LIBRARY, FanucPost, _sanitize_comment,
                                inject_faults, make_post)
from cncagent.geometry import generate_toolpaths
from cncagent.inputs import parse_natural_language, parse_request, parse_step
from cncagent.machines import (PROFILES, capability_summary,
                               effective_cutting_policy, get_profile)
from cncagent.planner import RulePlanner, inject_hallucination
from cncagent.simulator import _safe_resolution, gcode_trajectory, simulate_gcode
from cncagent.validator import Validator
from cncagent.vision.agent_tools import SafetyGovernor, VisionDiagnosisAgent
from cncagent.vision.closed_loop import ClosedLoopController
from cncagent.vision.detector import HeuristicDetector
from cncagent.vision.frames import DEMO_SCENARIO, SyntheticSource
from cncagent.vision.monitor import MonitorConfig, VisionMonitor
from cncagent.vision.protocols import DryRunAdapter

DEMO = "加工一块45#钢方块，长宽100×100mm，铣一个直径50mm圆形凹槽，深度3mm"
ROOT = Path(__file__).resolve().parent.parent


class TestInputLayer(unittest.TestCase):
    def test_natural_language(self):
        p = parse_natural_language(DEMO)
        self.assertEqual(p.material.key, "45#")
        self.assertEqual((p.stock_x, p.stock_y), (100.0, 100.0))
        self.assertEqual(len(p.features), 1)
        self.assertEqual(p.features[0].diameter, 50.0)
        self.assertEqual(p.features[0].depth, 3.0)

    def test_multi_feature_json(self):
        p = parse_request(str(ROOT / "examples" / "part.json"), "json")
        self.assertEqual(p.material.key, "6061")
        self.assertEqual(len(p.features), 4)
        self.assertEqual(p.features[0].kind.value, "face")

    def test_step_bounding_box(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "cube.step"
            f.write_text("ISO-10303-21;\nHEADER;ENDSEC;\nDATA;\n"
                         "#1=CARTESIAN_POINT('',(0.,0.,0.));\n"
                         "#2=CARTESIAN_POINT('',(100.,80.,20.));\n"
                         "ENDSEC;\nEND-ISO-10303-21;", encoding="utf-8")
            p = parse_step(str(f))
            self.assertEqual((p.stock_x, p.stock_y, p.stock_z), (100, 80, 20))
            self.assertTrue(p.warnings)   # 明确标注未做 B-Rep 识别


class TestProcessAndPost(unittest.TestCase):
    def setUp(self):
        self.part = parse_natural_language(DEMO)
        self.plan = RulePlanner().plan(self.part)

    def test_process_order_rough_before_finish(self):
        ids = [op.stage for op in self.plan.operations]
        self.assertLess(ids.index("rough"), ids.index("finish"))

    def test_hallucination_clamped_to_recommended(self):
        plan = inject_hallucination(
            RulePlanner().plan(self.part),
            {"spindle": 30000, "feed": 9000, "ap": 25.0})
        plan = clamp_plan(plan)
        for op in plan.operations:
            self.assertLessEqual(op.spindle, 8000)
            self.assertLessEqual(op.feed, 6000)
            self.assertLessEqual(op.ap, 3.0)
            # 越界参数应回退到按 Vc 计算的推荐值，而非贴着机床极限
            self.assertLess(op.spindle, 5000)
        self.assertTrue(any(op.adjustments for op in plan.operations))

    def test_three_dialects_validate_and_simulate(self):
        tps = generate_toolpaths(self.plan)
        for ctrl in ("FANUC", "SIEMENS", "HNC"):
            with self.subTest(controller=ctrl):
                prog = make_post(ctrl, self.plan).generate(tps)
                val = Validator(self.plan).validate(prog.text, ctrl)
                sim = simulate_gcode(prog.text, self.plan, ctrl)
                self.assertTrue(val.ok, [str(e) for e in val.errors])
                self.assertTrue(sim.ok, sim.issues)
                self.assertLess(sim.stats["volume_error_pct"], 3.0)

    def test_validator_catches_injected_faults(self):
        prog = make_post("FANUC", self.plan).generate(
            generate_toolpaths(self.plan))
        bad = inject_faults(prog.text, list(FAULT_LIBRARY))
        rep = Validator(self.plan).validate(bad, "FANUC")
        codes = {e.code for e in rep.errors}
        self.assertFalse(rep.ok)
        for expected in ("OVERTRAVEL", "RAPID_IN_MATERIAL", "RAPID_PLUNGE",
                         "UNKNOWN_G", "SPINDLE_OVER", "BAD_TOOL"):
            self.assertIn(expected, codes)

    def test_simulator_catches_rapid_crash(self):
        prog = make_post("FANUC", self.plan).generate(
            generate_toolpaths(self.plan))
        bad = inject_faults(prog.text, ["plunge", "overtravel"])
        sim = simulate_gcode(bad, self.plan, "FANUC")
        self.assertFalse(sim.ok)
        codes = {i["code"] for i in sim.errors}
        self.assertTrue(codes & {"SIM_RAPID_CRASH", "SIM_RAPID_PLUNGE",
                                 "SIM_OVERTRAVEL"})


class TestSelfRepair(unittest.TestCase):
    def test_hallucination_repaired_then_approved(self):
        result = CNCAgent().run(
            DEMO, inject={"spindle": 30000, "feed": 9000, "ap": 25.0},
            faults=["overtravel", "plunge", "bad_g", "overspeed"],
            write_files=False)
        self.assertTrue(result.approved)
        self.assertGreater(result.attempts, 1)
        self.assertTrue(result.validation.ok)
        self.assertTrue(result.simulation.ok)
        self.assertTrue(result.repair_log)
        self.assertEqual(result.repair_log[0]["gate"], "validator")

    def test_legacy_gcode_audit(self):
        info = diagnose_legacy(str(ROOT / "examples" / "legacy.nc"))
        self.assertGreater(len(info["suggestions"]), 0)
        self.assertTrue(any("超过机床" in s or "超程" in s
                            for s in info["suggestions"] + info["suspicious"]))


class TestMachineProfiles(unittest.TestCase):
    def test_all_profiles_valid(self):
        for key in PROFILES:
            with self.subTest(profile=key):
                p = get_profile(key)
                cap = capability_summary(p)
                self.assertGreater(cap["total"], 10)
                self.assertGreater(p.spec.max_spindle, 0)
                self.assertIn(p.post, ("FANUC", "SIEMENS", "HNC"))

    def test_five_axis_capabilities_explicitly_disabled(self):
        cap = capability_summary(get_profile("siemens_840d_dmu50"))
        disabled = [c for c in cap["capabilities"] if c["status"] == "disabled"]
        self.assertTrue(any("五轴" in c["name"] or "3+2" in c["name"]
                            for c in disabled))

    def test_tapping_center_gets_tighter_policy(self):
        loose = effective_cutting_policy(get_profile("vmc850_fanuc"))
        tight = effective_cutting_policy(get_profile("robodrill_d21"))
        self.assertLess(tight["ap_factor"], loose["ap_factor"])

    def test_switch_machine_runs_full_chain(self):
        from dataclasses import replace
        s = replace(get_settings(), machine_id="robodrill_d21")
        r = CNCAgent(s).run(DEMO, write_files=False)
        self.assertTrue(r.approved)
        self.assertEqual(r.controller, "FANUC")
        self.assertEqual(r.profile.id, "robodrill_d21")
        self.assertGreater(r.program.block_count, 0)


class TestVision(unittest.TestCase):
    def test_detector_six_faults_zero_false_positive(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = MonitorConfig()
            mon = VisionMonitor(SyntheticSource(), HeuristicDetector(),
                                Path(d), cfg, process_context={"machine": "test"})
            res = mon.run()
            self.assertEqual(res.frames, 145)
            self.assertEqual(len(res.events), 6)
            self.assertEqual({e.fault_code for e in res.events}, {
                "abnormal_sparks", "chip_accumulation", "tool_breakage",
                "coolant_missing", "workpiece_shift", "collision_debris"})
            scene_to_fault = {
                "sparks": "abnormal_sparks", "chips": "chip_accumulation",
                "tool_break": "tool_breakage", "no_coolant": "coolant_missing",
                "shift": "workpiece_shift", "debris": "collision_debris",
            }
            for e in res.events:
                span = next(s for s in DEMO_SCENARIO
                            if scene_to_fault.get(s.fault) == e.fault_code)
                self.assertGreaterEqual(e.frame_no, span.start)
                self.assertLessEqual(e.frame_no, span.end)

    def test_safety_governor_blocks_estop_and_clamps_override(self):
        from cncagent.vision.agent_tools import Diagnosis
        diag = Diagnosis(
            "EV1", "workpiece_shift", "c", "r", "critical", [],
            [{"signal": "EMERGENCY_STOP", "value": None},
             {"signal": "FEED_OVERRIDE", "value": 150},
             {"signal": "G999", "value": None}], {}, True)
        dec = SafetyGovernor(allow_auto_stop=False).review(diag, False)
        self.assertEqual(len(dec.blocked_signals), 2)
        self.assertEqual(len(dec.approved_signals), 1)
        self.assertEqual(dec.approved_signals[0]["value"], 100)
        dec2 = SafetyGovernor(allow_auto_stop=True).review(diag, True)
        self.assertEqual(len(dec2.approved_signals), 2)

    def test_adapter_never_soft_writes_estop(self):
        a = DryRunAdapter()
        r = a.write("EMERGENCY_STOP")
        self.assertFalse(r.ok)
        self.assertIn("硬件", r.detail)

    def test_closed_loop_reprogram_passes_both_gates(self):
        with tempfile.TemporaryDirectory() as d:
            from dataclasses import replace
            s = replace(get_settings(), machine_id="vmc850_fanuc")
            ctrl = ClosedLoopController(s, get_profile("vmc850_fanuc"),
                                        Path(d))
            detector = HeuristicDetector()
            res = ctrl.run_full(DEMO, detector, "测试",
                                source=SyntheticSource(),
                                protocol="dryrun")
            self.assertEqual(len(res.traces), 6)
            self.assertTrue(res.revised_ok)
            self.assertIsNotNone(res.revised_program)
            self.assertNotEqual(
                res.summary["original_sha"], res.summary["revised_sha"])
            self.assertGreater(res.summary["control_writes"], 0)
            self.assertGreaterEqual(res.summary["blocked_signals"], 1)


class TestHardeningGCode(unittest.TestCase):
    """本轮加固：固定循环返回平面三处一致、G80 模态、注释注入、
    参数夹紧、网格上界、圆弧按实际扫掠角采样。"""

    @classmethod
    def setUpClass(cls):
        # examples/part.json 带孔，才会输出 G81 / CYCLE81 固定循环
        cls.r = CNCAgent().run(
            (ROOT / "examples" / "part.json").read_text(encoding="utf-8"),
            source="json", write_files=False)

    def test_cycle_returns_to_r_plane_and_declares_g99(self):
        import re
        text = self.r.program.text
        self.assertIn("G99", text)
        m = re.search(r"G81[^R\n]*R([-\d.]+)", text)
        self.assertIsNotNone(m)
        r_level = float(m.group(1))
        cycles = [x for x in gcode_trajectory(text, self.r.plan)
                  if x.kind == "cycle"]
        self.assertTrue(cycles)
        for c in cycles:
            self.assertAlmostEqual(c.return_z, r_level, places=3)

    def test_siemens_cycle81_depth_uses_named_params(self):
        # 修复前 re.findall 会把行号 150 和 "81" 算进数字序列，DP 取成 0.0
        text = ("%_N_T_MPF\nG71 G17 G40 G90\nG54\n"
                "G00 X10.0 Y10.0 Z20.0\nG00 Z9.0\n"
                "N150 CYCLE81(9.0, 0.0, 9.0, -8.0, , )\nM30")
        cyc = [m for m in gcode_trajectory(text, self.r.plan)
               if m.kind == "cycle"]
        self.assertEqual(len(cyc), 1)
        self.assertAlmostEqual(cyc[0].z, -8.0, places=3)
        self.assertAlmostEqual(cyc[0].return_z, 9.0, places=3)

    def test_g80_restores_rapid_motion(self):
        text = ("%\nO0001\nG21 G17 G40 G49 G80 G90\nG54\n"
                "G00 X0.0 Y0.0 Z20.0\nG00 Z9.0\n"
                "G99\nG81 X0.0 Y0.0 Z-8.0 R9.0 F100.0\nG80\n"
                "X20.0\nM05\nM30\n%")
        moves = gcode_trajectory(text, self.r.plan)
        # 修复前 G80 把 motion 置 -1，省略 G 代码的段被误判成切削
        self.assertEqual(moves[-1].kind, "rapid")

    def test_comment_cannot_inject_program_lines(self):
        evil = "ROUGH\nM30\nG91 G00 Z-10\n( ) ; %"
        safe = _sanitize_comment(evil)
        for ch in ("\n", "\r", "%", "(", ")"):
            self.assertNotIn(ch, safe)
        self.assertNotIn("\n", FanucPost(self.r.plan).comment(evil))

    def test_stage_is_whitelisted_by_clamp(self):
        import copy
        plan = copy.deepcopy(self.r.plan)
        op = plan.operations[0]
        op.stage = "rough\nM30"
        clamp_plan(plan)
        self.assertEqual(op.stage, "rough")

    def test_zero_dimension_is_not_replaced_by_default(self):
        import copy
        plan = copy.deepcopy(self.r.plan)
        before = [(f.width, f.height, f.length) for f in plan.part.features]
        clamp_plan(plan)
        after = [(f.width, f.height, f.length) for f in plan.part.features]
        self.assertEqual(before, after)

    def test_simulation_grid_has_upper_bound(self):
        import math
        for bad in (0, 0.001, -1, float("nan"), 0.5):
            r = _safe_resolution(bad, 5000, 5000)
            self.assertTrue(math.isfinite(r) and r >= 0.1)
            self.assertLessEqual((10000 / r) ** 2, 4_000_000 * 1.01)

    def test_partial_arc_not_sampled_as_full_circle(self):
        text = ("%\nO0001\nG21 G17 G40 G49 G80 G90\nG54\n"
                "G00 X0.0 Y0.0 Z20.0\nS1000 M03\n"
                "G01 Z-1.0 F100\nG02 X10.0 Y10.0 I10.0 J0.0\n"
                "G00 Z20.0\nM05\nM30\n%")
        rep = Validator(self.r.plan).validate(text, "FANUC")
        # 90° 弧从 (0,0) 到 (10,10)；按整圆采样会虚报 X 到 20
        self.assertLessEqual(rep.stats["coord_max"]["X"], 10.01)

    def test_settings_clamp_invalid_resolution(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"SIM_RESOLUTION": "0"}):
            self.assertGreaterEqual(get_settings().simulator_resolution, 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
