"""切削参数计算 + 安全夹紧（Safety Clamp）。

这是抵消大模型幻觉的第一道硬屏障：无论 LLM 给出多离谱的 S / F / ap / ae，
进入后处理器之前都会被机床物理极限、材料许用值、主轴功率三重夹紧，
并且把每一次改动写进 adjustments，报告里可追溯。
"""
from __future__ import annotations

import math

from .models import (
    Feature, FeatureKind, MachineSpec, Material, Operation, PartSpec,
    ProcessPlan, Tool, VMC850, get_tool,
)


def compute_spindle(material: Material, tool: Tool, machine: MachineSpec,
                    rough: bool = True) -> int:
    """n = 1000 * Vc / (pi * D)，粗加工降 15% 保刀具寿命。"""
    vc = material.vc * (0.85 if rough else 1.0)
    n = 1000.0 * vc / (math.pi * tool.diameter)
    return int(round(n / 50.0) * 50)


def compute_feed(material: Material, tool: Tool, spindle: int,
                 rough: bool = True) -> int:
    """Vf = fz * z * n；小刀具按比例缩减每齿进给。"""
    fz = material.fz * (tool.diameter / 10.0) ** 0.25
    fz *= 1.0 if rough else 0.75
    vf = fz * tool.flutes * spindle
    return int(round(vf / 10.0) * 10)


def spindle_power_kw(material: Material, ap: float, ae: float, feed: int) -> float:
    """P = (ap * ae * Vf * kc) / (60 * 10^6)，单位 kW。"""
    return ap * ae * feed * material.kc / (60.0 * 1_000_000.0)


def choose_tool(feature: Feature, prefer: int | None = None) -> Tool:
    """按特征类型与尺寸选刀：型腔要能伸进去（D < 最小内宽/内圆角），大平面优先面铣刀。"""
    if prefer is not None:
        return get_tool(prefer)
    if feature.kind == FeatureKind.FACE:
        return get_tool(4)
    if feature.kind == FeatureKind.HOLE:
        # 选最接近孔径的钻头（演示库里只有 D8）
        return get_tool(6)
    if feature.kind == FeatureKind.CIRCLE_POCKET:
        d = feature.diameter
        for num in (3, 1, 2):
            t = get_tool(num)
            if t.diameter <= d * 0.6:
                return t
        return get_tool(2)
    if feature.kind == FeatureKind.RECT_POCKET:
        narrow = min(feature.width, feature.height)
        for num in (3, 1, 2):
            t = get_tool(num)
            if t.diameter <= narrow * 0.7:
                return t
        return get_tool(2)
    if feature.kind == FeatureKind.SLOT:
        for num in (1, 2):
            t = get_tool(num)
            if t.diameter <= feature.width * 0.9:
                return t
        return get_tool(2)
    return get_tool(1)


def default_ap_ae(material: Material, tool: Tool, feature: Feature,
                  stage: str) -> tuple[float, float]:
    """按材料许用切深比给出 ap/ae；精加工留 0.2mm 余量、小切宽。"""
    if feature.kind == FeatureKind.HOLE:
        return feature.depth, 0.0
    if feature.kind == FeatureKind.FACE:
        ap = 0.5 if stage == "rough" else 0.2
        ae = tool.diameter * 0.7
        return round(ap, 3), round(ae, 3)
    ap = min(tool.max_ap,
             tool.diameter * material.max_ap_ratio,
             feature.depth if stage == "finish" else feature.depth)
    ae = tool.diameter * (material.max_ae_ratio if stage == "rough" else 0.25)
    if stage == "finish":
        ap = min(ap, 0.3)
    return round(max(ap, 0.1), 3), round(max(ae, 0.5), 3)


