"""图纸解析层：工程图 -> 结构化零件 -> 可编辑 JSON -> 既有编程链路。

设计原则与项目其余部分一致，不把安全压在"模型猜得准"上：

* 主通路是 **DXF 矢量工程图**：几何图元是精确数值，标准库逐对解析即可，
  不存在 OCR 识别误差；尺寸标注只用来补充深度/公差/材料等几何里没有的信息。
* 位图（PNG/JPG）走 **多模态视觉模型适配器**，输出结构化 JSON；
  未配置 Key 时明确报错并提示改传 DXF，绝不用「假装识别」的空壳兜底。
* 几何实测尺寸与标注文字会做**一致性校验**，不一致时降置信度并告警，
  要求人工复核；无法确定的深度/公差一律填默认值并标注 warning，不静默猜测。
* 输出统一落到 `PartSpec` / `parse_json()` 的标准格式，后续夹紧、校验、
  体素仿真、下发准入全部复用既有链路，不新增旁路。

DXF 支持的实体：CIRCLE / ARC / LINE / LWPOLYLINE / POLYLINE+VERTEX /
TEXT / MTEXT / DIMENSION。图层约定（缺失时自动退化到几何包围盒）：
OUTLINE=毛坯外轮廓，FEATURE=加工特征，DIM=尺寸线，NOTE=文字标注。
"""
from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings
from .inputs import create_drawing_interpreter_payload, parse_json
from .models import (DEFAULT_MATERIAL, Feature, FeatureKind, PartSpec,
                     resolve_material)

SUPPORTED_SUFFIX = {".dxf", ".json", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}
VECTOR_SUFFIX = {".dxf"}
RASTER_SUFFIX = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
MAX_DRAWING_BYTES = 6 * 1024 * 1024

_NUM = r"(\d+(?:\.\d+)?)"


class DrawingError(ValueError):
    """图纸无法可靠解析。调用方应把消息直接展示给操作员，不允许降级猜测。"""


# --------------------------------------------------------------------------
# 结构化结果
# --------------------------------------------------------------------------
@dataclass
class DrawingAnalysis:
    part: PartSpec
    part_json: str                      # 可编辑、可直接喂给 parse_json 的规范 JSON
    svg: str                            # 图纸 + 识别结果叠加预览
    backend: str                        # dxf / json / vision_llm
    entities: list[dict[str, Any]] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)
    feature_rows: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    drawing_name: str = ""
    image_data_uri: str = ""            # 位图原件，供 SVG <image> 内嵌

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "drawing_name": self.drawing_name,
            "confidence": round(self.confidence, 3),
            "warnings": self.warnings,
            "part_json": self.part_json,
            "svg": self.svg,
            "entities": self.entities,
            "notes": self.notes,
            "feature_rows": self.feature_rows,
            "part": {
                "name": self.part.name,
                "material": self.part.material.name,
                "stock": {"x": self.part.stock_x, "y": self.part.stock_y,
                          "z": self.part.stock_z},
                "features": [f.label(i + 1)
                             for i, f in enumerate(self.part.features)],
            },
        }


