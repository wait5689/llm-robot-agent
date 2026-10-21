#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ReAct 主循环（Agent Core）
==========================

这是整个项目的核心：**把「一次性 JSON 翻译」升级为「观察-决策-行动」闭环**。

与旧实现 (`scripts/_legacy_llm_robot_agent.py`) 的本质差异
--------------------------------------------------------
| 维度           | 旧实现                          | 本实现                          |
|----------------|---------------------------------|---------------------------------|
| 规划时机       | 任务开始前一次性生成全部步骤        | 每一步之后重新决策                |
| 失败处理       | 记录失败后继续下一步              | 按 error_code 分类，重试/绕行/放弃 |
| LLM 参与度     | 只参与翻译                        | 参与每次决策（真正的 Agent）        |
| 终止条件       | 步骤列表走完                      | 模型显式调用 report_done/blocked |
| 成本控制       | 无                              | 轮数上限 + 超时 + 上下文压缩       |
| 可测试性       | 依赖 ROS，难以测试                | 纯 Python，可用 Mock 后端全量测试  |

循环结构
--------
```
task -> [LLM 决策] -> [工具执行] -> [观察注入] -> [判断终止]
              ^                                     |
              +-------------------------------------+
```
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .contracts import ErrorCode, PlanStep, RobotState, StepStatus, TaskRecord, ToolResult
from .prompts import (
    PLANNER_PROMPT,
    build_observation_message,
    build_system_prompt,
    build_task_message,
    summarize_trace,
)

log = logging.getLogger(__name__)


