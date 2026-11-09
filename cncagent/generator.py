"""阶段 3：确定性 G 代码后处理器（FANUC / SIEMENS 840D / HNC 华中）。

架构结论：模型不写 G 代码，本模块逐行生成。三种"方言"差异集中在
程序头、回零段、刀补调用、固定循环和注释符，是面试里讲"后置处理"的实证。
"""
from __future__ import annotations

import re
from typing import Iterable

from .geometry import Move, Toolpath
from .models import ProcessPlan, Program, get_tool


def fmt(v: float) -> str:
    """三位小数去尾零，整数保留一位小数，符合数控系统可读性习惯。"""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    if not s or s in ("-0", "+"):
        s = "0"
    if "." not in s:
        s += ".0"
    return s


CONTROLLERS = ("FANUC", "SIEMENS", "HNC")


def _sanitize_comment(text: str) -> str:
    """注释内容一律压成单行纯文本。

    模型输出不可信：工序 stage / note / 特征名里若带换行、括号或 '%'，
    未经净化就会在 "\n".join(lines) 时变成真实程序段（M30、G91、G00 Z-10…）
    被下游解析器当指令执行。这是安全边界，不能只靠参数夹紧。
    """
    s = str(text)
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace("(", "[").replace(")", "]").replace(";", ",")
    s = s.replace("%", " ")
    # 去掉其余控制字符，避免控制器把 0x00~0x1F 当作分隔符
    s = "".join(ch if ch >= " " else " " for ch in s)
    return s[:180]


