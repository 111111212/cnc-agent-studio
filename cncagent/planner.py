"""阶段 2：工艺规划 Agent。

两种规划器共用同一输出契约 ProcessPlan：
  RulePlanner —— 无 API Key 也能完整跑通链路的确定性专家规则（默认）
  LLMPlanner  —— OpenAI 兼容接口，输出 JSON 方案；解析失败/超时自动回退规则

无论哪种，产出都会经过 cutting.clamp_plan() 的二次夹紧。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Settings, masked_key
from .cutting import build_operation, choose_tool, clamp_plan, compute_feed, compute_spindle
from .models import (
    FeatureKind, MachineSpec, PartSpec, ProcessPlan, TOOLS, VMC850, get_tool,
)
from .prompts import build_repair_prompt, build_system_prompt, build_user_prompt


# --------------------------------------------------------------------------
# 规则规划器
# --------------------------------------------------------------------------
class RulePlanner:
    """把老师傅经验写成可执行规则。稳定、可复现、离线可用，是 LLM 的兜底基线。"""

    def plan(self, part: PartSpec, machine: MachineSpec = VMC850) -> ProcessPlan:
        ops = []
        thinking: list[str] = []
        for i, feat in enumerate(part.features):
            tool = choose_tool(feat)
            thinking.append(
                f"{feat.label(i + 1)}：选 {tool.name}，"
                f"刀具/特征宽度比 {tool.diameter / max(feat.width, feat.diameter, 1):.2f}"
            )
            if feat.kind == FeatureKind.HOLE:
                ops.append(build_operation(part, feat, i, "drill", get_tool(6), machine,
                                           raw={"strategy": "peck",
                                                "note": "啄钻排屑，D8 钻头一次到底"}))
                continue
            # 深特征分层粗加工
            n_layers = max(1, int(-(-feat.depth // max(
                min(tool.max_ap, tool.diameter * part.material.max_ap_ratio), 0.1))))
            ops.append(build_operation(
                part, feat, i, "rough", tool, machine,
                raw={"strategy": "contour" if feat.kind in
                     (FeatureKind.CIRCLE_POCKET, FeatureKind.RECT_POCKET) else "zigzag",
                     "note": f"粗加工分 {n_layers} 层，留 0.2mm 精加工余量"}))
            if feat.finish_pass and feat.depth >= 0.5:
                ops.append(build_operation(
                    part, feat, i, "finish", tool, machine,
                    raw={"strategy": "contour" if feat.kind in
                         (FeatureKind.CIRCLE_POCKET, FeatureKind.RECT_POCKET) else "zigzag",
                         "use_cutter_comp": True,
                         "note": "轮廓精修，启用 G41 半径补偿与 G43 长度补偿"}))

        n_ref = compute_spindle(part.material, get_tool(1), machine)
        f_ref = compute_feed(part.material, get_tool(1), n_ref)
        reasoning = (
            f"[{part.material.name}] 先建立顶面基准，再按特征逐个开粗、精修；"
            f"Vc={part.material.vc:g}m/min，D10 基准 S≈{n_ref}rpm、F≈{f_ref}mm/min，"
            f"按 n=1000Vc/(πD) 与 Vf=fz·z·n 计算并做主轴功率校核。"
            + " ".join(thinking)
            + " 同一把刀的工序合并排产以减少换刀；斜线下刀，快移一律抬到安全高度。"
        )
        plan = ProcessPlan(
            part=part,
            operations=ops,
            work_offset="G54",
            safe_z=20.0,
            clearance_z=5.0,
            coolant="M08",
            approach="ramp",
            planner="rule",
            reasoning=reasoning,
        )
        return clamp_plan(plan, machine)


# --------------------------------------------------------------------------
# LLM 规划器
# --------------------------------------------------------------------------
class LLMPlanner:
    """调用 OpenAI 兼容 /chat/completions。密钥只来自环境变量或本机 .env。"""

    def __init__(self, settings: Settings, machine: MachineSpec = VMC850):
        self.settings = settings
        self.machine = machine
        self.system_prompt = build_system_prompt(machine)
        self.last_error: str = ""

    def _chat(self, messages: list[dict[str, str]]) -> str:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.1,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.settings.llm_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any]:
        """容忍 ```json 围栏与前后废话。"""
        cleaned = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        if m:
            cleaned = m.group(1).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型输出中未找到 JSON 对象")
        return json.loads(cleaned[start:end + 1])

    def plan(self, part: PartSpec, feedback: list[str] | None = None,
             previous_json: str = "") -> ProcessPlan:
        part_payload = json.dumps({
            "name": part.name,
            "material": part.material.key,
            "stock": {"x": part.stock_x, "y": part.stock_y, "z": part.stock_z},
            "features": [f.__dict__ | {"kind": f.kind.value} for f in part.features],
        }, ensure_ascii=False, indent=2)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_user_prompt(part_payload)},
        ]
        if feedback:
            messages.append({"role": "user",
                             "content": build_repair_prompt(feedback, previous_json)})

        content = self._chat(messages)
        data = self._extract_json(content)

        # 模型若违反"禁止输出 G 代码"的约束，直接判为不合格输出
        if re.search(r"\b(G0?0|G0?1|G0?2|G0?3|M0?3|M30)\s*[XYZFS]", content):
            raise ValueError("模型违规输出了 G 代码文本，已拒绝该方案")

        ops = []
        for raw in data.get("operations", []):
            idx = int(raw.get("feature_index", 0))
            if not (0 <= idx < len(part.features)):
                continue  # 幻觉下标直接丢弃
            feat = part.features[idx]
            stage = str(raw.get("stage", "rough"))
            tool_no = int(raw.get("tool_number", choose_tool(feat).number))
            tool = get_tool(tool_no) if tool_no in TOOLS else choose_tool(feat)
            ops.append(build_operation(part, feat, idx, stage, tool, self.machine, raw))

        if not ops:
            raise ValueError("模型未给出任何有效工序")

        plan = ProcessPlan(
            part=part,
            operations=ops,
            work_offset=str(data.get("work_offset", "G54")),
            safe_z=float(data.get("safe_z", 20.0)),
            clearance_z=float(data.get("clearance_z", 5.0)),
            coolant=str(data.get("coolant", "M08")),
            approach=str(data.get("approach", "ramp")),
            planner="llm",
            reasoning=str(data.get("reasoning", ""))[:600],
            raw_llm_json=content[:4000],
        )
        return clamp_plan(plan, self.machine)


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------
class PlannerRouter:
    """优先 LLM，任何异常都静默降级到规则规划器，保证链路永不中断。"""

    def __init__(self, settings: Settings, machine: MachineSpec = VMC850):
        self.settings = settings
        self.machine = machine
        self.rule = RulePlanner()
        self.llm = LLMPlanner(settings, machine) if settings.llm_available else None
        self.fallback_reason = ""

    @property
    def mode(self) -> str:
        return "llm" if self.llm else "rule"

    @property
    def key_display(self) -> str:
        return masked_key(self.settings.llm_api_key)

    def plan(self, part: PartSpec, feedback: list[str] | None = None,
             previous_json: str = "") -> ProcessPlan:
        if self.llm is None:
            return self.rule.plan(part, self.machine)
        try:
            return self.llm.plan(part, feedback, previous_json)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError,
                json.JSONDecodeError, OSError, TypeError, IndexError,
                AttributeError) as exc:
            self.fallback_reason = f"{type(exc).__name__}: {exc}"
            self.llm = None
            plan = self.rule.plan(part, self.machine)
            plan.reasoning = (
                f"[LLM 不可用，已降级为规则规划器：{self.fallback_reason}] " + plan.reasoning
            )
            return plan


def inject_hallucination(plan: ProcessPlan, spec: dict[str, Any]) -> ProcessPlan:
    """演示专用：模拟 LLM 幻觉参数污染工艺方案。

    支持 S / F / ap / ae / tool_number / safe_z / work_offset 注入。
    这些污染值会在下一轮 clamp 阶段被拦截并记录，用来证明安全屏障有效。
    """
    injected: list[str] = []
    for op in plan.operations:
        for key in ("spindle", "feed", "plunge_feed", "ap", "ae", "tool_number"):
            if key in spec:
                setattr(op, key, type(getattr(op, key))(spec[key]))
                injected.append(f"{op.id}.{key}={spec[key]}")
    if "safe_z" in spec:
        plan.safe_z = float(spec["safe_z"])
        injected.append(f"safe_z={spec['safe_z']}")
    if "work_offset" in spec:
        plan.work_offset = str(spec["work_offset"])
        injected.append(f"work_offset={spec['work_offset']}")
    plan.reasoning += " [注入] 模拟幻觉：" + ", ".join(injected)
    return plan
