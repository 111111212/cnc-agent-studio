"""机床直连层：Modbus TCP 客户端 + 安全互锁 + 全量审计。

不依赖 pymodbus，用标准库 socket 实现 MBAP/FC01/03/05/06/16，
因此本机即可与 panel/plc_sim.py 虚拟机床网关完成真实 TCP 交互实测。

安全边界（工业现场硬要求）：
  * 只允许私网地址（loopback / 10 / 172.16~31 / 192.168 / link-local），
    拒绝公网 IP，避免把机床控制面暴露到互联网。
  * 服务端 PANEL_ALLOW_WRITE=1 且连接时显式 allow_write，才开放写入；
    默认只读遥测。
  * 每次写指令必须带 confirm 二次确认，白名单外指令一律拒绝，同信号限速。
  * 急停（EMERGENCY_STOP）没有任何软写地址，必须走硬件安全回路。
  * 所有连接、读写、拒绝事件都落 output/machine/audit.jsonl。

寄存器映射为示例，接入真机前必须按电气图 / PLC 变量表核对后再上线。
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
MACHINE_DIR = OUTPUT_DIR / "machine"
AUDIT_PATH = MACHINE_DIR / "audit.jsonl"

# 与 panel/plc_sim.py、cncagent/vision/protocols.py 保持一致的示例映射
COMMANDS: dict[str, dict[str, Any]] = {
    "FEED_HOLD":       {"kind": "coil", "addr": 0x0100, "label": "进给保持", "risk": "safe"},
    "CYCLE_START":     {"kind": "coil", "addr": 0x0103, "label": "循环启动", "risk": "caution"},
    "CYCLE_STOP":      {"kind": "coil", "addr": 0x0101, "label": "循环停止", "risk": "caution"},
    "SPINDLE_STOP":    {"kind": "coil", "addr": 0x0102, "label": "主轴停止", "risk": "caution"},
    "RESET":           {"kind": "coil", "addr": 0x0104, "label": "报警复位", "risk": "caution"},
    "COOLANT_ON":      {"kind": "coil", "addr": 0x0110, "label": "冷却液开关", "risk": "safe"},
    "ALARM_LIGHT":     {"kind": "coil", "addr": 0x0120, "label": "报警指示灯", "risk": "safe"},
    "FEED_OVERRIDE":   {"kind": "holding", "addr": 0x0200, "label": "进给倍率",
                        "min": 0, "max": 150, "risk": "caution"},
    "REQUEST_INSPECTION": {"kind": "local", "label": "请求人工检验", "risk": "safe"},
}
HARDWARE_ONLY = {"EMERGENCY_STOP", "SPINDLE_START", "AXIS_JOG", "TOOL_CHANGE"}

REG_TELEMETRY = 0x1000
REG_TELEMETRY_COUNT = 16
REG_OVERRIDE = 0x0200
REG_PROGRAM_BUFFER = 0x0300
REG_PROGRAM_COMMIT = 0x0310
REG_PROGRAM_BYTES = 0x0311
REG_PROGRAM_BLOCKS = 0x0312
STATE_NAME = {0: "待机", 1: "运行", 2: "进给保持", 3: "报警"}


def _s16(v: int) -> int:
    return v - 65536 if v > 32767 else v


def _private_ok(host: str) -> bool:
    """拒绝公网地址：控制面只能出现在车间内网 / VPN / 本机。"""
    h = (host or "").strip().lower()
    if h in {"localhost", "localhost.localdomain", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        # 域名形式：上线时应使用内网 DNS，这里保守拒绝，避免误连公网
        return False


def _audit(kind: str, payload: dict[str, Any]) -> None:
    MACHINE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, **payload}
    with AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_audit(limit: int = 100) -> list[dict]:
    if not AUDIT_PATH.is_file():
        return []
    out = []
    for line in AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


class ModbusError(RuntimeError):
    pass


class ModbusTCP:
    """精简 Modbus TCP 主站；单连接 + 锁，失败自动重连一次。"""

    def __init__(self, host: str, port: int, unit: int = 1, timeout: float = 2.0):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self.sock: socket.socket | None = None
        self.tid = 0
        self.lock = threading.RLock()

    def connect(self) -> None:
        with self.lock:
            self.close()
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock.settimeout(self.timeout)

    def close(self) -> None:
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except OSError:
                    pass
            self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("连接被对端关闭")
            buf += chunk
        return buf

    def _transact(self, pdu: bytes, retry: bool = True) -> bytes:
        with self.lock:
            if self.sock is None:
                self.connect()
            try:
                self.tid = (self.tid + 1) & 0xFFFF
                req = struct.pack(">HHHB", self.tid, 0, len(pdu) + 1, self.unit) + pdu
                self.sock.sendall(req)                       # type: ignore[union-attr]
                header = self._recv_exact(7)
                tid, pid, length, unit = struct.unpack(">HHHB", header)
                if pid != 0 or not 2 <= length <= 260:
                    raise ModbusError("MBAP 头非法")
                # 串包 / 错从站必须被识别，否则会把别人的响应当成本次写成功
                if tid != self.tid or unit != self.unit:
                    raise ModbusError(
                        f"MBAP 事务或从站不匹配（tid={tid}, unit={unit}）")
                payload = self._recv_exact(length - 1)
                if not payload:
                    raise ModbusError("Modbus 响应为空")
                if payload[0] & 0x80:
                    # 异常响应可能只有 1 字节，直接取 payload[1] 会 IndexError
                    code = payload[1] if len(payload) > 1 else 0xFF
                    raise ModbusError(f"Modbus 异常码 0x{code:02X}")
                return payload
            except (OSError, ModbusError, struct.error, IndexError,
                    ValueError) as exc:
                self.close()
                if retry:
                    return self._transact(pdu, retry=False)
                raise ModbusError(f"{type(exc).__name__}: {exc}") from exc

    def read_holding(self, addr: int, count: int) -> list[int]:
        pdu = bytes([0x03]) + struct.pack(">HH", addr, count)
        r = self._transact(pdu)
        if (len(r) < 2 + count * 2 or r[0] != 0x03
                or r[1] != count * 2):
            raise ModbusError("FC03 响应长度不符")
        return list(struct.unpack(">" + "H" * count, r[2:2 + count * 2]))

    def read_coils(self, addr: int, count: int) -> list[bool]:
        pdu = bytes([0x01]) + struct.pack(">HH", addr, count)
        r = self._transact(pdu)
        need = (count + 7) // 8
        if len(r) < 2 + need or r[0] != 0x01 or r[1] < need:
            raise ModbusError("FC01 响应长度不符")
        bits = r[2:2 + need]
        return [bool(bits[i // 8] & (1 << (i % 8))) for i in range(count)]

    def _check_echo(self, r: bytes, fc: int, addr: int,
                    expect_tail: tuple[int, ...]) -> None:
        """写类功能码必须回显功能码/地址/值，否则不能算写成功。"""
        if len(r) < 5 or r[0] != fc:
            raise ModbusError(f"FC{fc:02X} 响应不完整或功能码不符")
        got = struct.unpack(">HH", r[1:5])
        if got != (addr, *expect_tail):
            raise ModbusError(f"FC{fc:02X} 回显不一致：{got}")

    def write_coil(self, addr: int, value: bool) -> None:
        val = 0xFF00 if value else 0x0000
        pdu = bytes([0x05]) + struct.pack(">HH", addr, val)
        self._check_echo(self._transact(pdu), 0x05, addr, (val,))

    def write_register(self, addr: int, value: int) -> None:
        val = value & 0xFFFF
        pdu = bytes([0x06]) + struct.pack(">HH", addr, val)
        self._check_echo(self._transact(pdu), 0x06, addr, (val,))

    def write_registers(self, addr: int, values: list[int]) -> None:
        qty = len(values)
        pdu = (bytes([0x10]) + struct.pack(">HHB", addr, qty, qty * 2) +
               struct.pack(">" + "H" * qty, *[v & 0xFFFF for v in values]))
        self._check_echo(self._transact(pdu), 0x10, addr, (qty,))


@dataclass
class LinkConfig:
    host: str = "127.0.0.1"
    port: int = 15020
    unit: int = 1
    protocol: str = "modbus_tcp"
    machine_id: str = "vmc850_fanuc"
    allow_write: bool = False
    timeout: float = 2.0


class MachineLink:
    """面板全局单例：连接状态、遥测读取、指令门控、程序下发。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.client: ModbusTCP | None = None
        self.config = LinkConfig()
        self.connected = False
        self.last_error = ""
        self.last_telemetry: dict[str, Any] = {}
        self.last_read: float = 0.0
        self._last_cmd: dict[str, float] = {}
        self.server_allows_write = os.getenv("PANEL_ALLOW_WRITE", "0") == "1"

    @property
    def target(self) -> str:
        return f"{self.config.host}:{self.config.port}"

    @property
    def can_write(self) -> bool:
        return (self.connected and self.server_allows_write
                and self.config.allow_write)

    def connect(self, cfg: dict) -> dict:
        with self.lock:
            self.disconnect(silent=True)
            host = str(cfg.get("host") or "127.0.0.1").strip()
            port = int(cfg.get("port") or 15020)
            unit = int(cfg.get("unit") or 1)
            protocol = str(cfg.get("protocol") or "modbus_tcp")
            if protocol != "modbus_tcp":
                msg = (f"{protocol} 真实客户端需安装对应库并在服务器侧启用；"
                       "当前内置标准库实现为 Modbus TCP")
                self.last_error = msg
                _audit("connect_rejected", {"target": f"{host}:{port}", "reason": msg})
                return {"ok": False, "error": msg}
            if not _private_ok(host):
                msg = f"拒绝连接非私网地址 {host}：机床控制面不得暴露在公网"
                self.last_error = msg
                _audit("connect_rejected", {"target": f"{host}:{port}", "reason": msg})
                return {"ok": False, "error": msg}
            self.config = LinkConfig(
                host=host, port=port, unit=unit, protocol=protocol,
                machine_id=str(cfg.get("machine_id") or "vmc850_fanuc"),
                allow_write=bool(cfg.get("allow_write")),
                timeout=float(cfg.get("timeout") or 2.0))
            try:
                c = ModbusTCP(host, port, unit, self.config.timeout)
                c.connect()
                c.read_holding(REG_TELEMETRY, REG_TELEMETRY_COUNT)  # 握手读
                self.client = c
                self.connected = True
                self.last_error = ""
                _audit("connect", {"target": self.target, "unit": unit,
                                   "protocol": protocol,
                                   "machine_id": self.config.machine_id,
                                   "allow_write": self.config.allow_write,
                                   "server_allows_write": self.server_allows_write})
                return {"ok": True, "target": self.target,
                        "can_write": self.can_write,
                        "server_allows_write": self.server_allows_write,
                        "telemetry": self.telemetry()}
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                _audit("connect_failed", {"target": self.target,
                                          "reason": self.last_error})
                return {"ok": False, "error": self.last_error}

    def disconnect(self, silent: bool = False) -> dict:
        with self.lock:
            if self.client:
                self.client.close()
            self.client = None
            was = self.connected
            self.connected = False
            if was and not silent:
                _audit("disconnect", {"target": self.target})
            self.last_telemetry = {}
            return {"ok": True, "connected": False}

    def telemetry(self) -> dict:
        with self.lock:
            if not self.connected or self.client is None:
                return {"connected": False, "target": self.target,
                        "can_write": False, "server_allows_write": self.server_allows_write,
                        "last_error": self.last_error}
            try:
                r = self.client.read_holding(REG_TELEMETRY, REG_TELEMETRY_COUNT)
                ov = self.client.read_holding(REG_OVERRIDE, 1)[0]
                coils = self.client.read_coils(0x0100, 5)
                state = r[8]
                d = {
                    "connected": True, "target": self.target,
                    "protocol": self.config.protocol,
                    "machine_id": self.config.machine_id,
                    "can_write": self.can_write,
                    "server_allows_write": self.server_allows_write,
                    "allow_write": self.config.allow_write,
                    "spindle_cmd": r[0], "spindle_act": r[1],
                    "spindle_load": r[2], "feed_act": r[3],
                    "feed_override": ov,
                    "x": _s16(r[5]) / 10, "y": _s16(r[6]) / 10, "z": _s16(r[7]) / 10,
                    "state": state, "state_label": STATE_NAME.get(state, "未知"),
                    "alarm_code": r[9], "program_no": r[10],
                    "block_no": r[11], "block_total": r[12],
                    "coolant": bool(r[13]), "spindle_state": bool(r[14]),
                    "heartbeat": r[15],
                    "coils": {"feed_hold": coils[0], "cycle_stop": coils[1],
                              "spindle_stop": coils[2], "cycle_start": coils[3],
                              "reset": coils[4]},
                    "ts": time.time(),
                }
                self.last_telemetry = d
                self.last_read = time.time()
                self.last_error = ""
                return d
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                # 读失败必须回写连接状态：否则 status() 仍报「已连接 / 可写」，
                # 而 control 门控基于陈旧状态，等于在断线后继续允许写指令。
                if self.connected:
                    self.connected = False
                    _audit("link_lost", {"target": self.target,
                                         "reason": self.last_error})
                return {"connected": False, "target": self.target,
                        "can_write": False,
                        "last_error": self.last_error,
                        "last_telemetry": self.last_telemetry}

    def command(self, signal: str, value: Any, confirm: bool, actor: str = "panel") -> dict:
        with self.lock:
            sig = str(signal or "").upper().strip()
            base = {"signal": sig, "value": value, "target": self.target,
                    "actor": actor, "dry_run": False}

            def deny(reason: str) -> dict:
                _audit("command_blocked", base | {"reason": reason})
                return {"ok": False, "error": reason, **base}

            if sig in HARDWARE_ONLY:
                return deny("该动作无软件写入通道：急停/主轴启动/轴点动/换刀必须走硬件安全回路或机床操作面板")
            spec = COMMANDS.get(sig)
            if not spec:
                return deny(f"指令 {sig!r} 不在白名单内")
            if not self.connected:
                return deny("未连接机床")
            if not self.can_write:
                if not self.server_allows_write:
                    return deny("服务端未开启写入：需设置 PANEL_ALLOW_WRITE=1（生产环境默认关闭）")
                return deny("当前连接为只读遥测模式：连接时请显式勾选「启用控制写入」")
            if not confirm:
                return deny("缺少操作员二次确认")
            now = time.time()
            if now - self._last_cmd.get(sig, 0) < 0.8:
                return deny("指令过于频繁（同信号 0.8s 限速）")
            try:
                if spec["kind"] == "holding":
                    v = int(value)
                    if not spec["min"] <= v <= spec["max"]:
                        return deny(f"数值 {v} 超出允许范围 {spec['min']}~{spec['max']}")
                    self.client.write_register(spec["addr"], v)   # type: ignore[union-attr]
                    # 写后读回校验，防止写错寄存器/值
                    back = self.client.read_holding(spec["addr"], 1)[0]  # type: ignore[union-attr]
                    if back != v:
                        return deny(f"写后读回不一致：写入 {v}，读回 {back}")
                    detail = f"已写保持寄存器 0x{spec['addr']:04X}={v}，读回校验通过"
                elif spec["kind"] == "coil":
                    on = bool(value) if value is not None else True
                    self.client.write_coil(spec["addr"], on)      # type: ignore[union-attr]
                    # 线圈同样写后读回，和保持寄存器保持同一安全等级
                    back = self.client.read_coils(spec["addr"], 1)[0]  # type: ignore[union-attr]
                    if back != on:
                        return deny(f"线圈写后读回不一致：写入 {on}，读回 {back}")
                    detail = (f"已写线圈 0x{spec['addr']:04X}="
                              f"{'ON' if on else 'OFF'}，读回校验通过")
                else:
                    detail = "本地工单：已记录，请现场人工检验（不写机床）"
                self._last_cmd[sig] = now
                _audit("command", base | {"ok": True, "detail": detail,
                                          "address": hex(spec.get("addr", 0))})
                return {"ok": True, "detail": detail, "label": spec["label"], **base}
            except Exception as exc:
                return deny(f"写入失败：{type(exc).__name__}: {exc}")

    def send_program(self, text, confirm: bool, actor: str = "panel",
                     filename: str = "") -> dict:
        """DNC 程序下发：FC16 分块写入网关缓冲区，再写 COMMIT 入库。

        text 可为 bytes（从文件读原始字节，保留 CRLF）或 str（面板手工输入）。
        """
        with self.lock:
            def deny(reason: str) -> dict:
                _audit("program_blocked", {"target": self.target, "actor": actor,
                                           "filename": filename, "reason": reason})
                return {"ok": False, "error": reason}
            if not self.connected:
                return deny("未连接机床")
            if not self.can_write:
                return deny("连接为只读或服务端未开启写入")
            if not confirm:
                return deny("程序下发缺少二次确认")
            raw = text if isinstance(text, bytes) else \
                str(text or "").encode("ascii", errors="replace")
            if not raw:
                return deny("程序内容为空")
            if len(raw) > 60_000:
                return deny("程序超过 60KB 上限")
            if b"M30" not in raw.upper() and b"M02" not in raw.upper():
                return deny("程序缺少 M30/M02 结束指令，拒绝下发")
            if len(raw) % 2:
                raw += b"\n"
            try:
                values = [raw[i] << 8 | raw[i + 1] for i in range(0, len(raw), 2)]
                chunk = 100                       # 每帧 100 寄存器 = 200 字节
                for off in range(0, len(values), chunk):
                    part = values[off:off + chunk]
                    # 每帧写同一缓冲区基址，由网关按 TCP 顺序追加；
                    # 避免用寄存器地址做偏移（地址空间不够装 60KB 程序）
                    self.client.write_registers(      # type: ignore[union-attr]
                        REG_PROGRAM_BUFFER, part)
                self.client.write_register(REG_PROGRAM_COMMIT, 1)  # type: ignore[union-attr]
                got = self.client.read_holding(REG_PROGRAM_BYTES, 2)  # type: ignore[union-attr]
                rec = {"ok": True, "bytes": got[0], "blocks": got[1],
                       "frames": (len(values) + chunk - 1) // chunk,
                       "target": self.target, "filename": filename,
                       "detail": f"程序已写入网关缓冲区并入库：{got[0]} 字节 / {got[1]} 段"}
                _audit("program", {"target": self.target, "actor": actor,
                                   "filename": filename, **rec})
                return rec
            except Exception as exc:
                return deny(f"下发失败：{type(exc).__name__}: {exc}")

    def status(self) -> dict:
        with self.lock:
            return {
                "connected": self.connected, "target": self.target,
                "protocol": self.config.protocol, "unit": self.config.unit,
                "machine_id": self.config.machine_id,
                "allow_write": self.config.allow_write,
                "server_allows_write": self.server_allows_write,
                "can_write": self.can_write,
                "last_error": self.last_error,
                "commands": [{"signal": k, **{kk: vv for kk, vv in v.items()
                                              if kk != "addr"},
                              "address": hex(v["addr"]) if "addr" in v else "-"}
                             for k, v in COMMANDS.items()],
                "hardware_only": sorted(HARDWARE_ONLY),
            }


LINK = MachineLink()