def build_operation(part: PartSpec, feature: Feature, index: int,
                    stage: str, tool: Tool, machine: MachineSpec,
                    raw: dict | None = None) -> Operation:
    """把（可能来自 LLM 的）原始参数字典变成一条经过夹紧的安全工序。"""
    raw = raw or {}
    mat = part.material
    n_spindle = compute_spindle(mat, tool, machine, rough=(stage == "rough"))
    n_feed = compute_feed(mat, tool, n_spindle, rough=(stage == "rough"))
    ap, ae = default_ap_ae(mat, tool, feature, stage)

    spindle = int(raw.get("spindle", n_spindle))
    feed = int(raw.get("feed", n_feed))
    plunge = int(raw.get("plunge_feed", max(80, int(n_feed * 0.35))))
    ap = float(raw.get("ap", ap))
    ae = float(raw.get("ae", ae))
    strategy = str(raw.get("strategy", "contour" if feature.kind in
                           (FeatureKind.CIRCLE_POCKET, FeatureKind.RECT_POCKET)
                           else "zigzag"))

    adj: list[str] = []

    # --- 1. 机床硬极限夹紧 -------------------------------------------------
    if spindle > machine.max_spindle:
        adj.append(f"主轴转速 {spindle} > 机床上限 {machine.max_spindle} rpm，已夹紧")
        spindle = machine.max_spindle
    if spindle < machine.min_spindle:
        adj.append(f"主轴转速 {spindle} 低于下限，已设为 {machine.min_spindle} rpm")
        spindle = machine.min_spindle
    if feed > machine.max_feed:
        adj.append(f"进给 {feed} > 机床上限 {machine.max_feed} mm/min，已夹紧")
        feed = machine.max_feed
    if feed <= 0:
        adj.append("进给 <= 0，已恢复推荐值")
        feed = n_feed
    if plunge <= 0 or plunge > feed:
        adj.append(f"下刀进给 {plunge} 不合理，已限制为切削进给的 35%")
        plunge = max(50, int(feed * 0.35))

    # --- 2. 材料/刀具许用值夹紧 -------------------------------------------
    ap_limit = min(tool.max_ap, tool.diameter * mat.max_ap_ratio, feature.depth)
    if ap > ap_limit + 1e-6:
        adj.append(f"切深 ap={ap:g} 超过许用 {ap_limit:g}mm（材料/刃长/特征深度约束），已夹紧")
        ap = ap_limit
    if ap <= 0:
        adj.append("切深 <= 0，已恢复许用值")
        ap = ap_limit
    ae_limit = tool.diameter * mat.max_ae_ratio
    if ae > ae_limit + 1e-6:
        adj.append(f"行距 ae={ae:g} 超过 {ae_limit:g}mm，已夹紧")
        ae = ae_limit
    if ae <= 0:
        ae = min(tool.diameter * 0.5, max(0.5, feature.width * 0.5
                 if feature.kind == FeatureKind.SLOT else tool.diameter * 0.5))

    # 型腔/槽的刀具必须能进入，否则强制换小刀（几何层还会再查一次）
    if feature.kind == FeatureKind.SLOT and tool.diameter > feature.width:
        adj.append(f"刀具 D{tool.diameter:g} 大于槽宽 {feature.width:g}，几何检查会拦截")

    # --- 3. 主轴功率校核 ---------------------------------------------------
    power = spindle_power_kw(mat, ap, ae, feed)
    max_power = machine.power_kw * 0.8
    if power > max_power:
        ratio = max_power / power
        old_feed = feed
        feed = max(100, int(feed * ratio / 10) * 10)
        ap = round(min(ap, ap_limit), 3)
        adj.append(
            f"切削功率 {power:.2f}kW > 许用 {max_power:.2f}kW，进给 {old_feed}→{feed} mm/min"
        )

    # --- 4. 精加工余量 -----------------------------------------------------
    if stage == "finish":
        ap = round(min(ap, 0.3), 3)
        feed = max(80, int(feed * 0.8 / 10) * 10)

    use_comp = bool(raw.get("use_cutter_comp", stage == "finish" and
                            feature.kind in (FeatureKind.CIRCLE_POCKET,
                                             FeatureKind.RECT_POCKET)))
    if strategy not in ("contour", "zigzag", "peck", "helix"):
        adj.append(f"未知走刀策略 {strategy!r}，回退 contour")
        strategy = "contour"

    return Operation(
        id=f"OP{index + 1:02d}{'R' if stage == 'rough' else 'F'}",
        feature_index=index,
        stage=stage,
        tool_number=tool.number,
        spindle=spindle,
        feed=feed,
        plunge_feed=plunge,
        ap=round(ap, 3),
        ae=round(ae, 3),
        strategy=strategy,
        use_cutter_comp=use_comp,
        use_length_comp=True,
        note=str(raw.get("note", "")),
        adjustments=adj,
    )