class LLMRobotAgent:
    """基于 LLM 的移动机器人 Agent。"""

    def __init__(
        self,
        backend: Any,
        llm: Any,
        locations: Any,
        config: Optional[Dict[str, Any]] = None,
        verbose: bool = True,
    ):
        self.backend = backend
        self.llm = llm
        self.locations = locations
        self.cfg = (config or {}).get("agent", config or {})

        self.max_turns = int(self.cfg.get("max_turns", 20))
        self.max_nav_attempts = int(self.cfg.get("max_nav_attempts", 3))
        self.task_timeout = float(self.cfg.get("task_timeout_sec", 600))
        self.loop_delay = float(self.cfg.get("loop_delay_sec", 0.3))
        self.verbose = verbose

        # 延迟导入，避免 tools/__init__ 循环依赖
        from .tools.registry import build_default_tools

        self.tools = build_default_tools(backend, locations)
        self.tool_schemas = self.tools.schemas()

        self.system_prompt = build_system_prompt(locations, backend.name())
        self._llm_available = hasattr(llm, "chat")

        # 运行时状态
        self.record: Optional[TaskRecord] = None
        self._attempts: Dict[str, int] = {}
        self._consecutive_failures = 0

    # ==================================================================
    # 对外主接口
    # ==================================================================
    def run(self, instruction: str) -> Dict[str, Any]:
        """执行一次完整任务，返回可序列化的结果（供 Web / CLI / 日志使用）。"""
        self.record = TaskRecord(instruction=instruction)
        self._attempts.clear()
        self._consecutive_failures = 0

        self._say(f"\n{'=' * 68}\n任务：{instruction}\n{'=' * 68}")

        if not self._llm_available:
            result = self._run_offline(instruction)
        else:
            try:
                result = self._run_react(instruction)
            except Exception as exc:  # noqa: BLE001 - 任务级兜底
                log.exception("Agent 主循环异常")
                self.record.success = False
                self.record.final_message = f"Agent 内部异常：{exc}"
                result = {"success": False, "message": self.record.final_message}

        self.record.finished_at = time.time()
        return {
            "success": result.get("success", False),
            # summary 与 message 同源，保留双键以兼容两种调用约定
            "summary": self.record.final_message,
            "message": self.record.final_message,
            "turns": self.record.turns,
            "tool_calls": self.record.tool_calls,
            "replans": self.record.replans,
            "elapsed_sec": round(self.record.elapsed(), 1),
            "steps": [s.to_dict() for s in self.record.steps],
            "final_state": self.backend.get_state().to_dict(),
            "record": self.record.to_dict(),
        }

    # ==================================================================
    # ReAct 循环
    # ==================================================================
    def _run_react(self, instruction: str) -> Dict[str, Any]:
        state = self.backend.get_state()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": build_task_message(instruction, state.describe())},
        ]

        deadline = time.time() + self.task_timeout

        while self.record.turns < self.max_turns:
            if time.time() > deadline:
                return self._finish(False, "任务超时，已中止。")

            self.record.turns += 1
            self._say(f"\n--- 第 {self.record.turns} 轮 ---")

            resp = self.llm.chat(messages, tools=self.tool_schemas)

            if not resp.ok:
                self._say(f"[LLM 调用失败] {resp.error}")
                return self._finish(False, f"LLM 调用失败：{resp.error}")

            # 模型可能同时返回文本（思考）与工具调用
            if resp.content:
                self._say(f"[思考] {self._clip(resp.content, 300)}")

            if not resp.tool_calls:
                # 没有工具调用：说明模型认为任务结束或卡住了
                if self.record.steps and all(
                    s.status in (StepStatus.DONE, StepStatus.SKIPPED) for s in self.record.steps
                ):
                    return self._finish(True, resp.content or "任务完成。")
                self._say("[警告] 模型未调用工具就结束了，强制要求其继续或汇报")
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "请不要直接结束。要么调用工具继续执行，"
                            "要么调用 report_done / report_blocked 明确结束任务。"
                        ),
                    }
                )
                continue

            # 记录 assistant 消息（含 tool_calls），保持对话结构合法
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in resp.tool_calls
                ],
            }
            messages.append(assistant_msg)

            # 逐个执行工具调用
            for tc in resp.tool_calls:
                self.record.tool_calls += 1
                self._say(f"[行动] {tc.name}({self._clip(tc.arguments, 120)})")

                # report_done / report_blocked 直接终止
                if tc.name == "report_done":
                    args = self._safe_json(tc.arguments)
                    return self._finish(True, args.get("summary", "任务完成。"))
                if tc.name == "report_blocked":
                    args = self._safe_json(tc.arguments)
                    return self._finish(False, args.get("reason", "任务失败。"))

                result = self.tools.invoke_json(tc.name, tc.arguments)
                self._say(f"[观察] {result.brief()}")

                self._apply_result(tc.name, result)
                self._log_trace(tc.name, result)

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.brief(),
                    }
                )

                # 连续失败熔断：避免无限烧 token
                if not result.ok:
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 5:
                        return self._finish(
                            False,
                            f"连续 {self._consecutive_failures} 次工具调用失败，"
                            "已中止任务以避免无意义的重复尝试。",
                        )
                else:
                    self._consecutive_failures = 0

            # ---- 上下文压缩：超过 12 条后折叠历史 ----
            messages = self._compress(messages)

            if self.loop_delay:
                time.sleep(self.loop_delay)

        return self._finish(
            False, f"已达最大轮数 {self.max_turns}，任务未完成，判定为失败。",
            code=ErrorCode.BUDGET_EXCEEDED,
        )

    # ==================================================================
    # 结果标记与重规划统计
    # ==================================================================
    def _apply_result(self, tool_name: str, result: ToolResult) -> None:
        """把工具结果回写到计划步骤，用于最终汇报与重规划计数。"""
        if tool_name != "navigate_to":
            return

        loc = (result.data or {}).get("location")
        if not loc:
            return

        if result.ok:
            self._consecutive_failures = 0
            for step in self.record.steps:
                if step.target == loc and step.status != StepStatus.DONE:
                    step.status, step.attempts = StepStatus.DONE, step.attempts + 1
                    return
            # 模型自行追加的目标（不在初始计划内）
            self.record.steps.append(
                PlanStep(
                    step_id=len(self.record.steps) + 1,
                    action="navigate",
                    target=loc,
                    purpose="模型动态追加",
                    status=StepStatus.DONE,
                    attempts=1,
                )
            )
            return

        self._attempts[loc] = self._attempts.get(loc, 0) + 1
        self.record.replans += 1

        for step in self.record.steps:
            if step.target == loc and step.status != StepStatus.DONE:
                step.attempts += 1
                step.last_error = result.error_code.value
                step.status = StepStatus.FAILED
                # 不可达或次数用尽 → 标记跳过，避免死循环
                if (
                    result.error_code == ErrorCode.GOAL_UNREACHABLE
                    or self._attempts[loc] >= self.max_nav_attempts
                ):
                    step.status = StepStatus.SKIPPED
                return

    def _log_trace(self, tool_name: str, result: ToolResult) -> None:
        self.record.trace.append(
            {
                "event": "tool_result",
                "turn": self.record.turns,
                "tool": tool_name,
                "ok": result.ok,
                "error_code": result.error_code.value,
                "latency_ms": result.latency_ms,
                "data": result.data,
                "observation": result.observation[:300],
            }
        )

    # ==================================================================
    # 离线模式（无 LLM）
    # ==================================================================
    def _run_offline(self, instruction: str) -> Dict[str, Any]:
        """无 API Key 时的确定性执行路径。

        行为刻意与 LLM 路径保持一致：顺序执行 -> 失败重试 -> 汇报，
        这样即使断网，演示脚本仍然能展示项目的主干能力。
        """
        self._say("[离线模式] 使用规则规划器")
        plan = self.llm.plan(instruction, self.locations)

        if not plan:
            self.record.success = False
            self.record.final_message = "无法从指令中解析出任何地点，请使用更明确的地点名称。"
            self.record.finished_at = time.time()
            return {
                "success": False,
                "message": self.record.final_message,
                "steps": [],
                "final_state": self.backend.get_state().to_dict(),
                "record": self.record.to_dict(),
            }

        self.record.steps = [
            PlanStep(step_id=s["step_id"], action=s["action"],
                     target=s["target"], purpose=s.get("purpose", ""))
            for s in plan
        ]

        for step in self.record.steps:
            self.record.turns += 1
            success = False
            for attempt in range(1, self.max_nav_attempts + 1):
                self.record.tool_calls += 1
                self._say(f"[行动] navigate_to({step.target}) 第 {attempt} 次")
                result = self.tools.invoke("navigate_to", {"location": step.target})
                self._say(f"[观察] {result.brief()}")
                step.attempts = attempt
                self._log_trace("navigate_to", result)

                if result.ok:
                    step.status = StepStatus.DONE
                    success = True
                    break

                step.last_error = result.error_code.value
                # 不可达或不可重试的错误直接跳过
                if not result.retryable or result.error_code == ErrorCode.GOAL_UNREACHABLE:
                    break
                self.record.replans += 1

            if not success:
                step.status = StepStatus.SKIPPED

        done = [s for s in self.record.steps if s.status == StepStatus.DONE]
        skipped = [s for s in self.record.steps if s.status == StepStatus.SKIPPED]
        ok = len(done) == len(self.record.steps)

        lines = [f"[离线模式] 任务{'完成' if ok else '部分完成'}："]
        lines.append(f"成功 {len(done)}/{len(self.record.steps)} 个地点：" +
                     (", ".join(s.target for s in done) or "无"))
        if skipped:
            lines.append("失败：" + ", ".join(
                f"{s.target}({s.last_error})" for s in skipped))
        state = self.backend.get_state()
        lines.append(f"最终位置：{state.describe()}")

        self.record.success = ok
        self.record.final_message = "\n".join(lines)
        self.record.finished_at = time.time()
        self._say("\n" + self.record.final_message)

        return {
            "success": ok,
            "message": self.record.final_message,
            "turns": self.record.turns,
            "tool_calls": self.record.tool_calls,
            "replans": self.record.replans,
            "elapsed_sec": round(self.record.elapsed(), 1),
            "steps": [s.to_dict() for s in self.record.steps],
            "final_state": state.to_dict(),
            "record": self.record.to_dict(),
        }

    # ==================================================================
    # 工具方法
    # ==================================================================
    def _finish(self, success: bool, message: str,
                code: ErrorCode = ErrorCode.NONE) -> Dict[str, Any]:
        self.record.success = success
        self.record.final_message = message
        self.record.finished_at = time.time()
        self._say(f"\n{'✓' if success else '✗'} {message}")
        return {"success": success, "message": message, "error_code": code.value}

    def _compress(self, messages: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
        """保留 system + 首条任务 + 最近对话，中间折叠为进度摘要。

        切点必须落在「完整对话单元」的起点上。消息流中
        assistant(tool_calls) 与紧随其后的 tool 消息是成对的：
        若按固定条数从尾部切，切点很可能落在某条 tool 消息上，
        它的父 assistant 已被裁掉，就变成了一条孤立的 tool 消息，
        OpenAI 兼容接口会直接返回 400 Bad Request，整个任务中断。
        """
        if len(messages) <= limit:
            return messages

        head = messages[:2]

        start = len(messages) - (limit - 3)
        if start < 2:
            return messages
        # 跳过所有孤立 tool 消息，直到落在 assistant / user 边界上
        while start < len(messages) and messages[start].get("role") == "tool":
            start += 1
        if start >= len(messages):
            return messages

        recent = messages[start:]
        # 末尾若残留一条只有 tool_calls、没有 tool 回应的 assistant，
        # 同样不合法（tool_call_id 悬空），一并丢掉。
        while recent and recent[-1].get("role") == "assistant" and recent[-1].get("tool_calls"):
            recent = recent[:-1]
        if not recent:
            return messages

        summary = summarize_trace(self.record.trace) if self.record else "（无历史记录）"
        bridge = {
            "role": "user",
            "content": (
                "【此前执行摘要，供你回顾】\n"
                f"{summary}\n"
                "请在此基础上继续，不要重复已完成的地点。"
            ),
        }
        return head + [bridge] + recent

    @staticmethod
    def _safe_json(raw: str) -> Dict[str, Any]:
        try:
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _clip(text: str, n: int) -> str:
        text = (text or "").replace("\n", " ")
        return text if len(text) <= n else text[: n - 3] + "..."

    def _say(self, text: str) -> None:
        if self.verbose:
            try:
                print(text, flush=True)
            except UnicodeEncodeError:
                # Windows GBK 终端无法输出 ✓✗ 等 Unicode 字符，回退到 ASCII
                safe = text.replace("✓", "[OK]").replace("✗", "[FAIL]")
                print(safe.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)
        log.info(text)
