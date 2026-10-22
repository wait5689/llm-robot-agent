#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据契约层（Data Contracts）
============================

本模块定义 Agent 内部所有组件之间交换的数据结构。

设计原则
--------
1. **后端无关**：`ToolResult` / `RobotState` 不依赖 ROS 消息类型，
   因此同一个 Agent 循环既能跑在 Gazebo 上，也能跑在 Mock 后端的单元测试里。
2. **可序列化**：所有结构都能 `to_dict()`，方便写日志、回放、给 LLM 看。
3. **显式失败**：失败不是异常，而是带 `error_code` 的返回值。
   Agent 需要「读到」失败才能决策，异常会打断循环。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# 枚举
# --------------------------------------------------------------------------
class ErrorCode(str, Enum):
    """工具调用的标准化错误码。

    细分错误码的价值：LLM 可以根据不同的错误码做出**不同**决策。
    例如 PARAM_INVALID 应该改参数重试，而 GOAL_UNREACHABLE 应该换地点或放弃。
    """

    NONE = "NONE"
    PARAM_INVALID = "PARAM_INVALID"          # 参数非法，可修正后重试
    UNKNOWN_LOCATION = "UNKNOWN_LOCATION"    # 地点不存在，应换地点或询问
    NAV_TIMEOUT = "NAV_TIMEOUT"              # 导航超时，可重试
    GOAL_UNREACHABLE = "GOAL_UNREACHABLE"    # 目标不可达，重试无意义
    BLOCKED = "BLOCKED"                      # 路过受阻，需要绕行/换路线
    ROBOT_NOT_READY = "ROBOT_NOT_READY"      # 定位丢失、导航未就绪
    BACKEND_ERROR = "BACKEND_ERROR"          # 后端内部异常
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"        # 模型幻觉出了不存在的工具
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"      # 轮数/时间用尽

    @classmethod
    def from_value(cls, raw: str, default=None) -> "ErrorCode":
        """把字符串安全地转回枚举；找不到就返回 default（或 NONE）。"""
        if not raw:
            return default or cls.NONE
        for member in cls:
            if member.value == raw:
                return member
        return default or cls.NONE


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


# --------------------------------------------------------------------------
# 工具调用结果
# --------------------------------------------------------------------------
@dataclass
class ToolResult:
    """所有工具的唯一返回类型。

    `observation` 是给 LLM 看的自然语言摘要，`data` 是给程序用的结构化结果。
    两者分离，避免模型去解析 JSON 数字。
    """

    ok: bool
    observation: str
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: ErrorCode = ErrorCode.NONE
    retryable: bool = False
    latency_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["error_code"] = self.error_code.value
        return d

    def brief(self, max_len: int = 400) -> str:
        """压缩成一行，用于写入 LLM 上下文，控制 token 消耗。"""
        tag = "OK" if self.ok else f"FAIL:{self.error_code.value}"
        text = self.observation.replace("\n", " ").strip()
        if len(text) > max_len:
            text = text[: max_len - 3] + "..."
        return f"[{tag}] {text}"


@dataclass
class RobotState:
    """机器人的可观测状态。Mock 与 ROS 后端都必须产出这个结构。"""

    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    frame_id: str = "map"
    nav_state: str = "idle"          # idle / navigating / recovering / blocked
    current_goal: Optional[str] = None
    battery: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def describe(self) -> str:
        deg = self.yaw * 180.0 / 3.141592653589793
        parts = [
            f"位置=({self.x:.2f}, {self.y:.2f}) m",
            f"朝向={deg:.0f}°",
            f"导航状态={self.nav_state}",
        ]
        if self.current_goal:
            parts.append(f"当前目标={self.current_goal}")
        if self.battery is not None:
            parts.append(f"电量={self.battery:.0f}%")
        return "，".join(parts)


# --------------------------------------------------------------------------
# 计划与执行记录
# --------------------------------------------------------------------------
@dataclass
class PlanStep:
    """LLM 产出的一个计划步骤。

    注意 `status` 与 `attempts` 由 Agent 运行时维护，
    LLM 只需要提供 `action` / `target` / `purpose`。
    """

    step_id: int
    action: str
    target: Optional[str] = None
    purpose: str = ""
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    def label(self) -> str:
        if self.target:
            return f"#{self.step_id} {self.action}({self.target})"
        return f"#{self.step_id} {self.action}"


@dataclass
class TaskRecord:
    """一次完整任务的轨迹记录，用于汇报、复盘和长期记忆。"""

    instruction: str
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    success: Optional[bool] = None
    turns: int = 0
    tool_calls: int = 0
    replans: int = 0
    steps: List[PlanStep] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    final_message: str = ""

    # ---------------- LLM 成本计量 ----------------
    # 接口返回的 usage 字段原本被直接丢弃，导致「这个任务花了多少
    # token」无法回答。这里累计起来，供成本分析与回归对比使用。
    # 注意：部分第三方 OpenAI 兼容端点不返回 usage，此时保持 0。
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add_usage(self, usage: Optional[Dict[str, Any]]) -> None:
        """累计一次 LLM 调用的 token 用量。

        兼容两种字段命名：OpenAI 的 prompt_tokens/completion_tokens
        与部分端点的 input_tokens/output_tokens。缺失则忽略。
        """
        self.llm_calls += 1
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        try:
            self.prompt_tokens += int(prompt or 0)
            self.completion_tokens += int(completion or 0)
        except (TypeError, ValueError):
            pass

    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "instruction": self.instruction,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "success": self.success,
            "turns": self.turns,
            "tool_calls": self.tool_calls,
            "replans": self.replans,
            "elapsed_sec": round(self.elapsed(), 1),
            "steps": [s.to_dict() for s in self.steps],
            "trace": self.trace,
            "final_message": self.final_message,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }
