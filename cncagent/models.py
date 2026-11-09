"""领域模型：材料库、刀具库、机床规格、零件与工艺方案。

设计原则：所有"物理世界"的硬约束都集中在这里，LLM 只能产出参数建议，
最终数值一律经过 clamp_to_machine() 夹紧后才允许进入后处理器。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------
# 材料库（硬质合金刀具推荐值，用于教学演示，实际生产需查刀具厂商样本）
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Material:
    key: str
    name: str
    vc: float            # 推荐切削速度 m/min
    fz: float            # 基准每齿进给 mm/z（以 D=10mm 为基准）
    kc: float            # 单位切削力 N/mm^2，用于功率校核
    max_ap_ratio: float  # 最大轴向切深 / 刀具直径
    max_ae_ratio: float  # 最大径向切宽 / 刀具直径
    coolant: str = "M08"


MATERIALS: dict[str, Material] = {
    "45#": Material("45#", "45号钢", 120, 0.08, 2000, 0.50, 0.60),
    "45": Material("45", "45号钢", 120, 0.08, 2000, 0.50, 0.60),
    "Q235": Material("Q235", "低碳钢", 140, 0.09, 1800, 0.55, 0.65),
    "6061": Material("6061", "6061铝合金", 300, 0.12, 800, 0.80, 0.80),
    "AL": Material("AL", "铝合金", 300, 0.12, 800, 0.80, 0.80),
    "304": Material("304", "304不锈钢", 80, 0.06, 2400, 0.40, 0.40),
    "HT250": Material("HT250", "灰铸铁", 110, 0.10, 1300, 0.60, 0.70),
    "POM": Material("POM", "聚甲醛", 200, 0.15, 300, 0.80, 0.80),
}

DEFAULT_MATERIAL = "45#"


def resolve_material(text: str) -> Material:
    """从任意文本中模糊识别材料，识别不了就退回 45#钢（并在报告里标注）。"""
    upper = text.upper().replace(" ", "")
    for key, mat in MATERIALS.items():
        if key.upper() in upper:
            return mat
    aliases = {
        "钢": "45#", "碳钢": "45#", "铝": "6061", "不锈钢": "304",
        "铸铁": "HT250", "塑料": "POM", "尼龙": "POM",
    }
    for alias, key in aliases.items():
        if alias in text:
            return MATERIALS[key]
    return MATERIALS[DEFAULT_MATERIAL]


# --------------------------------------------------------------------------
# 机床规格
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class MachineSpec:
    name: str
    x_min: float; x_max: float
    y_min: float; y_max: float
    z_min: float; z_max: float
    max_spindle: int      # rpm
    min_spindle: int
    max_feed: int         # mm/min
    max_rapid: int        # mm/min
    power_kw: float       # 主轴额定功率
    tool_count: int = 16
    controller: str = "FANUC"


VMC850 = MachineSpec(
    name="VMC-850 三轴加工中心",
    x_min=-425, x_max=425,
    y_min=-200, y_max=200,
    z_min=-500, z_max=50,
    max_spindle=8000, min_spindle=200,
    max_feed=6000, max_rapid=20000,
    power_kw=7.5,
)


# --------------------------------------------------------------------------
# 刀具库
# --------------------------------------------------------------------------
class ToolType(str, Enum):
    FLAT = "flat"        # 平底立铣刀
    BALL = "ball"        # 球头刀
    FACE = "face"        # 面铣刀
    DRILL = "drill"      # 麻花钻


@dataclass(frozen=True)
class Tool:
    number: int
    name: str
    type: ToolType
    diameter: float
    flutes: int
    length: float        # 有效刃长
    max_ap: float        # 单次最大切深（绝对限制）
    description: str = ""

    @property
    def radius(self) -> float:
        return self.diameter / 2

    @property
    def t_code(self) -> str:
        return f"T{self.number:02d}"


TOOLS: dict[int, Tool] = {
    1: Tool(1, "D10-4F 平底立铣刀", ToolType.FLAT, 10, 4, 30, 8, "通用粗/精铣"),
    2: Tool(2, "D6-4F 平底立铣刀", ToolType.FLAT, 6, 4, 20, 5, "小特征/清角"),
    3: Tool(3, "D12-4F 平底立铣刀", ToolType.FLAT, 12, 4, 35, 10, "大余量开粗"),
    4: Tool(4, "D20-6F 面铣刀", ToolType.FACE, 20, 6, 40, 3, "平面铣削"),
    5: Tool(5, "D8-2F 球头刀", ToolType.BALL, 8, 2, 25, 4, "曲面/倒角"),
    6: Tool(6, "D8 麻花钻", ToolType.DRILL, 8, 2, 60, 40, "钻孔固定循环"),
}


