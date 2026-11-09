"""阶段 3：G 代码静态校验器（大模型幻觉的第一道机器关卡）。

不是简单正则，而是"词法解析 + 模态状态机 + 轨迹复原 + 规则检查"：
  - 复现 G00/G01/G02/G03 与 G90/G91 模态，还原每一段真实坐标
  - 按数控系统方言做白名单检查（西门子不认 O 号/G43 H/G91 G28，反之亦然）
  - 行程、转速、进给、切穿、材料内快移、换刀安全高度、模态冲突
  - 输出结构化 Issue，可直接回传给 Agent 做自修复
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .generator import strip_comments
from .models import MachineSpec, ProcessPlan, TOOLS, VMC850

TOKEN = re.compile(r"([A-Za-z])\s*([-+]?\d+(?:\.\d+)?)")

G_GROUPS: list[tuple[str, set[int]]] = [
    ("运动", {0, 1, 2, 3, 80, 81, 83}),
    ("平面", {17, 18, 19}),
    ("单位", {20, 21, 71}),
    ("回零/参考点", {28, 74}),
    ("刀半径补偿", {40, 41, 42}),
    ("刀长补偿", {43, 44, 49}),
    ("坐标系", {54, 55, 56, 57, 58, 59}),
    ("距离模式", {90, 91}),
    ("固定循环返回", {98, 99}),
]
KNOWN_G = set().union(*(g for _, g in G_GROUPS))
KNOWN_M = {0, 1, 3, 4, 5, 6, 7, 8, 9, 30}

DIALECT_RULES: dict[str, dict[str, Any]] = {
    "FANUC": {"metric": 21, "header": "O", "forbid_tokens": ["CYCLE", "G74", "G71", "D1"],
              "forbid_desc": "FANUC 不识别西门子 CYCLE/G74/G71/T-D 语法"},
    "HNC": {"metric": 21, "header": "%", "forbid_tokens": ["CYCLE", "G74", "G71"],
            "forbid_desc": "华中 HNC 不识别西门子 CYCLE/G74/G71 语法"},
    "SIEMENS": {"metric": 71, "header": "%_N_", "forbid_tokens": ["G43", "G28", "O0001"],
                "forbid_desc": "SINUMERIK 用 G71/T-D/CYCLE81，不识别 FANUC 的 G43 H/G91 G28/O 程序号"},
}


@dataclass
class Issue:
    level: str          # error | warning
    code: str
    line_no: int
    message: str
    suggestion: str = ""
    context: str = ""

    def __str__(self) -> str:
        return f"[{self.level.upper()}][{self.code}] 第{self.line_no}行 {self.message}"


@dataclass
class ValidationReport:
    controller: str
    issues: list[Issue] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def repair_hints(self) -> list[str]:
        return [f"{i.message}（第{i.line_no}行，建议：{i.suggestion}）"
                for i in self.errors]

    def to_dict(self) -> dict[str, Any]:
        return {"controller": self.controller, "ok": self.ok,
                "errors": [i.__dict__ for i in self.errors],
                "warnings": [i.__dict__ for i in self.warnings],
                "stats": self.stats}


@dataclass
class Block:
    line_no: int
    raw: str
    codes: dict[str, list[float]] = field(default_factory=dict)
    special: str = ""

    def get(self, letter: str, default: float | None = None) -> float | None:
        vals = self.codes.get(letter)
        return vals[0] if vals else default

    @property
    def g_codes(self) -> list[int]:
        return [int(v) for v in self.codes.get("G", [])]

    @property
    def m_codes(self) -> list[int]:
        return [int(v) for v in self.codes.get("M", [])]


def parse_program(text: str) -> list[Block]:
    """词法解析：剥离行号/注释/程序名，抽出字母-数值词。"""
    blocks: list[Block] = []
    for no, raw_line in enumerate(text.splitlines(), 1):
        line = strip_comments(raw_line).strip()
        if not line or line.startswith("%") or line.startswith("$"):
            continue
        if line.startswith("O") and re.match(r"O\d{3,5}", line):
            continue  # FANUC 程序名
        line = re.sub(r"^N\s*\d+\s*", "", line)
        if not line:
            continue
        b = Block(line_no=no, raw=raw_line.strip())
        if re.match(r"CYCLE\d+", line, re.IGNORECASE):
            b.special = "CYCLE"
            blocks.append(b)
            continue
        # Siemens G74 Z1=0：去掉 "1=" 这类轴编号后缀
        norm = re.sub(r"([XYZ])\d+=", r"\1", line)
        for letter, value in TOKEN.findall(norm):
            b.codes.setdefault(letter.upper(), []).append(float(value))
        blocks.append(b)
    return blocks


class Validator:
    def __init__(self, plan: ProcessPlan, machine: MachineSpec = VMC850):
        self.plan = plan
        self.machine = machine
        self.controller = ""
        self.dialect = DIALECT_RULES["FANUC"]

    def validate(self, program_text: str, controller: str = "FANUC") -> ValidationReport:
        self.controller = controller.upper()
        self.dialect = DIALECT_RULES.get(self.controller, DIALECT_RULES["FANUC"])
        report = ValidationReport(controller=self.controller)
        blocks = parse_program(program_text)

        state = {
            "x": 0.0, "y": 0.0, "z": self.plan.safe_z,
            "f": 0.0, "s": 0, "motion": 0, "cycle": 0, "absolute": True,
            "cycle_return_r": False,
            "units": self.dialect["metric"], "tool": 0, "spindle_on": False,
            "length_comp": False, "radius_comp": 0,
        }
        seen = {"G21_or_G71": False, "G17": False, "G90": False,
                "G40": False, "M30": False, "M03": False, "G54": False}
        max_coords = {"X": -1e9, "Y": -1e9, "Z": -1e9}
        min_coords = {"X": 1e9, "Y": 1e9, "Z": 1e9}
        s_values: list[int] = []
        f_values: list[int] = []
        motion_blocks = 0

        for b in blocks:
            raw = b.raw
            ln = b.line_no

            # --- 方言检查 --------------------------------------------------
            for token in self.dialect["forbid_tokens"]:
                if token.upper() in raw.upper():
                    report.issues.append(Issue(
                        "error", "DIALECT_MISMATCH", ln,
                        f"方言不匹配：{self.controller} 程序中出现 {token}",
                        f"切换到正确的后置处理器；{self.dialect['forbid_desc']}", raw))
                    break

            if b.special == "CYCLE":
                motion_blocks += 1
                # CYCLE81 的第一个参数是 RTP（退回平面）；静态状态机必须
                # 同步 Z，否则循环之后的相对 Z 判定会整体偏移。
                mm = re.search(r"CYCLE\s*\d+\s*\(([^)]*)\)", raw, re.IGNORECASE)
                if mm:
                    try:
                        rtp = float(mm.group(1).split(",")[0].strip())
                        if math.isfinite(rtp):
                            state["z"] = rtp
                    except (ValueError, IndexError):
                        pass
                continue

            # --- 指令白名单 ------------------------------------------------
            for raw_g in b.codes.get("G", []):
                if raw_g != int(raw_g):
                    report.issues.append(Issue(
                        "error", "UNKNOWN_G", ln, f"非法 G 指令 G{raw_g:g}（G 代码必须为整数）",
                        "删除该指令，核对后置处理配置", raw))
            for raw_m in b.codes.get("M", []):
                if raw_m != int(raw_m):
                    report.issues.append(Issue(
                        "error", "UNKNOWN_M", ln, f"非法 M 指令 M{raw_m:g}",
                        "删除该指令", raw))
            for g in b.g_codes:
                if g not in KNOWN_G:
                    report.issues.append(Issue(
                        "error", "UNKNOWN_G", ln, f"非法/不支持的 G 指令 G{g:g}",
                        "删除该指令或改用三轴铣削支持的 G 代码", raw))
                elif g in (81, 83, 41, 42, 43):
                    pass
            for m in b.m_codes:
                if m not in KNOWN_M:
                    report.issues.append(Issue(
                        "error", "UNKNOWN_M", ln, f"非法/不支持的 M 指令 M{m:g}",
                        "核对机床 PLC 支持的辅助功能", raw))

            # --- 同组模态冲突 ----------------------------------------------
            gs = b.g_codes
            for group_name, group in G_GROUPS:
                hit = [g for g in gs if g in group]
                if len(hit) > 1 and group_name != "运动":
                    report.issues.append(Issue(
                        "error", "MODAL_CONFLICT", ln,
                        f"同一程序段出现同组 G 指令{group_name}：{hit}",
                        "拆成两个程序段", raw))

            # --- 必备指令登记 ----------------------------------------------
            if any(g in (21, 71) for g in gs):
                seen["G21_or_G71"] = True
                state["units"] = 71 if 71 in gs else 21
            if 17 in gs: seen["G17"] = True
            if 90 in gs:
                seen["G90"] = True; state["absolute"] = True
            if 91 in gs: state["absolute"] = False
            if 40 in gs: seen["G40"] = True
            if any(54 <= g <= 59 for g in gs): seen["G54"] = True
            if 3 in b.m_codes: seen["M03"] = True; state["spindle_on"] = True
            if 5 in b.m_codes: state["spindle_on"] = False
            if 30 in b.m_codes: seen["M30"] = True
            for g in gs:
                if g in (0, 1, 2, 3): state["motion"] = g
                # 固定循环：G81/G83 的 Z 是进给孔底，不是 G00 快速扎刀；
                # G80 取消循环，循环期间 XY 定位发生在 R 平面之上，均不适用快移判据
                if g in (81, 83): state["cycle"] = g
                if g == 80: state["cycle"] = 0
                if g == 98: state["cycle_return_r"] = False
                if g == 99: state["cycle_return_r"] = True
                if g in (41, 42): state["radius_comp"] = g
                if g == 40: state["radius_comp"] = 0
                if g == 43: state["length_comp"] = True
                if g == 49: state["length_comp"] = False

            # --- 补偿与刀号 ------------------------------------------------
            tool = b.get("T")
            if tool is not None:
                t_no = int(tool)
                if t_no not in TOOLS:
                    report.issues.append(Issue(
                        "error", "BAD_TOOL", ln, f"刀库中没有 T{t_no:02d}",
                        "改用刀库中已登记的刀具，或先登记刀具几何", raw))
                if state["z"] < self.plan.safe_z - 0.01:
                    report.issues.append(Issue(
                        "error", "UNSAFE_TOOL_CHANGE", ln,
                        f"换刀时 Z={state['z']:.2f} 未回到安全高度 {self.plan.safe_z}",
                        "换刀前先 G00 Z 安全高度", raw))
                state["tool"] = t_no
            if any(g in (41, 42) for g in gs) and b.get("D") is None:
                report.issues.append(Issue(
                    "error", "MISSING_D", ln, "启用 G41/G42 刀半径补偿但未指定 D 号",
                    "补上 D01 或在程序中取消半径补偿", raw))
            if 43 in gs and b.get("H") is None:
                report.issues.append(Issue(
                    "error", "MISSING_H", ln, "启用 G43 刀长补偿但未指定 H 号",
                    "补上 H01（对应刀号）", raw))
            if any(g in (2, 3) for g in gs):
                if b.get("I") is None and b.get("J") is None and b.get("R") is None:
                    report.issues.append(Issue(
                        "error", "ARC_NO_CENTER", ln, "圆弧插补缺少 I/J 或 R 参数",
                        "补全圆心增量 I/J 或半径 R", raw))

            # --- S / F 极限 ------------------------------------------------
            s = b.get("S")
            if s is not None:
                s_values.append(int(s))
                if s > self.machine.max_spindle:
                    report.issues.append(Issue(
                        "error", "SPINDLE_OVER", ln,
                        f"S{int(s)} 超过机床最高转速 {self.machine.max_spindle} rpm",
                        "按 n=1000Vc/(pi*D) 重算并夹紧", raw))
                if s < self.machine.min_spindle:
                    report.issues.append(Issue(
                        "warning", "SPINDLE_LOW", ln,
                        f"S{int(s)} 低于主轴最低稳定转速", "提高转速或改用更大刀具", raw))
            f = b.get("F")
            if f is not None:
                f_values.append(int(f))
                state["f"] = f
                if f > self.machine.max_feed:
                    report.issues.append(Issue(
                        "error", "FEED_OVER", ln,
                        f"F{int(f)} 超过最大切削进给 {self.machine.max_feed} mm/min",
                        "按 Vf=fz*z*n 重算并夹紧", raw))
                if f <= 0:
                    report.issues.append(Issue(
                        "error", "FEED_ZERO", ln, "切削进给为 0",
                        "设置合法 F 值", raw))

            # --- 坐标复原与轨迹检查 -----------------------------------------
            has_axis = any(k in b.codes for k in ("X", "Y", "Z"))
            is_arc = state["motion"] in (2, 3) and \
                (b.get("I") is not None or b.get("J") is not None)
            if not has_axis and not is_arc:
                continue  # 圆弧段即使省略终点坐标也要做行程采样
            prev = (state["x"], state["y"], state["z"])
            target = []
            for axis in "XYZ":
                v = b.get(axis)
                if v is None:
                    target.append(state[axis.lower()])
                elif state["absolute"]:
                    target.append(v)
                else:
                    target.append(state[axis.lower()] + v)  # G91 增量
            nx, ny, nz = target

            # 行程（含圆弧采样）
            samples = [(nx, ny, nz)]
            if state["motion"] in (2, 3) and (b.get("I") is not None or b.get("J") is not None):
                cx = prev[0] + (b.get("I") or 0.0)
                cy = prev[1] + (b.get("J") or 0.0)
                r = math.hypot(prev[0] - cx, prev[1] - cy)
                a0 = math.atan2(prev[1] - cy, prev[0] - cx)
                # 按终点角度求实际扫掠角：90° 圆角弧不能被当成整圆采样，
                # 否则靠近行程边界时会虚报 OVERTRAVEL，与仿真器结论分叉。
                if abs(nx - prev[0]) + abs(ny - prev[1]) < 1e-6:
                    sweep = -2 * math.pi if state["motion"] == 2 else 2 * math.pi
                else:
                    sweep = math.atan2(ny - cy, nx - cx) - a0
                    if state["motion"] == 2 and sweep > 0:
                        sweep -= 2 * math.pi
                    if state["motion"] == 3 and sweep < 0:
                        sweep += 2 * math.pi
                for k in range(1, 9):
                    a = a0 + sweep * k / 8
                    samples.append((cx + r * math.cos(a), cy + r * math.sin(a), nz))
            for sx, sy, sz in samples:
                for axis, v in (("X", sx), ("Y", sy), ("Z", sz)):
                    max_coords[axis] = max(max_coords[axis], v)
                    min_coords[axis] = min(min_coords[axis], v)
                if not (self.machine.x_min <= sx <= self.machine.x_max):
                    report.issues.append(Issue(
                        "error", "OVERTRAVEL", ln,
                        f"X={sx:.2f} 超出行程 [{self.machine.x_min}, {self.machine.x_max}]",
                        "检查工件坐标系偏置与特征位置", raw))
                if not (self.machine.y_min <= sy <= self.machine.y_max):
                    report.issues.append(Issue(
                        "error", "OVERTRAVEL", ln,
                        f"Y={sy:.2f} 超出行程 [{self.machine.y_min}, {self.machine.y_max}]",
                        "检查工件坐标系偏置与特征位置", raw))
                if not (self.machine.z_min <= sz <= self.machine.z_max):
                    report.issues.append(Issue(
                        "error", "OVERTRAVEL", ln,
                        f"Z={sz:.2f} 超出行程 [{self.machine.z_min}, {self.machine.z_max}]",
                        "修正下刀深度", raw))

            # 切穿工件
            floor = -self.plan.part.stock_z
            if nz < floor - 0.001 and (state["motion"] != 0 or state["cycle"]):
                report.issues.append(Issue(
                    "error", "CUT_THROUGH", ln,
                    f"切削 Z={nz:.2f} 低于毛坯底面 {floor:.2f}，会切穿工作台",
                    "减小切深或增加分层", raw))

            # 材料内快速移动 / 快速扎刀
            in_stock = (abs(nx) <= self.plan.part.stock_x / 2 + 1 and
                        abs(ny) <= self.plan.part.stock_y / 2 + 1)
            lateral = abs(nx - prev[0]) + abs(ny - prev[1]) > 0.01
            if state["motion"] == 0 and not state["cycle"] and in_stock:
                if lateral and min(prev[2], nz) < -0.05:
                    report.issues.append(Issue(
                        "error", "RAPID_IN_MATERIAL", ln,
                        f"G00 在材料内横向快移（Z={min(prev[2], nz):.2f}），会撞刀",
                        "先抬刀到安全高度再横移", raw))
                if nz < prev[2] - 0.01 and nz < -0.05:
                    report.issues.append(Issue(
                        "error", "RAPID_PLUNGE", ln,
                        f"G00 快速下刀至 Z={nz:.2f} 进入材料",
                        "改用 G01 以下刀进给进刀，或螺旋/斜线下刀", raw))

            # 固定循环执行完按 G98/G99 返回初始平面或 R 平面，
            # 状态机不能停在孔底，否则后续段的相对 Z 判定会整体偏移
            if state["cycle"] and any(g in (81, 83) for g in b.g_codes):
                nz = (b.get("R") if state["cycle_return_r"]
                      and b.get("R") is not None else self.plan.safe_z)
            state["x"], state["y"], state["z"] = nx, ny, nz
            motion_blocks += 1

        # --- 程序级完整性 ---------------------------------------------------
        if not seen["G21_or_G71"]:
            report.issues.append(Issue("error", "NO_METRIC", 0,
                                       "缺少公制指令 G21/G71", "程序头补 G21（西门子补 G71）"))
        if not seen["G17"]:
            report.issues.append(Issue("warning", "NO_PLANE", 0,
                                       "未显式选择 G17 XY 平面", "程序头补 G17"))
        if not seen["G90"]:
            report.issues.append(Issue("error", "NO_ABS", 0,
                                       "缺少 G90 绝对编程", "程序头补 G90"))
        if not seen["G40"]:
            report.issues.append(Issue("warning", "NO_CANCEL_COMP", 0,
                                       "未显式取消刀补 G40", "程序头补 G40"))
        if not seen["G54"]:
            report.issues.append(Issue("warning", "NO_OFFSET", 0,
                                       "未指定工件坐标系 G54", "补 G54"))
        if not seen["M03"]:
            report.issues.append(Issue("error", "NO_SPINDLE", 0,
                                       "程序中没有 M03 主轴正转", "补 S... M03"))
        if not seen["M30"]:
            report.issues.append(Issue("error", "NO_END", 0,
                                       "缺少 M30 程序结束", "程序尾补 M30"))
        if state["radius_comp"] != 0:
            report.issues.append(Issue("error", "COMP_NOT_CANCELLED", 0,
                                       "程序结束时刀半径补偿仍处于开启状态",
                                       "结束前补 G40"))
        if state["absolute"] is False:
            report.issues.append(Issue("error", "INCREMENTAL_LEFT", 0,
                                       "程序结束时仍处于 G91 增量模式",
                                       "补 G90 恢复绝对模式"))

        report.stats = {
            "blocks": len(blocks),
            "motion_blocks": motion_blocks,
            "controller": self.controller,
            "coord_min": {k: round(v, 2) for k, v in min_coords.items()},
            "coord_max": {k: round(v, 2) for k, v in max_coords.items()},
            "spindle_range": (min(s_values), max(s_values)) if s_values else None,
            "feed_range": (min(f_values), max(f_values)) if f_values else None,
            "tools_used": sorted({int(v[0]) for b in blocks
                                  for k, v in b.codes.items() if k == "T"}),
        }
        return report
