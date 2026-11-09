"""机器视觉实时监控子模块。

链路：帧源(Camera/RTSP/Video/Synthetic) -> YOLO/启发式检测 -> 事件去抖
      -> 截图与检测结果作为工具输入交给 Agent -> 日志/告警
      -> 工业协议控制下发 -> 反哺工艺规划重新生成 G 代码
"""
from .detector import (Detection, FaultClass, VisionEvent, YOLODetector,
                       HeuristicDetector, create_detector)
from .frames import (FrameSource, SyntheticSource, CameraSource,
                     VideoFileSource, ImageSequenceSource, DEMO_SCENARIO,
                     make_source)
from .monitor import (VisionMonitor, MonitorResult, MonitorConfig,
                      VisionLogger, AlarmDispatcher, Debouncer)
from .agent_tools import (VisionDiagnosisAgent, Diagnosis, SafetyGovernor,
                          ControlDecision, VISION_TOOL_SPEC, FAULT_RULES)
from .protocols import (ProtocolAdapter, DryRunAdapter, ModbusTCPAdapter,
                        OPCUAAdapter, MQTTAdapter, create_adapter,
                        recommend_protocol)
from .closed_loop import ClosedLoopController, ClosedLoopResult, EventTrace

__all__ = [
    "Detection", "FaultClass", "VisionEvent", "YOLODetector",
    "HeuristicDetector", "create_detector",
    "FrameSource", "SyntheticSource", "CameraSource", "VideoFileSource",
    "ImageSequenceSource", "DEMO_SCENARIO", "make_source",
    "VisionMonitor", "MonitorResult", "MonitorConfig", "VisionLogger",
    "AlarmDispatcher", "Debouncer",
    "VisionDiagnosisAgent", "Diagnosis", "SafetyGovernor", "ControlDecision",
    "VISION_TOOL_SPEC", "FAULT_RULES",
    "ProtocolAdapter", "DryRunAdapter", "ModbusTCPAdapter", "OPCUAAdapter",
    "MQTTAdapter", "create_adapter", "recommend_protocol",
    "ClosedLoopController", "ClosedLoopResult", "EventTrace",
]
