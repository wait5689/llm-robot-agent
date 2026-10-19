#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mock 后端（Mock Backend）
=========================

一个可信的「假机器人」，用于在没有 ROS 的环境下跑通并测试整个 Agent。

设计要点
--------
1. **确定性的随机种子**：同一 seed 下失败模式可复现，适合做回归测试。
2. **可编排的故障场景**：通过 `fault_plan` 精确指定「第几次去某地点会失败、为什么失败」，
   这样「遇到障碍就重新规划」这条需求才能被真正验证，而不是靠运气。
3. **行为贴近 Nav2**：超时、受阻、不可达三种失败语义与原版导航栈一致，
   换真后端时 Agent 逻辑无需调整。

典型用法
--------
```python
mock = MockBackend(seed=42)
mock.inject_fault("room_a", mode="blocked", times=1, retryable=True)
# 第一次去 room_a 会失败（BLOCKED，可重试），Agent 应自行重试并成功
```
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..contracts import ErrorCode, RobotState, ToolResult
from ..tools.locations import LocationMap
from .base import RobotBackend

log = logging.getLogger(__name__)


@dataclass
class FaultSpec:
    """一种被注入的故障。"""

    location: str
    mode: str                     # timeout / blocked / unreachable / not_ready
    times: int = 1                # 剩余生效次数（-1 表示永久）
    retryable: bool = True

    def consume(self) -> bool:
        if self.times == 0:
            return False
        if self.times > 0:
            self.times -= 1
        return True


@dataclass
class MockStats:
    """运行统计，便于在演示与测试中断言。"""

    nav_calls: int = 0
    nav_success: int = 0
    nav_failed: int = 0
    cancel_calls: int = 0
    total_distance: float = 0.0
    visited: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "nav_calls": self.nav_calls,
            "nav_success": self.nav_success,
            "nav_failed": self.nav_failed,
            "cancel_calls": self.cancel_calls,
            "total_distance_m": round(self.total_distance, 2),
            "visited": self.visited,
        }


