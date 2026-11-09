"""阶段 4：2.5D 体素材料去除仿真（第二道安全关卡）。

输入是**校验后的 G 代码文本**，通过模态状态机复原刀具轨迹再做材料去除，
保证"仿真跑的程序"与"下发机床的程序"是同一个文件，而不是内部理想刀路对象。

检测能力：
  - G00 快移撞入材料 / 快速扎刀（撞刀）
  - 过切（切深超过目标面 / 切到不该切的区域）
  - 欠切残留（该去的地方没去干净）
  - 切穿毛坯底面、超出行程、夹具区域干涉
  - 体积守恒校验（实际去除量 vs 理论去除量）
"""
from __future__ import annotations

import math
import re
import zlib
import struct
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .models import FeatureKind, MachineSpec, ProcessPlan, TOOLS, VMC850
from .validator import parse_program


@dataclass
class SimMove:
    x: float
    y: float
    z: float
    kind: str          # rapid | cut | cycle
    radius: float
    feed: float = 0.0
    tool: int = 1
    line_no: int = 0
    return_z: float | None = None   # 固定循环退刀后的 Z 平面


@dataclass
class SimulationResult:
    ok: bool
    issues: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    height: np.ndarray | None = None
    target: np.ndarray | None = None
    extent: tuple[float, float, float, float] = (0, 0, 0, 0)
    trajectories: list[list[tuple[float, float, str]]] = field(default_factory=list)
    collision_points: list[tuple[float, float]] = field(default_factory=list)

    @property
    def errors(self) -> list[dict[str, Any]]:
        return [i for i in self.issues if i["level"] == "error"]

    def repair_hints(self) -> list[str]:
        return [i["message"] for i in self.errors]


