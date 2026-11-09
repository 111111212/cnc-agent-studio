"""监控循环：帧 -> 检测 -> 时间窗去抖 -> 截图留存 -> 日志/告警 -> 触发 Agent。

去抖是工业现场必需：单帧误检不能触发停机，必须连续多帧一致确认，
并且同一故障在冷却帧数内不重复告警。
"""
from __future__ import annotations

import base64
import json
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..simulator import encode_png
from .detector import FAULT_META, Detection, DetectorBase, FaultClass, VisionEvent
from .frames import FrameSource

COLORS = {
    "critical": (232, 62, 62),
    "high": (245, 158, 42),
    "medium": (240, 208, 62),
    "info": (86, 190, 140),
}


@dataclass
class MonitorConfig:
    window: int = 6               # 统计窗口帧数
    min_hits: int = 3             # 窗口内至少命中帧数
    cooldown: int = 14            # 同类故障触发冷却帧数
    conf_threshold: float = 0.55
    save_snapshots: bool = True
    webhook: str = ""
    max_frames: int | None = None


class Debouncer:
    """按故障类别做时间窗聚合与冷却，抑制单帧误报。"""

    def __init__(self, cfg: MonitorConfig):
        self.cfg = cfg
        self.hits: dict[str, deque] = defaultdict(deque)
        self.last_fire: dict[str, int] = {}

    def update(self, frame_no: int,
               detections: list[Detection]) -> VisionEvent | None:
        by_code: dict[str, list[Detection]] = defaultdict(list)
        for d in detections:
            by_code[d.fault_code].append(d)
        for code, ds in by_code.items():
            q = self.hits[code]
            q.append((frame_no, max(d.confidence for d in ds), ds))
            while q and frame_no - q[0][0] > self.cfg.window:
                q.popleft()
        for code in list(by_code):
            q = self.hits[code]
            if len(q) < self.cfg.min_hits:
                continue
            mean_conf = sum(x[1] for x in q) / len(q)
            if mean_conf < self.cfg.conf_threshold:
                continue
            last = self.last_fire.get(code, -10 ** 9)
            if frame_no - last < self.cfg.cooldown:
                continue
            self.last_fire[code] = frame_no
            recent = q[-1][2]
            meta = FAULT_META.get(FaultClass(code), FAULT_META[FaultClass.DEBRIS])
            return VisionEvent(
                id=f"EV{frame_no:04d}-{code[:6].upper()}",
                ts=datetime.now().isoformat(timespec="milliseconds"),
                frame_no=frame_no,
                fault_code=code,
                label=meta["label"],
                severity=meta["severity"],
                confidence=round(mean_conf, 3),
                detections=recent,
                hit_frames=len(q),
            )
        return None


def annotate(frame: np.ndarray, detections: list[Detection],
             frame_no: int) -> np.ndarray:
    """在截图上画检测框与状态条（numpy 绘制，避免额外依赖）。"""
    img = frame.copy()
    h, w = img.shape[:2]
    img[:26, :] = (22, 27, 35)
    for d in detections:
        c = COLORS.get(d.severity, (240, 208, 62))
        x1, y1, x2, y2 = [max(0, int(v)) for v in d.bbox]
        x2, y2 = min(w - 1, x2), min(h - 1, y2)
        t = 2
        img[y1:y1 + t, x1:x2] = c
        img[y2 - t:y2, x1:x2] = c
        img[y1:y2, x1:x1 + t] = c
        img[y1:y2, x2 - t:x2] = c
        img[max(26, y1 - 16):max(26, y1 - 12), x1:x2] = c
    return img


class VisionLogger:
    """JSONL 追加日志：帧级检测结果、事件、诊断、控制决策全部留痕可审计。"""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        self.records: list[dict[str, Any]] = []

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"),
               "kind": kind, **payload}
        self.records.append(rec)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec


