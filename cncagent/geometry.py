"""2.5D 刀路几何生成（确定性算法，不经过大模型）。

产出的是"运动语义" Move 序列，后处理器负责翻译成各数控系统方言，
仿真器消费同一份 Move 序列做材料去除 —— 保证仿真对象与下发对象严格一致。

支持的走刀：分层环切(contour)、往复行切(zigzag)、螺旋下刀(helix)、
整圆 G02/G03 圆弧插补、圆角矩形轮廓、啄钻固定循环。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Feature, FeatureKind, Operation, ProcessPlan, Tool, get_tool


@dataclass
class Move:
    """一条机床运动。坐标是刀具中心（刀位点）。"""
    kind: str                 # rapid | line | arc_cw | arc_ccw | cycle
    x: float
    y: float
    z: float
    feed: float = 0.0
    i: float = 0.0            # 圆弧圆心相对起点 X 增量
    j: float = 0.0            # 圆弧圆心相对起点 Y 增量
    r_level: float = 0.0      # 固定循环 R 平面
    comment: str = ""


@dataclass
class Toolpath:
    op: Operation
    tool: Tool
    moves: list[Move] = field(default_factory=list)
    cut_length: float = 0.0
    rapid_length: float = 0.0
    layer_count: int = 0

    def finish(self, start: tuple[float, float, float]) -> None:
        """统计行程，供节拍估算与报告使用。"""
        px, py, pz = start
        for m in self.moves:
            d = math.hypot(m.x - px, m.y - py, m.z - pz)
            if m.kind == "arc_cw" or m.kind == "arc_ccw":
                r = math.hypot(m.i, m.j)
                d = 2 * math.pi * r if r > 0 else d
            elif m.kind == "cycle":
                d = abs(m.r_level - m.z) * 2
            if m.kind == "rapid":
                self.rapid_length += d
            else:
                self.cut_length += d
            px, py, pz = m.x, m.y, m.z


# --------------------------------------------------------------------------
# 基础轮廓
# --------------------------------------------------------------------------
def _circle_rings(radius: float, stepover: float) -> list[float]:
    """型腔等距环半径序列：从最大允许半径向内递减到中心。"""
    if radius <= 0:
        return []
    rings: list[float] = []
    r = radius
    while r > stepover * 1.05:
        rings.append(round(r, 4))
        r -= stepover
    rings.append(round(r, 4))
    if rings[-1] > 0.01:
        rings.append(0.0)
    return rings


def _rect_loop(cx: float, cy: float, hx: float, hy: float,
               rc: float, z: float, feed: float) -> list[Move]:
    """圆角矩形闭合轮廓（逆时针），四段直线 + 四段 G03 圆弧。"""
    rc = max(0.0, min(rc, hx * 0.9, hy * 0.9))
    moves: list[Move] = []
    # 起点：上边右端
    moves.append(Move("line", cx + hx - rc, cy + hy, z, feed))
    if rc > 0.01:
        moves.append(Move("line", cx - hx + rc, cy + hy, z, feed))
        moves.append(Move("arc_ccw", cx - hx, cy + hy - rc, z, feed,
                          i=0.0, j=-rc))
        moves.append(Move("line", cx - hx, cy - hy + rc, z, feed))
        moves.append(Move("arc_ccw", cx - hx + rc, cy - hy, z, feed,
                          i=rc, j=0.0))
        moves.append(Move("line", cx + hx - rc, cy - hy, z, feed))
        moves.append(Move("arc_ccw", cx + hx, cy - hy + rc, z, feed,
                          i=0.0, j=rc))
        moves.append(Move("line", cx + hx, cy + hy - rc, z, feed))
        moves.append(Move("arc_ccw", cx + hx - rc, cy + hy, z, feed,
                          i=-rc, j=0.0))
    else:
        for x, y in ((-hx, hy), (-hx, -hy), (hx, -hy), (hx, hy)):
            moves.append(Move("line", cx + x, cy + y, z, feed))
    return moves


def _helix_entry(cx: float, cy: float, r: float, z_from: float,
                 z_to: float, feed: float, segments: int = 12) -> list[Move]:
    """螺旋下刀：一圈内 Z 线性下降，避免垂直扎刀（真实 CAM 做法）。"""
    if z_from - z_to <= 0.001:
        return []
    out: list[Move] = []
    for k in range(1, segments + 1):
        a = -math.pi / 2 + 2 * math.pi * k / segments
        z = z_from + (z_to - z_from) * k / segments
        out.append(Move("line", cx + r * math.cos(a), cy + r * math.sin(a),
                        round(z, 4), feed))
    return out


def _zigzag_rows(cx: float, cy: float, hx: float, hy: float,
                 stepover: float, z: float, feed: float) -> list[Move]:
    """往复行切：Y 向步进，X 向来回，减少抬刀。"""
    moves: list[Move] = []
    y = -hy
    row = 0
    while y <= hy + 1e-6:
        x0, x1 = (-hx, hx) if row % 2 == 0 else (hx, -hx)
        moves.append(Move("line", cx + x0, cy + y, z, feed))
        moves.append(Move("line", cx + x1, cy + y, z, feed))
        y += stepover
        row += 1
    return moves


# --------------------------------------------------------------------------
# 单工序刀路
# --------------------------------------------------------------------------
def _layer_depths(feature: Feature, op: Operation) -> list[float]:
    """按 ap 分层；有精加工工序时粗加工留 0.2mm 余量。"""
    allowance = 0.2 if feature.finish_pass and op.stage == "rough" else 0.0
    target = -(feature.depth - allowance)
    if op.stage == "finish":
        return [-feature.depth]
    if op.stage == "drill":
        return [-feature.depth]
    n = max(1, math.ceil(abs(target) / max(op.ap, 0.05)))
    return [round(-min(k * op.ap, abs(target)), 4) for k in range(1, n + 1)]


def build_operation_toolpath(plan: ProcessPlan, op: Operation, tool: Tool) -> Toolpath:
    feat = plan.part.features[op.feature_index]
    tp = Toolpath(op=op, tool=tool)
    safe_z, clear_z = plan.safe_z, plan.clearance_z
    feed, plunge = float(op.feed), float(op.plunge_feed)
    cx, cy = feat.cx, feat.cy

    def add(m: Move) -> None:
        """从材料内抬刀时先垂直退刀，再水平定位。

        G00 同时改 XY/Z 时，机床不保证先抬 Z（可能是斜线联动），
        在层底直接横移会撞刀。这里在刀路层就拆成两段，从源头消除，
        而不是指望校验器在下游拦下来。
        """
        if m.kind == "rapid" and tp.moves:
            p = tp.moves[-1]
            if (p.z < -0.05 and m.z > p.z + 1e-6
                    and (abs(m.x - p.x) > 1e-6 or abs(m.y - p.y) > 1e-6)):
                tp.moves.append(Move("rapid", p.x, p.y, m.z,
                                     comment="RETRACT Z FIRST"))
                tp.moves.append(Move("rapid", m.x, m.y, m.z,
                                     comment=m.comment or "REPOSITION"))
                return
        tp.moves.append(m)

    # 孔：固定循环，点位 + 啄钻
    if feat.kind == FeatureKind.HOLE:
        add(Move("rapid", cx, cy, safe_z, comment="HOLE POSITION"))
        add(Move("rapid", cx, cy, clear_z))
        add(Move("cycle", cx, cy, -feat.depth, plunge,
                 r_level=clear_z, comment=f"G81 DRILL D{feat.diameter:g} DEP{feat.depth:g}"))
        add(Move("rapid", cx, cy, safe_z))
        tp.finish((0, 0, safe_z))
        return tp

    layers = _layer_depths(feat, op)
    tp.layer_count = len(layers)

    # 面铣 / 直槽：行切
    if feat.kind in (FeatureKind.FACE, FeatureKind.SLOT):
        if feat.kind == FeatureKind.FACE:
            hx = plan.part.stock_x / 2 + tool.radius
            hy = plan.part.stock_y / 2 - tool.radius * 0.5
            rows = _zigzag_rows(0, 0, hx, hy, op.ae, 0, feed)
        else:
            a = math.radians(feat.angle)
            rows = []
            half = max(0.0, feat.length / 2 - tool.radius)
            offsets = [0.0]
            if feat.width > tool.diameter * 1.4:
                offsets = [-feat.width / 2 + tool.radius, 0.0,
                           feat.width / 2 - tool.radius]
            for off in offsets:
                ox, oy = -math.sin(a) * off, math.cos(a) * off
                x0 = cx - math.cos(a) * half + ox
                y0 = cy - math.sin(a) * half + oy
                x1 = cx + math.cos(a) * half + ox
                y1 = cy + math.sin(a) * half + oy
                rows.append(Move("line", x0, y0, 0, feed))
                rows.append(Move("line", x1, y1, 0, feed))
        for idx, z in enumerate(layers):
            first = rows[0]
            add(Move("rapid", first.x, first.y, safe_z,
                     comment=f"LAYER {idx + 1} POSITION"))
            add(Move("rapid", first.x, first.y, clear_z))
            # 斜线下刀：沿第一段边走边下 Z
            second = rows[1]
            steps = 6
            for k in range(1, steps + 1):
                t = k / steps
                add(Move("line",
                         first.x + (second.x - first.x) * t * 0.15,
                         first.y + (second.y - first.y) * t * 0.15,
                         round(clear_z + (z - clear_z) * t, 4), plunge))
            for m in rows:
                add(Move(m.kind, m.x, m.y, z, feed))
            add(Move("rapid", rows[-1].x, rows[-1].y, clear_z))
        add(Move("rapid", 0, 0, safe_z))
        tp.finish((0, 0, safe_z))
        return tp

    # 圆槽 / 矩形型腔：分层等距环切
    if feat.kind == FeatureKind.CIRCLE_POCKET:
        max_reach = feat.diameter / 2 - tool.radius
        # 精加工同样走满所有环：既要光侧壁也要光底面，
        # 只走最外环会在型腔中心留下 0.2mm 台阶（仿真会判欠切）。
        rings = _circle_rings(max_reach, op.ae)
        helix_r = min(tool.radius * 1.2, max_reach * 0.6,
                      max(0.5, max_reach - 1.0))
    else:
        max_hx = feat.width / 2 - tool.radius
        max_hy = feat.height / 2 - tool.radius
        max_reach = min(max_hx, max_hy)
        # 矩形用"向内偏移量"序列：0, ae, 2ae ... 到不能再偏
        offsets = [round(k * op.ae, 4)
                   for k in range(int(max_reach // max(op.ae, 0.1)) + 1)]
        helix_r = min(tool.radius * 1.2, max(0.5, max_reach * 0.6))
    if max_reach <= 0:
        # 刀具放不进特征：留空刀路，几何预检会报错
        tp.finish((0, 0, safe_z))
        return tp

    for li, z in enumerate(layers):
        # 在安全高度快速定位到螺旋下刀起点
        add(Move("rapid", cx, cy - helix_r, safe_z,
                 comment=f"LAYER {li + 1} SAFE POSITION"))
        add(Move("rapid", cx, cy - helix_r, clear_z))
        # 螺旋下刀到本层深度
        if plan.approach in ("ramp", "helix"):
            for hm in _helix_entry(cx, cy, helix_r, clear_z, z, plunge):
                add(hm)
        else:
            add(Move("line", cx, cy - helix_r, z, plunge,
                     comment="VERTICAL PLUNGE - SOFT MATERIAL ONLY"))

        if feat.kind == FeatureKind.CIRCLE_POCKET:
            for ri, r in enumerate(rings):
                sx, sy = cx + r, cy
                add(Move("line", sx, sy, z, feed,
                         comment=f"RING {ri + 1} R{r:g}" if ri == 0 else ""))
                if r > 0.01:
                    add(Move("arc_cw", sx, sy, z, feed,
                             i=-r, j=0.0, comment=f"FULL CIRCLE G02 R{r:g}"))
            add(Move("line", cx, cy, z, feed))
        else:
            for ri, off in enumerate(offsets):
                hx = max(0.2, max_hx - off)
                hy = max(0.2, max_hy - off)
                rc = max(0.0, feat.corner_radius - tool.radius)
                loop = _rect_loop(cx, cy, hx, hy, rc, z, feed)
                add(Move("line", loop[0].x, loop[0].y, z, feed,
                         comment=f"RECT LOOP {ri + 1}" if ri == 0 else ""))
                for m in loop[1:]:
                    add(m)
                add(Move("line", loop[0].x, loop[0].y, z, feed))
        add(Move("rapid", cx, cy, clear_z, comment="RETRACT AFTER LAYER"))

    add(Move("rapid", 0, 0, safe_z))
    tp.finish((0, 0, safe_z))
    return tp


def generate_toolpaths(plan: ProcessPlan) -> list[Toolpath]:
    out: list[Toolpath] = []
    for op in plan.operations:
        tool = get_tool(op.tool_number)
        out.append(build_operation_toolpath(plan, op, tool))
    return out


def geometric_warnings(plan: ProcessPlan) -> list[str]:
    """几何层预检：刀具能否进入特征、特征是否超毛坯、深度是否超毛坯厚度。"""
    issues: list[str] = []
    p = plan.part
    for op in plan.operations:
        f = p.features[op.feature_index]
        tool = get_tool(op.tool_number)
        if f.kind == FeatureKind.SLOT and tool.diameter > f.width + 1e-6:
            issues.append(f"{op.id}: 刀具 D{tool.diameter:g} 大于槽宽 {f.width:g}，必然过切")
        if f.kind == FeatureKind.CIRCLE_POCKET and tool.diameter >= f.diameter:
            issues.append(f"{op.id}: 刀具 D{tool.diameter:g} 不小于圆槽直径 {f.diameter:g}")
        if f.kind == FeatureKind.RECT_POCKET and \
                tool.diameter >= min(f.width, f.height):
            issues.append(f"{op.id}: 刀具无法进入 {f.width:g}x{f.height:g} 型腔")
    for i, f in enumerate(p.features):
        if f.depth >= p.stock_z:
            issues.append(f"特征F{i + 1}: 切深 {f.depth:g} >= 毛坯厚度 {p.stock_z:g}，会切穿")
        if f.kind in (FeatureKind.CIRCLE_POCKET, FeatureKind.RECT_POCKET,
                      FeatureKind.HOLE, FeatureKind.SLOT):
            reach_x = abs(f.cx) + max(f.diameter / 2, f.width / 2, f.length / 2)
            # 旋转直槽的 Y 向包络同样可能由 length 决定，漏掉会让
            # 90° 直槽超出毛坯却不被几何预检发现。
            reach_y = abs(f.cy) + max(f.diameter / 2, f.height / 2,
                                      f.width / 2, f.length / 2)
            if reach_x > p.stock_x / 2 + 1e-6 or reach_y > p.stock_y / 2 + 1e-6:
                issues.append(f"特征F{i + 1}: 超出毛坯边界")
    return issues