# --------------------------------------------------------------------------
# G 代码 -> 刀具轨迹（模态状态机，与校验器同源）
# --------------------------------------------------------------------------
def gcode_trajectory(text: str, plan: ProcessPlan) -> list[SimMove]:
    blocks = parse_program(text)
    moves: list[SimMove] = []
    st = {"x": 0.0, "y": 0.0, "z": plan.safe_z,
          "motion": 0, "absolute": True, "tool": 1, "f": 0.0,
          "prev_motion": 0, "cycle_return_r": False}
    prev = (st["x"], st["y"], st["z"])

    for b in blocks:
        ln = b.line_no
        if b.special == "CYCLE":
            # CYCLE81(RTP, RFP, SDIS, DP, DPR)：必须按参数位置取值。
            # 用 re.findall 会把行号 N150 与循环名里的 "81" 一起算进数字序列，
            # 导致 DP 取到 0.0、SDIS 取到 20.0，孔加工被仿真成完全没切削。
            mm = re.search(r"CYCLE\s*\d+\s*\(([^)]*)\)", b.raw, re.IGNORECASE)
            params = [s.strip() for s in mm.group(1).split(",")] if mm else []

            def _param(i: int, default: float) -> float:
                try:
                    v = float(params[i])
                    return v if math.isfinite(v) else default
                except (IndexError, ValueError):
                    return default

            rtp = _param(0, plan.clearance_z)
            depth = min(_param(3, -1.0), rtp - 0.1)
            tool = TOOLS.get(st["tool"], TOOLS[1])
            moves.append(SimMove(st["x"], st["y"], depth, "cycle",
                                 tool.diameter / 2, st["f"], st["tool"], ln,
                                 return_z=rtp))
            st["z"] = rtp
            prev = (st["x"], st["y"], st["z"])
            continue

        gs = b.g_codes
        if 90 in gs: st["absolute"] = True
        if 91 in gs: st["absolute"] = False
        if 98 in gs: st["cycle_return_r"] = False
        if 99 in gs: st["cycle_return_r"] = True
        for g in gs:
            if g in (0, 1, 2, 3, 81, 83):
                if g in (81, 83) and st["motion"] in (0, 1, 2, 3):
                    st["prev_motion"] = st["motion"]
                st["motion"] = g
            elif g == 80:
                # G80 只取消固定循环，运动模态应回到循环之前的值；
                # 置 -1 会让后续省略 G 代码的段被误判为切削、绕过撞刀检测。
                st["motion"] = st["prev_motion"]
        t = b.get("T")
        if t is not None:
            st["tool"] = int(t)
        f = b.get("F")
        if f is not None:
            st["f"] = f
        tool = TOOLS.get(st["tool"], TOOLS[1])
        radius = tool.diameter / 2

        coords = {}
        for axis in "XYZ":
            v = b.get(axis)
            coords[axis] = (v if st["absolute"] else st[axis.lower()] + v) \
                if v is not None else st[axis.lower()]

        motion = st["motion"]
        is_arc = motion in (2, 3) and (b.get("I") is not None or b.get("J") is not None)
        if not any(k in b.codes for k in ("X", "Y", "Z")) and not is_arc:
            continue
        nx, ny, nz = coords["X"], coords["Y"], coords["Z"]

        if motion in (81, 83):       # 固定循环：一次钻到底，按 G98/G99 退刀
            # 返回平面只取程序文本自身的信息：G99 用 R，G98 用进入循环
            # 之前的 Z（初始平面）。不依赖 plan.safe_z —— 自修复轮次里
            # plan 可能与当前程序文本不同步。
            r = b.get("R")
            return_z = (r if st["cycle_return_r"] and r is not None
                        else st["z"])
            moves.append(SimMove(nx, ny, nz, "cycle", radius,
                                 st["f"], st["tool"], ln,
                                 return_z=return_z))
            nz = return_z
        elif motion in (2, 3) and (b.get("I") is not None or b.get("J") is not None):
            cx = prev[0] + (b.get("I") or 0.0)
            cy = prev[1] + (b.get("J") or 0.0)
            r = math.hypot(prev[0] - cx, prev[1] - cy)
            if r < 1e-6:
                moves.append(SimMove(nx, ny, nz, "cut", radius, st["f"], st["tool"], ln))
            else:
                a0 = math.atan2(prev[1] - cy, prev[0] - cx)
                if abs(nx - prev[0]) + abs(ny - prev[1]) < 1e-6:
                    # 整圆：终点=起点，用 I/J 定义的圆心走满 360°
                    sweep = -2 * math.pi if motion == 2 else 2 * math.pi
                else:
                    sweep = math.atan2(ny - cy, nx - cx) - a0
                    if motion == 2 and sweep > 0:
                        sweep -= 2 * math.pi
                    if motion == 3 and sweep < 0:
                        sweep += 2 * math.pi
                steps = max(8, int(abs(sweep) / (math.pi / 12)))
                for k in range(1, steps + 1):
                    a = a0 + sweep * k / steps
                    zz = prev[2] + (nz - prev[2]) * k / steps
                    moves.append(SimMove(cx + r * math.cos(a), cy + r * math.sin(a),
                                         zz, "cut", radius, st["f"], st["tool"], ln))
        else:
            kind = "rapid" if motion == 0 else "cut"
            moves.append(SimMove(nx, ny, nz, kind, radius, st["f"], st["tool"], ln))

        prev = (nx, ny, nz)
        st["x"], st["y"], st["z"] = nx, ny, nz
    return moves