def part_to_json(part: PartSpec) -> str:
    """PartSpec -> parse_json() 的输入格式。面板上编辑的就是这个 JSON。"""
    data: dict[str, Any] = {
        "name": part.name,
        "material": part.material.key,
        "stock": {"x": part.stock_x, "y": part.stock_y, "z": part.stock_z},
        "features": [
            {
                "kind": f.kind.value,
                "depth": f.depth, "cx": f.cx, "cy": f.cy,
                "diameter": f.diameter, "width": f.width, "height": f.height,
                "length": f.length, "angle": f.angle,
                "corner_radius": f.corner_radius, "tolerance": f.tolerance,
                "surface_roughness": f.surface_roughness,
                "finish_pass": f.finish_pass,
            }
            for f in part.features
        ],
        "warnings": list(part.warnings),
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------
# 编码与单位：真实 CAD 导出的 DXF 常是 GBK / ANSI_1252，单位也可能不是 mm
# --------------------------------------------------------------------------
_CODEPAGE_ENC = {
    "ANSI_936": "gbk", "ANSI_950": "big5", "ANSI_932": "cp932",
    "ANSI_949": "cp949", "ANSI_1250": "cp1250", "ANSI_1251": "cp1251",
    "ANSI_1252": "cp1252", "ANSI_1253": "cp1253", "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255", "ANSI_1256": "cp1256", "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258", "ANSI_874": "cp874", "UTF-8": "utf-8",
}
# $INSUNITS (group code 70)：图纸单位 -> mm
_INSUNITS_MM = {0: 1.0, 1: 25.4, 2: 304.8, 4: 1.0, 5: 10.0, 6: 1000.0,
                8: 0.001, 13: 0.0000254, 14: 25.4, 15: 10.0, 16: 1000.0}


def decode_dxf(data: bytes) -> str:
    """按 $DWGCODEPAGE 选编码，再按 utf-8 / gbk / cp1252 / latin-1 降级。

    中文 AutoCAD 的 R12/R2000 ASCII DXF 默认 ANSI_936(GBK)；用
    utf-8 + errors='replace' 强解会把所有中文标注变成 U+FFFD，
    使深度/材料/毛坯/公差关键词全部失配，退回默认值还静默不报错。
    """
    candidates: list[str] = []
    try:
        # latin-1 是无损字节视图，仅用于扫 group code 与 ASCII 变量名
        pairs = _pairs(data.decode("latin-1", errors="replace"))
        for i, (code, val) in enumerate(pairs):
            if code == 9 and val.strip().upper() == "$DWGCODEPAGE":
                for c, v in pairs[i + 1:i + 4]:
                    if c == 3:
                        enc = _CODEPAGE_ENC.get(v.strip().upper())
                        if enc:
                            candidates.append(enc)
                        break
                break
    except Exception:
        pass
    candidates += ["utf-8-sig", "gbk", "cp1252", "latin-1"]
    for enc in dict.fromkeys(candidates):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", errors="replace")


def dxf_unit_scale(text: str) -> float:
    """读 HEADER 段 $INSUNITS，返回换算到 mm 的系数；未声明按 mm。"""
    pairs = _pairs(text)
    for i, (code, val) in enumerate(pairs):
        if code == 9 and val.strip().upper() == "$INSUNITS":
            for c, v in pairs[i + 1:i + 5]:
                if c == 70:
                    try:
                        return _INSUNITS_MM.get(int(float(v)), 1.0)
                    except (TypeError, ValueError):
                        return 1.0
            break
    return 1.0


def _scale_entities(ents: list[_Entity], notes: list[dict], scale: float) -> None:
    """把图元几何整体换算到 mm（标注文本里的数值不动，会另行告警）。"""
    for e in ents:
        d = e.data
        for k in ("x", "y", "x1", "y1", "x2", "y2", "r", "corner_r"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                d[k] = v * scale
        for k in ("points", "hull"):
            if k in d:
                d[k] = [(p[0] * scale, p[1] * scale) for p in d[k]]
    for n in notes:
        n["x"] *= scale
        n["y"] *= scale


# --------------------------------------------------------------------------
# DXF 词法：group code / value 成对行
# --------------------------------------------------------------------------
def _pairs(text: str) -> list[tuple[int, str]]:
    # 按 code 行驱动，跳过段间空行/杂行；旧实现固定步长 2，
    # 只要文件里多一个空行，后续所有配对就整体错位。
    lines = text.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines) - 1:
        code = lines[i].strip()
        if not re.fullmatch(r"-?\d+", code):
            i += 1
            continue
        out.append((int(code), lines[i + 1].strip()))
        i += 2
    return out


def _to_f(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _section_blocks(pairs: list[tuple[int, str]],
                    section: str) -> list[dict[str, Any]]:
    """取指定 SECTION，按 code 0 切成实体块。"""
    start = None
    for i, (code, val) in enumerate(pairs):
        if code != 0 or val.upper() != "SECTION":
            continue
        j = i + 1
        while j < len(pairs) and pairs[j][0] != 0:
            if pairs[j][0] == 2 and pairs[j][1].upper() == section:
                start = j + 1
                break
            j += 1
        if start is not None:
            break
    if start is None:
        return []

    blocks: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for code, val in pairs[start:]:
        if code == 0:
            if val.upper() == "ENDSEC":
                break
            if cur:
                blocks.append(cur)
            cur = {"type": val.upper(), "g": []}
        elif cur is not None:
            cur["g"].append((code, val))
    if cur:
        blocks.append(cur)
    return blocks


def _block_defs(pairs: list[tuple[int, str]]) -> dict[str, list[dict[str, Any]]]:
    """BLOCKS 段：块名 -> 块内图元。真实 CAD 的图框、标准件、孔阵列
    大量使用块参照，不展开会让整块几何静默消失。"""
    defs: dict[str, list[dict[str, Any]]] = {}
    name: str | None = None
    items: list[dict[str, Any]] = []
    for b in _section_blocks(pairs, "BLOCKS"):
        t = b["type"]
        if t == "BLOCK":
            name = _first(b, 2, "").strip().upper() or None
            items = []
        elif t == "ENDBLK":
            if name:
                defs[name] = items
            name, items = None, []
        elif name:
            items.append(b)
    return defs


def _transform_block(b: dict[str, Any], dx: float, dy: float,
                     sx: float, sy: float, rot_deg: float) -> dict[str, Any]:
    """对块内图元应用 INSERT 的平移 / 缩放 / 旋转。"""
    a = math.radians(rot_deg)
    ca, sa = math.cos(a), math.sin(a)
    out: list[tuple[int, str]] = []
    pending: dict[int, float] = {}
    for c, v in b["g"]:
        if 10 <= c <= 18:
            pending[c] = _to_f(v)
            out.append((c, v))
        elif 20 <= c <= 28 and (c - 10) in pending:
            x, y = pending.pop(c - 10) * sx, _to_f(v) * sy
            rx, ry = x * ca - y * sa + dx, x * sa + y * ca + dy
            for i in range(len(out) - 1, -1, -1):
                if out[i][0] == c - 10:
                    out[i] = (c - 10, f"{rx:.6f}")
                    break
            out.append((c, f"{ry:.6f}"))
        elif c == 40:                       # 半径 / 字高
            out.append((c, f"{_to_f(v) * (abs(sx) + abs(sy)) / 2:.6f}"))
        elif c in (50, 51):                 # 圆弧起止角
            out.append((c, f"{_to_f(v) + rot_deg:.6f}"))
        else:
            out.append((c, v))
    return {"type": b["type"], "g": out}


def _expand_inserts(blocks: list[dict[str, Any]],
                    defs: dict[str, list[dict[str, Any]]],
                    depth: int = 0) -> list[dict[str, Any]]:
    """展开 INSERT（含矩形阵列与有限深度嵌套），带总量上限防止炸内存。"""
    if not defs or depth > 3:
        return blocks
    out: list[dict[str, Any]] = []
    total = 0
    for b in blocks:
        if b["type"] != "INSERT" or total > 400:
            out.append(b)
            continue
        inner = defs.get(_first(b, 2, "").strip().upper())
        if not inner:
            continue
        dx, dy = _first_num(b, 10), _first_num(b, 20)
        sx = _first_num(b, 41, 1.0) or 1.0
        sy = _first_num(b, 42, 1.0) or 1.0
        rot = _first_num(b, 50)
        cols = max(1, min(int(_first_num(b, 70, 1.0)) or 1, 50))
        rows = max(1, min(int(_first_num(b, 71, 1.0)) or 1, 50))
        col_dx, row_dy = _first_num(b, 44), _first_num(b, 45)
        for r in range(rows):
            for c in range(cols):
                if total > 400:
                    break
                ox, oy = dx + c * col_dx, dy + r * row_dy
                for ib in inner:
                    out.append(_transform_block(ib, ox, oy, sx, sy, rot))
                    total += 1
    if any(x["type"] == "INSERT" for x in out):
        return _expand_inserts(out, defs, depth + 1)
    return out


def _first_num(b: dict, code: int, default: float = 0.0) -> float:
    for c, v in b["g"]:
        if c == code:
            return _to_f(v, default)
    return default


def _entity_blocks(pairs: list[tuple[int, str]]) -> list[dict[str, Any]]:
    """取 ENTITIES 段并展开块参照。"""
    blocks = _section_blocks(pairs, "ENTITIES")
    return _expand_inserts(blocks, _block_defs(pairs))


def _first(block: dict, code: int, default: str = "") -> str:
    for c, v in block["g"]:
        if c == code:
            return v
    return default


def _num(block: dict, code: int, default: float = 0.0) -> float:
    try:
        return float(_first(block, code, str(default)))
    except (TypeError, ValueError):
        return default


def _vertices(block: dict) -> list[tuple[float, float]]:
    """LWPOLYLINE 顶点（不含凸度）；POLYLINE+VERTEX 由调用方合并。"""
    pts: list[tuple[float, float]] = []
    x: float | None = None
    for c, v in block["g"]:
        if c == 10:
            try:
                x = float(v)
            except ValueError:
                x = None
        elif c == 20 and x is not None:
            try:
                pts.append((x, float(v)))
            except ValueError:
                pass
            x = None
    return pts


def _discretize_bulge(raw: list[tuple[float, float, float]], closed: bool,
                      seg_per_arc: int = 8
                      ) -> tuple[list[tuple[float, float]], float,
                                 list[tuple[float, float]]]:
    """LWPOLYLINE 的 code 42 凸度 -> 圆弧采样点。

    b = tan(theta/4)，theta 是该段的有符号圆心角；b=0 即直线。
    返回 (点列, 估算圆角半径)。不给凸度时点列与旧实现完全一致。
    """
    hull = [(x, y) for x, y, _ in raw]
    if not raw:
        return [], 0.0, hull
    # 没有凸度时必须逐点原样返回：闭合多段线常自带重复首点，
    # 多追加一个点会让 _axis_rect 认不出 4 角矩形。
    if not any(abs(b) > 1e-9 for _, _, b in raw):
        return hull, 0.0, hull
    out: list[tuple[float, float]] = []
    corner_r = 0.0
    n = len(raw)
    segs = n if closed else n - 1
    for i in range(segs):
        x1, y1, b = raw[i]
        x2, y2, _ = raw[(i + 1) % n]
        out.append((x1, y1))
        if abs(b) < 1e-9:
            continue
        theta = 4 * math.atan(b)
        dx, dy = x2 - x1, y2 - y1
        chord = math.hypot(dx, dy)
        if chord < 1e-9 or abs(theta) < 1e-9:
            continue
        r = chord * (1 + b * b) / (4 * abs(b))
        if not corner_r:
            corner_r = r
        s = math.sin(abs(theta) / 2)
        if abs(s) < 1e-9:
            continue
        h = math.sqrt(max(0.0, r * r - (chord / 2) ** 2))
        nx, ny = -dy / chord, dx / chord
        side = (1 if b > 0 else -1) * (1 if abs(theta) < math.pi else -1)
        cx = (x1 + x2) / 2 + side * h * nx
        cy = (y1 + y2) / 2 + side * h * ny
        a0 = math.atan2(y1 - cy, x1 - cx)
        for k in range(1, seg_per_arc):
            a = a0 + theta * k / seg_per_arc
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    out.append((raw[-1][0], raw[-1][1]))
    return out, corner_r, hull


def _vertices_bulge(block: dict) -> tuple[list[tuple[float, float]], float,
                                          list[tuple[float, float]]]:
    """带 code 42 凸度的 LWPOLYLINE 顶点，返回 (点列, 圆角半径)。"""
    raw: list[tuple[float, float, float]] = []
    x: float | None = None
    y: float | None = None
    bulge = 0.0

    def commit() -> None:
        nonlocal x, y, bulge
        if x is not None and y is not None:
            raw.append((x, y, bulge))
        x, y, bulge = None, None, 0.0

    for c, v in block["g"]:
        if c == 10:
            commit()
            try:
                x = float(v)
            except ValueError:
                pass
        elif c == 20:
            try:
                y = float(v)
            except ValueError:
                pass
        elif c == 42:
            try:
                bulge = float(v)
            except ValueError:
                pass
    commit()
    return _discretize_bulge(raw, bool(int(_num(block, 70)) & 1))


@dataclass
class _Entity:
    kind: str
    layer: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "layer": self.layer, **self.data}


def parse_dxf_entities(text: str) -> tuple[list[_Entity], list[dict[str, Any]]]:
    """返回 (几何/文字实体, 标注列表)。纯标准库，无 ezdxf 依赖。"""
    blocks = _entity_blocks(_pairs(text))
    ents: list[_Entity] = []
    notes: list[dict[str, Any]] = []

    # POLYLINE 旧格式的 VERTEX 会紧跟在 POLYLINE 后面，先收集再合并
    pending_poly: _Entity | None = None
    for b in blocks:
        t = b["type"]
        layer = _first(b, 8, "0").upper()

        if t == "VERTEX" and pending_poly is not None:
            p = pending_poly.data.setdefault("points", [])
            p.append((_num(b, 10), _num(b, 20)))
            continue
        if pending_poly is not None:
            if len(pending_poly.data.get("points", [])) >= 3:
                ents.append(pending_poly)
            pending_poly = None

        if t == "CIRCLE":
            ents.append(_Entity("circle", layer, {
                "x": _num(b, 10), "y": _num(b, 20), "r": _num(b, 40)}))
        elif t == "ARC":
            a0, a1 = _num(b, 50), _num(b, 51)
            sweep = (a1 - a0) % 360.0
            if sweep < 1e-6:
                sweep = 360.0
            ents.append(_Entity("arc", layer, {
                "x": _num(b, 10), "y": _num(b, 20), "r": _num(b, 40),
                "a0": a0, "a1": a1,
                # 有些 CAM 把整圆导出成 ARC(0,360)，应和 CIRCLE 一样识别成孔
                "full": abs(sweep - 360.0) < 1.0}))
        elif t == "LINE":
            ents.append(_Entity("line", layer, {
                "x1": _num(b, 10), "y1": _num(b, 20),
                "x2": _num(b, 11), "y2": _num(b, 21)}))
        elif t == "LWPOLYLINE":
            pts, corner_r, hull = _vertices_bulge(b)
            ents.append(_Entity("polyline", layer, {
                "points": pts,
                "hull": hull,
                "closed": bool(int(_num(b, 70)) & 1),
                "bulge": corner_r > 0,
                "corner_r": round(corner_r, 4)}))
        elif t == "POLYLINE":
            pending_poly = _Entity("polyline", layer, {
                "points": [], "closed": bool(int(_num(b, 70)) & 1)})
        elif t in ("TEXT", "MTEXT", "DIMENSION", "ATTRIB"):
            content = " ".join(
                v for c, v in b["g"] if c in (1, 3) and v).strip()
            if not content and t == "DIMENSION":
                measured = _num(b, 42)
                content = f"<{measured:g}>" if measured else "<>"
            if content:
                x, y = _num(b, 10), _num(b, 20)
                if t == "DIMENSION" and not x and not y:
                    x, y = _num(b, 13), _num(b, 23)
                notes.append({"text": _clean_dxf_text(content),
                              "x": x, "y": y, "kind": t.lower()})
                ents.append(_Entity("note", layer, {"text": content,
                                                    "x": x, "y": y}))
    if pending_poly is not None and len(pending_poly.data.get("points", [])) >= 3:
        ents.append(pending_poly)
    scale = dxf_unit_scale(text)
    if abs(scale - 1.0) > 1e-12:
        _scale_entities(ents, notes, scale)
    return ents, notes


def _clean_dxf_text(s: str) -> str:
    """DXF 控制码 -> 可读文本：%%c=直径符号、%%d=度、%%p=±、\\P=换行。"""
    s = re.sub(r"%%[cC]", "Φ", s)
    s = re.sub(r"%%[dD]", "°", s)
    s = re.sub(r"%%[pP]", "±", s)
    s = s.replace("\\P", " ").replace("\\~", " ")
    s = re.sub(r"\\A\d+;", "", s)
    s = re.sub(r"\{\\[^;]*;([^}]*)\}", r"\1", s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# 标注文本解析
# --------------------------------------------------------------------------
def _stock(note_text: str) -> tuple[float, float, float] | None:
    m = re.search(r"(?:毛坯|坯料|STOCK|BLOCK)\s*[:：]?\s*" + _NUM +
                  r"\s*[x×X*]\s*" + _NUM + r"\s*[x×X*]\s*" + _NUM,
                  note_text, re.IGNORECASE)
    return (float(m.group(1)), float(m.group(2)), float(m.group(3))) if m else None


def _material(note_text: str) -> str:
    m = re.search(r"(?:材料|材质|MATERIAL|MAT)\s*[:：]?\s*([^\s,，;；]+)",
                  note_text, re.IGNORECASE)
    return m.group(1) if m else ""


def _depth(note_text: str) -> float | None:
    m = (re.search(r"(?:深\s*度?|DEPTH|DEP)\s*[:：]?\s*" + _NUM,
                   note_text, re.IGNORECASE)
         or re.search(_NUM + r"\s*(?:mm\s*)?(?:深|DEEP)", note_text,
                      re.IGNORECASE))
    return float(m.group(1)) if m else None


def _tolerance(note_text: str) -> float | None:
    m = (re.search(r"[±+]\s*" + _NUM, note_text)
         or re.search(r"(?:公差|TOL)\s*[:：]?\s*[±+]?\s*" + _NUM,
                      note_text, re.IGNORECASE))
    return float(m.group(1)) if m else None


def _roughness(note_text: str) -> float | None:
    m = re.search(r"Ra\s*[:：]?\s*" + _NUM, note_text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _diameters(note_text: str) -> list[float]:
    return [float(x) for x in
            re.findall(r"(?:Φ|φ|⌀|%%c|Ø|D)\s*" + _NUM, note_text,
                       re.IGNORECASE)]


def _corner_radius(note_text: str) -> float | None:
    # 排除 Ra3.2 这类粗糙度写法
    m = re.search(r"(?<![A-Za-z])R\s*[:：]?\s*" + _NUM + r"(?!\s*[.a-zA-Z])",
                  note_text)
    return float(m.group(1)) if m else None


def _rect_dims(note_text: str) -> tuple[float, float] | None:
    m = re.search(_NUM + r"\s*[x×X*]\s*" + _NUM, note_text)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _class_hint(note_text: str) -> str:
    if re.search(r"孔|钻|HOLE|DRILL|通孔|螺纹|攻丝", note_text, re.IGNORECASE):
        return "hole"
    if re.search(r"槽|型腔|POCKET|凹|铣", note_text, re.IGNORECASE):
        return "pocket"
    return ""


# --------------------------------------------------------------------------
# 几何 -> 特征
# --------------------------------------------------------------------------
# 真实 CAD 图层名不会刚好叫 OUTLINE/DIM/CENTER，改用关键词模糊识别，
# 兼容中文图层与厂商模板；匹配不到时仍退化到几何包围盒。
OUTLINE_LAYER_HINTS = ("OUTLINE", "BORDER", "FRAME", "轮廓", "外框", "图框", "毛坯")
ANNOTATION_LAYER_HINTS = ("DIM", "NOTE", "TEXT", "CENTER",
                          "标注", "尺寸", "中心线", "文字")


def _is_outline_layer(layer: str) -> bool:
    u = (layer or "").upper()
    return any(h in u for h in OUTLINE_LAYER_HINTS)


def _is_annotation_layer(layer: str) -> bool:
    u = (layer or "").upper()
    return any(h in u for h in ANNOTATION_LAYER_HINTS)


def _bbox(ents: list[_Entity]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for e in ents:
        d = e.data
        if e.kind == "circle":
            xs += [d["x"] - d["r"], d["x"] + d["r"]]
            ys += [d["y"] - d["r"], d["y"] + d["r"]]
        elif e.kind == "arc":
            # 只取实际扫过的弧段极值；整圆外接框会把毛坯撑大。
            a0 = math.radians(d.get("a0", 0.0))
            a1 = math.radians(d.get("a1", 360.0))
            while a1 <= a0:
                a1 += 2 * math.pi
            angles = [a0, a1]
            for k in range(4):
                a = k * math.pi / 2
                while a < a0:
                    a += 2 * math.pi
                if a <= a1:
                    angles.append(a)
            xs += [d["x"] + d["r"] * math.cos(a) for a in angles]
            ys += [d["y"] + d["r"] * math.sin(a) for a in angles]
        elif e.kind == "line":
            xs += [d["x1"], d["x2"]]
            ys += [d["y1"], d["y2"]]
        elif e.kind == "polyline":
            xs += [p[0] for p in d.get("points", [])]
            ys += [p[1] for p in d.get("points", [])]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else (-50, -50, 50, 50)


def _axis_rect(points: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    """4 点轴对齐矩形 -> (cx, cy, w, h)，非矩形返回 None。兼容重复首点闭合。"""
    if len(points) > 1 and math.dist(points[0], points[-1]) < 1e-6:
        points = points[:-1]
    if len(points) != 4:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    if w <= 0.5 or h <= 0.5:
        return None
    # 每点必须落在四个角上
    corners = {(round(min(xs), 4), round(min(ys), 4)),
               (round(min(xs), 4), round(max(ys), 4)),
               (round(max(xs), 4), round(min(ys), 4)),
               (round(max(xs), 4), round(max(ys), 4))}
    if {(round(x, 4), round(y, 4)) for x, y in points} != corners:
        return None
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, w, h)


def _outline_of(ents: list[_Entity]) -> tuple[float, float, float, float]:
    """毛坯外轮廓：优先 OUTLINE 层矩形，否则用几何包围盒。"""
    outlines = [e for e in ents if _is_outline_layer(e.layer)]
    rects = [_axis_rect(e.data.get("points", []))
             for e in outlines if e.kind == "polyline" and e.data.get("closed")]
    rects = [r for r in rects if r]
    if rects:
        cx, cy, w, h = max(rects, key=lambda r: r[2] * r[3])
        return cx, cy, w, h
    pool = outlines or [e for e in ents
                        if e.kind != "note" and not _is_annotation_layer(e.layer)]
    minx, miny, maxx, maxy = _bbox(pool)
    return ((minx + maxx) / 2, (miny + maxy) / 2,
            maxx - minx, maxy - miny)


def _nearby_notes(notes: list[dict], x: float, y: float,
                  radius: float = 30.0) -> str:
    """只取特征附近的标注；全局技术要求由调用方按字段兜底，避免串味。"""
    picked = [(math.dist((n["x"], n["y"]), (x, y)), n["text"])
              for n in notes]
    picked = [p for p in picked if p[0] <= radius]
    picked.sort(key=lambda p: p[0])
    return " ".join(t for _, t in picked)


def _features_from_dxf(
    ents: list[_Entity], notes: list[dict], global_text: str
) -> tuple[tuple[list[Feature], list[dict], list[str], float],
           tuple[float, float, float, float]]:
    """返回 ((特征, 行, 告警, 置信度), (原点x, 原点y, 毛坯宽, 毛坯高))。"""
    ox, oy, sw, sh = _outline_of(ents)
    # TEXT 是左对齐插入点，文字会向右延伸，半径按图纸尺度放大才覆盖得到
    near_radius = max(40.0, min(sw, sh) * 0.40)
    # 深度只认特征就近标注：全局文本里的「深3」属于别的特征，串用会切错深度
    g_depth = None
    # 通用公差只从「未注公差 / 技术要求」里取，不能拿第一个特征的局部 ±0.05
    # 去填另一个没标注的特征（会把精加工公差错误地套到钻孔上）
    general_text = " ".join(
        n["text"] for n in notes
        if re.search(r"未注|通用|技术要求|GENERAL|标题栏", n["text"], re.IGNORECASE))
    g_tol = _tolerance(general_text)
    g_ra = _roughness(global_text)
    warnings: list[str] = []
    features: list[Feature] = []
    rows: list[dict[str, Any]] = []
    scores: list[float] = []

    geom = [e for e in ents
            if not _is_outline_layer(e.layer)
            and not _is_annotation_layer(e.layer) and e.kind != "note"]
    if not geom:
        geom = [e for e in ents if e.kind != "note"
                and not _is_outline_layer(e.layer)]

    def add_feature(cx_dxf: float, cy_dxf: float, kind: FeatureKind,
                    note_text: str, **kw) -> None:
        cx, cy = round(cx_dxf - ox, 3), round(cy_dxf - oy, 3)
        depth = _depth(note_text) or g_depth
        tol = _tolerance(note_text) or g_tol or 0.10
        ra = g_ra or 3.2
        local = bool(note_text.strip())
        score = 0.55 + (0.20 if local else 0.0) + (0.15 if depth else 0.0)
        if depth is None:
            depth = 2.0
            warnings.append(
                f"{kind.value} @({cx:g},{cy:g}) 未读到深度标注，按默认 2mm 填充，须人工复核")
        if not local:
            warnings.append(
                f"{kind.value} @({cx:g},{cy:g}) 附近无尺寸标注，尺寸取自几何图元，须核对")
        f = Feature(kind=kind, depth=depth, cx=cx, cy=cy,
                    tolerance=tol, surface_roughness=ra, **kw)
        features.append(f)
        rows.append({
            "index": len(features), "kind": kind.value, "cx": cx, "cy": cy,
            "depth": depth, "tolerance": tol, "roughness": ra,
            "note": note_text.strip() or "（无就近标注）",
            "confidence": round(min(1.0, score), 2), **kw,
        })
        scores.append(min(1.0, score))

    # 圆形特征（CAM 导出的整圆 ARC 也一起识别）
    for e in [x for x in geom if x.kind == "circle"
              or (x.kind == "arc" and x.data.get("full"))]:
        d = e.data
        dia = round(2 * d["r"], 3)
        near = _nearby_notes(notes, d["x"], d["y"], near_radius)
        hint = _class_hint(near)
        if hint == "hole":
            kind = FeatureKind.HOLE
        elif hint == "pocket":
            kind = FeatureKind.CIRCLE_POCKET
        else:
            kind = (FeatureKind.HOLE if dia < 12
                    else FeatureKind.CIRCLE_POCKET)
        dims = _diameters(near)
        if dims and not any(abs(v - dia) <= 0.6 for v in dims):
            warnings.append(
                f"圆 @({d['x'] - ox:g},{d['y'] - oy:g}) 几何直径 Φ{dia:g} 与标注 "
                f"{'/'.join('Φ' + format(v, 'g') for v in dims)} 不一致，以几何为准，须复核")
            scores.append(0.35)
        add_feature(d["x"], d["y"], kind, near, diameter=dia)

    # 矩形型腔
    for e in [x for x in geom if x.kind == "polyline"]:
        r = _axis_rect(e.data.get("points", []))
        if not r and e.data.get("bulge"):
            # 圆角矩形被凸度离散成多点后不再是 4 个角点；此时用外接
            # 尺寸近似，并把圆角半径交给几何层生成圆角刀路。
            pts = e.data.get("hull") or e.data.get("points", [])
            if len(pts) >= 4:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                w, h = max(xs) - min(xs), max(ys) - min(ys)
                if w > 0.5 and h > 0.5:
                    r = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, w, h)
        if not r:
            continue
        cx, cy, w, h = r
        if abs(w - sw) < 0.6 and abs(h - sh) < 0.6:
            continue                       # 外框，不是特征
        near = _nearby_notes(notes, cx, cy, near_radius)
        rd = _rect_dims(near)
        if rd and not (abs(rd[0] - w) <= 0.8 and abs(rd[1] - h) <= 0.8) \
                and not (abs(rd[0] - h) <= 0.8 and abs(rd[1] - w) <= 0.8):
            warnings.append(
                f"矩形槽 @({cx - ox:g},{cy - oy:g}) 几何 {w:g}×{h:g} 与标注 "
                f"{rd[0]:g}×{rd[1]:g} 不一致，以几何为准，须复核")
            scores.append(0.35)
        add_feature(cx, cy, FeatureKind.RECT_POCKET, near,
                    width=round(w, 3), height=round(h, 3),
                    corner_radius=_corner_radius(near)
                    or e.data.get("corner_r", 0.0))

    if not features:
        warnings.append("未在图纸中识别到可加工特征；请检查图层命名或改用结构化 JSON")
    return (features, rows, warnings,
            round(sum(scores) / len(scores), 3) if scores else 0.0), (ox, oy, sw, sh)


# --------------------------------------------------------------------------
# SVG 预览：真实图元 + 识别结果叠加
# --------------------------------------------------------------------------
def _svg_escape(s: str) -> str:
    return html.escape(str(s), quote=True)


def render_svg(ents: list[_Entity], notes: list[dict],
               features: list[Feature], ox: float, oy: float,
               sw: float, sh: float,
               image_data_uri: str = "") -> str:
    pad = 16.0
    minx, miny = ox - sw / 2, oy - sh / 2
    maxx, maxy = ox + sw / 2, oy + sh / 2
    W, H = (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad

    def X(v: float) -> float:
        return round(v - minx + pad, 2)

    def Y(v: float) -> float:
        return round(maxy - v + pad, 2)      # DXF Y 向上，SVG Y 向下

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.2f} {H:.2f}" '
           f'width="100%" height="100%" preserveAspectRatio="xMidYMid meet" '
           f'role="img" aria-label="工程图与识别结果">',
           f'<rect x="0" y="0" width="{W:.2f}" height="{H:.2f}" fill="#111820"/>']
    # 网格（10mm）
    grid = []
    gx = math.ceil(minx / 10) * 10
    while gx <= maxx:
        grid.append(f'<line x1="{X(gx):.2f}" y1="0" x2="{X(gx):.2f}" '
                    f'y2="{H:.2f}"/>')
        gx += 10
    gy = math.ceil(miny / 10) * 10
    while gy <= maxy:
        grid.append(f'<line x1="0" y1="{Y(gy):.2f}" x2="{W:.2f}" '
                    f'y2="{Y(gy):.2f}"/>')
        gy += 10
    out.append('<g stroke="#1e2b38" stroke-width="0.25">' + "".join(grid) + '</g>')

    if image_data_uri:
        out.append(f'<image href="{_svg_escape(image_data_uri)}" x="{X(minx):.2f}" '
                   f'y="{Y(maxy):.2f}" width="{sw:.2f}" height="{sh:.2f}" '
                   f'opacity="0.55" preserveAspectRatio="xMidYMid meet"/>')

    # 图元
    shapes: list[str] = []
    for e in ents:
        d = e.data
        layer = e.layer
        color = ("#e6edf3" if layer == "OUTLINE" else
                 "#7c8794" if layer in ("DIM", "CENTER") else
                 "#f0b357" if layer == "NOTE" else "#4fc3f7")
        if e.kind == "circle":
            shapes.append(f'<circle cx="{X(d["x"]):.2f}" cy="{Y(d["y"]):.2f}" '
                          f'r="{d["r"]:.2f}" fill="none" stroke="{color}" '
                          f'stroke-width="0.55"/>')
        elif e.kind == "arc":
            a0, a1 = math.radians(d["a0"]), math.radians(d["a1"])
            x0, y0 = d["x"] + d["r"] * math.cos(a0), d["y"] + d["r"] * math.sin(a0)
            x1, y1 = d["x"] + d["r"] * math.cos(a1), d["y"] + d["r"] * math.sin(a1)
            sweep = (a1 - a0) % (2 * math.pi)
            shapes.append(f'<path d="M {X(x0):.2f} {Y(y0):.2f} A {d["r"]:.2f} '
                          f'{d["r"]:.2f} 0 {1 if sweep > math.pi else 0} 0 '
                          f'{X(x1):.2f} {Y(y1):.2f}" fill="none" '
                          f'stroke="{color}" stroke-width="0.55"/>')
        elif e.kind == "line":
            shapes.append(f'<line x1="{X(d["x1"]):.2f}" y1="{Y(d["y1"]):.2f}" '
                          f'x2="{X(d["x2"]):.2f}" y2="{Y(d["y2"]):.2f}" '
                          f'stroke="{color}" stroke-width="0.45"/>')
        elif e.kind == "polyline":
            pts = " ".join(f"{X(px):.2f},{Y(py):.2f}"
                           for px, py in d.get("points", []))
            tag = "polygon" if d.get("closed") else "polyline"
            shapes.append(f'<{tag} points="{pts}" fill="none" stroke="{color}" '
                          f'stroke-width="0.55"/>')
    out.append('<g>' + "".join(shapes) + '</g>')

    # 识别到的特征中心 + 编号
    for i, f in enumerate(features, 1):
        cx, cy = X(ox + f.cx), Y(oy + f.cy)
        out.append(f'<g><circle cx="{cx:.2f}" cy="{cy:.2f}" r="2.6" '
                   f'fill="#2ea87b" stroke="#0c151d" stroke-width="0.4"/>'
                   f'<text x="{cx:.2f}" y="{cy + 1:.2f}" text-anchor="middle" '
                   f'font-size="3" font-family="monospace" fill="#08120e">'
                   f'F{i}</text></g>')

    # 标注文字
    for n in notes:
        out.append(f'<text x="{X(n["x"]):.2f}" y="{Y(n["y"]):.2f}" '
                   f'font-size="2.5" font-family="sans-serif" fill="#f0b357">'
                   f'{_svg_escape(n["text"][:46])}</text>')
    out.append(f'<text x="{pad / 2:.2f}" y="{H - 2:.2f}" font-size="2.4" '
               f'font-family="sans-serif" fill="#66788a">'
               f'OUTLINE {sw:g}×{sh:g} mm · {len(features)} features · '
               f'F1..Fn = 识别特征</text>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def analyze_dxf(text: str, name: str = "drawing.dxf") -> DrawingAnalysis:
    ents, notes = parse_dxf_entities(text)
    if not ents:
        raise DrawingError("DXF 中未找到 ENTITIES 段或任何图元，请确认是 ASCII DXF（R12+）")
    global_text = " ".join(n["text"] for n in notes)
    (features, rows, warns, conf), (ox, oy, sw, sh) = _features_from_dxf(
        ents, notes, global_text)
    scale = dxf_unit_scale(text)
    if abs(scale - 1.0) > 1e-12:
        warns.append(
            f"图纸声明的单位不是 mm，已按 1 单位 = {scale:g} mm 换算全部几何；"
            "标注文字里的尺寸单位需人工复核")
    stock = _stock(global_text)
    sx, sy, sz = stock if stock else (round(sw, 3), round(sh, 3), 20.0)
    if stock is None:
        warns.append("未读到毛坯标注，X/Y 取自外轮廓几何、Z 默认 20mm，须人工复核")
        conf = round(max(0.0, conf - 0.1), 3)
    mat_key = _material(global_text) or DEFAULT_MATERIAL
    part = PartSpec(
        name=Path(name).stem or "图纸零件",
        material=resolve_material(mat_key),
        stock_x=float(sx), stock_y=float(sy), stock_z=float(sz),
        features=features, source="dxf_drawing",
        raw_text=text[:500], warnings=list(warns),
    )
    # 几何与标注毛坯不一致时显式告警
    if abs(sx - sw) > 0.8 or abs(sy - sh) > 0.8:
        part.warnings.append(
            f"毛坯标注 {sx:g}×{sy:g} 与外框几何 {sw:g}×{sh:g} 不一致，以标注为准")
    svg = render_svg(ents, notes, features, ox, oy, sw, sh)
    return DrawingAnalysis(
        part=part, part_json=part_to_json(part), svg=svg, backend="dxf",
        entities=[e.as_dict() for e in ents], notes=notes,
        feature_rows=rows, confidence=conf, warnings=part.warnings,
        drawing_name=name,
    )


def analyze_json(text: str, name: str = "drawing.json") -> DrawingAnalysis:
    """视觉模型 / OCR / 人工填写的结构化 JSON 落点。"""
    try:
        part = parse_json(text)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        raise DrawingError(f"结构化 JSON 不合法：{exc}") from exc
    part.source = "drawing_json"
    warns = ["结构化 JSON 由外部识别结果提供，尺寸未经几何复核，须人工确认"] \
        + list(part.warnings)
    part.warnings = warns
    svg = _features_svg(part)
    rows = [{"index": i + 1, "kind": f.kind.value, "cx": f.cx, "cy": f.cy,
             "depth": f.depth, "tolerance": f.tolerance,
             "note": "结构化输入", "confidence": 0.8,
             "diameter": f.diameter, "width": f.width, "height": f.height}
            for i, f in enumerate(part.features)]
    return DrawingAnalysis(
        part=part, part_json=part_to_json(part), svg=svg,
        backend="json", notes=[], feature_rows=rows,
        confidence=0.8, warnings=warns, drawing_name=name,
    )


def _features_svg(part: PartSpec, image_data_uri: str = "") -> str:
    """没有原始图元时，用结构化特征画一张示意图（明确区别于真实工程图）。"""
    pad, sx, sy = 16.0, part.stock_x, part.stock_y
    W, H = sx + 2 * pad, sy + 2 * pad
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.2f} {H:.2f}" '
           f'width="100%" height="100%" role="img" aria-label="结构化特征示意图">',
           f'<rect width="{W:.2f}" height="{H:.2f}" fill="#111820"/>']
    if image_data_uri:
        out.append(f'<image href="{_svg_escape(image_data_uri)}" x="{pad}" '
                   f'y="{pad}" width="{sx}" height="{sy}" opacity="0.35" '
                   f'preserveAspectRatio="xMidYMid meet"/>')
    out.append(f'<rect x="{pad}" y="{pad}" width="{sx}" height="{sy}" '
               f'fill="none" stroke="#e6edf3" stroke-width="0.6"/>')
    out.append(f'<line x1="{pad + sx / 2}" y1="{pad}" x2="{pad + sx / 2}" '
               f'y2="{pad + sy}" stroke="#2b3a48" stroke-width="0.3"/>'
               f'<line x1="{pad}" y1="{pad + sy / 2}" x2="{pad + sx}" '
               f'y2="{pad + sy / 2}" stroke="#2b3a48" stroke-width="0.3"/>')
    for i, f in enumerate(part.features, 1):
        cx, cy = pad + sx / 2 + f.cx, pad + sy / 2 - f.cy
        if f.kind in (FeatureKind.CIRCLE_POCKET, FeatureKind.HOLE):
            out.append(f'<circle cx="{cx}" cy="{cy}" r="{f.diameter / 2}" '
                       f'fill="none" stroke="#4fc3f7" stroke-width="0.6"/>')
        elif f.kind == FeatureKind.RECT_POCKET:
            out.append(f'<rect x="{cx - f.width / 2}" y="{cy - f.height / 2}" '
                       f'width="{f.width}" height="{f.height}" fill="none" '
                       f'stroke="#4fc3f7" stroke-width="0.6"/>')
        elif f.kind == FeatureKind.SLOT:
            out.append(f'<line x1="{cx - f.length / 2}" y1="{cy}" '
                       f'x2="{cx + f.length / 2}" y2="{cy}" stroke="#4fc3f7" '
                       f'stroke-width="{max(0.6, f.width)}" opacity="0.7"/>')
        out.append(f'<text x="{cx}" y="{cy}" font-size="3" text-anchor="middle" '
                   f'font-family="monospace" fill="#2ea87b">F{i}</text>')
    out.append(f'<text x="{pad / 2}" y="{H - 3}" font-size="2.6" '
               f'fill="#66788a" font-family="sans-serif">'
               f'结构化特征示意图（非原始工程图）</text></svg>')
    return "".join(out)


def interpret_raster(data: bytes, mime: str, name: str,
                     hint: str = "") -> DrawingAnalysis:
    """位图 -> 多模态视觉模型 -> 结构化 JSON。无 Key 时明确失败，不假装识别。"""
    s = get_settings()
    if not s.llm_api_key:
        raise DrawingError(
            "位图图纸需要多模态视觉模型，但当前未配置 LLM_API_KEY。"
            "请改用 DXF 矢量图纸（examples/drawing_plate.dxf），"
            "或在 .env 配置 LLM_API_KEY + 支持视觉的 CNC_VISION_MODEL。")
    model = os.getenv("CNC_VISION_MODEL", "").strip() or s.llm_model
    uri = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    schema = create_drawing_interpreter_payload(hint or f"工程图 {name}")
    body = {
        "model": model, "temperature": 0, "max_tokens": 1800,
        "messages": [
            {"role": "system", "content":
                "你是机械工程图识读助手。严格按给定 schema 只输出 JSON，"
                "不得输出解释文字；无法确定的字段填 null，禁止猜测。"},
            {"role": "user", "content": [
                {"type": "text", "text": schema},
                {"type": "image_url", "image_url": {"url": uri}},
            ]},
        ],
    }
    req = urllib.request.Request(
        s.llm_base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {s.llm_api_key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(30, s.llm_timeout)) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        raw = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, TimeoutError, KeyError,
            json.JSONDecodeError, IndexError) as exc:
        raise DrawingError(f"视觉模型调用失败：{type(exc).__name__}: {exc}") from exc
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "features" not in parsed:
            raise ValueError("缺少 features 字段")
        part = parse_json(json.dumps(parsed, ensure_ascii=False))
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise DrawingError(f"视觉模型返回的结构化结果不可用：{exc}") from exc
    part.source = "vision_llm"
    warns = ["位图由视觉模型解读，几何尺寸未经矢量复核，上机前必须人工核对图纸"] \
        + list(part.warnings)
    part.warnings = warns
    return DrawingAnalysis(
        part=part, part_json=part_to_json(part),
        svg=_features_svg(part, uri), backend="vision_llm",
        feature_rows=[{"index": i + 1, "kind": f.kind.value, "cx": f.cx,
                       "cy": f.cy, "depth": f.depth, "tolerance": f.tolerance,
                       "note": "视觉模型识别", "confidence": 0.7}
                      for i, f in enumerate(part.features)],
        confidence=0.7, warnings=warns, drawing_name=name,
        image_data_uri=uri,
    )


def analyze_file(path: Path, hint: str = "") -> DrawingAnalysis:
    """按扩展名路由。文件必须已由调用方做过白名单与大小校验。"""
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIX:
        raise DrawingError(f"不支持的图纸格式 {suffix!r}，支持 {sorted(SUPPORTED_SUFFIX)}")
    if suffix == ".dxf":
        try:
            text = decode_dxf(path.read_bytes())
        except OSError as exc:
            raise DrawingError(f"读取 DXF 失败：{exc}") from exc
        return analyze_dxf(text, path.name)
    if suffix == ".json":
        return analyze_json(decode_dxf(path.read_bytes()), path.name)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".bmp": "image/bmp", ".webp": "image/webp"}[suffix]
    return interpret_raster(path.read_bytes(), mime, path.name, hint)


def analyze_bytes(name: str, data: bytes, hint: str = "") -> DrawingAnalysis:
    """面板上传通路：内存解析，不落盘（调用方决定是否归档）。"""
    if len(data) > MAX_DRAWING_BYTES:
        raise DrawingError(f"图纸超过 {MAX_DRAWING_BYTES // 1024 // 1024}MB 限制")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIX:
        raise DrawingError(f"不支持的图纸格式 {suffix!r}，支持 {sorted(SUPPORTED_SUFFIX)}")
    if suffix == ".dxf":
        return analyze_dxf(decode_dxf(data), name)
    if suffix == ".json":
        return analyze_json(decode_dxf(data), name)
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".bmp": "image/bmp", ".webp": "image/webp"}[suffix]
    return interpret_raster(data, mime, name, hint)
