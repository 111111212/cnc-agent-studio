"""主 Agent：编排五阶段链路 + 校验/仿真失败后的自修复闭环。

安全策略（项目最核心的主张）：
  1. 任一关卡未通过，程序绝不进入"可下发"状态；
  2. 修复轮次用尽仍失败 -> 明确拒绝下发（fail-closed），只输出诊断报告；
  3. 下发前必须生成带哈希的人工复核清单，AI 无权绕过人工确认。
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import OUTPUT_DIR, Settings, get_settings
from .cutting import clamp_plan, estimate_cycle_time
from .generator import FAULT_LIBRARY, inject_faults, make_post
from .geometry import generate_toolpaths, geometric_warnings
from .inputs import parse_request
from .machines import (MachineProfile, capability_summary, effective_cutting_policy,
                       get_profile)
from .models import PartSpec, ProcessPlan, Program, VMC850
from .planner import PlannerRouter, inject_hallucination
from .simulator import SimulationResult, simulate_gcode
from .validator import ValidationReport, Validator


@dataclass
class StageRecord:
    name: str
    status: str                  # pass | fail | repaired | blocked
    detail: str
    issues: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__


@dataclass
class RunResult:
    part: PartSpec
    plan: ProcessPlan
    program: Program | None
    validation: ValidationReport | None
    simulation: SimulationResult | None
    stages: list[StageRecord]
    attempts: int
    approved: bool
    controller: str
    output_files: dict[str, str] = field(default_factory=dict)
    blocked_reason: str = ""
    repair_log: list[dict[str, Any]] = field(default_factory=list)
    profile: MachineProfile | None = None

    @property
    def safe_to_deliver(self) -> bool:
        return bool(self.approved and self.program and
                    self.validation and self.validation.ok and
                    self.simulation and self.simulation.ok)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _local_repair(plan: ProcessPlan, codes: set[str], hints: list[str],
                  round_no: int, machine=VMC850) -> ProcessPlan:
    """确定性修复：把机器检出的证据映射成具体的工艺参数调整。

    不是"再问一次模型碰运气"，而是每条修复都有明确工程依据。
    """
    p = copy.deepcopy(plan)
    actions: list[str] = []

    if codes & {"SIM_UNDERCUT", "UNDERCUT"}:
        for op in p.operations:
            op.ae = round(max(0.5, op.ae * 0.65), 3)
        actions.append("行距 ae x0.65，增加清根覆盖率")
    if codes & {"SIM_OVERCUT", "SIM_STRAY_CUT", "SIM_CUT_THROUGH",
                "OVERCUT", "CUT_THROUGH"}:
        for op in p.operations:
            op.ap = round(max(0.1, op.ap * 0.6), 3)
            op.tool_number = 2 if op.tool_number in (3, 4) else op.tool_number
        p.approach = "ramp"
        actions.append("切深 ap x0.6 / 换小刀 / 强制斜线下刀")
    if codes & {"SIM_RAPID_CRASH", "SIM_RAPID_PLUNGE", "RAPID_IN_MATERIAL",
                "RAPID_PLUNGE", "SIM_CLAMP_HIT"}:
        p.safe_z = min(40.0, p.safe_z + 5.0)
        p.clearance_z = min(10.0, p.clearance_z + 2.0)
        p.approach = "ramp"
        actions.append(f"安全高度提高到 Z{p.safe_z:g}，全部改斜线下刀")
    if codes & {"OVERTRAVEL", "SIM_OVERTRAVEL"}:
        for op in p.operations:
            op.ae = round(max(0.5, op.ae * 0.8), 3)
        actions.append("收窄走刀范围，复核工件坐标系偏置")
    if codes & {"SPINDLE_OVER", "FEED_OVER", "FEED_ZERO", "BAD_TOOL",
                "UNKNOWN_G", "UNKNOWN_M", "DIALECT_MISMATCH", "NO_END",
                "MISSING_D", "MISSING_H", "ARC_NO_CENTER"}:
        # 这类错误源于被污染/错误拼接的程序段：确定性后处理器重新生成即可根除，
        # 参数层再夹紧一次，保证不会把幻觉参数带进新程序。
        actions.append("丢弃被污染程序，由确定性后处理器重新生成并二次参数夹紧")
    if not actions:
        actions.append("按反馈重新规划一轮并重新夹紧全部参数")

    note = (f"[第{round_no}轮自修复] 依据校验/仿真证据：" +
            "；".join(actions) + "。证据：" + " | ".join(hints[:3]))
    p.reasoning = (p.reasoning + " " + note)[-2000:]

    # 所有修复结果必须重新过夹紧器，不允许修复动作本身引入越界参数
    return clamp_plan(p, machine)


class CNCAgent:
    def __init__(self, settings: Settings | None = None, profile=None):
        self.settings = settings or get_settings()
        self.router = PlannerRouter(self.settings)
        self.profile = profile or get_profile(self.settings.machine_id)

    def run(
        self,
        request: str,
        source: str = "auto",
        controller: str | None = None,
        inject: dict[str, Any] | None = None,
        faults: list[str] | None = None,
        program_number: int = 1,
        write_files: bool = True,
    ) -> RunResult:
        t0 = time.perf_counter()
        machine = self.profile.spec
        # 方言默认跟随机型；命令行 --controller 或 .env 的 CNC_CONTROLLER 可覆盖
        ctrl = (controller or self.settings.controller or self.profile.post).upper()
        stages: list[StageRecord] = []
        repair_log: list[dict[str, Any]] = []

        def stage(name: str, status: str, detail: str,
                  issues: list[str] | None = None, start: float | None = None) -> None:
            stages.append(StageRecord(
                name=name, status=status, detail=detail,
                issues=issues or [],
                elapsed_ms=round((time.perf_counter() - (start or t0)) * 1000, 1)))

        # ---------------- 阶段 1 输入层 ----------------
        s = time.perf_counter()
        parsed = parse_request(request, source)
        if isinstance(parsed, dict):
            raise ValueError("该输入是旧 G 代码体检结果，请用 diagnose_legacy() 处理")
        part: PartSpec = parsed
        geom_issues = geometric_warnings(ProcessPlan(part=part, operations=[]))
        stage("阶段1 输入解析", "pass",
              f"来源 {part.source}｜{part.material.name}｜毛坯 "
              f"{part.stock_x:g}x{part.stock_y:g}x{part.stock_z:g}mm｜"
              f"{len(part.features)} 个加工特征",
              part.warnings + geom_issues, s)
        if geom_issues:
            return RunResult(part, ProcessPlan(part=part, operations=[]), None,
                             None, None, stages, 0, False, ctrl,
                             blocked_reason="零件几何本身不可加工：" + "；".join(geom_issues))

        # ---------------- 阶段 2 工艺规划 ----------------
        s = time.perf_counter()
        plan = self.router.plan(part)
        # 机型专属工艺策略：钻攻中心/雕铣中心按刀柄刚性收紧 ap/ae/F
        policy = effective_cutting_policy(self.profile)
        if policy["ap_factor"] != 1.0:
            for op in plan.operations:
                op.ap = round(op.ap * policy["ap_factor"], 3)
                op.ae = round(op.ae * policy["ae_factor"], 3)
                op.feed = max(80, int(op.feed * policy["feed_factor"] / 10) * 10)
            plan.reasoning += (f" [机型策略] {self.profile.display}：{policy['reason']}")
        if inject:
            plan = inject_hallucination(plan, inject)
        plan = clamp_plan(plan, machine)       # 参数夹紧层拦截幻觉
        clamp_notes = [f"{op.id}: {a}" for op in plan.operations
                       for a in op.adjustments]
        stage("阶段2 工艺规划 + 参数夹紧", "pass",
              f"机型 {self.profile.display}｜{self.profile.controller}｜"
              f"规划器 {plan.planner}｜{len(plan.operations)} 道工序｜"
              f"密钥 {self.router.key_display}｜夹紧 {len(clamp_notes)} 项",
              clamp_notes + [policy["reason"]], s)

        validator = Validator(plan, machine)
        program: Program | None = None
        val_report: ValidationReport | None = None
        sim: SimulationResult | None = None
        feedback: list[str] = []
        previous_json = plan.raw_llm_json
        rounds = self.settings.max_repair_rounds
        blocked = ""

        for attempt in range(1, rounds + 1):
            # ---------------- 阶段 3 生成 + 静态校验 ----------------
            s = time.perf_counter()
            toolpaths = generate_toolpaths(plan)
            post = make_post(ctrl, plan, self.profile)
            program = post.generate(toolpaths, program_number)
            program.attempts = attempt
            # 只在首轮注入"幻觉 G 代码"，模拟 AI 直接写程序时的污染
            if faults and attempt == 1:
                program.text = inject_faults(program.text, faults)
            val_report = validator.validate(program.text, ctrl)
            val_hints = val_report.repair_hints()
            if val_report.ok:
                stage(f"阶段3 G代码生成/校验 (第{attempt}轮)",
                      "repaired" if attempt > 1 else "pass",
                      f"{ctrl} 后置｜{program.block_count} 个程序段｜"
                      f"行程 X[{val_report.stats['coord_min']['X']}, "
                      f"{val_report.stats['coord_max']['X']}]｜"
                      f"转速 {val_report.stats['spindle_range']}｜"
                      f"进给 {val_report.stats['feed_range']}",
                      [str(w) for w in val_report.warnings], s)
            else:
                stage(f"阶段3 G代码生成/校验 (第{attempt}轮)", "fail",
                      f"拦截 {len(val_report.errors)} 项语法/行程/安全错误",
                      [str(e) for e in val_report.errors], s)
                feedback += val_hints
                repair_log.append({"round": attempt, "gate": "validator",
                                   "issues": val_hints})
                plan = self._repair_plan(part, plan, plan, codes_of(val_report),
                                         feedback, attempt)
                validator = Validator(plan, machine)
                continue

            # ---------------- 阶段 4 材料去除仿真 ----------------
            s = time.perf_counter()
            sim = simulate_gcode(
                program.text, plan, ctrl, machine=machine,
                resolution=self.settings.simulator_resolution,
                strict=self.settings.strict_simulation)
            sim_hints = sim.repair_hints()
            if sim.ok:
                stage(f"阶段4 体素仿真 (第{attempt}轮)",
                      "repaired" if attempt > 1 else "pass",
                      f"网格 {sim.stats['grid']}｜{sim.stats['moves']} 段轨迹｜"
                      f"去除体积 {sim.stats['removed_volume_mm3']}mm³ "
                      f"(理论 {sim.stats['expected_volume_mm3']}mm³, "
                      f"误差 {sim.stats['volume_error_pct']}%)｜"
                      f"欠切 {sim.stats['undercut_ratio']}%｜碰撞 0",
                      [i["message"] for i in sim.issues], s)
                break
            stage(f"阶段4 体素仿真 (第{attempt}轮)", "fail",
                  f"拦截 {len(sim.errors)} 项碰撞/过切/欠切问题",
                  sim_hints, s)
            feedback += sim_hints
            repair_log.append({"round": attempt, "gate": "simulator",
                               "issues": sim_hints})
            plan = self._repair_plan(part, plan, plan,
                                     {i["code"] for i in sim.errors},
                                     feedback, attempt)
            validator = Validator(plan, machine)
        else:
            blocked = (f"连续 {rounds} 轮自修复后校验/仿真仍未通过，"
                       f"按 fail-closed 策略拒绝下发机床，请人工工艺评审")

        approved = not blocked and bool(
            val_report and val_report.ok and sim and sim.ok)
        stage("阶段5 下发准入", "pass" if approved else "blocked",
              "校验+仿真双关卡通过，生成带哈希的人工复核清单"
              if approved else blocked, [blocked] if blocked else [])

        result = RunResult(
            part=part, plan=plan, program=program,
            validation=val_report, simulation=sim, stages=stages,
            attempts=min(rounds, len(repair_log) + 1),
            approved=approved, controller=ctrl,
            blocked_reason=blocked, repair_log=repair_log,
            profile=self.profile,
        )
        if write_files:
            from .report import render_report, save_report
            path = save_report(result, self.settings, OUTPUT_DIR)
            result.output_files["report"] = str(path)
            cap_path = OUTPUT_DIR / "capabilities.json"
            cap_path.write_text(
                json.dumps(capability_summary(self.profile), ensure_ascii=False, indent=2),
                encoding="utf-8")
            result.output_files["capabilities"] = str(cap_path)
            nc_path = OUTPUT_DIR / (
                f"{_safe_name(part.name)}_{self.profile.id}_{ctrl}.nc")
            nc_path.parent.mkdir(parents=True, exist_ok=True)
            # 只有双关卡通过才落盘可下发程序；被拦截时写 .blocked.txt 供排查
            target = nc_path if approved else nc_path.with_suffix(".blocked.txt")
            target.write_text((program.text if program else "") +
                              (f"\n(BLOCKED: {blocked})\n" if blocked else ""),
                              encoding="ascii", errors="replace")
            result.output_files["gcode"] = str(target)
            result.output_files["summary"] = str(write_summary(result, OUTPUT_DIR))
        return result

    def _repair_plan(self, part: PartSpec, plan: ProcessPlan,
                     _unused: ProcessPlan, codes: set[str],
                     hints: list[str], round_no: int) -> ProcessPlan:
        """优先让 LLM 依据证据重规划；LLM 不可用/失败时走确定性本地修复。"""
        if self.router.mode == "llm":
            try:
                llm_plan = self.router.plan(part, feedback=hints,
                                            previous_json=plan.raw_llm_json)
                # LLM 的新方案也必须叠加本地工程修复（例如减小行距）
                return _local_repair(llm_plan, codes, hints, round_no,
                                     self.profile.spec)
            except Exception:
                pass
        return _local_repair(plan, codes, hints, round_no, self.profile.spec)


def codes_of(report: ValidationReport) -> set[str]:
    return {e.code for e in report.errors}


def _safe_name(name: str) -> str:
    # 机床侧文件名保持纯 ASCII，中文/空格一律替换
    return ("".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "_"
                    for c in name).strip("_")[:24] or "part")


def write_summary(result: RunResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "approved": result.approved,
        "controller": result.controller,
        "machine": result.profile.to_dict() if result.profile else None,
        "planner": result.plan.planner,
        "attempts": result.attempts,
        "blocked_reason": result.blocked_reason,
        "program_sha256": _hash_text(result.program.text) if result.program else "",
        "stages": [s.to_dict() for s in result.stages],
        "repair_log": result.repair_log,
        "operations": [op.to_dict() for op in result.plan.operations],
        "validation": result.validation.to_dict() if result.validation else None,
        "simulation": result.simulation.stats if result.simulation else None,
        "simulation_issues": result.simulation.issues if result.simulation else [],
        "cycle_time_min": estimate_cycle_time(
            result.plan, result.program.metadata.get("cut_length", 0)) if result.program else 0,
    }
    path = output_dir / "run_summary.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def diagnose_legacy(path: str) -> dict[str, Any]:
    """旧 G 代码 AI 优化/排错入口：读程序 -> 体检 -> 生成修正建议。"""
    from .inputs import parse_legacy_gcode
    info = parse_legacy_gcode(path)
    suggestions: list[str] = []
    if info["spindle_range"] and info["spindle_range"][1] > VMC850.max_spindle:
        suggestions.append(
            f"最高 S{info['spindle_range'][1]} 超过机床 {VMC850.max_spindle} rpm，"
            f"按 n=1000Vc/(πD) 重算并夹紧")
    if info["feed_range"] and info["feed_range"][1] > VMC850.max_feed:
        suggestions.append(
            f"最高 F{info['feed_range'][1]} 超过 {VMC850.max_feed} mm/min，需降进给")
    if info["axis_range"]["Z"] and info["axis_range"]["Z"][0] < VMC850.z_min:
        suggestions.append("Z 已超机床下行程，检查工件坐标系偏置")
    for t in info["tools"]:
        if t > 16:
            suggestions.append(f"T{t:02d} 超出刀库容量，需登记刀具")
    suggestions += [f"疑似危险段：{s}" for s in info["suspicious"]]
    if not suggestions:
        suggestions.append("未发现明显参数越界，建议再走一遍本项目的体素仿真确认形貌")
    return {k: v for k, v in info.items() if k != "text"} | {
        "suggestions": suggestions,
        "note": "旧程序只做参数体检与风险提示，正式优化请用本系统重新规划并双关卡复核",
    }