# --------------------------------------------------------------------------
# 目标几何（允许被去除到的深度）
# --------------------------------------------------------------------------
def build_target(plan: ProcessPlan, xs: np.ndarray,
                 ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    X, Y = np.meshgrid(xs, ys)
    p = plan.part
    target = np.zeros_like(X)
    inside_stock = (np.abs(X) <= p.stock_x / 2) & (np.abs(Y) <= p.stock_y / 2)
    for f in p.features:
        z = -min(f.depth, p.stock_z)
        if f.kind == FeatureKind.FACE:
            mask = inside_stock.copy()
        elif f.kind in (FeatureKind.CIRCLE_POCKET, FeatureKind.HOLE):
            mask = (X - f.cx) ** 2 + (Y - f.cy) ** 2 <= (f.diameter / 2) ** 2
        elif f.kind == FeatureKind.RECT_POCKET:
            mask = (np.abs(X - f.cx) <= f.width / 2) & \
                   (np.abs(Y - f.cy) <= f.height / 2)
        elif f.kind == FeatureKind.SLOT:
            a = math.radians(f.angle)
            dx, dy = X - f.cx, Y - f.cy
            u = dx * math.cos(a) + dy * math.sin(a)
            v = -dx * math.sin(a) + dy * math.cos(a)
            mask = (np.abs(u) <= f.length / 2) & (np.abs(v) <= f.width / 2)
        else:
            mask = np.zeros_like(X, dtype=bool)
        target = np.where(mask, np.minimum(target, z), target)
    return target, inside_stock


def _dilate(mask: np.ndarray, iterations: int = 1) -> np.ndarray:
    """简单十字膨胀，用于给网格离散化留几何容差。"""
    out = mask.copy()
    for _ in range(iterations):
        n = out.copy()
        n[1:, :] |= out[:-1, :]
        n[:-1, :] |= out[1:, :]
        n[:, 1:] |= out[:, :-1]
        n[:, :-1] |= out[:, 1:]
        out = n
    return out


MAX_GRID_CELLS = 4_000_000       # 约 16MB float32 主网格 + 若干中间数组


def _clamp_stock(part, machine) -> None:
    """夹紧毛坯尺寸：非有限值 / 非正数回退默认，过大值截断到机床行程尺度。"""
    span = max(machine.x_max - machine.x_min,
               machine.y_max - machine.y_min, 100.0)
    for attr, fallback, upper in (("stock_x", 100.0, span),
                                  ("stock_y", 100.0, span),
                                  ("stock_z", 20.0, span)):
        try:
            v = float(getattr(part, attr, fallback))
        except (TypeError, ValueError):
            v = fallback
        if not math.isfinite(v) or v <= 0:
            v = fallback
        setattr(part, attr, min(v, upper))


def _safe_resolution(resolution: float, half_x: float, half_y: float) -> float:
    """把分辨率钳到 [0.1, 2.0]，并保证网格总格数不超过上限。"""
    r = resolution if math.isfinite(resolution) and resolution > 0 else 0.5
    r = min(max(r, 0.1), 2.0)
    span_x, span_y = max(half_x * 2, 1.0), max(half_y * 2, 1.0)
    if span_x * span_y / (r * r) > MAX_GRID_CELLS:
        r = max(r, math.sqrt(span_x * span_y / MAX_GRID_CELLS))
    return r


# --------------------------------------------------------------------------
# 仿真主体
# --------------------------------------------------------------------------
def simulate_gcode(text: str, plan: ProcessPlan, controller: str = "FANUC",
                   machine: MachineSpec = VMC850,
                   resolution: float = 0.5,
                   strict: bool = True) -> SimulationResult:
    p = plan.part
    _clamp_stock(p, machine)
    pad = max(16.0, max(t.diameter for t in TOOLS.values()))
    half_x, half_y = p.stock_x / 2 + pad, p.stock_y / 2 + pad
    # 毛坯尺寸与分辨率都可能来自 LLM / DXF / 面板参数，直接 np.arange
    # 会分配 GB 级数组甚至 OOM；在此统一收口。
    resolution = _safe_resolution(resolution, half_x, half_y)
    xs = np.arange(-half_x, half_x + resolution / 2, resolution)
    ys = np.arange(-half_y, half_y + resolution / 2, resolution)
    ny, nx = len(ys), len(xs)
    H = np.zeros((ny, nx), dtype=np.float32)          # 当前材料顶面
    target, stock_mask = build_target(plan, xs, ys)
    allowed = _dilate(target < -1e-6, iterations=2)   # 允许被切削的区域（含离散容差）
    floor = -p.stock_z

    issues: list[dict[str, Any]] = []
    moves = gcode_trajectory(text, plan)
    collisions: list[tuple[float, float]] = []
    trajectories: list[list[tuple[float, float, str]]] = []
    current_path: list[tuple[float, float, str]] = []
    max_tool_radius = max(m.radius for m in moves) if moves else 5.0
    rapid_in_material = 0
    rapid_plunge = 0
    clamp_hits = 0
    out_of_travel = 0
    cycle_count = 0

    def grid_index(x: float, y: float) -> tuple[int, int]:
        return int(round((x + half_x) / resolution)), int(round((y + half_y) / resolution))

    def cut_disk(x: float, y: float, z: float, radius: float,
                 line_no: int) -> None:
        """刀具圆柱扫过：区域内材料顶面降到刀底 Z（只切不改高）。"""
        nonlocal H
        i0 = max(0, int((x - radius + half_x) / resolution) - 1)
        i1 = min(nx, int((x + radius + half_x) / resolution) + 2)
        j0 = max(0, int((y - radius + half_y) / resolution) - 1)
        j1 = min(ny, int((y + radius + half_y) / resolution) + 2)
        if i0 >= i1 or j0 >= j1:
            return
        subx = xs[i0:i1][None, :]
        suby = ys[j0:j1][:, None]
        disk = (subx - x) ** 2 + (suby - y) ** 2 <= radius ** 2
        sub = H[j0:j1, i0:i1]
        np.copyto(sub, z, where=disk & (sub > z))
        H[j0:j1, i0:i1] = sub

    def material_height(x: float, y: float, radius: float) -> float:
        """刀具覆盖范围内的最高材料面（判断快移是否撞入材料）。"""
        i0 = max(0, int((x - radius + half_x) / resolution) - 1)
        i1 = min(nx, int((x + radius + half_x) / resolution) + 2)
        j0 = max(0, int((y - radius + half_y) / resolution) - 1)
        j1 = min(ny, int((y + radius + half_y) / resolution) + 2)
        if i0 >= i1 or j0 >= j1:
            return 0.0
        subx = xs[i0:i1][None, :]
        suby = ys[j0:j1][:, None]
        disk = (subx - x) ** 2 + (suby - y) ** 2 <= radius ** 2
        sub = H[j0:j1, i0:i1]
        return float(np.max(sub[disk])) if disk.any() else 0.0

    prev = (0.0, 0.0, plan.safe_z)
    for m in moves:
        # 行程 / 夹具干涉
        if not (machine.x_min <= m.x <= machine.x_max and
                machine.y_min <= m.y <= machine.y_max):
            out_of_travel += 1
            if out_of_travel <= 3:
                issues.append({"level": "error", "code": "SIM_OVERTRAVEL",
                               "line": m.line_no,
                               "message": f"仿真轨迹超出机床行程 ({m.x:.1f},{m.y:.1f})"})
        clamp_band_x = p.stock_x / 2 + 2 < abs(m.x) < p.stock_x / 2 + 14
        clamp_band_y = p.stock_y / 2 + 2 < abs(m.y) < p.stock_y / 2 + 14
        if (clamp_band_x or clamp_band_y) and m.z < 0:
            clamp_hits += 1
            if clamp_hits <= 3:
                issues.append({"level": "error", "code": "SIM_CLAMP_HIT",
                               "line": m.line_no,
                               "message": f"仿真检出夹具干涉 ({m.x:.1f},{m.y:.1f},Z{m.z:.1f})"})

        if m.kind == "cycle":
            cycle_count += 1
            cut_disk(m.x, m.y, m.z, m.radius, m.line_no)
            current_path.append((m.x, m.y, "cycle"))
            # 固定循环执行完会退到 R / 初始平面；若把 prev 停在孔底，
            # 下一段的抬刀插值会被当成「材料内快移」而误报撞刀。
            prev = (m.x, m.y, m.return_z if m.return_z is not None
                    else max(m.z, 0.0))
            continue

        # 段内插值采样（快移也要采样，才能抓到中途撞刀）
        dist = math.hypot(m.x - prev[0], m.y - prev[1], m.z - prev[2])
        steps = max(1, min(400, int(dist / max(resolution * 0.9, 0.2)) + 1))
        for k in range(1, steps + 1):
            t = k / steps
            x = prev[0] + (m.x - prev[0]) * t
            y = prev[1] + (m.y - prev[1]) * t
            z = prev[2] + (m.z - prev[2]) * t
            if m.kind == "rapid":
                top = material_height(x, y, m.radius)
                if z < top - 0.08 or z < floor:
                    if abs(m.x - prev[0]) + abs(m.y - prev[1]) > 0.01:
                        rapid_in_material += 1
                    else:
                        rapid_plunge += 1
                    if len(collisions) < 200:
                        collisions.append((x, y))
            else:
                cut_disk(x, y, z, m.radius, m.line_no)
                current_path.append((x, y, "cut"))
        if m.kind == "rapid" and len(current_path) > 20:
            trajectories.append(current_path)
            current_path = []
        prev = (m.x, m.y, m.z)
    if current_path:
        trajectories.append(current_path)

    if rapid_in_material:
        issues.append({
            "level": "error", "code": "SIM_RAPID_CRASH", "line": 0,
            "message": f"仿真检出 {rapid_in_material} 处 G00 快移撞入材料（撞刀风险）",
            "suggestion": "抬刀到安全高度后再横移；把该段改成 G01 切削进给"})
    if rapid_plunge:
        issues.append({
            "level": "error", "code": "SIM_RAPID_PLUNGE", "line": 0,
            "message": f"仿真检出 {rapid_plunge} 处 G00 快速扎刀",
            "suggestion": "改用 G01 下刀进给或螺旋/斜线下刀"})
    if out_of_travel:
        issues.append({
            "level": "error", "code": "SIM_OVERTRAVEL", "line": 0,
            "message": f"仿真检出 {out_of_travel} 段轨迹超出机床行程",
            "suggestion": "检查工件坐标系偏置与特征坐标"})
    if clamp_hits:
        issues.append({
            "level": "error", "code": "SIM_CLAMP_HIT", "line": 0,
            "message": f"仿真检出 {clamp_hits} 处夹具/压板区域干涉",
            "suggestion": "调整装夹位置或抬高安全高度"})

    # --- 体积与形貌比对 --------------------------------------------------
    tol = 0.08
    overcut = (H < target - tol) & stock_mask
    # 允许区域外的任何去除都属于误切
    stray_cut = (H < -tol) & stock_mask & (~allowed)
    undercut = (H > target + tol) & (target < -tol) & stock_mask
    through = (H < floor - tol) & stock_mask
    cell_area = resolution ** 2
    removed = float(np.sum(np.maximum(0.0, -H) * stock_mask) * cell_area)
    expected = float(np.sum(np.maximum(0.0, -target) * stock_mask) * cell_area)

    if stray_cut.any():
        issues.append({
            "level": "error", "code": "SIM_STRAY_CUT", "line": 0,
            "message": f"仿真检出 {int(stray_cut.sum())} 个网格在特征区域外被误切",
            "suggestion": "检查刀补方式与轮廓偏移，缩小刀具直径"})
    if overcut.any():
        issues.append({
            "level": "error", "code": "SIM_OVERCUT", "line": 0,
            "message": f"仿真检出 {int(overcut.sum())} 个网格过切 "
                       f"(最大深度 {float(np.min(H - target)):.2f}mm)",
            "suggestion": "减少切深、检查 Z 分层与刀具长度补偿"})
    if through.any():
        issues.append({
            "level": "error", "code": "SIM_CUT_THROUGH", "line": 0,
            "message": f"仿真检出 {int(through.sum())} 个网格切穿毛坯底面",
            "suggestion": "减小总切深或增加毛坯厚度"})
    under_ratio = float(undercut.sum() / max(1, allowed.sum()))
    if under_ratio > 0.05:
        issues.append({
            "level": "error" if strict else "warning",
            "code": "SIM_UNDERCUT", "line": 0,
            "message": f"仿真检出欠切残留 {under_ratio * 100:.1f}% 的加工区域",
            "suggestion": "减小行距 ae / 增加清根环 / 检查刀具能否进入型腔"})

    ok = not any(i["level"] == "error" for i in issues)
    stats = {
        "resolution": resolution,
        "grid": f"{nx}x{ny}",
        "moves": len(moves),
        "sampled_points": sum(len(p) for p in trajectories),
        "cutting_paths": len(trajectories),
        "fixed_cycles": cycle_count,
        "collision_points": len(collisions),
        "rapid_in_material": rapid_in_material,
        "rapid_plunge": rapid_plunge,
        "overtravel_segments": out_of_travel,
        "clamp_interference": clamp_hits,
        "overcut_cells": int(overcut.sum()),
        "stray_cut_cells": int(stray_cut.sum()),
        "undercut_cells": int(undercut.sum()),
        "undercut_ratio": round(under_ratio * 100, 2),
        "removed_volume_mm3": round(removed, 1),
        "expected_volume_mm3": round(expected, 1),
        "volume_error_pct": round(abs(removed - expected) / max(expected, 1) * 100, 2),
        "max_cut_depth": round(float(np.min(H)), 3),
        "floor": floor,
        "max_tool_radius": max_tool_radius,
    }
    return SimulationResult(
        ok=ok, issues=issues, stats=stats, height=H, target=target,
        extent=(-half_x, half_x, -half_y, half_y),
        trajectories=trajectories, collision_points=collisions,
    )


# --------------------------------------------------------------------------
# 纯标准库 PNG 编码：把 heightmap 变成可内嵌进 HTML 报告的位图
# --------------------------------------------------------------------------
def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data +
            struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png(rgb: np.ndarray) -> bytes:
    h, w, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[y].astype(np.uint8).tobytes() for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) +
            _png_chunk(b"IDAT", zlib.compress(raw, 6)) + _png_chunk(b"IEND", b""))


