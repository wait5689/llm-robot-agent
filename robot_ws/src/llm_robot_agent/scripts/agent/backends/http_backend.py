#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP 远程导航后端（HTTP Remote Backend）
=======================================

运行在 Windows 侧，把导航请求通过 HTTP 转发给 WSL 里的 nav_bridge.py。
接口与 RosBackend 完全一致（RobotBackend 子类），对 Agent 层透明。

架构
----
    web_server.py → HttpBackend → HTTP → nav_bridge.py → RosBackend → move_base

用法
----
    # WSL 先启动桥接
    python3 nav_bridge.py --port 9090

    # Windows 启动 web_server，指定远程后端
    python web_server.py --backend http --bridge-url http://<WSL-IP>:9090
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

import requests

from ..contracts import ErrorCode, RobotState, ToolResult
from ..tools.locations import LocationMap
from .base import RobotBackend

log = logging.getLogger(__name__)


class HttpBackend(RobotBackend):
    """通过 HTTP 调用 WSL 侧 nav_bridge 的远程导航后端。"""

    def __init__(
        self,
        locations: LocationMap,
        bridge_url: str = "http://localhost:9090",
        timeout_sec: float = 120.0,
        connect_timeout: float = 5.0,
    ):
        self.loc = locations
        self.bridge_url = bridge_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self._ready = False
        self._state = RobotState(x=0.0, y=0.0, yaw=0.0, frame_id="map")

        # 检测桥接服务是否可达
        try:
            r = requests.get(f"{self.bridge_url}/health", timeout=connect_timeout)
            if r.ok:
                self._ready = True
                log.info("导航桥接已连接: %s", self.bridge_url)
            else:
                log.warning("桥接返回异常: %s", r.text[:200])
        except requests.ConnectionError:
            log.warning(
                "无法连接导航桥接 %s —— 导航将以模拟模式运行。\n"
                "请在 WSL 中启动: python3 nav_bridge.py --port 9090",
                self.bridge_url,
            )
        except Exception as exc:
            log.warning("桥接检测失败: %s", exc)

    def is_ready(self) -> bool:
        return self._ready

    def navigate_to(self, location: str, timeout_sec: Optional[float] = None) -> ToolResult:
        pose = self.loc.pose(location)
        if pose is None:
            return ToolResult(
                ok=False,
                observation=f"地点 '{location}' 没有坐标定义。",
                error_code=ErrorCode.UNKNOWN_LOCATION,
                retryable=True,
            )

        if not self._ready:
            # 降级为模拟导航
            return self._simulate_navigate(location, pose)

        try:
            r = requests.post(
                f"{self.bridge_url}/navigate",
                json={"location": location, "timeout_sec": timeout_sec or self.timeout_sec},
                timeout=(10, (timeout_sec or self.timeout_sec) + 10),
            )
            data = r.json()

            # 更新本地位姿
            if data.get("data") and "pose" in data["data"]:
                p = data["data"]["pose"]
                if len(p) >= 3:
                    self._state = RobotState(
                        x=p[0], y=p[1], yaw=p[2],
                        frame_id="map",
                        nav_state="idle",
                    )

            code = ErrorCode.from_value(data.get("error_code", "")) if not data.get("ok") else ErrorCode.NONE
            return ToolResult(
                ok=data.get("ok", False),
                observation=data.get("observation", ""),
                error_code=code,
                retryable=code in (ErrorCode.NAV_TIMEOUT, ErrorCode.BLOCKED, ErrorCode.BACKEND_ERROR),
                data=data.get("data"),
            )
        except requests.Timeout:
            return ToolResult(
                ok=False,
                observation=f"前往 {location} 的导航请求超时。",
                error_code=ErrorCode.NAV_TIMEOUT,
                retryable=True,
            )
        except Exception as exc:
            log.exception("桥接调用失败")
            return ToolResult(
                ok=False,
                observation=f"桥接通信异常: {exc}",
                error_code=ErrorCode.BACKEND_ERROR,
                retryable=True,
            )

    def cancel_nav(self) -> ToolResult:
        if not self._ready:
            return ToolResult(ok=True, observation="模拟模式，已取消。", data={"cancelled": True})
        try:
            r = requests.post(f"{self.bridge_url}/cancel", timeout=5)
            data = r.json()
            return ToolResult(
                ok=data.get("ok", False),
                observation=data.get("observation", ""),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                observation=f"取消失败: {exc}",
                error_code=ErrorCode.BACKEND_ERROR,
                retryable=True,
            )

    def get_state(self) -> RobotState:
        if not self._ready:
            return self._state
        try:
            r = requests.get(f"{self.bridge_url}/status", timeout=3)
            data = r.json()
            if data.get("robot"):
                rob = data["robot"]
                self._state = RobotState(
                    x=rob.get("x", 0), y=rob.get("y", 0), yaw=rob.get("yaw", 0),
                    frame_id=rob.get("frame_id", "map"),
                    nav_state=rob.get("nav_state", "idle"),
                    current_goal=rob.get("current_goal"),
                )
        except Exception:
            pass
        return self._state

    def name(self) -> str:
        return "HttpBackend"

    def shutdown(self) -> None:
        self._ready = False

    # ------------------------------------------------------------------
    # 模拟降级（桥接不可达时用）
    # ------------------------------------------------------------------
    def _simulate_navigate(self, location: str, pose) -> ToolResult:
        """桥接不可达时模拟导航（与 MockBackend 行为一致）。"""
        log.info("[模拟] 导航至 %s (%.2f, %.2f)", location, pose[0], pose[1])
        time.sleep(1.5)  # 模拟导航耗时
        x, y, yaw = pose
        dist = math.hypot(x - self._state.x, y - self._state.y)
        self._state = RobotState(
            x=x, y=y, yaw=yaw, frame_id="map",
            nav_state="idle",
        )
        return ToolResult(
            ok=True,
            observation=f"[模拟] 已到达 {location}，位置 ({x:.2f}, {y:.2f})，行驶约 {dist:.1f} m。",
            data={"location": location, "pose": [x, y, yaw], "simulated": True},
            latency_ms=1500,
        )