class PostProcessor:
    name = "FANUC"
    comment_open, comment_close = "(", ")"
    metric = "G21"
    program_ext = ".nc"

    def __init__(self, plan: ProcessPlan, profile=None):
        self.plan = plan
        self.profile = profile       # MachineProfile，可为 None（纯后处理单测）
        self.lines: list[str] = []
        self.n = 0

    # -- 方言钩子 ----------------------------------------------------------
    def header(self, program_number: int) -> list[str]:
        raise NotImplementedError

    def footer(self) -> list[str]:
        raise NotImplementedError

    def tool_change(self, tool_number: int, spindle: int) -> list[str]:
        raise NotImplementedError

    def cycle_line(self, m: Move) -> list[str]:
        raise NotImplementedError

    def comment(self, text: str) -> str:
        return f"{self.comment_open}{_sanitize_comment(text)}{self.comment_close}"

    # -- 通用流程 ----------------------------------------------------------
    def emit(self, body: str) -> None:
        self.n += 10
        self.lines.append(f"N{self.n:<4d} {body}")

    def emit_comment(self, text: str) -> None:
        self.lines.append(self.comment(text))

    def _move_lines(self, tp: Toolpath, state: dict) -> Iterable[str]:
        """把 Move 序列翻译成程序段；省略未变化的坐标以体现 G 代码模态特性
        （静态校验器必须实现模态状态机才能正确复原轨迹）。"""
        for m in tp.moves:
            if m.comment and m.kind in ("rapid", "cycle"):
                yield ("COMMENT", m.comment)
            g = {"rapid": "G00", "line": "G01", "arc_cw": "G02",
                 "arc_ccw": "G03"}.get(m.kind)
            if m.kind == "cycle":
                for line in self.cycle_line(m):
                    yield line
                state["x"], state["y"] = m.x, m.y
                # G99 / CYCLE81(RTP=R) 均返回 R 平面；G80 后运动模态回到 G00。
                # 显式写回状态，避免下一段被压成「无 G 代码的裸坐标」，
                # 使静态校验器与仿真器对同一段的快移/切削判定分叉。
                state["z"] = m.r_level
                state["g"] = "G00"
                continue

            parts: list[str] = []
            if g != state["g"]:
                parts.append(g)
                state["g"] = g
            for axis, value in (("X", m.x), ("Y", m.y), ("Z", m.z)):
                # 整圆的终点=起点，但圆弧段必须显式写 X/Y，
                # 否则下游解析器会因"无坐标轴字"把整圆段丢掉。
                force = m.kind.startswith("arc") and axis in ("X", "Y")
                if force or abs(value - state[axis.lower()]) > 1e-6:
                    parts.append(f"{axis}{fmt(value)}")
                    state[axis.lower()] = value
            if m.kind.startswith("arc"):
                parts.append(f"I{fmt(m.i)}")
                parts.append(f"J{fmt(m.j)}")
            if m.kind != "rapid" and abs(m.feed - state["f"]) > 1e-6:
                parts.append(f"F{fmt(m.feed)}")
                state["f"] = m.feed
            if not parts:
                continue
            yield " ".join(parts)

    def generate(self, toolpaths: list[Toolpath], program_number: int = 1) -> Program:
        p = self.plan
        self.lines = []
        self.n = 0
        for line in self.header(program_number):
            self.lines.append(line)
        # 下发机床的程序保持纯 ASCII 注释：多数控制器不支持中文字符集。
        self.emit_comment("=== CNC AGENT SAFE PROGRAM / VERIFIED ===")
        if self.profile:
            pr = self.profile
            self.emit_comment(f"MACHINE {pr.brand} {pr.model} | "
                              f"CTRL {pr.controller} | TAPER {pr.spindle_taper}")
            self.emit_comment(f"POST {self.name} {pr.fidelity.upper()} | "
                              f"TRAVEL {pr.spec.x_max*2:g}x{pr.spec.y_max*2:g} | "
                              f"SMAX {pr.spec.max_spindle} | FMAX {pr.spec.max_feed}")
            if pr.fidelity == "iso_compatible":
                self.emit_comment("NOTICE: ISO-COMPATIBLE POST - VERIFY M-CODES "
                                  "WITH MACHINE BUILDER MANUAL BEFORE RUN")
        self.emit_comment(f"CTRL {self.name} | MAT {p.part.material.key} | "
                          f"STOCK {p.part.stock_x:g}x{p.part.stock_y:g}x{p.part.stock_z:g}")
        self.emit_comment(f"PLANNER {p.planner} | WCS {p.work_offset} | "
                          f"SAFE Z{fmt(p.safe_z)}")
        self.emit_comment("CUTTER COMP: COMPUTER (TOOL CENTER PATH) - NO G41/G42 ISSUED")
        for i, f in enumerate(p.part.features):
            self.emit_comment(f"F{i + 1} {f.kind.value.upper()} "
                              f"D={f.diameter:g} W={f.width:g} H={f.height:g} "
                              f"DEP={f.depth:g} TOL={f.tolerance:g}")
        for t in sorted({tp.tool.number for tp in toolpaths}):
            tool = get_tool(t)
            self.emit_comment(f"T{t:02d} {tool.type.value.upper()} "
                              f"D{tool.diameter:g} Z{tool.flutes} FLUTE")

        init_codes = ("G71 G17 G40 G90" if self.name == "SIEMENS"
                      else f"{self.metric} G17 G40 G49 G80 G90")
        self.emit(init_codes)
        self.emit(p.work_offset)
        self.lines.append("")

        state = {"g": "", "x": 0.0, "y": 0.0, "z": p.safe_z, "f": 0.0}
        current_tool = None
        current_spindle = None
        for tp in toolpaths:
            self.lines.append(self.comment(
                f"--- {tp.op.id} F{tp.op.feature_index + 1} "
                f"{tp.op.stage.upper()} AP{abs(tp.op.ap):g} "
                f"AE{tp.op.ae:g} S{tp.op.spindle} F{tp.op.feed} ---"))
            if tp.tool.number != current_tool:
                for line in self.tool_change(tp.tool.number, tp.op.spindle):
                    self.emit(line)
                current_tool = tp.tool.number
                current_spindle = tp.op.spindle
                state["g"] = ""
                state["z"] = p.safe_z
                self.emit(self._spindle_line(tp.op.spindle))
                if self.name == "SIEMENS":
                    self.emit(f"G00 Z{fmt(p.safe_z)}")
                    state["g"] = "G00"
                else:
                    self.emit(f"G43 H{current_tool:02d} Z{fmt(p.safe_z)}")
                    state["g"] = "G43"
                self.emit(p.coolant)
                state["z"] = p.safe_z
                state["g"] = "G43" if self.name != "SIEMENS" else "G00"
            elif tp.op.spindle != current_spindle:
                self.emit(self._spindle_line(tp.op.spindle))
                current_spindle = tp.op.spindle

            for line in self._move_lines(tp, state):
                if isinstance(line, tuple) and line[0] == "COMMENT":
                    self.lines.append(self.comment(line[1]))
                else:
                    self.emit(line)
            self.lines.append("")

        self.emit("M09")
        for line in self.footer():
            if line.startswith("%"):
                self.lines.append(line)   # 程序结束符不带行号
            else:
                self.emit(line)
        return Program(
            text="\n".join(self.lines).rstrip() + "\n",
            controller=self.name,
            program_number=program_number,
            metadata={"toolpaths": len(toolpaths),
                      "cut_length": round(sum(t.cut_length for t in toolpaths), 1)},
        )

    def _spindle_line(self, spindle: int) -> str:
        return f"S{spindle} M03"


