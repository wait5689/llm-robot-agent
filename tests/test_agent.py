#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试套件（Test Suite）
======================

四个测试组，逐步逼近真实系统：

1. TestLocationMap        —— 语义地点解析（别名、口语、错误输入）
2. TestToolRegistry       —— 参数校验、幻觉工具、异常兜底
3. TestMockBackend        —— 故障语义（BLOCKED / UNREACHABLE / TIMEOUT）
4. TestAgentOffline       —— 端到端任务执行：重试、跳过、汇报
5. TestAgentReact         —— 用 FakeLLM 驱动真实 ReAct 循环

第 5 组是关键：它用一个脚本化的假模型验证「观察 → 决策」闭环真的成立了，
而不是靠真实 API 的运气。这条测试是简历上「有完整测试覆盖」的底气。

运行：
    python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List

import pytest

# 让测试能直接 import agent 包（必须在 import agent 之前执行）
_SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "robot_ws", "src", "llm_robot_agent", "scripts",
)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from agent import LocationMap, MockBackend, default_location_map  # noqa: E402
from agent.contracts import ErrorCode  # noqa: E402
from agent.core import LLMRobotAgent  # noqa: E402
from agent.llm_client import LLMResponse, OfflinePlanner, ToolCall  # noqa: E402
from agent.tools.registry import ToolRegistry, ToolSpec, build_default_tools  # noqa: E402


# ==========================================================================
# 1. 地点解析
# ==========================================================================
class TestLocationMap:
    def test_resolve_canonical(self):
        lm = default_location_map()
        assert lm.resolve("room_a") == "room_a"

    def test_resolve_alias_chinese(self):
        lm = default_location_map()
        assert lm.resolve("房间A") == "room_a"
        assert lm.resolve("起点") == "home"

    def test_resolve_colloquial(self):
        lm = default_location_map()
        # 「去房间B」这类带动词的说法也应能解析
        assert lm.resolve("去房间B") == "room_b"

    def test_resolve_failure(self):
        lm = default_location_map()
        assert lm.resolve("火星基地") is None
        assert lm.resolve("") is None

    def test_pose_exists(self):
        lm = default_location_map()
        pose = lm.pose("room_a")
        assert pose is not None and len(pose) == 3

    def test_nearest(self):
        lm = default_location_map()
        name, dist = lm.nearest(-3.4, 2.4)
        assert name == "room_a"
        assert dist < 0.5


# ==========================================================================
# 2. 工具注册表
# ==========================================================================
class TestToolRegistry:
    def _registry(self):
        lm = default_location_map()
        backend = MockBackend(lm, seed=1, step_delay=0)
        return build_default_tools(backend, lm), lm

    def test_schema_export(self):
        reg, _ = self._registry()
        schemas = reg.schemas()
        names = {s["function"]["name"] for s in schemas}
        assert {"navigate_to", "get_robot_state", "report_done"} <= names
        # 必须是合法的 OpenAI function-calling 结构
        for s in schemas:
            assert s["type"] == "function"
            assert s["function"]["parameters"]["type"] == "object"

    def test_navigate_ok(self):
        reg, _ = self._registry()
        result = reg.invoke("navigate_to", {"location": "room_b"})
        assert result.ok
        assert result.data["location"] == "room_b"

    def test_navigate_alias(self):
        reg, _ = self._registry()
        result = reg.invoke("navigate_to", {"location": "房间B"})
        assert result.ok
        # 别名必须被解析成规范名
        assert result.data["location"] == "room_b"

    def test_navigate_unknown_location(self):
        reg, _ = self._registry()
        result = reg.invoke("navigate_to", {"location": "火星"})
        assert not result.ok
        assert result.error_code == ErrorCode.UNKNOWN_LOCATION
        assert result.retryable

    def test_hallucinated_tool(self):
        reg, _ = self._registry()
        result = reg.invoke("launch_missile", {})
        assert not result.ok
        assert result.error_code == ErrorCode.TOOL_NOT_FOUND

    def test_missing_required_param(self):
        reg, _ = self._registry()
        result = reg.invoke("navigate_to", {})
        assert not result.ok
        assert result.error_code == ErrorCode.PARAM_INVALID

    def test_tool_exception_is_contained(self):
        """工具内部抛异常时，注册表必须兜住并返回 ToolResult。"""
        reg = ToolRegistry()

        def boom(**kwargs):
            raise RuntimeError("后端炸了")

        reg.register(ToolSpec("boom", "会爆炸的工具", {}, boom))
        result = reg.invoke("boom", {})
        assert not result.ok
        assert result.error_code == ErrorCode.BACKEND_ERROR

    def test_invalid_json_arguments(self):
        reg, _ = self._registry()
        result = reg.invoke_json("navigate_to", '{"location": "room_a"')
        assert not result.ok
        assert result.error_code == ErrorCode.PARAM_INVALID