def get_tool(number: int) -> Tool:
    if number not in TOOLS:
        raise ValueError(f"刀库中没有 T{number:02d}，可用：{sorted(TOOLS)}")
    return TOOLS[number]


# --------------------------------------------------------------------------
# 零件与特征
# --------------------------------------------------------------------------
class FeatureKind(str, Enum):
    CIRCLE_POCKET = "circle_pocket"   # 圆形凹槽
    RECT_POCKET = "rect_pocket"       # 矩形型腔
    SLOT = "slot"                     # 直线槽
    FACE = "face"                     # 面铣
    HOLE = "hole"                     # 孔（固定循环）


@dataclass
class Feature:
    kind: FeatureKind
    depth: float                       # 从毛坯顶面 Z0 向下的切深（正数）
    cx: float = 0.0
    cy: float = 0.0
    diameter: float = 0.0              # 圆槽/孔
    width: float = 0.0                 # 矩形槽 X 向
    height: float = 0.0                # 矩形槽 Y 向
    length: float = 0.0                # 直槽长度
    angle: float = 0.0                 # 直槽角度（度）
    corner_radius: float = 0.0
    tolerance: float = 0.10            # 尺寸公差 mm
    surface_roughness: float = 3.2     # Ra um
    finish_pass: bool = True           # 是否安排精加工走刀

    def label(self, index: int) -> str:
        names = {
            FeatureKind.CIRCLE_POCKET: f"F{index} 圆形凹槽 Φ{self.diameter:g} 深{self.depth:g}",
            FeatureKind.RECT_POCKET: f"F{index} 矩形型腔 {self.width:g}x{self.height:g} 深{self.depth:g}",
            FeatureKind.SLOT: f"F{index} 直槽 {self.length:g}x{self.width:g} 深{self.depth:g}",
            FeatureKind.FACE: f"F{index} 面铣 去除{self.depth:g}",
            FeatureKind.HOLE: f"F{index} 孔 Φ{self.diameter:g} 深{self.depth:g}",
        }
        return names[self.kind]


@dataclass
class PartSpec:
    name: str
    material: Material
    stock_x: float                     # 毛坯 X 长
    stock_y: float                     # 毛坯 Y 宽
    stock_z: float                     # 毛坯 Z 厚
    features: list[Feature] = field(default_factory=list)
    source: str = ""                   # 输入来源：natural_language / json / step / gcode
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def top_z(self) -> float:
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# --------------------------------------------------------------------------
# 工序与工艺方案
# --------------------------------------------------------------------------
@dataclass
class Operation:
    id: str
    feature_index: int
    stage: str                 # rough / finish / drill
    tool_number: int
    spindle: int               # S
    feed: int                  # F 切削进给
    plunge_feed: int           # 下刀进给
    ap: float                  # 每层轴向切深
    ae: float                  # 径向切宽（行距）
    strategy: str              # contour / zigzag / peck / helix
    use_cutter_comp: bool = False   # G41/G42
    use_length_comp: bool = True    # G43
    note: str = ""
    adjustments: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProcessPlan:
    """LLM / 规则引擎的唯一输出物 —— 结构化工艺方案，绝不含 G 代码文本。"""
    part: PartSpec
    operations: list[Operation]
    work_offset: str = "G54"
    safe_z: float = 20.0
    clearance_z: float = 5.0
    coolant: str = "M08"
    approach: str = "ramp"       # ramp 斜线下刀 / vertical 垂直下刀 / helix 螺旋
    planner: str = "rule"        # rule / llm
    reasoning: str = ""          # 规划器给出的工艺思路（报告里展示）
    raw_llm_json: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "part": self.part.to_dict(),
            "operations": [op.to_dict() for op in self.operations],
            "work_offset": self.work_offset,
            "safe_z": self.safe_z,
            "clearance_z": self.clearance_z,
            "coolant": self.coolant,
            "approach": self.approach,
            "planner": self.planner,
            "reasoning": self.reasoning,
        }


# --------------------------------------------------------------------------
# G 代码程序
# --------------------------------------------------------------------------
@dataclass
class Program:
    text: str
    controller: str
    program_number: int = 1
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def lines(self) -> list[str]:
        return self.text.splitlines()

    @property
    def block_count(self) -> int:
        return len([l for l in self.lines if l.strip() and not l.strip().startswith("(")])