def reclamp_operation(op: Operation, plan: ProcessPlan,
                      machine: MachineSpec = VMC850) -> Operation:
    """可重入的单工序夹紧：任何来源（LLM / 幻觉注入 / 修复动作）的参数
    在进入后处理器之前都必须过这一关，且每次改动都留痕。"""
    mat = plan.part.material
    feat = plan.part.features[op.feature_index]
    tool = get_tool(op.tool_number)
    adj = op.adjustments
    is_rough = op.stage == "rough"
    n_rec = compute_spindle(mat, tool, machine, rough=is_rough)
    f_rec = compute_feed(mat, tool, n_rec, rough=is_rough)
    ap_rec, ae_rec = default_ap_ae(mat, tool, feat, op.stage)

    # 越界参数视为不可信（典型幻觉）：回退到工艺公式推荐值，
    # 而不是贴着机床极限运行 —— 合法 != 合理。
    if op.spindle > machine.max_spindle or op.spindle > n_rec * 1.5:
        adj.append(f"主轴转速 {op.spindle} 越界/偏离推荐值，"
                   f"回退按 Vc 计算的 S{n_rec} rpm")
        op.spindle = n_rec
    if op.spindle < machine.min_spindle:
        adj.append(f"主轴转速 {op.spindle} 低于下限，已设为 {n_rec} rpm")
        op.spindle = n_rec
    if op.feed > machine.max_feed or op.feed > f_rec * 1.5 or op.feed <= 0:
        adj.append(f"进给 {op.feed} 越界/偏离推荐值，"
                   f"回退按 Vf=fz*z*n 计算的 F{f_rec} mm/min")
        op.feed = f_rec
    if op.plunge_feed <= 0 or op.plunge_feed > op.feed:
        adj.append(f"下刀进给 {op.plunge_feed} 不合理，已限制为切削进给的 35%")
        op.plunge_feed = max(50, int(op.feed * 0.35))

    ap_limit = min(tool.max_ap, tool.diameter * mat.max_ap_ratio, feat.depth)
    if op.ap > ap_limit + 1e-6:
        adj.append(f"切深 ap={op.ap:g} 超过许用 {ap_limit:g}mm，"
                   f"回退推荐 {ap_rec:g}mm")
        op.ap = ap_rec
    if op.ap <= 0:
        op.ap = ap_rec
    ae_limit = tool.diameter * mat.max_ae_ratio
    if op.ae > ae_limit + 1e-6:
        adj.append(f"行距 ae={op.ae:g} 超过 {ae_limit:g}mm，"
                   f"回退推荐 {ae_rec:g}mm")
        op.ae = ae_rec
    if op.ae <= 0:
        op.ae = ae_rec

    power = spindle_power_kw(mat, op.ap, op.ae, op.feed)
    max_power = machine.power_kw * 0.8
    if power > max_power:
        old = op.feed
        op.feed = max(100, int(op.feed * max_power / power / 10) * 10)
        adj.append(f"切削功率 {power:.2f}kW > 许用 {max_power:.2f}kW，"
                   f"进给 {old}→{op.feed} mm/min")
    return op