class AlarmDispatcher:
    """告警分发：控制台 + 可选 Webhook（企业微信/钉钉机器人）+ 冷却去重。"""

    def __init__(self, webhook: str = "", cooldown_sec: float = 2.0):
        self.webhook = webhook
        self.cooldown = cooldown_sec
        self.last: dict[str, datetime] = {}
        self.sent: list[dict[str, Any]] = []

    def dispatch(self, event: VisionEvent, message: str) -> None:
        now = datetime.now()
        last = self.last.get(event.fault_code)
        if last and (now - last).total_seconds() < self.cooldown:
            return
        self.last[event.fault_code] = now
        rec = {"ts": now.isoformat(timespec="seconds"),
               "event_id": event.id, "level": event.severity,
               "message": message}
        self.sent.append(rec)
        print(f"  [ALARM][{event.severity.upper():<8}] {event.id} "
              f"{event.label} conf={event.confidence} | {message}")
        if self.webhook:
            try:
                body = json.dumps({"msgtype": "text",
                                   "text": {"content": message}}).encode("utf-8")
                req = urllib.request.Request(
                    self.webhook, data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=3).close()
            except Exception as exc:
                self.sent.append({"webhook_error": str(exc)})


@dataclass
class MonitorResult:
    frames: int
    events: list[VisionEvent]
    backend: str
    fallback_reason: str
    timeline: list[dict[str, Any]] = field(default_factory=list)
    log_path: str = ""
    snapshot_dir: str = ""
    alarms: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        by_fault: dict[str, int] = defaultdict(int)
        for e in self.events:
            by_fault[e.label] += 1
        return {"frames": self.frames, "events": len(self.events),
                "by_fault": dict(by_fault), "backend": self.backend,
                "fallback_reason": self.fallback_reason,
                "alarms": len(self.alarms)}


class VisionMonitor:
    def __init__(self, source: FrameSource, detector: DetectorBase,
                 output_dir: Path, cfg: MonitorConfig | None = None,
                 process_context: dict[str, Any] | None = None,
                 fallback_reason: str = ""):
        self.source = source
        self.detector = detector
        self.output_dir = Path(output_dir)
        self.cfg = cfg or MonitorConfig()
        self.context = process_context or {}
        self.fallback_reason = fallback_reason
        self.snapshot_dir = self.output_dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.logger = VisionLogger(self.output_dir / "events.jsonl")
        self.alarms = AlarmDispatcher(self.cfg.webhook)
        self.debouncer = Debouncer(self.cfg)

    def run(self, on_event: Callable[[VisionEvent], None] | None = None,
            ) -> MonitorResult:
        events: list[VisionEvent] = []
        timeline: list[dict[str, Any]] = []
        frame_no = 0
        for frame_no, frame in enumerate(self.source.frames()):
            if self.cfg.max_frames and frame_no >= self.cfg.max_frames:
                break
            dets = self.detector.detect(frame)
            top = max((d.confidence for d in dets), default=0.0)
            label = max(dets, key=lambda d: d.confidence).label if dets else "正常"
            timeline.append({"frame": frame_no, "label": label,
                             "confidence": round(top, 3),
                             "detections": [d.fault_code for d in dets]})
            self.logger.append("frame", {
                "frame": frame_no, "detections": [d.to_dict() for d in dets]})

            event = self.debouncer.update(frame_no, dets)
            if event is None:
                continue
            event.context = dict(self.context)
            if self.cfg.save_snapshots:
                shot = annotate(frame, event.detections, frame_no)
                png = encode_png(shot)
                path = self.snapshot_dir / f"{event.id}.png"
                path.write_bytes(png)
                event.snapshot_path = str(path)
                event.snapshot_b64 = base64.b64encode(png).decode("ascii")
            events.append(event)
            self.logger.append("event", event.to_dict())
            msg = (f"[{self.context.get('machine', 'CNC')}] 第{frame_no}帧检出"
                   f"{event.label}（置信度 {event.confidence}，"
                   f"连续 {event.hit_frames} 帧确认）｜{FAULT_META[FaultClass(event.fault_code)]['hint']}")
            self.alarms.dispatch(event, msg)
            if on_event:
                on_event(event)
        self.source.release()
        return MonitorResult(
            frames=frame_no + 1, events=events,
            backend=self.detector.name,
            fallback_reason=self.fallback_reason,
            timeline=timeline,
            log_path=str(self.logger.path),
            snapshot_dir=str(self.snapshot_dir),
            alarms=self.alarms.sent,
            config=self.cfg.__dict__,
        )
