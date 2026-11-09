"""CNC Agent 操控面板后端。

设计约束（面向上线的工业系统，宁可少功能也不放松安全）：
  1. 零新增依赖：只用 Python 标准库 + 项目已有的 numpy。
  2. 默认 ``PANEL_MODE=demo``：可跑完整链路，但所有控制下发强制 dry-run，
     不开放 --live / 任意协议写地址 / 任意文件路径。
  3. ``PANEL_MODE=readonly``：公网演示模式，禁用一切执行类 POST，
     只保留只读查看、报告打开与纯计算的报警诊断。
  4. 执行类任务全局串行（多个链路会共写 output/，并发会互相覆盖产物），
     每个任务结束后把产物快照到 output/panel_runs/<task_id>/ 供回溯。
  5. 静态/产物访问做 realpath 目录逃逸校验；可选 PANEL_TOKEN + Cookie 鉴权。

启动：python panel/server.py   或   python -m panel.server
"""
from __future__ import annotations

import base64
import dataclasses
import io
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cncagent.agent import CNCAgent                       # noqa: E402
from cncagent.config import OUTPUT_DIR, get_settings, masked_key  # noqa: E402
from cncagent.cutting import estimate_cycle_time          # noqa: E402
from cncagent.diagnose import (SAMPLE_ALARM_LOG, alarm_summary,  # noqa: E402
                               build_delivery, diagnose_alarms)
from cncagent.drawing import (MAX_DRAWING_BYTES, RASTER_SUFFIX,  # noqa: E402
                              SUPPORTED_SUFFIX, DrawingError, analyze_bytes)
from cncagent.machines import (PROFILES, capability_summary,  # noqa: E402
                               get_profile, list_profiles)
from cncagent.vision.closed_loop import ClosedLoopController  # noqa: E402
from cncagent.vision.detector import create_detector          # noqa: E402
from cncagent.vision.frames import make_source                # noqa: E402
from cncagent.vision.monitor import MonitorConfig, VisionMonitor  # noqa: E402
from panel.machine import LINK, read_audit               # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
SNAPSHOT_ROOT = OUTPUT_DIR / "panel_runs"
DRAWING_DIR = OUTPUT_DIR / "drawings"
EXAMPLE_DRAWING = ROOT / "examples" / "drawing_plate.dxf"
DEMO_TEXT = "加工一块45#钢方块，长宽100×100mm，铣一个直径50mm圆形凹槽，深度3mm"
VERSION = "1.0.0"