def _clamp_part_geometry(plan: ProcessPlan, machine: MachineSpec) -> None:
    """夹紧零件几何：这是体素仿真网格与所有切削公式的输入，
    LLM / DXF / 面板都可能给出非有限值或超大值，必须在此收口。"""
    part = plan.part
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
    for f in part.features:
        for attr, fallback in (("depth", 2.0), ("diameter", 10.0),
                               ("width", 20.0), ("height", 20.0),
                               ("length", 30.0), ("tolerance", 0.1)):
            try:
                v = float(getattr(f, attr, fallback))
            except (TypeError, ValueError):
                v = fallback
            # 0 是「该维度不适用」的合法值（圆形特征没有 width/length，
            # 矩形特征没有 diameter），不能当成非法值回退成默认尺寸，
            # 否则会凭空改变几何、把不该切的地方算进目标形状。
            if not math.isfinite(v) or v < 0:
                v = fallback
            # 故意不把 depth 截断到 stock_z：切穿属于几何预检要报的错，
            # 在这里悄悄截断会把真实的工艺错误掩盖掉。
            setattr(f, attr, min(v, span))
        for attr in ("cx", "cy"):
            try:
                v = float(getattr(f, attr, 0.0))
            except (TypeError, ValueError):
                v = 0.0
            if not math.isfinite(v):
                v = 0.0
            setattr(f, attr, max(-span, min(span, v)))


def clamp_plan(plan: ProcessPlan, machine: MachineSpec = VMC850) -> ProcessPlan:
    """对整个方案做二次夹紧：逐工序参数、安全高度、坐标系、刀具号、非法工序。"""
    _clamp_part_geometry(plan, machine)
    for op in plan.operations:
        if op.stage not in ("rough", "finish", "drill"):
            op.adjustments.append(f"非法工序阶段 {op.stage!r}，回退 rough")
            op.stage = "rough"
        if op.tool_number not in (1, 2, 3, 4, 5, 6):
            op.adjustments.append(f"非法刀号 T{op.tool_number}，回退 T01")
            op.tool_number = 1
        if op.feature_index < 0 or op.feature_index >= len(plan.part.features):
            op.adjustments.append("工序引用了不存在的特征，已丢弃该工序")
    plan.operations = [op for op in plan.operations
                       if 0 <= op.feature_index < len(plan.part.features)]
    for op in plan.operations:
        reclamp_operation(op, plan, machine)

    if plan.safe_z < 5 or plan.safe_z > machine.z_max:
        plan.reasoning += f" [夹紧] 安全高度 {plan.safe_z:g} 越界，重置为 20.0。".strip()
        plan.safe_z = 20.0
    if plan.clearance_z >= plan.safe_z or plan.clearance_z < 1:
        plan.clearance_z = 5.0
    if plan.work_offset not in ("G54", "G55", "G56", "G57", "G58", "G59"):
        plan.reasoning += f" [夹紧] 非法工件坐标系 {plan.work_offset}，重置为 G54。"
        plan.work_offset = "G54"
    if plan.approach not in ("ramp", "vertical", "helix"):
        plan.approach = "ramp"
    if plan.coolant not in ("M08", "M07", "M09"):
        plan.coolant = "M08"

    # 按刀号聚合工序减少换刀，但必须守住两条工艺铁律：
    #   1) 面铣建基准面永远最先；2) 同一特征内先粗后精（不能先精后粗）。
    def sort_key(op):
        feat = plan.part.features[op.feature_index]
        base_first = 0 if feat.kind.name == "FACE" else 1
        stage_order = 0 if op.stage in ("rough", "drill") else 1
        return (base_first, op.tool_number, op.feature_index, stage_order)

    order_before = [op.id for op in plan.operations]
    plan.operations.sort(key=sort_key)
    if [op.id for op in plan.operations] != order_before:
        plan.reasoning += (
            " [优化] 已按「面铣优先 → 同刀号聚合 → 同特征先粗后精」重排工序，"
            "减少换刀次数且不破坏工艺顺序。"
        )
    return plan


def estimate_cycle_time(plan: ProcessPlan, path_length_mm: float) -> float:
    """粗估加工时间（分钟）：切削行程 / 进给 + 换刀 + 快速定位。"""
    if not plan.operations:
        return 0.0
    avg_feed = sum(op.feed for op in plan.operations) / len(plan.operations)
    cut_min = path_length_mm / max(avg_feed, 1.0)
    tool_changes = len({op.tool_number for op in plan.operations}) * 0.12
    rapid_min = path_length_mm * 0.15 / 10000.0
    return round(cut_min + tool_changes + rapid_min, 2)
