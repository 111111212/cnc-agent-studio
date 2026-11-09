"""工业协议下发适配：Modbus TCP / OPC UA / MQTT / FOCAS / 厂商网关。

默认且强烈建议 dry-run：只生成指令包并落审计日志，不建立到机床的连接。
真实写入需要显式开关、现场电气图核对与安全 PLC 互锁。

寄存器/节点地址均为示例映射，正式接入必须以机床电气图与 PLC 变量表为准。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

# 示例寄存器映射（线圈 / 保持寄存器 / OPC UA 节点 / MQTT topic）
REGISTER_MAP: dict[str, dict[str, Any]] = {
    "FEED_HOLD":          {"coil": 0x0100, "node": "ns=2;s=CNC.FeedHold", "topic": "cnc/control/feed_hold"},
    "CYCLE_STOP":         {"coil": 0x0101, "node": "ns=2;s=CNC.CycleStop", "topic": "cnc/control/cycle_stop"},
    "SPINDLE_STOP":       {"coil": 0x0102, "node": "ns=2;s=CNC.SpindleStop", "topic": "cnc/control/spindle_stop"},
    "COOLANT_ON":         {"coil": 0x0110, "node": "ns=2;s=CNC.CoolantOn", "topic": "cnc/control/coolant"},
    "FEED_OVERRIDE":      {"holding": 0x0200, "node": "ns=2;s=CNC.FeedOverride", "topic": "cnc/control/feed_override"},
    "ALARM_LIGHT":        {"coil": 0x0120, "node": "ns=2;s=CNC.AlarmLight", "topic": "cnc/control/alarm_light"},
    "REQUEST_INSPECTION": {"local": True, "topic": "cnc/alert/inspection"},
    # 急停不提供任何软写地址：必须走硬件安全回路
    "EMERGENCY_STOP":     {"hardware_only": True},
}


@dataclass
class ControlResult:
    ts: str
    adapter: str
    signal: str
    value: Any
    ok: bool
    dry_run: bool
    detail: str
    address: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProtocolAdapter(ABC):
    name = "base"
    dry_run = True

    @abstractmethod
    def write(self, signal: str, value: Any = None) -> ControlResult:
        ...

    def status(self) -> dict[str, Any]:
        return {"adapter": self.name, "dry_run": self.dry_run}

    def close(self) -> None:
        pass


class DryRunAdapter(ProtocolAdapter):
    """默认适配器：不连机床，只生成可审计的指令包。"""

    name = "dryrun"

    def __init__(self, log_path: Path | None = None, reason: str = ""):
        self.log_path = log_path
        self.reason = reason
        self.records: list[ControlResult] = []
        self.dry_run = True

    def write(self, signal: str, value: Any = None) -> ControlResult:
        info = REGISTER_MAP.get(signal, {})
        if info.get("hardware_only"):
            return ControlResult(
                datetime.now().isoformat(timespec="seconds"), self.name,
                signal, value, False, True,
                "急停为硬件安全回路专用，适配器拒绝任何软写；请按现场急停按钮",
                "HARDWARE_SAFETY_LOOP")
        addr = (f"coil 0x{info['coil']:04X}" if "coil" in info else
                f"holding 0x{info['holding']:04X}" if "holding" in info else
                info.get("node", "LOCAL_LOG"))
        res = ControlResult(
            datetime.now().isoformat(timespec="seconds"), self.name,
            signal, value, True, True,
            f"DRY-RUN 未连接机床；指令已记入审计日志",
            addr)
        self.records.append(res)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(res.to_dict(), ensure_ascii=False) + "\n")
        return res

    def status(self) -> dict[str, Any]:
        return super().status() | {"reason": self.reason,
                                   "records": len(self.records)}


class ModbusTCPAdapter(ProtocolAdapter):
    """Modbus TCP：pymodbus 可选依赖；库缺失自动回退 dry-run。"""

    name = "modbus_tcp"

    def __init__(self, host: str = "127.0.0.1", port: int = 502, unit: int = 1,
                 dry_run: bool = True, log_path: Path | None = None):
        self.host, self.port, self.unit = host, port, unit
        self.dry_run = dry_run
        self.log_path = log_path
        self.client = None
        self.records: list[ControlResult] = []
        if not dry_run:
            try:
                from pymodbus.client import ModbusTcpClient
                self.client = ModbusTcpClient(host, port=port)
            except ImportError as exc:
                raise RuntimeError("Modbus TCP 需要 pip install pymodbus") from exc

    def write(self, signal: str, value: Any = None) -> ControlResult:
        info = REGISTER_MAP.get(signal, {})
        if info.get("hardware_only"):
            return ControlResult(datetime.now().isoformat(timespec="seconds"),
                                 self.name, signal, value, False, self.dry_run,
                                 "急停必须走硬件安全回路，拒绝软写",
                                 "HARDWARE_SAFETY_LOOP")
        addr = ("coil 0x%04X" % info["coil"]) if "coil" in info else \
               ("holding 0x%04X" % info["holding"]) if "holding" in info else "LOCAL_LOG"
        if self.dry_run or self.client is None:
            detail, ok = "DRY-RUN：未建立 Modbus 连接，仅记录指令", True
        elif "coil" not in info and "holding" not in info:
            # 本地工单没有寄存器映射，不能把它报成「已写入设备」
            detail, ok = "本地工单：无寄存器映射，仅记录未写机床", True
        else:
            try:
                if "coil" in info:
                    r = self.client.write_coil(
                        info["coil"], bool(value or True), unit=self.unit)
                else:
                    r = self.client.write_register(
                        info["holding"], int(value or 0), unit=self.unit)
                # pymodbus 的错误响应不会抛异常，必须显式检查 isError()
                if getattr(r, "isError", lambda: False)():
                    raise RuntimeError(f"设备返回错误响应：{r}")
                detail, ok = f"已写入 {self.host}:{self.port} {addr}（响应已校验）", True
            except Exception as exc:
                detail, ok = f"写入失败：{type(exc).__name__}: {exc}", False
        res = ControlResult(datetime.now().isoformat(timespec="seconds"),
                            self.name, signal, value, ok, self.dry_run, detail, addr)
        self.records.append(res)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(res.to_dict(), ensure_ascii=False) + "\n")
        return res

    def close(self) -> None:
        if self.client:
            self.client.close()


class OPCUAAdapter(ProtocolAdapter):
    """OPC UA：适配 SINUMERIK 828D/840D、华中 848D 的产线集成接口。"""

    name = "opc_ua"

    def __init__(self, endpoint: str = "opc.tcp://127.0.0.1:4840",
                 dry_run: bool = True, log_path: Path | None = None):
        self.endpoint, self.dry_run, self.log_path = endpoint, dry_run, log_path
        self.client = None
        self.records: list[ControlResult] = []
        if not dry_run:
            try:
                from opcua import Client
                self.client = Client(endpoint)
                self.client.connect()
            except ImportError as exc:
                raise RuntimeError("OPC UA 需要 pip install opcua") from exc

    def write(self, signal: str, value: Any = None) -> ControlResult:
        info = REGISTER_MAP.get(signal, {})
        if info.get("hardware_only"):
            return ControlResult(datetime.now().isoformat(timespec="seconds"),
                                 self.name, signal, value, False, self.dry_run,
                                 "急停必须走硬件安全回路，拒绝软写",
                                 "HARDWARE_SAFETY_LOOP")
        node = info.get("node", "LOCAL_LOG")
        if self.dry_run or self.client is None:
            detail, ok = "DRY-RUN：未连接 OPC UA 服务器", True
        else:
            try:
                self.client.get_node(node).set_value(value if value is not None else True)
                detail, ok = f"已写节点 {node}", True
            except Exception as exc:
                detail, ok = f"写入失败：{type(exc).__name__}: {exc}", False
        res = ControlResult(datetime.now().isoformat(timespec="seconds"),
                            self.name, signal, value, ok, self.dry_run, detail, node)
        self.records.append(res)
        return res

    def close(self) -> None:
        if self.client:
            self.client.disconnect()


class MQTTAdapter(ProtocolAdapter):
    """MQTT：把控制建议发到产线消息总线，由 PLC/上位机订阅执行。"""

    name = "mqtt"

    def __init__(self, broker: str = "127.0.0.1", port: int = 1883,
                 topic_prefix: str = "cnc", dry_run: bool = True,
                 log_path: Path | None = None):
        self.broker, self.port, self.topic_prefix = broker, port, topic_prefix
        self.dry_run, self.log_path = dry_run, log_path
        self.client = None
        self.records: list[ControlResult] = []
        if not dry_run:
            try:
                import paho.mqtt.client as mqtt
                self.client = mqtt.Client()
                self.client.connect(broker, port, 60)
            except ImportError as exc:
                raise RuntimeError("MQTT 需要 pip install paho-mqtt") from exc

    def write(self, signal: str, value: Any = None) -> ControlResult:
        info = REGISTER_MAP.get(signal, {})
        if info.get("hardware_only"):
            return ControlResult(datetime.now().isoformat(timespec="seconds"),
                                 self.name, signal, value, False, self.dry_run,
                                 "急停必须走硬件安全回路，拒绝软写",
                                 "HARDWARE_SAFETY_LOOP")
        topic = info.get("topic", f"{self.topic_prefix}/control/{signal.lower()}")
        if self.dry_run or self.client is None:
            detail, ok = "DRY-RUN：未连接 MQTT Broker", True
        else:
            try:
                sent = self.client.publish(topic, json.dumps(
                    {"signal": signal, "value": value}, ensure_ascii=False))
                # paho-mqtt 的 publish 返回 MQTTMessageInfo，rc != 0 表示没发出去
                rc = getattr(sent, "rc", 0)
                if rc:
                    raise RuntimeError(f"MQTT publish 返回 rc={rc}")
                detail, ok = f"已发布 {topic}（rc 已校验）", True
            except Exception as exc:
                detail, ok = f"发布失败：{type(exc).__name__}: {exc}", False
        res = ControlResult(datetime.now().isoformat(timespec="seconds"),
                            self.name, signal, value, ok, self.dry_run, detail, topic)
        self.records.append(res)
        return res


def recommend_protocol(profile) -> str:
    """按机型能力推荐工业协议（对应"根据机型生成其应有功能"）。"""
    for proto in ("OPC UA", "FOCAS", "PROFINET", "Modbus TCP", "Ethernet DNC"):
        if proto in profile.protocols:
            return {"OPC UA": "opcua", "FOCAS": "modbus",
                    "PROFINET": "opcua", "Modbus TCP": "modbus",
                    "Ethernet DNC": "mqtt"}[proto]
    return "dryrun"


def create_adapter(protocol: str, profile=None, host: str = "127.0.0.1",
                   port: int = 0, dry_run: bool = True,
                   log_path: Path | None = None) -> tuple[ProtocolAdapter, str]:
    """返回 (适配器, 说明)。任何不匹配/缺依赖都安全回退到 dry-run。"""
    key = protocol.lower().strip()
    note = ""
    if key in ("", "dryrun", "none", "auto"):
        key = recommend_protocol(profile) if profile else "dryrun"
        note = f"按机型 {profile.display if profile else '默认'} 推荐协议 {key}"
    if profile and key in ("modbus", "opcua", "mqtt", "focas"):
        needed = {"modbus": "Modbus TCP", "opcua": "OPC UA",
                  "mqtt": "Ethernet DNC", "focas": "FOCAS"}[key]
        if needed not in profile.protocols:
            note += (f"；⚠ 机型 {profile.display} 未声明支持 {needed}，"
                     f"已强制 dry-run")
            dry_run = True
    if dry_run:
        return DryRunAdapter(log_path, note), note
    try:
        if key == "modbus":
            return ModbusTCPAdapter(host, port or 502, dry_run=False,
                                    log_path=log_path), note
        if key == "opcua":
            ep = f"opc.tcp://{host}:{port or 4840}"
            return OPCUAAdapter(ep, dry_run=False, log_path=log_path), note
        if key == "mqtt":
            return MQTTAdapter(host, port or 1883, dry_run=False,
                               log_path=log_path), note
        return DryRunAdapter(log_path, f"协议 {key} 暂未实现写入，已 dry-run"), note
    except Exception as exc:
        return DryRunAdapter(log_path,
                             f"{key} 初始化失败（{type(exc).__name__}: {exc}），已回退 dry-run"), ""
