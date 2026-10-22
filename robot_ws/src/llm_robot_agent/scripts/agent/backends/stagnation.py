#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
停滞检测器（Stagnation Monitor）
================================

纯 Python、不依赖 ROS，因此可以被确定性地单元测试。

为什么单独抽出来
----------------
早期实现把「机器人是否在动」的判断直接写在 `RosBackend.navigate_to`
的 200 毫秒轮询循环里，而且判据写错了 —— 它看的是 move_base 的
action 状态（ACTIVE/PENDING/SUCCEEDED），而不是机器人位姿。

机器人在正常导航的整个过程中，action 状态**一直是 ACTIVE**，
于是「无进展计时器」每一轮都被刷新，45 秒判定永远不会触发，
那段代码从来没有执行过（死代码）。

实测证据：注入持续零速指令让机器人卡死，90 秒后返回的是
`NAV_TIMEOUT` 而不是设计意图中的 `BLOCKED`。

后果不只是少一个错误码：`BLOCKED` 的语义是「可重试/可绕行」，
`NAV_TIMEOUT` 是「等太久了」。两者对 Agent 的后续决策不同，
所以错误码错了会导致策略也错。

现在的判据
----------
只看**位姿是否真的变化**：
  - 记录上一次「被认定为有进展」的位置
  - 当前位置与它相距 >= `dist_threshold` 才算有进展，重置计时
  - 连续 `timeout` 秒没有达到这个位移 -> 判定停滞

把这段逻辑抽成独立类的收益是可测试性：可以让时间加速、
让位姿按预设序列变化，从而精确断言「第几秒触发」。
"""

from __future__ import annotations

import math
from typing import Optional, Tuple


class StagnationMonitor:
    """判断机器人在导航过程中是否已经停滞。

    用法（每轮循环调用一次 `update`）：

        mon = StagnationMonitor(timeout=25.0, dist_threshold=0.10)
        mon.reset(x, y)                 # 发送目标后初始化
        ...
        if mon.update(x, y, now):       # 返回 True 表示判定停滞
            return BLOCKED

    时间由调用方传入，不依赖 `time.time()`，便于测试。
    """

    def __init__(self, timeout: float = 25.0, dist_threshold: float = 0.10):
        if timeout <= 0:
            raise ValueError("timeout 必须为正数")
        if dist_threshold <= 0:
            raise ValueError("dist_threshold 必须为正数")
        self.timeout = float(timeout)
        self.dist_threshold = float(dist_threshold)

        self._ref: Optional[Tuple[float, float]] = None
        self._last_progress: float = 0.0
        self._started: bool = False

    # ------------------------------------------------------------------
    def reset(self, x: float, y: float, now: float) -> None:
        """记录起点并开始计时。"""
        self._ref = (float(x), float(y))
        self._last_progress = float(now)
        self._started = True

    def update(self, x: float, y: float, now: float) -> bool:
        """喂入当前位置；返回 True 表示已停滞超时。

        若尚未 reset，则把当前点当作起点（容错，避免调用方漏初始化
        导致第一轮就误判停滞）。
        """
        if not self._started or self._ref is None:
            self.reset(x, y, now)
            return False

        ref_x, ref_y = self._ref
        if math.hypot(float(x) - ref_x, float(y) - ref_y) >= self.dist_threshold:
            # 有实质进展：把参考点移到当前位置并重置计时
            self._ref = (float(x), float(y))
            self._last_progress = float(now)
            return False

        return (float(now) - self._last_progress) > self.timeout

    # ------------------------------------------------------------------
    def elapsed_without_progress(self, now: float) -> float:
        """距离上次进展过了多少秒（用于日志与观测文案）。"""
        if not self._started:
            return 0.0
        return max(0.0, float(now) - self._last_progress)

    @property
    def reference(self) -> Optional[Tuple[float, float]]:
        """当前参考点（上一次被认定有进展的位置）。"""
        return self._ref