MODE = os.getenv("PANEL_MODE", "demo").strip().lower()
READONLY = MODE in {"readonly", "online", "public", "read-only"}
TOKEN = os.getenv("PANEL_TOKEN", "").strip()
HOST = os.getenv("PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("PANEL_PORT", "8787"))
MAX_LOG_LINES = 900
MAX_TASKS = 30
ALLOWED_SUFFIX = {".html", ".htm", ".json", ".jsonl", ".nc", ".txt",
                  ".md", ".png", ".jpg", ".jpeg", ".log", ".csv"}

_TASKS: dict[str, dict] = {}
# 可重入锁：任务列表序列化、日志追加、任务创建存在嵌套调用，
# 普通 Lock 会在「持锁遍历时调 _task_public」自死锁，导致 /api/tasks 永久无响应。
_TASKS_LOCK = threading.RLock()
_RUN_LOCK = threading.Lock()          # 全链路任务串行执行
_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}   # 来源 IP -> 近 60s 失败时间戳
STARTED_AT = time.time()


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _jsonable(obj):
    """把 dataclass / Path / 任意对象安全转成 JSON 可序列化的结构。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if hasattr(obj, "to_dict"):
        try:
            return _jsonable(obj.to_dict())
        except Exception:
            pass
    return str(obj)


def _rel(path) -> str:
    """把 output 下的绝对路径转成 /view/ 可用的相对路径。"""
    try:
        return Path(path).resolve().relative_to(OUTPUT_DIR.resolve()).as_posix()
    except Exception:
        return ""


def _safe_existing(rel: str, root: Path, want_dir: bool = False) -> Path | None:
    """防目录穿越：解析后的真实路径必须仍在 root 内且真实存在。"""
    try:
        target = (root / unquote(str(rel))).resolve()
        root_r = root.resolve()
        if target != root_r and root_r not in target.parents:
            return None
        ok = target.is_dir() if want_dir else target.is_file()
        return target if ok else None
    except Exception:
        return None


def _safe_path(rel: str, root: Path) -> Path | None:
    return _safe_existing(rel, root)


def _tail_jsonl(path: Path, limit: int = 100) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    out.append({"raw": line})
    except Exception:
        return []
    return out


def _log(task: dict, line: str) -> None:
    with _TASKS_LOCK:
        task["logs"].append(line)
        if len(task["logs"]) > MAX_LOG_LINES:
            del task["logs"][:len(task["logs"]) - MAX_LOG_LINES]


class _LineCapture(io.TextIOBase):
    """把任务内 print 的 stdout 按行实时收进任务日志。"""

    def __init__(self, task: dict):
        self.task = task
        self.buf = ""

    def write(self, s: str) -> int:
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            _log(self.task, line.rstrip("\r"))
        return len(s)

    def flush(self) -> None:
        if self.buf.strip():
            _log(self.task, self.buf.rstrip("\r"))
        self.buf = ""


def _task_public(task: dict, include_result: bool = True) -> dict:
    with _TASKS_LOCK:
        d = {
            "id": task["id"], "type": task["type"], "status": task["status"],
            "created": task["created"], "started": task["started"],
            "finished": task["finished"], "params": task["params"],
            "logs": task["logs"][:], "error": task.get("error", ""),
            "snapshot": task.get("snapshot", ""),
        }
        if include_result:
            d["result"] = task.get("result")
    return d


# --------------------------------------------------------------------------
# 任务执行
# --------------------------------------------------------------------------
def _safe_upload_name(name: str) -> str | None:
    """上传文件名只保留基名 + 白名单字符，杜绝 ../ 与绝对路径。"""
    base = Path(str(name).replace("\\", "/")).name
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]{1,80}", base):
        return None
    if Path(base).suffix.lower() not in SUPPORTED_SUFFIX:
        return None
    return base


def _decode_upload(body: dict) -> tuple[str, bytes]:
    name = _safe_upload_name(body.get("name") or "")
    if not name:
        raise DrawingError(
            f"文件名或扩展名不合法，支持 {sorted(SUPPORTED_SUFFIX)}")
    if isinstance(body.get("text"), str):
        data = body["text"].encode("utf-8")
    else:
        try:
            data = base64.b64decode(body.get("content_base64") or "",
                                    validate=True)
        except Exception as exc:
            raise DrawingError(f"base64 解码失败：{exc}") from exc
    if not data:
        raise DrawingError("图纸内容为空")
    if len(data) > MAX_DRAWING_BYTES:
        raise DrawingError(
            f"图纸超过 {MAX_DRAWING_BYTES // 1024 // 1024}MB 限制")
    if len(body.get("hint") or "") > 300:
        raise DrawingError("补充提示过长")
    return name, data


def _do_drawing_analyze(name: str, data: bytes, hint: str = "",
                        save: bool = True) -> dict:
    """解析 + 归档。只读模式不落盘、不触发外部视觉模型。"""
    if READONLY and Path(name).suffix.lower() in RASTER_SUFFIX:
        raise DrawingError(
            "只读模式禁用位图识读（会调用外部视觉模型）；请改用 DXF 或结构化 JSON")
    analysis = analyze_bytes(name, data, hint)
    if save and not READONLY:
        DRAWING_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(name).stem
        (DRAWING_DIR / (_safe_upload_name(name) or "upload.dxf")).write_bytes(data)
        (DRAWING_DIR / f"{stem}_analysis.json").write_text(
            json.dumps(analysis.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8")
        (DRAWING_DIR / f"{stem}_preview.svg").write_text(
            analysis.svg, encoding="utf-8")
    return analysis.to_dict()


def _settings_for(p: dict):
    s = get_settings()
    machine = (p.get("machine") or s.machine_id or "vmc850_fanuc").strip()
    if machine not in PROFILES:
        raise ValueError(f"未知机型 {machine!r}")
    s = replace(s, machine_id=machine)
    ctrl = (p.get("controller") or "").strip().upper()
    # 空 = 跟随机型自带方言；非空只允许三种已实现的后置
    s = replace(s, controller=ctrl if ctrl in {"FANUC", "SIEMENS", "HNC"} else "")
    return s


def _vision_source(p: dict, max_frames=None):
    kind = (p.get("frames") or "demo").strip().lower()
    if kind not in {"demo", "camera", "video", "images"}:
        kind = "demo"
    # 面板不接真实摄像头硬件：避免 demo 面板误触发本机采集设备
    if kind == "camera":
        raise ValueError("面板默认不启用本机摄像头采集，请在 CLI 用 monitor --frames camera 接入")
    return make_source(kind, device=int(p.get("device", 0) or 0),
                       file=p.get("video", ""), directory=p.get("images", ""),
                       max_frames=max_frames)


def _dispatch_run(task: dict, p: dict) -> dict:
    s = _settings_for(p)
    agent = CNCAgent(s)
    inject = faults = None
    if p.get("hallucination"):
        inject = {"spindle": 30000, "feed": 9000, "ap": 25.0}
        faults = ["overtravel", "plunge", "bad_g", "overspeed"]
        _log(task, "[面板] 已注入幻觉参数 S=30000 F=9000 ap=25 + 4 条错误 G 代码段")
    write_files = not bool(p.get("no_files"))
    r = agent.run(p.get("text") or DEMO_TEXT, source=p.get("source") or "auto",
                  inject=inject, faults=faults, write_files=write_files)
    result = {
        "approved": r.approved,
        "blocked_reason": r.blocked_reason,
        "controller": r.controller,
        "planner": r.plan.planner,
        "attempts": r.attempts,
        "machine": r.profile.to_dict() if r.profile else None,
        "stages": [st.to_dict() for st in r.stages],
        "operations": [op.to_dict() for op in r.plan.operations],
        "validation": r.validation.to_dict() if r.validation else None,
        "simulation": r.simulation.stats if r.simulation else None,
        "simulation_issues": r.simulation.issues if r.simulation else [],
        "cycle_time_min": estimate_cycle_time(
            r.plan, r.program.metadata.get("cut_length", 0)) if r.program else 0,
        "program": {
            "blocks": r.program.block_count if r.program else 0,
            "text": r.program.text if r.program else "",
            "lines": (r.program.text.splitlines()[:80] if r.program else []),
        },
        "output_files": {k: _rel(v) for k, v in r.output_files.items()},
    }
    deliver = p.get("deliver") or "none"
    if deliver in {"usb", "dnc"} and write_files:
        pkg = build_delivery(r, OUTPUT_DIR, deliver)
        (OUTPUT_DIR / "delivery.json").write_text(
            json.dumps(pkg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        result["delivery"] = {"approved": pkg.approved,
                              "sha256": pkg.sha256, "note": pkg.note,
                              "mode": deliver, "dry_run": True}
        _log(task, f"[下发] {'允许' if pkg.approved else '拒绝'} ｜ "
                   f"SHA256 {(pkg.sha256 or '-')[:24]} ｜ dry-run，不连机床")
    return _jsonable(result)


def _dispatch_monitor(task: dict, p: dict) -> dict:
    s = _settings_for(p)
    profile = get_profile(s.machine_id)
    detector, note = create_detector(p.get("weights") or s.vision_weights)
    max_frames = p.get("max_frames") or None
    source = _vision_source(p, max_frames)
    ctx = {"machine": profile.display, "controller": profile.controller,
           "mode": "panel_standalone"}
    cfg = MonitorConfig(max_frames=max_frames)
    mon = VisionMonitor(source, detector, OUTPUT_DIR / "vision", cfg,
                        process_context=ctx, fallback_reason=note)
    _log(task, f"[监控] 帧源 {source.name} ｜ 检测器 {detector.name} ｜ {note or 'YOLO 权重已加载'}")
    res = mon.run()
    events = []
    for e in res.events:
        d = e.to_dict()
        d.pop("snapshot_b64", None)          # 图片走 /view/，不塞进 JSON
        d["snapshot_url"] = ("/view/" + _rel(e.snapshot_path)) if e.snapshot_path else ""
        events.append(_jsonable(d))
    return _jsonable({"summary": res.summary(), "events": events,
                      "log_path": _rel(res.log_path)})


def _dispatch_closed_loop(task: dict, p: dict) -> dict:
    s = _settings_for(p)
    profile = get_profile(s.machine_id)
    detector, note = create_detector(p.get("weights") or s.vision_weights)
    max_frames = p.get("max_frames") or None
    source = _vision_source(p, max_frames)
    ctl = ClosedLoopController(s, profile, OUTPUT_DIR)
    # 面板硬约束：永远 dry_run、永远不模拟"操作员已确认"
    result = ctl.run_full(
        p.get("text") or DEMO_TEXT, detector, note, source=source,
        protocol="dryrun", dry_run=True, human_confirmed=False,
        cfg=MonitorConfig(max_frames=max_frames))
    traces = []
    for t in ctl.traces:
        ev = t.event.to_dict()
        ev.pop("snapshot_b64", None)
        traces.append(_jsonable({
            "event": ev,
            "snapshot_url": ("/view/" + _rel(t.event.snapshot_path))
                            if t.event.snapshot_path else "",
            "cause": t.diagnosis.cause,
            "reasoning": t.diagnosis.reasoning,
            "source": t.diagnosis.source,
            "actions": t.diagnosis.immediate_actions,
            "approved_signals": t.decision.approved_signals,
            "blocked_signals": t.decision.blocked_signals,
            "controls": [c.to_dict() for c in t.controls],
        }))
    return _jsonable({
        "summary": result.summary,
        "traces": traces,
        "parameter_diff": result.parameter_diff,
        "adapter_note": result.adapter_note,
        "output_files": {k: _rel(v) for k, v in result.output_files.items()},
        "safety": "面板固定 dry-run；无真实协议写入，急停仅走硬件安全 PLC",
    })


_DISPATCH = {"run": _dispatch_run, "monitor": _dispatch_monitor,
             "closed_loop": _dispatch_closed_loop}


def _snapshot(task: dict) -> None:
    """把本次运行涉及的关键产物另存一份，避免后续任务覆盖历史报告。"""
    try:
        dest = SNAPSHOT_ROOT / task["id"]
        dest.mkdir(parents=True, exist_ok=True)
        copied = []
        for f in sorted(OUTPUT_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in ALLOWED_SUFFIX:
                shutil.copy2(f, dest / f.name)
                copied.append(f.name)
        vdir = OUTPUT_DIR / "vision"
        if vdir.is_dir():
            for f in sorted(vdir.glob("*.jsonl")):
                shutil.copy2(f, dest / ("vision_" + f.name))
                copied.append("vision/" + f.name)
        (dest / "task.json").write_text(
            json.dumps(_task_public(task), ensure_ascii=False, indent=2),
            encoding="utf-8")
        with _TASKS_LOCK:
            task["snapshot"] = dest.relative_to(OUTPUT_DIR).as_posix()
        _log(task, f"[面板] 产物快照已保存 {len(copied)} 个文件 -> output/{task['snapshot']}")
    except Exception as exc:
        _log(task, f"[面板] 产物快照失败：{exc}")


def _execute(task: dict) -> None:
    task["status"] = "running"
    task["started"] = time.time()
    capture = _LineCapture(task)
    try:
        with _RUN_LOCK:
            with redirect_stdout(capture):
                task["result"] = _DISPATCH[task["type"]](task, task["params"])
        capture.flush()
        task["status"] = "done"
        _log(task, "[面板] 任务完成")
    except Exception as exc:
        capture.flush()
        task["status"] = "error"
        task["error"] = f"{type(exc).__name__}: {exc}"
        _log(task, "[错误] " + traceback.format_exc(limit=4).strip())
    finally:
        task["finished"] = time.time()
        _snapshot(task)


# --------------------------------------------------------------------------
# 只读状态
# --------------------------------------------------------------------------
def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def api_state() -> dict:
    s = get_settings()
    run = _read_json(OUTPUT_DIR / "run_summary.json", {})
    loop = _read_json(OUTPUT_DIR / "closed_loop_summary.json", {})
    delivery = _read_json(OUTPUT_DIR / "delivery.json", {})
    events = _tail_jsonl(OUTPUT_DIR / "vision" / "events.jsonl", limit=500)
    files = api_files()["files"]
    return {
        "mode": MODE, "readonly": READONLY,
        "llm": {"available": s.llm_available, "model": s.llm_model,
                "key_display": masked_key(s.llm_api_key)},
        "vision": {"weights_configured": bool(s.vision_weights),
                   "protocol": s.vision_protocol,
                   "auto_stop_allowed": s.allow_auto_stop},
        "machine_id": s.machine_id,
        "last_run": {
            "approved": run.get("approved"),
            "controller": run.get("controller"),
            "attempts": run.get("attempts"),
            "validation": run.get("validation", {}).get("ok") if isinstance(run.get("validation"), dict) else None,
            "simulation": run.get("simulation", {}),
            "cycle_time_min": run.get("cycle_time_min"),
            "program_sha256": run.get("program_sha256"),
        } if run else None,
        "last_closed_loop": loop or None,
        "delivery": {"approved": delivery.get("approved"),
                     "sha256": delivery.get("sha256"),
                     "mode": delivery.get("mode")} if delivery else None,
        "event_count": len(events),
        "file_count": len(files),
        "reports": [f for f in files if f["path"].endswith(".html")][:8],
        "machine": LINK.status(),
    }


def api_files() -> dict:
    out = []
    if OUTPUT_DIR.is_dir():
        for f in OUTPUT_DIR.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in ALLOWED_SUFFIX:
                continue
            if f.name == ".gitkeep":
                continue
            try:
                st = f.stat()
                out.append({"path": f.relative_to(OUTPUT_DIR).as_posix(),
                            "size": st.st_size, "mtime": st.st_mtime,
                            "url": "/view/" + f.relative_to(OUTPUT_DIR).as_posix()})
            except Exception:
                continue
    out.sort(key=lambda x: -x["mtime"])
    return {"files": out[:120], "total": len(out)}


def api_health() -> dict:
    s = get_settings()
    return {
        "ok": True, "version": VERSION, "mode": MODE, "readonly": READONLY,
        "auth_required": bool(TOKEN), "uptime_sec": round(time.time() - STARTED_AT, 1),
        "machine_id": s.machine_id, "machine_count": len(PROFILES),
        "llm_available": s.llm_available,
        "yolo_weights": bool(s.vision_weights),
        "control_mode": "machine_link (write gate: " +
                        ("enabled" if LINK.server_allows_write else "disabled") + ")",
        "machine_linked": LINK.connected,
        "machine_target": LINK.target,
        "busy": _RUN_LOCK.locked(),
    }


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = f"CNCPanel/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # 走 stderr，避免与任务 stdout 捕获互相污染；请求行里的令牌必须脱敏
        msg = fmt % args
        if TOKEN:
            msg = msg.replace(TOKEN, "***")
        msg = re.sub(r"([?&](?:token|key|secret|access_token)=)[^&\s]+",
                     r"\1***", msg, flags=re.IGNORECASE)
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), msg))

    # ---- 基础响应 -------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 内置预览 / 看板常以 iframe 嵌入，SAMEORIGIN 会让页面直接白屏。
        # 仅 demo 本机模式放宽（可用 PANEL_ALLOW_EMBED=0 关掉）；
        # 车间内网 / 公网只读模式仍保持 SAMEORIGIN，防点击劫持。
        if not (MODE == "demo" and os.getenv("PANEL_ALLOW_EMBED", "1") == "1"):
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(_jsonable(obj), ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _serve_file(self, path: Path | None, download: bool = False):
        if path is None:
            return self._err("文件不存在或路径不合法", 404)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix.lower() in {".html", ".htm"}:
            ctype = "text/html; charset=utf-8"
        elif path.suffix.lower() in {".json", ".jsonl", ".nc", ".txt", ".md", ".log", ".csv"}:
            ctype = "text/plain; charset=utf-8"
        extra = {}
        if download:
            extra["Content-Disposition"] = f'attachment; filename="{path.name}"'
        try:
            self._send(200, path.read_bytes(), ctype, extra)
        except Exception as exc:
            self._err(f"读取失败：{exc}", 500)

    # ---- 鉴权 -----------------------------------------------------------
    def _authed(self) -> bool:
        if not TOKEN:
            return True
        if self.headers.get("X-Panel-Token", "") == TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        if f"panel_token={TOKEN}" in cookie:
            return True
        # 不接受 ?token=...：它会进入浏览器历史、Referer 与反向代理
        # access log；在 PANEL_ALLOW_WRITE=1 的车间部署下等价于泄露控制权限。
        return False

    def _body(self, limit: int = 2_000_000) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n <= 0 or n > limit:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path == "/":
                return self._serve_file(_safe_path("index.html", STATIC_DIR))
            if path.startswith("/static/"):
                # 静态资源（登录页 CSS/JS）不含敏感数据，必须免鉴权，
                # 否则设置 PANEL_TOKEN 后登录框自身都加载不出来。
                return self._serve_file(_safe_path(path[len("/static/"):], STATIC_DIR))
            if path.startswith("/view/") or path.startswith("/download/"):
                if not self._authed():
                    return self._err("需要访问令牌", 401)
                rel = path.split("/", 2)[2] if path.count("/") >= 2 else ""
                return self._serve_file(_safe_path(rel, OUTPUT_DIR),
                                        download=path.startswith("/download/"))
            if path == "/api/health":
                return self._json(api_health())
            if not self._authed():
                return self._err("需要访问令牌", 401)
            if path == "/api/state":
                return self._json(api_state())
            if path == "/api/machines":
                return self._json({"machines": [p.to_dict() for p in list_profiles()],
                                   "current": get_settings().machine_id,
                                   "controllers": ["FANUC", "SIEMENS", "HNC"]})
            m = re.fullmatch(r"/api/machines/([A-Za-z0-9_\-]+)", path)
            if m:
                if m.group(1) not in PROFILES:
                    return self._err("未知机型", 404)
                return self._json(capability_summary(get_profile(m.group(1))))
            if path == "/api/files":
                return self._json(api_files())
            if path == "/api/tasks":
                # 只在锁内取快照，锁外做序列化，避免在持锁区间调用任何加锁函数
                with _TASKS_LOCK:
                    snapshot = sorted(_TASKS.values(),
                                      key=lambda x: -x["created"])[:MAX_TASKS]
                items = [_task_public(t, include_result=False) for t in snapshot]
                return self._json({"tasks": items, "busy": _RUN_LOCK.locked()})
            m = re.fullmatch(r"/api/tasks/([A-Za-z0-9\-]+)", path)
            if m:
                with _TASKS_LOCK:
                    t = _TASKS.get(m.group(1))
                if not t:
                    return self._err("任务不存在", 404)
                return self._json(_task_public(t))
            if path == "/api/events":
                n = min(200, int(qs.get("limit", ["60"])[0]))
                return self._json({"events": _tail_jsonl(
                    OUTPUT_DIR / "vision" / "events.jsonl", n)})
            if path == "/api/audit":
                return self._json({"records": _tail_jsonl(
                    OUTPUT_DIR / "vision" / "control_audit.jsonl", 100)})
            if path == "/api/machine/status":
                return self._json(LINK.status())
            if path == "/api/machine/telemetry":
                return self._json(LINK.telemetry())
            if path == "/api/machine/audit":
                n = min(200, int(qs.get("limit", ["80"])[0]))
                return self._json({"records": read_audit(n)})
            if path == "/api/drawing/example":
                try:
                    return self._json({"ok": True, **_do_drawing_analyze(
                        EXAMPLE_DRAWING.name, EXAMPLE_DRAWING.read_bytes(),
                        save=False)})
                except (DrawingError, OSError) as exc:
                    return self._err(f"示例图纸不可用：{exc}", 400)
            return self._err("未知接口", 404)
        except Exception as exc:
            return self._err(f"{type(exc).__name__}: {exc}", 500)

    # ---- POST -----------------------------------------------------------
    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/api/login":
                body = self._body()
                # 弱令牌可无限爆破：按来源 IP 做 60s / 8 次失败限速
                peer = self.client_address[0] if self.client_address else "?"
                now = time.time()
                with _LOGIN_LOCK:
                    hits = [t for t in _LOGIN_FAILS.get(peer, []) if now - t < 60]
                    _LOGIN_FAILS[peer] = hits
                    too_many = len(hits) >= 8
                if too_many:
                    return self._err("登录尝试过于频繁，请 1 分钟后再试", 429)
                if not TOKEN or body.get("token") == TOKEN:
                    with _LOGIN_LOCK:
                        _LOGIN_FAILS.pop(peer, None)
                    extra = {"Set-Cookie": f"panel_token={TOKEN}; Path=/; HttpOnly; SameSite=Strict"} \
                            if TOKEN else {}
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    payload = b'{"ok":true}'
                    self.send_header("Content-Length", str(len(payload)))
                    for k, v in extra.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                with _LOGIN_LOCK:
                    _LOGIN_FAILS.setdefault(peer, []).append(now)
                return self._err("令牌错误", 401)
            if not self._authed():
                return self._err("需要访问令牌", 401)

            if path == "/api/drawing/analyze":
                body = self._body(12_000_000)
                try:
                    name, data = _decode_upload(body)
                    return self._json({"ok": True, **_do_drawing_analyze(
                        name, data, str(body.get("hint") or ""))})
                except DrawingError as exc:
                    return self._err(f"图纸识别失败：{exc}", 400)
            if path == "/api/analyze":
                body = self._body()
                text = body.get("text") or SAMPLE_ALARM_LOG
                ds = diagnose_alarms(str(text))
                return self._json({"diagnoses": _jsonable(ds),
                                   "summary": alarm_summary(ds)})

            if path == "/api/machine/connect":
                cfg = self._body()
                if READONLY:
                    cfg["allow_write"] = False      # 公网只读模式永不开放写入
                return self._json(LINK.connect(cfg))
            if path == "/api/machine/disconnect":
                return self._json(LINK.disconnect())
            if path == "/api/machine/command":
                if READONLY:
                    return self._json({"ok": False, "readonly": True,
                                       "error": "只读模式：机床写入指令已禁用"}, 403)
                b = self._body()
                return self._json(LINK.command(
                    str(b.get("signal") or ""), b.get("value"),
                    bool(b.get("confirm")), str(b.get("actor") or "panel")))
            if path == "/api/machine/program":
                if READONLY:
                    return self._json({"ok": False, "readonly": True,
                                       "error": "只读模式：程序下发已禁用"}, 403)
                b = self._body()
                filename = str(b.get("filename") or "")
                if b.get("path"):
                    f = _safe_path(str(b["path"]), OUTPUT_DIR)
                    if f is None or f.suffix.lower() not in {".nc", ".txt"}:
                        return self._err("程序路径不合法或不是 G 代码文件", 400)
                    # 按原始字节下发：保留 CRLF，避免换行规范化改变机床程序
                    payload = f.read_bytes()
                    filename = f.name
                else:
                    payload = str(b.get("text") or "")
                return self._json(LINK.send_program(
                    payload, bool(b.get("confirm")),
                    str(b.get("actor") or "panel"), filename))
            if path == "/api/tasks":
                if READONLY:
                    return self._json({
                        "ok": False,
                        "error": "只读演示模式：已禁用全部执行类操作（编程/监控/闭环/下发）。"
                                 "公网部署只开放产物查看与报告，控制链路必须留在受控内网。",
                        "readonly": True}, 403)
                body = self._body()
                kind = body.get("type")
                if kind not in _DISPATCH:
                    return self._err("任务类型须为 run / monitor / closed_loop", 400)
                if _RUN_LOCK.locked():
                    return self._json({
                        "ok": False,
                        "error": "已有链路任务运行中（产物共写 output/，必须串行）。请等待完成后重试。",
                        "busy": True}, 409)
                p = body.get("params") or {}
                # 面板层硬过滤：任何情况下都不接受 live 控制 / 人工确认伪造 / 任意下发路径
                for banned in ("live", "human_confirmed", "allow_auto_stop",
                               "output_dir", "device"):
                    p.pop(banned, None)
                # 客户端给的文件 / 目录路径一律限制在受控目录内，否则可借
                # video / images / weights 参数读任意本地文件并随报告外泄。
                for key, root, is_dir in (("video", OUTPUT_DIR, False),
                                          ("images", OUTPUT_DIR, True),
                                          ("weights", ROOT, False)):
                    raw = str(p.get(key) or "").strip()
                    if not raw:
                        p.pop(key, None)
                        continue
                    f = _safe_existing(raw, root, want_dir=is_dir)
                    if f is None:
                        return self._err(f"{key} 路径不合法或不在允许目录内", 400)
                    p[key] = str(f)
                p["protocol"] = "dryrun"
                tid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
                task = {"id": tid, "type": kind, "params": p, "status": "queued",
                        "created": time.time(), "started": None, "finished": None,
                        "logs": [], "result": None, "error": "", "snapshot": ""}
                with _TASKS_LOCK:
                    _TASKS[tid] = task
                    keys = sorted(_TASKS, key=lambda k: -_TASKS[k]["created"])
                    for k in keys[MAX_TASKS:]:
                        _TASKS.pop(k, None)
                _log(task, f"[面板] 任务 {tid} 已排队：{kind}")
                threading.Thread(target=_execute, args=(task,), daemon=True).start()
                return self._json({"ok": True, "task_id": tid, "status": "queued"}, 202)
            return self._err("未知接口", 404)
        except Exception as exc:
            return self._err(f"{type(exc).__name__}: {exc}", 500)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CNC 操控面板 v{VERSION} | http://{HOST}:{PORT} | 模式 {MODE}"
          f"{' (只读，执行类 API 已禁用)' if READONLY else ' (dry-run)'}"
          f" | 鉴权 {'开启' if TOKEN else '关闭'}")
    # 连接状态只存在内存里，服务重启后会变「未连接」，用户打开面板就以为不能操控。
    # 仅 demo 模式 + 本机回环目标 + 服务端开启写入时自动连本地虚拟机床网关；
    # 车间内网部署把 PANEL_MODE 设为 intranet / readonly 即不会自动连任何设备。
    if (MODE == "demo" and not READONLY
            and os.getenv("PANEL_AUTO_CONNECT", "1") != "0"
            and LINK.server_allows_write):
        raw = os.getenv("PANEL_LINK_TARGET", "127.0.0.1:15020")
        parts = raw.rsplit(":", 1)
        host = parts[0]
        port = int(parts[1]) if len(parts) == 2 else 15020
        if host in {"127.0.0.1", "localhost", "::1"}:
            try:
                r = LINK.connect({"host": host, "port": port, "unit": 1,
                                  "protocol": "modbus_tcp",
                                  "allow_write": True})
                print(f"[自动连接] {raw} -> "
                      f"{'成功，控制写入已就绪' if r.get('ok') else r.get('error')}")
            except Exception as exc:
                print(f"[自动连接] 失败（可在页面手动连接）：{exc}")
        else:
            print(f"[自动连接] 跳过：{host} 非本机地址，真机请手动连接")
    print("Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