class FanucPost(PostProcessor):
    name = "FANUC"

    def header(self, number: int) -> list[str]:
        return ["%", f"O{number:04d} (CNC_AGENT)"]

    def footer(self) -> list[str]:
        return ["M05", "G91 G28 Z0.", "G90", "M30", "%"]

    def tool_change(self, tool_number: int, spindle: int) -> list[str]:
        return ["M05", f"T{tool_number:02d} M06"]

    def cycle_line(self, m: Move) -> list[str]:
        # 显式声明 G99（返回 R 平面），让校验器 / 仿真器 / 后处理器
        # 对循环后的 Z 位置有同一份约定，而不是各自猜一个默认值。
        return ["G99",
                f"G81 X{fmt(m.x)} Y{fmt(m.y)} Z{fmt(m.z)} "
                f"R{fmt(m.r_level)} F{fmt(m.feed)}", "G80"]


class SiemensPost(PostProcessor):
    """SIEMENS 840D sl：G71 公制、T/D 刀补、CYCLE81 钻孔、分号注释。"""
    name = "SIEMENS"
    comment_open, comment_close = ";", ""
    metric = "G71"

    def header(self, number: int) -> list[str]:
        return [f"%_N_CNCAGENT{number:02d}_MPF", ";$PATH=/_N_MPF_DIR"]

    def footer(self) -> list[str]:
        return ["M05", "G74 Z1=0", "G74 X1=0 Y1=0", "M30"]

    def comment(self, text: str) -> str:
        return "; " + _sanitize_comment(text)

    def tool_change(self, tool_number: int, spindle: int) -> list[str]:
        return ["M05", f"T{tool_number} D1 M06"]

    def cycle_line(self, m: Move) -> list[str]:
        # CYCLE81(RTP, RFP, SDIS, DP, DPR)：只做教学级映射
        # RTP 与 FANUC/HNC 的 G99 保持一致：循环结束退到 R 平面。
        return [f"CYCLE81({fmt(m.r_level)}, {fmt(0.0)}, "
                f"{fmt(m.r_level)}, {fmt(m.z)}, , )"]

    def _spindle_line(self, spindle: int) -> str:
        return f"S{spindle} M03"


class HncPost(FanucPost):
    """华中 HNC-8：FANUC 兼容方言，差异在程序名、回零段与固定循环返回值。"""
    name = "HNC"

    def header(self, number: int) -> list[str]:
        return [f"%{number:04d}", "(HNC-8 POST)"]

    def footer(self) -> list[str]:
        return ["M05", "G91 G28 Z0", "G90", "M30"]

    def cycle_line(self, m: Move) -> list[str]:
        return ["G99", f"G81 X{fmt(m.x)} Y{fmt(m.y)} Z{fmt(m.z)} "
                f"R{fmt(m.r_level)} F{fmt(m.feed)}", "G80"]


def make_post(controller: str, plan: ProcessPlan, profile=None) -> PostProcessor:
    key = controller.upper().strip()
    if key in ("FANUC", "FANUC 0I", "法兰克"):
        return FanucPost(plan, profile)
    if key in ("SIEMENS", "SIEMENS 840D", "西门子", "840D"):
        return SiemensPost(plan, profile)
    if key in ("HNC", "HUAZHONG", "华中"):
        return HncPost(plan, profile)
    raise ValueError(f"不支持的数控系统 {controller!r}，可选 {CONTROLLERS}")


# --------------------------------------------------------------------------
# 幻觉故障注入（仅用于演示，证明校验器 + 仿真确实能拦住错误 G 代码）
# --------------------------------------------------------------------------
FAULT_LIBRARY: dict[str, str] = {
    "overtravel": "G00 X999.0 Y0.0 Z0.0",           # 超出行程
    "plunge": "G00 X10.0 Y10.0 Z-8.0",              # 材料内部快移（扎刀）
    "bad_g": "G99.9 X0.0",                          # 非法 G 指令
    "bad_tool": "T99 M06",                          # 不存在的刀号
    "overspeed": "S30000 M03",                      # 超主轴转速
    "no_return": "G01 Z-50.0 F100",                 # 切穿工件/过切
}


def inject_faults(text: str, faults: list[str]) -> str:
    """把典型"幻觉 G 代码"插到程序尾段之前，供校验器/仿真器检出。"""
    lines = text.splitlines()
    insert_at = max(1, len(lines) - 8)
    payload = [f"(FAULT INJECTION: {f})" for f in faults]
    payload += [FAULT_LIBRARY[f] for f in faults if f in FAULT_LIBRARY]
    lines[insert_at:insert_at] = payload
    return "\n".join(lines) + "\n"


def strip_comments(line: str) -> str:
    """去掉 (...) 与 ; 注释，返回可执行部分。"""
    line = re.sub(r"\([^)]*\)", "", line)
    line = re.sub(r";.*$", "", line)
    return line.strip()