# ==========================================================================
# 3. Mock 后端的故障语义
# ==========================================================================
class TestMockBackend:
    def test_nav_success_updates_pose(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        result = mock.navigate_to("room_a")
        assert result.ok
        state = mock.get_state()
        assert abs(state.x - (-3.5)) < 0.01
        assert abs(state.y - 2.5) < 0.01

    def test_blocked_is_retryable(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        mock.inject_fault("room_a", "blocked", times=1)
        first = mock.navigate_to("room_a")
        assert not first.ok
        assert first.error_code == ErrorCode.BLOCKED
        assert first.retryable
        # 第二次应当成功（故障只注入一次）
        second = mock.navigate_to("room_a")
        assert second.ok

    def test_unreachable_is_not_retryable(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        mock.inject_fault("room_c", "unreachable", times=5, retryable=False)
        result = mock.navigate_to("room_c")
        assert not result.ok
        assert result.error_code == ErrorCode.GOAL_UNREACHABLE
        assert not result.retryable

    def test_timeout(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        mock.inject_fault("room_b", "timeout", times=1)
        result = mock.navigate_to("room_b")
        assert result.error_code == ErrorCode.NAV_TIMEOUT

    def test_not_ready(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        mock.set_ready(False)
        result = mock.navigate_to("room_a")
        assert result.error_code == ErrorCode.ROBOT_NOT_READY

    def test_stats_tracking(self):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0)
        mock.navigate_to("room_a")
        mock.navigate_to("room_b")
        assert mock.stats.nav_calls == 2
        assert mock.stats.nav_success == 2
        assert mock.stats.visited == ["room_a", "room_b"]


# ==========================================================================
# 4. 离线模式端到端
# ==========================================================================
class TestAgentOffline:
    def _agent(self, faults: List[tuple] = None):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0, seed=7)
        for loc, mode, times in (faults or []):
            mock.inject_fault(loc, mode, times=times, retryable=(mode != "unreachable"))
        agent = LLMRobotAgent(
            backend=mock, llm=OfflinePlanner(), locations=lm,
            config={"agent": {"max_nav_attempts": 3, "loop_delay_sec": 0}},
            verbose=False,
        )
        return agent, mock

    def test_multi_location_task(self):
        agent, mock = self._agent()
        result = agent.run("去房间 A，然后去房间 B，最后回到起点")
        assert result["success"]
        assert result["tool_calls"] == 3
        assert mock.stats.visited == ["room_a", "room_b", "home"]

    def test_blocked_then_retry_succeeds(self):
        """核心需求验证：路上遇到障碍 → 自动重试 → 最终成功。"""
        agent, mock = self._agent(faults=[("room_a", "blocked", 1)])
        result = agent.run("去房间 A，然后去房间 B")
        assert result["success"]
        assert result["replans"] >= 1
        assert mock.stats.nav_calls == 3  # room_a 失败1次 + 成功1次 + room_b

    def test_unreachable_is_skipped(self):
        agent, mock = self._agent(faults=[("room_c", "unreachable", 3)])
        result = agent.run("去房间 C，然后去房间 A")
        # room_c 跳过后 room_a 仍应完成
        steps = {s["target"]: s["status"] for s in result["steps"]}
        assert steps["room_c"] == "skipped"
        assert steps["room_a"] == "done"
        assert not result["success"]  # 有跳过项，整体不算完全成功

    def test_unparseable_instruction(self):
        agent, _ = self._agent()
        result = agent.run("给我讲个笑话")
        assert not result["success"]
        assert "地点" in result["message"]

    def test_persistent_blocked_gives_up(self):
        agent, mock = self._agent(faults=[("room_a", "blocked", 10)])
        result = agent.run("去房间 A")
        assert not result["success"]
        # 重试次数受 max_nav_attempts 约束，不会无限循环
        assert mock.stats.nav_calls == 3


# ==========================================================================
# 5. ReAct 循环（用 FakeLLM 驱动）
# ==========================================================================
class FakeLLM:
    """按脚本逐轮返回预置响应，用来验证 Agent 的决策链路。

    每一步都可以断言「Agent 是否把上一步的观察喂回了模型」。
    """

    def __init__(self, script: List[LLMResponse]):
        self.script = list(script)
        self.calls: List[List[Dict[str, Any]]] = []

    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, temperature=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if self.script:
            return self.script.pop(0)
        return LLMResponse(ok=True, content="done")


def _call(name: str, args: str, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(ok=True, content="", tool_calls=[ToolCall(call_id, name, args)])


class TestAgentReact:
    def _agent(self, llm, faults=None):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0, seed=3)
        for loc, mode, times in (faults or []):
            mock.inject_fault(loc, mode, times=times, retryable=(mode != "unreachable"))
        agent = LLMRobotAgent(
            backend=mock, llm=llm, locations=lm,
            config={"agent": {"max_turns": 8, "loop_delay_sec": 0, "max_nav_attempts": 3}},
            verbose=False,
        )
        return agent, mock

    def test_single_step_then_done(self):
        llm = FakeLLM([
            _call("navigate_to", '{"location": "room_a"}'),
            _call("report_done", '{"summary": "已到达房间 A"}'),
        ])
        agent, mock = self._agent(llm)
        result = agent.run("去房间 A")
        assert result["success"]
        assert mock.stats.visited == ["room_a"]
        assert "已到达房间 A" in result["message"]

    def test_observation_is_fed_back(self):
        """关键断言：工具结果必须出现在下一轮的对话上下文里。"""
        llm = FakeLLM([
            _call("navigate_to", '{"location": "room_b"}'),
            _call("report_done", '{"summary": "ok"}'),
        ])
        agent, _ = self._agent(llm)
        agent.run("去房间 B")
        assert len(llm.calls) == 2
        second_round = llm.calls[1]
        tool_msgs = [m for m in second_round if m.get("role") == "tool"]
        assert tool_msgs, "第二轮上下文中缺少工具观察结果"
        assert "room_b" in tool_msgs[0]["content"]

    def test_llm_sees_failure_and_retries(self):
        """模型看到 BLOCKED 后应决定重试——验证失败反馈真的进了上下文。"""
        llm = FakeLLM([
            _call("navigate_to", '{"location": "room_a"}'),   # 会失败
            _call("navigate_to", '{"location": "room_a"}', "c2"),  # 重试
            _call("report_done", '{"summary": "重试后到达"}', "c3"),
        ])
        agent, mock = self._agent(llm, faults=[("room_a", "blocked", 1)])
        result = agent.run("去房间 A")
        assert result["success"]
        # 第二轮上下文中应包含 BLOCKED 观察
        assert "BLOCKED" in str(llm.calls[1])
        assert mock.stats.nav_success == 1

    def test_report_blocked_terminates(self):
        llm = FakeLLM([
            _call("report_blocked", '{"reason": "目标不可达，已尝试三次"}'),
        ])
        agent, _ = self._agent(llm)
        result = agent.run("去房间 C")
        assert not result["success"]
        assert "不可达" in result["message"]

    def test_max_turns_guard(self):
        """模型无限循环调用工具时，必须被轮数上限截断。"""
        script = [_call("get_robot_state", "{}", f"c{i}") for i in range(20)]
        llm = FakeLLM(script)
        agent, _ = self._agent(llm)
        result = agent.run("随便看看")
        assert not result["success"]
        assert result["turns"] <= 8

    def test_unknown_tool_recovered(self):
        """模型幻觉出工具时，Agent 不应崩溃，且能继续。"""
        llm = FakeLLM([
            _call("fly_to_moon", '{"location": "moon"}'),
            _call("report_done", '{"summary": "放弃"}', "c2"),
        ])
        agent, _ = self._agent(llm)
        result = agent.run("去房间 A")
        assert "fly_to_moon" not in result.get("message", "")
        # 至少不应抛异常，且返回结构完整
        assert "steps" in result

    def test_compress_keeps_message_structure_valid(self):
        """上下文压缩后，消息结构必须仍对 OpenAI 兼容接口合法。

        回归用例：曾按固定条数从尾部切片，切点落在 tool 消息上，
        造成孤立 tool 消息（父 assistant 已被裁掉），接口返回
        400 Bad Request。表现是任何超过 6 步的任务必然中断。
        """
        # 8 轮工具调用，足以触发多次压缩
        script = [_call("navigate_to", '{"location": "room_a"}', f"c{i}")
                  for i in range(8)]
        script.append(_call("report_done", '{"summary": "完成"}', "cd"))
        llm = FakeLLM(script)
        agent, _ = self._agent(llm)
        agent.run("反复去房间 A")

        # 检查每一次发给模型的消息序列都合法
        for idx, msgs in enumerate(llm.calls):
            pending = set()
            for pos, m in enumerate(msgs):
                role = m["role"]
                if role == "assistant":
                    for tc in m.get("tool_calls") or []:
                        pending.add(tc["id"])
                elif role == "tool":
                    tid = m.get("tool_call_id")
                    assert tid in pending, (
                        f"第 {idx} 次调用第 {pos} 条为孤立 tool 消息 "
                        f"(tool_call_id={tid})，父 assistant 已被压缩裁掉"
                    )
                    pending.discard(tid)
                elif role in ("system", "user"):
                    assert not pending, (
                        f"第 {idx} 次调用第 {pos} 条 {role} 消息插在"
                        f"未回应的 tool_calls {pending} 之前"
                    )
            assert not pending, (
                f"第 {idx} 次调用末尾遗留未回应的 tool_calls {pending}"
            )

    def test_compress_reduces_length(self):
        """超过 limit 时必须真的折叠，否则上下文会无限增长。"""
        msgs = [{"role": "system", "content": "s"},
                {"role": "user", "content": "t"}]
        for i in range(20):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "navigate_to", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "[OK]"})

        agent, _ = self._agent(FakeLLM([]))
        out = agent._compress(msgs, limit=12)
        assert len(out) < len(msgs)
        # 摘要桥接消息必须存在
        assert any("此前执行摘要" in (m.get("content") or "") for m in out)
        # 首条 system 与任务必须保留
        assert out[0]["role"] == "system"
        assert out[1]["role"] == "user"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
