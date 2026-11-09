"""配置加载：只读 .env，不依赖第三方库；任何密钥都不写入仓库。"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
EXAMPLE_DIR = ROOT / "examples"


def _load_dotenv() -> None:
    """极简 .env 解析器：KEY=VALUE，忽略注释与空行，不覆盖已有环境变量。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        text = env_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # 中文 Windows 上用记事本另存的 .env 常是 GBK，不能让一份编码
        # 异常的 .env 把整个 cncagent 包的 import 打断。
        for enc in ("gbk", "latin-1"):
            try:
                text = env_file.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_timeout: int = 40
    controller: str = ""          # 留空 = 跟随所选机型；填了才强制覆盖方言
    machine_id: str = "vmc850_fanuc"
    simulator_resolution: float = 0.5
    max_repair_rounds: int = 3
    strict_simulation: bool = True
    vision_weights: str = ""
    vision_protocol: str = "dryrun"
    allow_auto_stop: bool = False

    @property
    def llm_available(self) -> bool:
        return bool(self.llm_api_key)


def _int_env(key: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        v = default
    return min(max(v, lo), hi)


def _float_env(key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        v = default
    if not math.isfinite(v):
        v = default
    return min(max(v, lo), hi)


def get_settings() -> Settings:
    return Settings(
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        llm_timeout=_int_env("LLM_TIMEOUT", 40, 1, 600),
        controller=os.getenv("CNC_CONTROLLER", "").upper(),
        machine_id=os.getenv("CNC_MACHINE", "vmc850_fanuc"),
        simulator_resolution=_float_env("SIM_RESOLUTION", 0.5, 0.1, 2.0),
        max_repair_rounds=_int_env("MAX_REPAIR_ROUNDS", 3, 0, 10),
        strict_simulation=os.getenv("STRICT_SIMULATION", "1") == "1",
        vision_weights=os.getenv("YOLO_WEIGHTS", ""),
        vision_protocol=os.getenv("VISION_PROTOCOL", "dryrun"),
        allow_auto_stop=os.getenv("ALLOW_AUTO_STOP", "0") == "1",
    )


def masked_key(key: str) -> str:
    """日志/报告里只显示打码后的密钥，绝不回显明文。"""
    if not key:
        return "(未配置)"
    if len(key) <= 10:
        return f"*({len(key)} 字符)"
    return f"{key[:6]}{'*' * 8}{key[-4:]} (长度 {len(key)})"
