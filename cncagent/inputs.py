"""阶段 1：输入层。

四种输入通路全部有真实实现（不写空壳）：
  natural_language : 正则抽取材质 / 毛坯尺寸 / 特征 / 公差
  json             : 结构化零件描述（图片 OCR 与视觉模型的标准落点）
  step             : 从 STEP AP203/AP214 的 CARTESIAN_POINT 提取包围盒
  legacy_gcode     : 读取旧程序，抽取 S/F/行程/刀具/危险指令用于 AI 优化与排错

图纸/图片走"视觉模型 -> 结构化 JSON -> 本模块"的接口，OCR 后端可插拔。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import (
    Feature, FeatureKind, PartSpec, resolve_material, DEFAULT_MATERIAL,
)

_NUM = r"(\d+(?:\.\d+)?)"


def _find(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _stock_size(text: str) -> tuple[float | None, float | None, float | None]:
    """识别 100x100x20 / 长宽100×100 / 厚20 等写法。"""
    m = re.search(_NUM + r"\s*(?:×|x|\*|-)\s*" + _NUM +
                  r"(?:\s*(?:×|x|\*)\s*" + _NUM + r")?", text, re.IGNORECASE)
    if m:
        x, y = float(m.group(1)), float(m.group(2))
        z = float(m.group(3)) if m.group(3) else None
    else:
        x = _find(r"(?:长|长度)\s*(?:为)?\s*" + _NUM, text)
        y = _find(r"(?:宽|宽度)\s*(?:为)?\s*" + _NUM, text)
        z = None
    z = z or _find(r"(?:厚|厚度|高|高度)\s*(?:为)?\s*" + _NUM, text)
    return x, y, z


def _depth_of(chunk: str, default: float = 2.0) -> float:
    d = (_find(r"深\s*(?:度)?\s*(?:为|约|=)?\s*" + _NUM, chunk)
         or _find(_NUM + r"\s*mm\s*深", chunk)
         or _find(r"切深\s*" + _NUM, chunk))
    return d if d and d > 0 else default


def _tolerance(text: str) -> float:
    return (_find(r"[±+]\s*" + _NUM, text)
            or _find(r"公差\s*" + _NUM, text) or 0.10)


def _last_pair(chunk: str) -> tuple[float, float] | None:
    """取块内最后一个 WxH 数字对。毛坯尺寸通常在句首，特征尺寸更靠后，
    用"最后一个"可避免把 120x80x25 错当成 40x30 的型腔尺寸。"""
    matches = list(re.finditer(_NUM + r"\s*(?:×|x|\*)\s*" + _NUM,
                               chunk, re.IGNORECASE))
    if not matches:
        return None
    m = matches[-1]
    return float(m.group(1)), float(m.group(2))


def parse_natural_language(text: str) -> PartSpec:
    """中文自然语言 -> PartSpec。识别不了的字段给默认值并记 warning。"""
    warnings: list[str] = []
    mat = resolve_material(text)
    if mat.key == DEFAULT_MATERIAL and not re.search(r"45\s*#?|钢", text):
        warnings.append(f"未识别到材料，默认按 {mat.name} 计算切削参数")

    x, y, z = _stock_size(text)
    if x is None or y is None:
        x, y, z = 100.0, 100.0, 20.0
        warnings.append("未识别到毛坯尺寸，默认 100x100x20 mm")
    if z is None:
        z = 20.0
        warnings.append(f"未识别到毛坯厚度，默认 {z:g} mm")

    tol = _tolerance(text)
    roughness = _find(r"Ra\s*" + _NUM, text) or 3.2
    features: list[Feature] = []

    # 按逗号/分号切块，逐块识别特征，避免多个特征互相串尺寸。
    # 但"深度3mm"这类独立补充块要并回前一块，否则深度会丢。
    raw_chunks = [c.strip() for c in re.split(r"[,，;；。\n]", text) if c.strip()]
    chunks: list[str] = []
    for c in raw_chunks:
        is_supplement = bool(re.fullmatch(
            r"(?:深\s*(?:度)?|切深|公差|Ra|圆角)\s*(?:为|约|=)?\s*"
            r"[±+]?\d+(?:\.\d+)?\s*(?:mm)?", c, re.IGNORECASE))
        if is_supplement and chunks:
            chunks[-1] += "，" + c
        else:
            chunks.append(c)

    # 全文级深度作为兜底，任何局部块没写深度时使用
    global_depth = _depth_of(text, 0) or None

    for chunk in chunks:
        c = chunk.strip()
        if not c:
            continue
        depth = _depth_of(c, global_depth or 2.0)
        # 圆形凹槽 / 圆形型腔
        if re.search(r"(?:圆形?凹槽|圆形?型腔|圆槽|圆坑|铣圆|沉孔)", c):
            d = (_find(r"(?:直径|Φ|φ|phi|D)\s*" + _NUM, c)
                 or _find(_NUM + r"\s*mm\s*(?:直径|圆)", c) or 30.0)
            features.append(Feature(FeatureKind.CIRCLE_POCKET, depth,
                                    diameter=d, tolerance=tol,
                                    surface_roughness=roughness))
        # 矩形型腔
        elif re.search(r"(?:矩形槽|方形槽|矩形型腔|方槽|型腔)", c):
            pair = _last_pair(c)
            w, h = pair if pair else (40.0, 30.0)
            cr = _find(r"(?:圆角|R)\s*" + _NUM, c) or 0.0
            features.append(Feature(FeatureKind.RECT_POCKET, depth,
                                    width=w, height=h, corner_radius=cr,
                                    tolerance=tol, surface_roughness=roughness))
        # 直槽
        elif re.search(r"(?:直槽|键槽|开槽|铣槽)", c):
            pair = _last_pair(c)
            length, width = pair if pair else (60.0, 10.0)
            features.append(Feature(FeatureKind.SLOT, depth,
                                    length=length, width=width,
                                    tolerance=tol, surface_roughness=roughness))
        # 钻孔
        elif re.search(r"(?:钻孔|打孔|通孔|螺纹底孔|孔)", c):
            d = _find(r"(?:直径|Φ|φ|D)\s*" + _NUM, c) or 8.0
            features.append(Feature(FeatureKind.HOLE, depth, diameter=d,
                                    tolerance=tol, finish_pass=False))
        # 面铣
        elif re.search(r"(?:面铣|铣平|铣平面|光面|见光)", c):
            features.append(Feature(FeatureKind.FACE, depth if depth else 0.5,
                                    width=x, height=y, tolerance=tol,
                                    surface_roughness=roughness))

    if not features:
        # 兜底：全文里出现"凹槽/型腔"但切块没命中时，按圆槽处理
        if re.search(r"(?:凹槽|型腔|铣一个|铣个)", text):
            d = _find(r"(?:直径|Φ|φ)\s*" + _NUM, text) or 50.0
            features.append(Feature(FeatureKind.CIRCLE_POCKET, _depth_of(text),
                                    diameter=d, tolerance=tol,
                                    surface_roughness=roughness))
        else:
            warnings.append("未识别到加工特征，已生成面铣工序作为演示")

    # 面铣若与其他特征共存，排在最前（先基准面后特征）
    features.sort(key=lambda f: 0 if f.kind == FeatureKind.FACE else 1)

    return PartSpec(
        name="自然语言解析零件",
        material=mat,
        stock_x=x, stock_y=y, stock_z=z,
        features=features,
        source="natural_language",
        raw_text=text,
        warnings=warnings,
    )


def parse_json(text_or_path: str) -> PartSpec:
    """结构化零件描述。图片 OCR / 视觉模型 / STEP 特征识别的结果都落到这个格式。"""
    p = Path(text_or_path)
    raw = p.read_text(encoding="utf-8") if p.exists() and p.is_file() else text_or_path
    data: dict[str, Any] = json.loads(raw)
    mat = resolve_material(str(data.get("material", DEFAULT_MATERIAL)))
    stock = data.get("stock", {})
    features = []
    for item in data.get("features", []):
        kind = FeatureKind(str(item["kind"]))
        features.append(Feature(
            kind=kind,
            depth=float(item["depth"]),
            cx=float(item.get("cx", 0)), cy=float(item.get("cy", 0)),
            diameter=float(item.get("diameter", 0)),
            width=float(item.get("width", 0)), height=float(item.get("height", 0)),
            length=float(item.get("length", 0)), angle=float(item.get("angle", 0)),
            corner_radius=float(item.get("corner_radius", 0)),
            tolerance=float(item.get("tolerance", 0.1)),
            surface_roughness=float(item.get("surface_roughness", 3.2)),
            finish_pass=bool(item.get("finish_pass", True)),
        ))
    return PartSpec(
        name=str(data.get("name", "结构化零件")),
        material=mat,
        stock_x=float(stock.get("x", 100)),
        stock_y=float(stock.get("y", 100)),
        stock_z=float(stock.get("z", 20)),
        features=features,
        source="json",
        raw_text=raw[:500],
        warnings=list(data.get("warnings", [])),
    )


def parse_step(path: str) -> PartSpec:
    """从 STEP 文件提取 CARTESIAN_POINT 包围盒 -> 毛坯尺寸。

    说明：这是教学级实现，只能得到包围盒；真实 CAM 需要 OpenCascade/FreeCAD
    做 B-Rep 面识别。接口处在 PartSpec 层保持一致，后续可直接替换。
    """
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    pts = re.findall(
        r"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*"
        r"([-+]?[\d.Ee+-]+)\s*,\s*([-+]?[\d.Ee+-]+)\s*,\s*([-+]?[\d.Ee+-]+)\s*\)",
        text, re.IGNORECASE)
    if not pts:
        raise ValueError("STEP 文件中未找到 CARTESIAN_POINT，无法推断包围盒")
    xs, ys, zs = zip(*[(float(a), float(b), float(c)) for a, b, c in pts])
    sx, sy, sz = max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)
    warnings = [
        "STEP 仅提取包围盒，未做 B-Rep 特征识别；请搭配 JSON 描述加工特征",
        f"共解析 {len(pts)} 个 CARTESIAN_POINT",
    ]
    return PartSpec(
        name=Path(path).stem,
        material=resolve_material(Path(path).stem),
        stock_x=round(max(sx, 1), 2), stock_y=round(max(sy, 1), 2),
        stock_z=round(max(sz, 1), 2),
        features=[Feature(FeatureKind.FACE, 0.5, width=sx, height=sy)],
        source="step", raw_text=text[:300], warnings=warnings,
    )


def parse_legacy_gcode(path: str) -> dict[str, Any]:
    """旧程序体检：抽取 S/F 范围、坐标范围、刀号、可疑指令，供 AI 优化/排错。"""
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    lines = [l.strip() for l in text.splitlines()]
    s_vals = [int(v) for v in re.findall(r"\bS(\d+)", text)]
    f_vals = [int(v) for v in re.findall(r"\bF(\d+)", text)]
    tools = sorted({int(v) for v in re.findall(r"\bT(\d+)", text)})
    axes = {a: [float(v) for v in re.findall(rf"\b{a}([-+]?\d+(?:\.\d+)?)", text)]
            for a in "XYZ"}
    suspicious = []
    for i, line in enumerate(lines, 1):
        if re.search(r"\bG0?0\b", line) and re.search(r"\bZ-", line):
            suspicious.append(f"N{i}: 快速定位 G00 中出现负 Z，存在扎刀风险 -> {line}")
        if re.search(r"\bS(\d{5,})", line):
            suspicious.append(f"N{i}: 主轴转速数值异常 -> {line}")
        if re.search(r"\bG4[1-2]\b", line) and not re.search(r"\bD\d+", line):
            suspicious.append(f"N{i}: 刀补 G41/G42 未指定 D 号 -> {line}")
    return {
        "path": path,
        "line_count": len(lines),
        "spindle_range": (min(s_vals), max(s_vals)) if s_vals else None,
        "feed_range": (min(f_vals), max(f_vals)) if f_vals else None,
        "tools": tools,
        "axis_range": {a: (min(v), max(v)) if v else None for a, v in axes.items()},
        "suspicious": suspicious,
        "text": text,
    }


def create_drawing_interpreter_payload(hint: str = "") -> str:
    """图纸/图片通路：输出给视觉模型的结构化抽取 Schema。

    真实部署时把图片发给多模态模型，令其返回该 JSON，再交给 parse_json()。
    这里返回的是可直接粘进 Prompt 的模板，避免把 OCR 做成永远跑不通的空壳。
    """
    return json.dumps({
        "task": "从工程图/图片中抽取零件信息，只返回 JSON，不要解释",
        "hint": hint,
        "schema": {
            "name": "string",
            "material": "45# | 6061 | 304 | ...",
            "stock": {"x": "mm", "y": "mm", "z": "mm"},
            "features": [{
                "kind": "circle_pocket | rect_pocket | slot | face | hole",
                "depth": "mm", "cx": "mm", "cy": "mm",
                "diameter": "mm", "width": "mm", "height": "mm",
                "length": "mm", "tolerance": "mm",
            }],
        },
        "rules": [
            "尺寸一律换算成 mm，公差带转换成单边绝对值",
            "无法确定的字段填 null，禁止猜测",
            "标题栏材质与技术要求都要读取",
        ],
    }, ensure_ascii=False, indent=2)


def parse_request(text_or_path: str, source: str = "auto") -> PartSpec | dict[str, Any]:
    """统一入口。source=legacy 时返回体检字典而不是 PartSpec。"""
    p = Path(text_or_path)
    is_file = p.exists() and p.is_file()
    suffix = p.suffix.lower() if is_file else ""

    if source == "legacy" or suffix in (".nc", ".cnc", ".tap", ".gcode"):
        return parse_legacy_gcode(text_or_path)
    if source == "step" or suffix in (".step", ".stp"):
        return parse_step(text_or_path)
    if source == "json" or suffix == ".json" or (
            not is_file and text_or_path.lstrip().startswith("{")):
        return parse_json(text_or_path)
    raw = p.read_text(encoding="utf-8") if is_file else text_or_path
    return parse_natural_language(raw)
