"""YOLO 目标检测层：真实权重可插拔，无权重时用像素启发式后端离线跑通。

检测类别为机加工场景自定义故障类（需要自训练 YOLO 权重，COCO 权重不适用）。
工厂函数优先加载 ultralytics YOLO；不可用时自动回退 HeuristicDetector，
并在报告里标注 backend，绝不假装"用了 YOLO"。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np


class FaultClass(str, Enum):
    NORMAL = "normal"
    ABNORMAL_SPARKS = "abnormal_sparks"        # 异常火花 / 干切 / 崩刀前兆
    CHIP_ACCUMULATION = "chip_accumulation"    # 切屑堆积 / 排屑不畅
    TOOL_BREAKAGE = "tool_breakage"            # 刀具破损 / 崩刃
    WORKPIECE_SHIFT = "workpiece_shift"        # 工件位移 / 装夹松动
    COOLANT_MISSING = "coolant_missing"        # 冷却液缺失
    DEBRIS = "collision_debris"                # 碎片飞溅 / 疑似碰撞


FAULT_META: dict[FaultClass, dict[str, str]] = {
    FaultClass.ABNORMAL_SPARKS: {
        "label": "异常火花", "severity": "high", "icon": "✦",
        "hint": "疑似刀具磨损、切削速度过高或冷却失效"},
    FaultClass.CHIP_ACCUMULATION: {
        "label": "切屑堆积", "severity": "medium", "icon": "≋",
        "hint": "排屑不畅/冷却冲屑不足，易造成二次切削与刮伤"},
    FaultClass.TOOL_BREAKAGE: {
        "label": "刀具破损", "severity": "critical", "icon": "✕",
        "hint": "刃口崩缺，继续加工将过切、撞刀甚至损伤主轴"},
    FaultClass.WORKPIECE_SHIFT: {
        "label": "工件位移", "severity": "critical", "icon": "⇄",
        "hint": "装夹松动或压板失效，坐标系已失效"},
    FaultClass.COOLANT_MISSING: {
        "label": "冷却液缺失", "severity": "high", "icon": "◌",
        "hint": "干切导致刀具急剧磨损与热变形"},
    FaultClass.DEBRIS: {
        "label": "碎片飞溅", "severity": "high", "icon": "✳",
        "hint": "疑似碰撞或工件崩裂，需立即核查"},
    FaultClass.NORMAL: {
        "label": "正常", "severity": "info", "icon": "✓", "hint": ""},
}


@dataclass
class Detection:
    label: str
    fault_code: str
    confidence: float
    bbox: tuple[int, int, int, int]   # x1,y1,x2,y2
    severity: str = "medium"
    extra: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"bbox": list(self.bbox)}


@dataclass
class VisionEvent:
    id: str
    ts: str
    frame_no: int
    fault_code: str
    label: str
    severity: str
    confidence: float
    detections: list[Detection]
    snapshot_path: str = ""
    snapshot_b64: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    hit_frames: int = 0

    def to_dict(self, with_image: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id, "ts": self.ts, "frame_no": self.frame_no,
            "fault_code": self.fault_code, "label": self.label,
            "severity": self.severity, "confidence": round(self.confidence, 3),
            "detections": [x.to_dict() for x in self.detections],
            "snapshot_path": self.snapshot_path, "context": self.context,
            "hit_frames": self.hit_frames,
        }
        if with_image:
            d["snapshot_b64"] = self.snapshot_b64
        return d


class DetectorBase:
    name = "base"
    classes = [c.value for c in FaultClass if c != FaultClass.NORMAL]

    def detect(self, frame: np.ndarray) -> list[Detection]:
        raise NotImplementedError


class YOLODetector(DetectorBase):
    """ultralytics YOLO 后端。weights 指向自训练的机加工故障检测权重。"""

    name = "yolo"

    def __init__(self, weights: str, conf: float = 0.45, imgsz: int = 640,
                 class_map: dict[int, str] | None = None):
        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise RuntimeError(
                "YOLO 后端需要 pip install ultralytics；或留空权重使用离线启发式后端"
            ) from exc
        self.model = YOLO(weights)
        self.conf, self.imgsz = conf, imgsz
        self.class_map = class_map or {}

    def detect(self, frame: np.ndarray) -> list[Detection]:
        results = self.model.predict(frame, conf=self.conf, imgsz=self.imgsz,
                                     verbose=False)[0]
        out: list[Detection] = []
        for box in results.boxes:
            cls_id = int(box.cls.item())
            raw = self.class_map.get(cls_id) or results.names.get(cls_id, "")
            code = self._normalize(raw)
            if code == FaultClass.NORMAL:
                continue
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
            meta = FAULT_META[code]
            out.append(Detection(meta["label"], code.value,
                                 float(box.conf.item()), (x1, y1, x2, y2),
                                 meta["severity"]))
        return out

    @staticmethod
    def _normalize(raw: str) -> FaultClass:
        key = raw.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "spark": FaultClass.ABNORMAL_SPARKS, "sparks": FaultClass.ABNORMAL_SPARKS,
            "fire": FaultClass.ABNORMAL_SPARKS, "chip": FaultClass.CHIP_ACCUMULATION,
            "chips": FaultClass.CHIP_ACCUMULATION, "swarf": FaultClass.CHIP_ACCUMULATION,
            "broken_tool": FaultClass.TOOL_BREAKAGE, "tool_break": FaultClass.TOOL_BREAKAGE,
            "shift": FaultClass.WORKPIECE_SHIFT, "displacement": FaultClass.WORKPIECE_SHIFT,
            "no_coolant": FaultClass.COOLANT_MISSING, "dry_cut": FaultClass.COOLANT_MISSING,
            "debris": FaultClass.DEBRIS, "collision": FaultClass.DEBRIS,
        }
        if key in aliases:
            return aliases[key]
        for fc in FaultClass:
            if fc.value == key or key.startswith(fc.value[:6]):
                return fc
        return FaultClass.DEBRIS


class HeuristicDetector(DetectorBase):
    """像素启发式后端：颜色/亮度/几何统计，从合成帧或真实帧真算检测结果。

    不是返回注入的标签 —— 它只看 frame 的像素，因此离线演示同样能验证
    检测 -> 诊断 -> 控制 -> 工艺反哺链路是否成立。
    """

    name = "heuristic"

    def __init__(self, conf: float = 0.42):
        self.conf = conf

    @staticmethod
    def _bbox(mask: np.ndarray, ox: int = 0, oy: int = 0) -> tuple[int, int, int, int] | None:
        ys, xs = np.where(mask)
        if len(xs) < 8:
            return None
        return (int(xs.min() + ox), int(ys.min() + oy),
                int(xs.max() + ox), int(ys.max() + oy))

    @staticmethod
    def _score(count: int, lo: float, hi: float) -> float:
        if count <= lo:
            return 0.0
        return round(min(0.99, 0.45 + 0.54 * (count - lo) / max(hi - lo, 1)), 3)

    def detect(self, frame: np.ndarray) -> list[Detection]:
        f = frame.astype(np.int16)
        R, G, B = f[:, :, 0], f[:, :, 1], f[:, :, 2]
        h, w = R.shape
        sx, sy = w / 640.0, h / 360.0
        out: list[Detection] = []

        def roi(x0, y0, x1, y1):
            return (slice(int(y0 * sy), int(y1 * sy)),
                    slice(int(x0 * sx), int(x1 * sx)))

        # --- 异常火花：橙黄高亮像素 ---
        rr = roi(200, 140, 460, 280)
        sr = R[rr]; sg = G[rr]; sb = B[rr]
        spark_mask = (sr > 205) & (sg > 100) & (sg < 235) & (sb < 120) & (sr - sg > 35)
        n_spark = int(spark_mask.sum())
        score = self._score(n_spark, 55, 420)
        if score >= self.conf:
            bb = self._bbox(spark_mask, int(200 * sx), int(140 * sy))
            if bb:
                out.append(self._mk(FaultClass.ABNORMAL_SPARKS, score, bb,
                                    {"spark_px": float(n_spark)}))

        # --- 切屑堆积：银白近中性高亮 ---
        cr = roi(210, 185, 430, 270)
        crr, cgg, cbb = R[cr], G[cr], B[cr]
        mx = np.maximum(np.maximum(crr, cgg), cbb)
        mn = np.minimum(np.minimum(crr, cgg), cbb)
        chip_mask = (mn > 195) & ((mx - mn) < 42)
        n_chip = int(chip_mask.sum())
        # 正常工况也有少量切屑，用高阈值抑制误报
        score = self._score(n_chip, 620, 2600)
        if score >= self.conf:
            bb = self._bbox(chip_mask, int(210 * sx), int(185 * sy))
            if bb:
                out.append(self._mk(FaultClass.CHIP_ACCUMULATION, score, bb,
                                    {"chip_px": float(n_chip)}))

        # --- 刀具破损：刀刃本体区域出现暗缺口 ---
        # ROI 必须严格落在刀具宽度内（306~334 会把两侧深色背景算进来，
        # 导致正常帧也判破损）；正常刀刃灰度约 120，破损缺口约 34。
        tr = roi(310, 196, 330, 212)
        trr = R[tr]
        dark_ratio = float((trr < 80).mean())
        if dark_ratio > 0.12:
            out.append(self._mk(FaultClass.TOOL_BREAKAGE,
                                round(min(0.98, 0.6 + dark_ratio), 3),
                                (int(310 * sx), int(196 * sy),
                                 int(330 * sx), int(212 * sy)),
                                {"dark_ratio": round(dark_ratio, 3)}))

        # --- 冷却液缺失：接触区蓝白雾像素显著偏少 ---
        br = roi(240, 168, 410, 250)
        brr, bgg, bbb = R[br], G[br], B[br]
        cool_mask = (bbb > 150) & (bbb - brr > 14) & (bgg > 140)
        cool_ratio = float(cool_mask.mean())
        if cool_ratio < 0.012 and n_spark < 80 and dark_ratio < 0.10:
            out.append(self._mk(FaultClass.COOLANT_MISSING, 0.82,
                                (int(250 * sx), int(180 * sy),
                                 int(390 * sx), int(240 * sy)),
                                {"coolant_ratio": round(cool_ratio, 4)}))

        # --- 工件位移：检测工件左边缘列位置 ---
        er = roi(120, 248, 220, 274)
        err, egg, ebb = R[er], G[er], B[er]
        metal = (err > 125) & (err < 205) & (np.abs(err - egg) < 14)
        cols = np.where(metal.sum(axis=0) > 6)[0]
        if len(cols):
            left_edge = int(cols.min() + 120 * sx)
            if left_edge > 162 * sx:
                out.append(self._mk(FaultClass.WORKPIECE_SHIFT, 0.9,
                                    (left_edge, int(248 * sy),
                                     int(215 * sx), int(274 * sy)),
                                    {"left_edge_x": float(left_edge)}))

        # --- 碎片飞溅：只在"非接触区"统计离散亮斑 ---
        # 切屑堆积也产生大量银白像素，但集中在接触区；用外围区域区分二者。
        dr = roi(100, 150, 540, 320)
        drr = R[dr]
        debris_mask = drr > 225
        h_dr, w_dr = debris_mask.shape
        cx0, cx1 = int((220 - 100) * sx), int((420 - 100) * sx)
        cy0, cy1 = int((185 - 150) * sy), int((260 - 150) * sy)
        inner = np.zeros_like(debris_mask)
        inner[max(0, cy0):min(h_dr, cy1), max(0, cx0):min(w_dr, cx1)] = True
        outer_mask = debris_mask & (~inner)
        n_debris = int(outer_mask.sum())
        if n_debris > 120:
            bb = self._bbox(outer_mask, int(100 * sx), int(150 * sy))
            if bb:
                out.append(self._mk(FaultClass.DEBRIS,
                                    self._score(n_debris, 120, 900), bb,
                                    {"outer_debris_px": float(n_debris)}))

        return out

    @staticmethod
    def _mk(fc: FaultClass, conf: float, bbox, extra) -> Detection:
        meta = FAULT_META[fc]
        return Detection(meta["label"], fc.value, conf, bbox,
                         meta["severity"], extra)


def create_detector(weights: str = "", conf: float = 0.42,
                    class_map: dict[int, str] | None = None
                    ) -> tuple[DetectorBase, str]:
    """返回 (检测器, 回退原因)。weights 为空则直接用启发式后端。"""
    if weights:
        try:
            return YOLODetector(weights, conf, class_map=class_map), ""
        except Exception as exc:
            return HeuristicDetector(conf), (
                f"YOLO 权重 {weights!r} 不可用（{type(exc).__name__}: {exc}），"
                f"已回退启发式后端")
    return HeuristicDetector(conf), "未配置 YOLO 权重，使用离线像素启发式后端"
