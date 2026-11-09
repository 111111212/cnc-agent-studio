"""视觉故障 -> Agent 工具调用 -> 处置方案 -> 安全治理。

用户要求的链路在这里落地：把【截图 + 检测结果 + 工艺上下文】作为
工具输入（function/tool calling 契约）交给大模型，模型输出故障原因与处置方案。

安全边界：模型输出的是"建议"，不是机床指令。所有控制信号必须经过
SafetyGovernor 白名单、值域夹紧与人工确认门控，才能交给协议适配器。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from typing import Any

from ..config import Settings, masked_key
from .detector import FAULT_META, FaultClass, VisionEvent

VISION_TOOL_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "analyze_machining_fault",
        "description": "分析机加工过程中的视觉异常，给出故障原因、处置方案与建议控制信号",
        "parameters": {
            "type": "object",
            "properties": {
                "snapshot_base64": {"type": "string",
                                    "description": "触发告警的加工现场截图（PNG base64）"},
                "detections": {"type": "array", "items": {"type": "object"},
                               "description": "YOLO 检测框：类别/置信度/bbox/严重度"},
                "process_context": {"type": "object",
                                    "description": "机型、材料、刀具、S/F/ap/ae、程序指纹等工况"},
            },
            "required": ["snapshot_base64", "detections", "process_context"],
        },
    },
}


VISION_SYSTEM_PROMPT = """你是机加工现场故障诊断专家，服务于 CNC Agent 的视觉监控节点。
你会收到一张加工现场截图、YOLO 检测结果与当前工艺上下文，请分析故障原因并给出处置方案。

# 硬约束
1. 只输出一个 JSON 对象，不要 Markdown 围栏，不要解释性文字。
2. 你**不直接控制机床**：只能给出建议控制信号，实际下发由安全 PLC 与人工确认门控决定。
3. 禁止建议"提高进给/提高转速/继续加工"这类会放大风险的动作；只允许降参数、停机、报警、请求检查。
4. 不得编造检测截图里不存在的故障；不确定时把 confidence_note 写清楚并要求人工复核。
5. 处置建议必须区分"立即动作"与"工艺参数整改"，并考虑该工序的材料、刀具与切深。