def heightmap_png(result: SimulationResult, part,
                  upscale: int = 2) -> bytes:
    """色带：未加工浅灰 / 已加工蓝 / 过切红 / 残留黄 / 毛坯外深灰。
    第二个参数是 PartSpec（报告层直接传 result.part）。"""
    H, target = result.height, result.target
    assert H is not None and target is not None
    diff = H - target
    rgb = np.zeros((*H.shape, 3), dtype=np.uint8)
    # 背景（毛坯外）
    rgb[:] = (34, 39, 48)
    stock = (np.abs(np.linspace(result.extent[0], result.extent[1], H.shape[1])[None, :])
             <= part.stock_x / 2 + 1e-6) & \
            (np.abs(np.linspace(result.extent[2], result.extent[3], H.shape[0])[:, None])
             <= part.stock_y / 2 + 1e-6)
    rgb[stock] = (224, 226, 222)
    cut = H < -0.08
    depth_ratio = np.clip(-H / max(0.1, part.stock_z), 0, 1)
    blue = (np.array([38, 96, 156]) +
            depth_ratio[..., None] * np.array([-10, -30, -40])).astype(np.uint8)
    rgb[cut & stock] = blue[cut & stock]
    rgb[(diff < -0.08) & stock] = (214, 58, 58)        # 过切：红
    rgb[(diff > 0.08) & (target < -0.08) & stock] = (232, 178, 46)  # 残留：黄
    rgb[(H < -0.08) & (target > -0.08) & stock] = (166, 76, 196)    # 误切：紫
    if upscale > 1:
        rgb = np.repeat(np.repeat(rgb, upscale, axis=0), upscale, axis=1)
    return encode_png(rgb)
