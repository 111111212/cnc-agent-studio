"""帧源抽象：真实摄像头/RTSP/视频/图片序列 + 离线合成加工画面。

统一输出 RGB uint8 (H, W, 3)。真实接入需要 opencv-python（可选依赖），
离线演示完全不需要，直接用 numpy 合成带故障的帧，检测器从像素真算结果。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

W, H = 640, 360
CONTACT = (320, 208)          # 刀尖与工件接触点
WORKPIECE = (150, 208, 490, 318)


class FrameSource(ABC):
    name = "base"

    @abstractmethod
    def frames(self) -> Iterator[np.ndarray]:
        ...

    def release(self) -> None:
        pass


class CameraSource(FrameSource):
    """本机摄像头 / RTSP 网络相机 / 工业相机（经 OpenCV）。"""

    name = "camera"

    def __init__(self, device=0, width=640, height=360, fps=25,
                 max_frames: int | None = None):
        self.device, self.width, self.height = device, width, height
        self.fps, self.max_frames = fps, max_frames
        self.cap = None

    def frames(self) -> Iterator[np.ndarray]:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("摄像头接入需要 pip install opencv-python") from exc
        self.cap = cv2.VideoCapture(self.device)
        if not self.cap.isOpened():
            raise RuntimeError(f"无法打开视频源 {self.device!r}（本机演示请用 --demo）")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        self.cap.set(cv2.CAP_PROP_FPS, self.fps)
        n = 0
        while True:
            if self.max_frames and n >= self.max_frames:
                break
            ok, frame = self.cap.read()
            if not ok:
                break
            n += 1
            yield frame[:, :, ::-1].copy()   # BGR -> RGB

    def release(self) -> None:
        if self.cap is not None:
            self.cap.release()


class VideoFileSource(FrameSource):
    name = "video"

    def __init__(self, path: str, step: int = 1, max_frames: int | None = None):
        self.path, self.step, self.max_frames = path, max(1, step), max_frames

    def frames(self) -> Iterator[np.ndarray]:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("视频读取需要 pip install opencv-python") from exc
        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise RuntimeError(f"无法打开视频 {self.path}")
        i = n = 0
        try:
            while True:
                if self.max_frames and n >= self.max_frames:
                    break
                ok, frame = cap.read()
                if not ok:
                    break
                if i % self.step == 0:
                    n += 1
                    yield frame[:, :, ::-1].copy()
                i += 1
        finally:
            cap.release()


class ImageSequenceSource(FrameSource):
    name = "images"

    def __init__(self, directory: str):
        self.directory = Path(directory)

    def frames(self) -> Iterator[np.ndarray]:
        files = sorted(p for p in self.directory.glob("*")
                       if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"))
        if not files:
            raise RuntimeError(f"目录 {self.directory} 中没有图片")
        for p in files:
            try:
                from PIL import Image
                yield np.asarray(Image.open(p).convert("RGB"))
            except ImportError:
                raise RuntimeError("图片序列需要 pip install pillow")


@dataclass
class SceneSpan:
    start: int
    end: int
    fault: str          # normal | sparks | chips | tool_break | shift | no_coolant | debris
    severity: float = 0.8


DEMO_SCENARIO: list[SceneSpan] = [
    SceneSpan(0, 8, "normal"),
    SceneSpan(9, 22, "sparks", 0.85),          # 异常火花 -> 疑似崩刀/干切
    SceneSpan(23, 30, "normal"),
    SceneSpan(31, 46, "chips", 0.9),           # 切屑堆积 -> 排屑/冷却不足
    SceneSpan(47, 54, "normal"),
    SceneSpan(55, 70, "tool_break", 0.95),     # 刀具破损 -> 立即停机
    SceneSpan(71, 78, "normal"),
    SceneSpan(79, 92, "no_coolant", 0.7),      # 冷却液缺失 -> 降参数
    SceneSpan(93, 100, "normal"),
    SceneSpan(101, 114, "shift", 0.9),         # 工件位移 -> 安全停机
    SceneSpan(115, 122, "normal"),
    SceneSpan(123, 136, "debris", 0.85),       # 碎片飞溅 -> 疑似碰撞
    SceneSpan(137, 144, "normal"),
]


class SyntheticSource(FrameSource):
    """离线合成工况：用 numpy 真实绘制工件、刀具、切屑、火花、冷却雾与故障。

    检测器看到的是像素，不是注入的标签 —— 因此离线演示同样能验证
    "检测 -> Agent 诊断 -> 控制 -> 工艺反哺"整条链路的真实性。
    """

    name = "synthetic"

    def __init__(self, scenario: list[SceneSpan] | None = None,
                 seed: int = 7, width: int = W, height: int = H):
        self.scenario = scenario or DEMO_SCENARIO
        self.rng = np.random.default_rng(seed)
        self.width, self.height = width, height
        self.total = max(s.end for s in self.scenario) + 1

    def _state(self, i: int) -> tuple[str, float]:
        for s in self.scenario:
            if s.start <= i <= s.end:
                return s.fault, s.severity
        return "normal", 0.0

    def frames(self) -> Iterator[np.ndarray]:
        for i in range(self.total):
            yield self.render(i, *self._state(i))

    def render(self, i: int, fault: str, severity: float) -> np.ndarray:
        sx = self.width / W
        sy = self.height / H
        f = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        f[:] = (38, 42, 48)
        # 机床内腔噪点/暗角
        noise = self.rng.integers(-6, 7, f.shape[:2], dtype=np.int16)
        f[:, :, 0] = np.clip(f[:, :, 0].astype(np.int16) + noise, 0, 255).astype(np.uint8)
        f[:, :, 1] = np.clip(f[:, :, 1].astype(np.int16) + noise, 0, 255).astype(np.uint8)
        f[:, :, 2] = np.clip(f[:, :, 2].astype(np.int16) + noise, 0, 255).astype(np.uint8)

        def rect(x0, y0, x1, y1, color):
            a, b = int(y0 * sy), int(y1 * sy)
            c, d = int(x0 * sx), int(x1 * sx)
            f[max(0, a):min(self.height, b), max(0, c):min(self.width, d)] = color
            return c, a, d, b

        # 工作台
        rect(60, 300, 580, 340, (72, 76, 82))
        # 工件（位移故障时整体右移并倾斜）
        shift = 26 if fault == "shift" else 0
        wx0, wy0, wx1, wy1 = WORKPIECE
        box = rect(wx0 + shift, wy0, wx1 + shift, wy1, (148, 152, 158))
        rect(wx0 + shift, wy0, wx1 + shift, wy0 + 12, (176, 180, 186))
        # 已加工的圆槽投影
        self._ellipse(f, 320 + shift, 258, 78 * sx, 24 * sy, (88, 94, 102))

        # 主轴 + 刀具
        rect(292, 0, 348, 62, (96, 100, 108))
        rect(304, 62, 336, 150, (188, 192, 198))
        rect(310, 150, 330, 208, (206, 210, 216))
        # 刀刃
        rect(310, 196, 330, 210, (120, 126, 134))
        if fault == "tool_break":
            # 刀刃缺口 + 崩落碎片
            rect(310, 194, 322, 212, (34, 38, 44))
            for _ in range(int(18 * severity)):
                x = int(self.rng.integers(270, 380) * sx)
                y = int(self.rng.integers(205, 245) * sy)
                f[y:y + 3, x:x + 3] = (225, 230, 236)

        # 冷却雾（正常工况）：蓝白半透明散射
        if fault != "no_coolant":
            self._coolant(f, sx, sy, alpha=0.55)
        else:
            # 干切：接触区偏暖
            self._glow(f, CONTACT[0] * sx, CONTACT[1] * sy, 46 * sx,
                       (108, 74, 46), 0.35)

        # 正常少量切屑
        self._chips(f, sx, sy, count=10, x_range=(300, 400), y_range=(210, 235))

        if fault == "sparks":
            self._sparks(f, sx, sy, int(70 * severity))
        elif fault == "chips":
            self._chips(f, sx, sy, count=int(150 * severity),
                        x_range=(232, 330), y_range=(196, 250), bright=True)
        elif fault == "debris":
            for _ in range(int(60 * severity)):
                x = int(self.rng.integers(120, 520) * sx)
                y = int(self.rng.integers(160, 320) * sy)
                f[y:y + 4, x:x + 4] = (235, 238, 244)
        elif fault == "shift":
            # 位移基准线（虚线）暴露偏移
            for x in range(150, 490, 18):
                f[int(206 * sy):int(209 * sy), int(x * sx):int((x + 9) * sx)] = (60, 190, 210)

        # 帧号水印（便于日志与截图对账）
        self._ellipse(f, 24, 24, 10, 10, (20, 24, 30))
        return f

    # -- 绘制工具 ---------------------------------------------------------
    def _ellipse(self, f, cx, cy, rx, ry, color):
        h, w = f.shape[:2]
        yy, xx = np.ogrid[:h, :w]
        mask = ((xx - cx) / max(rx, 1)) ** 2 + ((yy - cy) / max(ry, 1)) ** 2 <= 1
        f[mask] = color

    def _glow(self, f, cx, cy, radius, color, alpha):
        h, w = f.shape[:2]
        yy, xx = np.ogrid[:h, :w]
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        mask = d < radius
        wgt = (1 - d / radius)[mask] * alpha
        sub = f[mask].astype(np.float32)
        f[mask] = np.clip(sub * (1 - wgt[..., None]) +
                          np.array(color, dtype=np.float32) * wgt[..., None],
                          0, 255).astype(np.uint8)

    def _coolant(self, f, sx, sy, alpha=0.5):
        cx, cy = CONTACT[0] * sx, CONTACT[1] * sy
        for _ in range(90):
            x = int(cx + self.rng.normal(0, 34))
            y = int(cy + self.rng.normal(0, 20))
            if 0 <= x < f.shape[1] and 0 <= y < f.shape[0]:
                blend = f[y:y + 3, x:x + 3].astype(np.float32)
                f[y:y + 3, x:x + 3] = np.clip(
                    blend * (1 - alpha) + np.array([176, 208, 226]) * alpha,
                    0, 255).astype(np.uint8)

    def _chips(self, f, sx, sy, count, x_range, y_range, bright=False):
        color = (232, 236, 242) if bright else (186, 190, 196)
        for _ in range(count):
            x = int(self.rng.integers(*x_range) * sx)
            y = int(self.rng.integers(*y_range) * sy)
            ln = int(self.rng.integers(3, 10))
            f[y:y + 2, x:min(f.shape[1], x + ln)] = color

    def _sparks(self, f, sx, sy, count):
        cx, cy = CONTACT[0] * sx, CONTACT[1] * sy
        for _ in range(count):
            a = self.rng.uniform(0, 2 * np.pi)
            r = self.rng.uniform(6, 86)
            x = int(cx + np.cos(a) * r)
            y = int(cy + np.sin(a) * r * 0.6)
            if 0 <= x < f.shape[1] and 0 <= y < f.shape[0]:
                tone = self.rng.choice([(255, 196, 60), (255, 138, 32), (255, 232, 140)])
                f[max(0, y - 1):y + 2, max(0, x - 1):x + 2] = tone


def make_source(kind: str, **kwargs) -> FrameSource:
    """帧源工厂。kind: demo | camera | video | images"""
    kind = kind.lower()
    if kind in ("demo", "synthetic"):
        return SyntheticSource(**{k: v for k, v in kwargs.items()
                                  if k in ("scenario", "seed", "width", "height")})
    if kind == "camera":
        return CameraSource(kwargs.get("device", 0),
                            kwargs.get("width", 640), kwargs.get("height", 360),
                            kwargs.get("fps", 25), kwargs.get("max_frames"))
    if kind == "video":
        return VideoFileSource(kwargs["file"], kwargs.get("step", 1),
                               kwargs.get("max_frames"))
    if kind == "images":
        return ImageSequenceSource(kwargs["directory"])
    raise ValueError(f"未知帧源 {kind}，可选 demo/camera/video/images")