# 输出格式
{
  "cause": "故障根因，80 字以内",
  "reasoning": "依据检测结果与工艺上下文的推理，120 字以内",
  "severity": "critical|high|medium|info",
  "immediate_actions": ["立即动作1", "立即动作2"],
  "control_signals": [{"signal": "FEED_HOLD", "value": null, "reason": "..."}],
  "process_adjustments": {"ap_factor": 0.8, "ae_factor": 0.9,
                          "feed_factor": 0.85, "spindle_factor": 1.0,
                          "change_tool": false, "recheck_wcs": false},
  "requires_human_confirmation": true,
  "confidence_note": "..."
}
signal 只能取：FEED_HOLD / CYCLE_STOP / SPINDLE_STOP / EMERGENCY_STOP /
COOLANT_ON / FEED_OVERRIDE / ALARM_LIGHT / REQUEST_INSPECTION。
所有 factor 必须在 0.3~1.0（只允许降低，不允许提高）。
"""


# --------------------------------------------------------------------------
# 规则知识库：无 API Key / 模型失败时的确定性兜底
# --------------------------------------------------------------------------
FAULT_RULES: dict[str, dict[str, Any]] = {
    "tool_breakage": {
        "cause": "刃口崩缺或刀具疲劳断裂，继续加工会造成过切、尺寸报废甚至撞刀",
        "reasoning": "检测框集中在刀刃区且出现暗缺口/碎片；当前 ap 与材料许用值接近上限，"
                     "冲击载荷大，切削力突增时易崩刃。",
        "severity": "critical",
        "immediate_actions": ["进给保持并停止主轴", "停机检查刀具与工件已加工面",
                              "更换同规格刀具并重新对刀", "复检首件尺寸"],
        "control_signals": [
            {"signal": "FEED_HOLD", "value": None, "reason": "立即停止进给"},
            {"signal": "SPINDLE_STOP", "value": None, "reason": "防止断刀继续切削"},
            {"signal": "ALARM_LIGHT", "value": 1, "reason": "现场声光提示"},
            {"signal": "REQUEST_INSPECTION", "value": None, "reason": "需人工确认"},
        ],
        "process_adjustments": {"ap_factor": 0.7, "ae_factor": 0.85,
                                "feed_factor": 0.85, "spindle_factor": 0.95,
                                "change_tool": True, "recheck_wcs": False},
    },
    "workpiece_shift": {
        "cause": "装夹松动或压板失效导致工件位移，G54 工件坐标系已失效",
        "reasoning": "工件左边缘相对基准线偏移，说明切削力或夹紧力不足；"
                     "此时所有刀路坐标都不再对应实际工件。",
        "severity": "critical",
        "immediate_actions": ["执行安全停机（须硬件急停回路确认）", "复查压板与夹紧力",
                              "重新找正并设定 G54", "报废或复检当前件"],
        "control_signals": [
            {"signal": "FEED_HOLD", "value": None, "reason": "立即停止进给"},
            {"signal": "CYCLE_STOP", "value": None, "reason": "终止当前循环"},
            {"signal": "EMERGENCY_STOP", "value": None, "reason": "位移可能引发碰撞"},
            {"signal": "REQUEST_INSPECTION", "value": None, "reason": "必须人工确认"},
        ],
        "process_adjustments": {"ap_factor": 0.8, "ae_factor": 0.8,
                                "feed_factor": 0.8, "spindle_factor": 1.0,
                                "change_tool": False, "recheck_wcs": True},
    },
    "abnormal_sparks": {
        "cause": "切削温度过高：刀具磨损、线速度偏高或冷却不足，存在崩刀前兆",
        "reasoning": "接触区出现密集橙黄火花像素，通常意味着干摩擦与积屑瘤；"
                     "结合当前 Vc 与材料许用切削速度核对，应降线速度并检查冷却。",
        "severity": "high",
        "immediate_actions": ["降低进给倍率观察", "确认冷却液喷嘴对准切削区",
                              "检查刀具磨损带"],
        "control_signals": [
            {"signal": "FEED_OVERRIDE", "value": 70, "reason": "降倍率抑制切削热"},
            {"signal": "COOLANT_ON", "value": 1, "reason": "恢复冷却"},
            {"signal": "ALARM_LIGHT", "value": 1, "reason": "提示操作员"},
        ],
        "process_adjustments": {"ap_factor": 0.9, "ae_factor": 0.9,
                                "feed_factor": 0.85, "spindle_factor": 0.9,
                                "change_tool": False, "recheck_wcs": False},
    },
    "chip_accumulation": {
        "cause": "排屑不畅或冲屑压力不足，切屑二次切削会划伤已加工面并加速刀具磨损",
        "reasoning": "接触区银白高亮切屑面积持续超标；型腔环切时切屑滞留概率高，"
                     "需加强冲屑并适当减小行距改善容屑空间。",
        "severity": "medium",
        "immediate_actions": ["提高冷却冲屑压力", "短暂停刀清理切屑",
                              "检查排屑器"],
        "control_signals": [
            {"signal": "COOLANT_ON", "value": 1, "reason": "冲屑"},
            {"signal": "FEED_OVERRIDE", "value": 85, "reason": "降低切屑生成速率"},
        ],
        "process_adjustments": {"ap_factor": 1.0, "ae_factor": 0.85,
                                "feed_factor": 0.9, "spindle_factor": 1.0,
                                "change_tool": False, "recheck_wcs": False},
    },
    "coolant_missing": {
        "cause": "冷却液未供给或喷嘴偏离，钢件干切会急剧磨损刀具并产生热变形",
        "reasoning": "接触区蓝白冷却雾像素显著低于正常基线且无异常火花，"
                     "判定冷却失效；应降参数并在恢复冷却前限制加工时长。",
        "severity": "high",
        "immediate_actions": ["检查液位、泵与喷嘴", "降倍率短时观察",
                              "冷却未恢复则停机"],
        "control_signals": [
            {"signal": "COOLANT_ON", "value": 1, "reason": "尝试恢复冷却"},
            {"signal": "FEED_OVERRIDE", "value": 70, "reason": "降低切削热"},
            {"signal": "ALARM_LIGHT", "value": 1, "reason": "提示操作员"},
        ],
        "process_adjustments": {"ap_factor": 0.9, "ae_factor": 0.9,
                                "feed_factor": 0.8, "spindle_factor": 0.85,
                                "change_tool": False, "recheck_wcs": False},
    },
    "collision_debris": {
        "cause": "疑似刀具碰撞或工件崩裂产生大量碎片飞溅",
        "reasoning": "全画面离散高亮斑点骤增且分布无规律，与正常切屑形态不符，"
                     "需按疑似碰撞处理。",
        "severity": "high",
        "immediate_actions": ["进给保持", "停机检查刀具、夹具与工件",
                              "调出仿真报告核对刀路"],
        "control_signals": [
            {"signal": "FEED_HOLD", "value": None, "reason": "立即停止进给"},
            {"signal": "CYCLE_STOP", "value": None, "reason": "终止循环"},
            {"signal": "REQUEST_INSPECTION", "value": None, "reason": "人工确认"},
        ],
        "process_adjustments": {"ap_factor": 0.8, "ae_factor": 0.8,
                                "feed_factor": 0.85, "spindle_factor": 1.0,
                                "change_tool": False, "recheck_wcs": True},
    },
}


@dataclass
class Diagnosis:
    event_id: str
    fault_code: str
    cause: str
    reasoning: str
    severity: str
    immediate_actions: list[str]
    control_signals: list[dict[str, Any]]
    process_adjustments: dict[str, Any]
    requires_human_confirmation: bool
    source: str = "rule"
    confidence_note: str = ""
    tool_input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, with_image: bool = False) -> dict[str, Any]:
        d = asdict(self)
        ti = d.pop("tool_input", {})
        if not with_image:
            ti.pop("snapshot_base64", None)
        d["tool_input"] = ti
        return d


@dataclass
class ControlDecision:
    approved_signals: list[dict[str, Any]] = field(default_factory=list)
    blocked_signals: list[dict[str, Any]] = field(default_factory=list)
    requires_human: bool = True
    human_confirmed: bool = False
    note: str = ""


class VisionDiagnosisAgent:
    """把截图与检测结果打包成工具输入，调用多模态 LLM；失败自动回退规则库。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_error = ""

    def _build_tool_input(self, event: VisionEvent) -> dict[str, Any]:
        return {
            "tool": VISION_TOOL_SPEC["function"]["name"],
            "snapshot_base64": event.snapshot_b64,
            "detections": [d.to_dict() for d in event.detections],
            "process_context": event.context,
        }

    def _call_llm(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        user_text = (
            "请调用 analyze_machining_fault 工具分析以下视觉异常，只返回 JSON。\n\n"
            f"检测结果：{json.dumps(tool_input['detections'], ensure_ascii=False)}\n"
            f"工艺上下文：{json.dumps(tool_input['process_context'], ensure_ascii=False)}\n"
            f"截图见随附图片。"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        if tool_input["snapshot_base64"]:
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + tool_input["snapshot_base64"]}})
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.1,
            "tools": [VISION_TOOL_SPEC],
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.settings.llm_api_key}"},
            method="POST")
        with urllib.request.urlopen(req, timeout=self.settings.llm_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        msg = body["choices"][0]["message"]
        # 兼容 function call 与纯文本 JSON 两种返回
        raw = ""
        if msg.get("tool_calls"):
            raw = msg["tool_calls"][0]["function"]["arguments"]
        else:
            raw = msg.get("content", "")
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            raise ValueError("模型未返回 JSON 对象")
        return json.loads(m.group(0))

    def diagnose(self, event: VisionEvent) -> Diagnosis:
        tool_input = self._build_tool_input(event)
        data: dict[str, Any] | None = None
        source = "rule"
        if self.settings.llm_available:
            try:
                data = self._call_llm(tool_input)
                source = "llm"
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                data = None
        if data is None:
            data = dict(FAULT_RULES.get(event.fault_code,
                                        FAULT_RULES["collision_debris"]))
            data["confidence_note"] = (
                "未配置多模态模型或调用失败，已使用规则知识库；"
                + (self.last_error or f"密钥 {masked_key(self.settings.llm_api_key)}"))
        diag = Diagnosis(
            event_id=event.id,
            fault_code=event.fault_code,
            cause=str(data.get("cause", "未识别故障")),
            reasoning=str(data.get("reasoning", "")),
            severity=str(data.get("severity", event.severity)),
            immediate_actions=list(data.get("immediate_actions", [])),
            control_signals=list(data.get("control_signals", [])),
            process_adjustments=dict(data.get("process_adjustments", {})),
            requires_human_confirmation=bool(
                data.get("requires_human_confirmation", True)),
            source=source,
            confidence_note=str(data.get("confidence_note", "")),
            tool_input={"tool": tool_input["tool"],
                        "detections": tool_input["detections"],
                        "process_context": tool_input["process_context"],
                        "snapshot_base64": tool_input["snapshot_base64"]},
        )
        return self._clamp_adjustments(diag)

    @staticmethod
    def _clamp_adjustments(d: Diagnosis) -> Diagnosis:
        """模型给的整改系数也必须夹紧：只允许降参数，禁止借诊断之名提速。"""
        for key in ("ap_factor", "ae_factor", "feed_factor", "spindle_factor"):
            v = d.process_adjustments.get(key)
            if v is None:
                continue
            try:
                d.process_adjustments[key] = round(min(1.0, max(0.3, float(v))), 3)
            except (TypeError, ValueError):
                d.process_adjustments[key] = 1.0
        return d


ALLOWED_SIGNALS = {
    "FEED_HOLD", "CYCLE_STOP", "SPINDLE_STOP", "EMERGENCY_STOP",
    "COOLANT_ON", "FEED_OVERRIDE", "ALARM_LIGHT", "REQUEST_INSPECTION",
}


class SafetyGovernor:
    """控制信号白名单 + 值域夹紧 + 人工确认门控（AI 不直连安全回路）。"""

    def __init__(self, allow_auto_stop: bool = False):
        self.allow_auto_stop = allow_auto_stop

    def review(self, diag: Diagnosis, human_confirmed: bool = False) -> ControlDecision:
        dec = ControlDecision(
            requires_human=diag.requires_human_confirmation,
            human_confirmed=human_confirmed)
        for item in diag.control_signals:
            sig = str(item.get("signal", "")).upper()
            rec = dict(item)
            if sig not in ALLOWED_SIGNALS:
                rec["blocked_reason"] = "不在控制信号白名单内"
                dec.blocked_signals.append(rec)
                continue
            if sig == "EMERGENCY_STOP" and not (human_confirmed or self.allow_auto_stop):
                rec["blocked_reason"] = ("急停必须由硬件安全 PLC/人工确认执行，"
                                         "AI 不得直连安全回路")
                dec.blocked_signals.append(rec)
                dec.requires_human = True
                continue
            if sig == "FEED_OVERRIDE":
                try:
                    v = float(item.get("value", 100))
                except (TypeError, ValueError):
                    v = 100
                v = min(100.0, max(0.0, v))   # 绝不允许多模态模型提高倍率
                rec["value"] = v
            rec["signal"] = sig
            dec.approved_signals.append(rec)
        dec.note = (
            f"批准 {len(dec.approved_signals)} 条 / 拦截 {len(dec.blocked_signals)} 条；"
            + ("需人工确认后执行" if dec.requires_human and not human_confirmed
               else "已满足执行条件"))
        return dec
