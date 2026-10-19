#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后端抽象接口（Backend Abstraction）
===================================

这是整个项目最重要的一个架构决策点。

```
        ┌─────────────────────────────┐
        │   LLM Agent 循环 (ReAct)    │   ← 与后端完全无关
        └──────────────┬──────────────┘
                       │ ToolResult / RobotState
        ┌──────────────┴──────────────┐
        │      ToolRegistry           │   ← 参数校验、错误归一化
        └──────────────┬──────────────┘
                       │ navigate_to / get_state / cancel_nav
        ┌──────────────┴──────────────┐
        │      RobotBackend (ABC)     │
        └───────┬─────────────┬───────┘
                │             │
        ┌───────┴──────┐  ┌───┴──────────┐
        │ MockBackend  │  │  RosBackend  │
        │ (单元测试)    │  │ (Gazebo)     │
        └──────────────┘  └──────────────┘
```

由此获得的能力：
- 没有 ROS 环境也能跑通全部 Agent 逻辑；
- 换仿真器、换真机，只改一个后端类；
- 失败场景（超时、受阻、不可达）可以在 Mock 里确定性复现，
  这是做「失败重试与任务恢复」测试的前提。
"""

from __future__ import annotations

import abc
from typing import Optional

from ..contracts import RobotState, ToolResult


class RobotBackend(abc.ABC):
    """机器人后端必须实现的全部能力。"""

    @abc.abstractmethod
    def navigate_to(self, location: str, timeout_sec: Optional[float] = None) -> ToolResult:
        """前往一个**已经校验过**的规范地点名。

        约定：该方法阻塞直到到达、失败或超时；返回前机器人必须停稳。
        """

    @abc.abstractmethod
    def cancel_nav(self) -> ToolResult:
        """取消当前导航并停止机器人。"""

    @abc.abstractmethod
    def get_state(self) -> RobotState:
        """返回当前可观测状态。任何情况下都不应抛异常。"""

    @abc.abstractmethod
    def shutdown(self) -> None:
        """释放资源（ROS 节点、Action 客户端等）。"""

    # ---------------- 可选能力 ----------------
    def is_ready(self) -> bool:
        """导航栈是否就绪（定位收敛、move_base 可用）。"""
        return True

    def name(self) -> str:
        return self.__class__.__name__