class MockBackend(RobotBackend):
    """内存中的机器人模拟器。"""

    def __init__(
        self,
        locations: LocationMap,
        seed: Optional[int] = None,
        step_delay: float = 0.0,
        base_failure_rate: float = 0.0,
        speed_mps: float = 0.8,
    ):
        """
        参数
        ----
        locations : LocationMap
            用于把地点名解析成坐标，并计算模拟行驶距离。
        seed : int, optional
            随机种子。给定后失败模式可复现。
        step_delay : float
            每次导航人为增加的耗时（秒），用于模拟真实导航的时间成本。
            设为 0 可让测试瞬间跑完。
        base_failure_rate : float
            未注入故障时，导航失败的基础概率。默认 0（全成功）。
        speed_mps : float
            模拟行驶速度，仅用于估算耗时与里程，不做真实等待。
        """
        self.loc = locations
        self.rng = random.Random(seed)
        self.step_delay = step_delay
        self.base_failure_rate = base_failure_rate
        self.speed_mps = speed_mps

        # 初始位姿：起点
        home = locations.pose("home") or (0.0, 0.0, 0.0)
        self.state = RobotState(x=home[0], y=home[1], yaw=home[2], nav_state="idle")

        self._faults: List[FaultSpec] = []
        self._ready = True
        self.stats = MockStats()

    # ------------------------------------------------------------------
    # 故障编排
    # ------------------------------------------------------------------
    def inject_fault(self, location: str, mode: str, times: int = 1,
                     retryable: bool = True) -> None:
        """为某地点注入故障。

        mode:
          - "blocked"      路径被临时占用（可重试）
          - "timeout"      导航超时（可重试）
          - "unreachable"  目标不可达（重试无意义，Agent 应换策略）
          - "not_ready"    导航栈未就绪（可重试）
        """
        self._faults.append(
            FaultSpec(location=location, mode=mode, times=times, retryable=retryable)
        )
        log.info("注入故障：%s -> %s x%s", location, mode, times)

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    def _match_fault(self, location: str) -> Optional[FaultSpec]:
        for fault in self._faults:
            if fault.location == location and fault.consume():
                return fault
        return None

    # ------------------------------------------------------------------
    # RobotBackend 实现
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        return self._ready

    def navigate_to(self, location: str, timeout_sec: Optional[float] = None) -> ToolResult:
        self.stats.nav_calls += 1

        pose = self.loc.pose(location)
        if pose is None:
            self.stats.nav_failed += 1
            return ToolResult(
                ok=False,
                observation=f"地点 '{location}' 没有坐标定义，无法导航。",
                error_code=ErrorCode.UNKNOWN_LOCATION,
                retryable=True,
            )

        if not self._ready:
            self.stats.nav_failed += 1
            return ToolResult(
                ok=False,
                observation="导航栈尚未就绪：定位可能未收敛，或被临时故障阻断。",
                error_code=ErrorCode.ROBOT_NOT_READY,
                retryable=True,
            )

        # 计算模拟耗时与里程
        dist = math.hypot(pose[0] - self.state.x, pose[1] - self.state.y)
        self.state.nav_state = "navigating"
        self.state.current_goal = location

        # step_delay == 0 表示「零延迟模式」（单元测试），完全不做真实等待；
        # 否则按模拟行驶时间等待，并做上限保护避免演示卡死。
        if self.step_delay > 0:
            sim_time = dist / max(self.speed_mps, 0.1) + self.step_delay
            time.sleep(min(sim_time, 3.0))

        fault = self._match_fault(location)
        if fault is None and self.base_failure_rate > 0:
            if self.rng.random() < self.base_failure_rate:
                fault = FaultSpec(location, "blocked", times=1, retryable=True)

        if fault is not None:
            return self._apply_fault(fault, pose, dist)

        # 成功
        self.state.x, self.state.y, self.state.yaw = pose
        self.state.nav_state = "idle"
        self.state.current_goal = None
        self.stats.nav_success += 1
        self.stats.total_distance += dist
        self.stats.visited.append(location)

        return ToolResult(
            ok=True,
            observation=(
                f"已到达 {location}，位置 ({pose[0]:.2f}, {pose[1]:.2f})，"
                f"行驶约 {dist:.1f} m。"
            ),
            data={"location": location, "pose": list(pose), "distance_m": round(dist, 2)},
        )

    def _apply_fault(self, fault: FaultSpec, pose, dist: float) -> ToolResult:
        """根据故障类型生成符合语义的返回值，并模拟部分行驶。"""
        self.stats.nav_failed += 1
        self.state.nav_state = "idle"
        self.state.current_goal = None

        # 受阻时机器人只走了一半路，这是真实 Nav2 的常见行为
        if fault.mode in ("blocked", "timeout"):
            ratio = 0.5 if fault.mode == "blocked" else 0.65
            self.state.x = self.state.x + (pose[0] - self.state.x) * ratio
            self.state.y = self.state.y + (pose[1] - self.state.y) * ratio
            self.stats.total_distance += dist * ratio

        if fault.mode == "blocked":
            return ToolResult(
                ok=False,
                observation=(
                    f"前往 {fault.location} 途中遇到障碍物，路径被占用，"
                    "已退回安全位置并停止。"
                ),
                error_code=ErrorCode.BLOCKED,
                retryable=fault.retryable,
                data={"location": fault.location, "mode": fault.mode},
            )
        if fault.mode == "timeout":
            return ToolResult(
                ok=False,
                observation=f"前往 {fault.location} 导航超时，未能在规定时间内到达。",
                error_code=ErrorCode.NAV_TIMEOUT,
                retryable=fault.retryable,
                data={"location": fault.location, "mode": fault.mode},
            )
        if fault.mode == "unreachable":
            return ToolResult(
                ok=False,
                observation=(
                    f"目标 {fault.location} 不可达：全局规划器找不到可行路径。"
                    "重复尝试不会改变结果。"
                ),
                error_code=ErrorCode.GOAL_UNREACHABLE,
                retryable=False,
                data={"location": fault.location, "mode": fault.mode},
            )
        return ToolResult(
            ok=False,
            observation="导航栈未知故障。",
            error_code=ErrorCode.BACKEND_ERROR,
            retryable=True,
        )

    def cancel_nav(self) -> ToolResult:
        self.stats.cancel_calls += 1
        self.state.nav_state = "idle"
        self.state.current_goal = None
        return ToolResult(
            ok=True,
            observation="已取消当前导航，机器人已停止。",
            data={"cancelled": True},
        )

    def get_state(self) -> RobotState:
        return RobotState(**{**self.state.to_dict()})

    def shutdown(self) -> None:
        log.debug("MockBackend 关闭")
