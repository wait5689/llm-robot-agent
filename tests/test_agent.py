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


def _find_scripts_dir() -> str:
    """向上查找包含 agent 包的 scripts 目录。

    不能把层级写死成「测试文件的上两级 + robot_ws/...」，
    因为 tests/ 有时被拷到别处（例如放进 ROS 包内部），
    那样推算出来的路径不存在，pytest 会在收集阶段
    直接报 ModuleNotFoundError: No module named 'agent'，
    看起来像测试代码坏了，其实只是目录层级不对。

    这里改成逐级向上找：既可以命中
        <repo>/robot_ws/src/llm_robot_agent/scripts
    也能命中直接把 scripts 放在旁边的布局。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(8):
        for cand in (
            os.path.join(cur, "scripts"),
            os.path.join(cur, "robot_ws", "src", "llm_robot_agent", "scripts"),
        ):
            if os.path.isdir(os.path.join(cand, "agent")):
                return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise RuntimeError(
        "找不到含 agent 包的 scripts 目录（从 %s 向上找了 8 级）。"
        "请确认在仓库根目录运行 pytest。" % here)


_SCRIPTS = _find_scripts_dir()
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from agent import LocationMap, MockBackend, default_location_map  # noqa: E402
from agent.contracts import ErrorCode  # noqa: E402
from agent.core import LLMRobotAgent  # noqa: E402
from agent.llm_client import LLMResponse, OfflinePlanner, ToolCall  # noqa: E402
from agent.tools.registry import ToolRegistry, ToolSpec, build_default_tools  # noqa: E402

# StagnationMonitor 位于 backends 包内，但不 import rospy，可安全单测
from agent.backends.stagnation import StagnationMonitor  # noqa: E402


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

    # ---------------- 参数 schema 校验（类型）----------------
    def test_wrong_type_is_rejected(self):
        """模型传入错误类型（数字当地点）应被拒绝，而不是穿透到后端。"""
        reg, _ = self._registry()
        result = reg.invoke("navigate_to", {"location": 3})
        assert not result.ok
        assert result.error_code == ErrorCode.PARAM_INVALID
        assert "类型" in result.observation

    def test_unknown_location_still_reaches_handler(self):
        """enum 不做字面量比对，未知地点由 handler 的 resolve() 判定。

        这样别名（a / 房间a / 去房间A）才不会被 schema 层误杀。
        """
        reg, backend = self._registry()
        result = reg.invoke("navigate_to", {"location": "火星基地"})
        assert not result.ok
        assert result.error_code == ErrorCode.UNKNOWN_LOCATION
        # 提示里要列出可用地点，模型才能自我纠正
        assert "room_a" in result.observation

    def test_alias_still_works_through_validation(self):
        """加参数校验后，别名必须依然能正常导航。"""
        reg, backend = self._registry()
        result = reg.invoke("navigate_to", {"location": "房间a"})
        assert result.ok, f"别名应可用，实际：{result.observation}"

    def test_timeout_accepts_int_and_float(self):
        """number 类型应同时接受 int 与 float。"""
        reg, _ = self._registry()
        for v in (30, 30.5):
            result = reg.invoke("navigate_to",
                                {"location": "room_a", "timeout_sec": v})
            assert result.ok, f"timeout_sec={v!r} 应被接受"

    def test_bool_is_not_accepted_as_number(self):
        """Python 里 True 是 int 的子类，不能让 bool 冒充 number。"""
        reg, _ = self._registry()
        result = reg.invoke("navigate_to",
                            {"location": "room_a", "timeout_sec": True})
        assert not result.ok
        assert result.error_code == ErrorCode.PARAM_INVALID

    def test_numeric_string_is_rejected(self):
        """'30' 不是 number，不能隐式转换（显式优于隐式）。"""
        reg, _ = self._registry()
        result = reg.invoke("navigate_to",
                            {"location": "room_a", "timeout_sec": "30"})
        assert not result.ok
        assert result.error_code == ErrorCode.PARAM_INVALID

    # ---------------- 工具禁用（借鉴 NASA JPL ROSA 的 blacklist）----------------
    def test_disabled_tool_removed_from_schema(self):
        """被禁用的工具必须从 schema 里消失。

        只在调用时拒绝是不够的：模型只要看得见就会一直尝试，
        白白消耗轮次和 token。从 schema 移除才是真正「不可用」。
        """
        reg, _ = self._registry()
        before = {s["function"]["name"] for s in reg.schemas()}
        assert "navigate_to" in before

        reg.disable("navigate_to", reason="演示期间锁定移动")
        after = {s["function"]["name"] for s in reg.schemas()}

        assert "navigate_to" not in after
        assert "get_robot_state" in after  # 其它工具不受影响

    def test_disabled_tool_call_is_rejected_with_reason(self):
        """模型执意调用被禁工具时，要说明原因且不可重试。"""
        reg, _ = self._registry()
        reg.disable("navigate_to", reason="演示期间锁定移动")

        result = reg.invoke("navigate_to", {"location": "room_a"})
        assert not result.ok
        assert result.error_code == ErrorCode.TOOL_NOT_FOUND
        # 不可重试：重试也不会成功
        assert result.retryable is False
        # 提示里要带上禁用原因，模型才知道不是参数写错
        assert "演示期间锁定移动" in result.observation

    def test_enable_restores_tool(self):
        """禁用后可以恢复。"""
        reg, _ = self._registry()
        reg.disable("navigate_to", reason="临时锁定")
        assert not reg.is_enabled("navigate_to")

        reg.enable("navigate_to")
        assert reg.is_enabled("navigate_to")
        assert reg.invoke("navigate_to", {"location": "room_a"}).ok

    def test_disable_unknown_tool_raises(self):
        """禁用不存在的工具应当立刻报错，避免拼写错误静默失效。"""
        reg, _ = self._registry()
        with pytest.raises(KeyError):
            reg.disable("no_such_tool")

    def test_enabled_names_excludes_disabled(self):
        reg, _ = self._registry()
        reg.disable("cancel_navigation", reason="测试")
        assert "cancel_navigation" not in reg.enabled_names()
        # names() 仍列出全部（含禁用），便于排查配置
        assert "cancel_navigation" in reg.names()


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

    def __init__(self, script: List[LLMResponse], usage=None):
        self.script = list(script)
        self.calls: List[List[Dict[str, Any]]] = []
        # 模拟接口返回的 token 用量；None 表示端点不返回 usage
        self.usage = usage

    def available(self) -> bool:
        return True

    def chat(self, messages, tools=None, temperature=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if self.script:
            resp = self.script.pop(0)
        else:
            resp = LLMResponse(ok=True, content="done")
        # 把用量挂到本次响应上，模拟真实客户端的行为
        resp.usage = self.usage
        return resp


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


# ==========================================================================
# 6. 停滞检测（回归：这段逻辑曾经是死代码）
#
# 为什么值得单测：旧实现判断的是 move_base 的 action 状态而非位姿，
# 而导航全程 action 都是 ACTIVE，导致停滞判定永远不触发。
# 仿真里这个 bug 只在「真卡住」时才显形，很容易漏掉。
# 这里用可控的时间序列精确断言触发时机。
# ==========================================================================
class TestStagnationMonitor:
    def test_no_trigger_while_moving(self):
        """持续前进时永远不判定停滞。"""
        mon = StagnationMonitor(timeout=10.0, dist_threshold=0.10)
        mon.reset(0.0, 0.0, now=0.0)
        # 每 1 秒前进 0.2 m，共 60 秒
        for i in range(1, 61):
            assert not mon.update(0.2 * i, 0.0, float(i)), \
                f"第 {i}s 仍在移动，不应判定停滞"

    def test_triggers_after_timeout_when_frozen(self):
        """完全不动时，应在 timeout 之后立刻触发。"""
        mon = StagnationMonitor(timeout=10.0, dist_threshold=0.10)
        mon.reset(0.0, 0.0, now=0.0)
        for i in range(0, 10):
            assert not mon.update(0.0, 0.0, float(i)), f"{i}s 时不应触发"
        # 第 11 秒超过 10s 阈值
        assert mon.update(0.0, 0.0, 11.0)

    def test_small_jitter_does_not_reset_timer(self):
        """位姿抖动小于阈值不算进展，不能无限续命。"""
        mon = StagnationMonitor(timeout=10.0, dist_threshold=0.10)
        mon.reset(0.0, 0.0, now=0.0)
        # 每次只抖动 0.05 m（< 0.10 阈值）
        for i in range(1, 21):
            jitter = 0.05 if i % 2 else -0.05
            triggered = mon.update(jitter, 0.0, float(i))
            if i <= 10:
                assert not triggered, f"{i}s 抖动不应触发"
        # 到 20 秒必然已超过 10 秒无实质进展
        assert mon.update(0.05, 0.0, 21.0)

    def test_progress_resets_timer(self):
        """中途明确前进过一次，超时计时应重新开始。"""
        mon = StagnationMonitor(timeout=10.0, dist_threshold=0.10)
        mon.reset(0.0, 0.0, now=0.0)
        # 静止到第 9 秒
        for i in range(0, 10):
            assert not mon.update(0.0, 0.0, float(i))
        # 第 9.5 秒前进 0.2 m -> 计时重置
        assert not mon.update(0.2, 0.0, 9.5)
        # 从 9.5 起算，到 18.0 仍未超过 10s
        assert not mon.update(0.2, 0.0, 18.0)
        # 到 20.0 超过 10s 了
        assert mon.update(0.2, 0.0, 20.0)

    def test_rejects_invalid_params(self):
        """非法参数应当立刻报错，而不是静默产生诡异行为。"""
        with pytest.raises(ValueError):
            StagnationMonitor(timeout=0)
        with pytest.raises(ValueError):
            StagnationMonitor(dist_threshold=0)

    def test_elapsed_without_progress(self):
        mon = StagnationMonitor(timeout=10.0, dist_threshold=0.10)
        mon.reset(0.0, 0.0, now=5.0)
        assert mon.elapsed_without_progress(8.0) == pytest.approx(3.0)
        mon.update(0.5, 0.0, 8.0)  # 有进展 -> 重置
        assert mon.elapsed_without_progress(9.0) == pytest.approx(1.0)


# ==========================================================================
# 7. 配置驱动的工具禁用 + token 计量
#
# 工具禁用借鉴 NASA JPL ROSA 的 blacklist：安全策略不应该写死在代码里，
# 而应由配置决定（同一条代码，演示时锁移动、真机时锁急停）。
# ==========================================================================
class TestToolPolicyAndUsage:
    def _agent(self, llm=None, agent_cfg=None):
        lm = default_location_map()
        mock = MockBackend(lm, step_delay=0, seed=5)
        cfg = {"max_turns": 8, "loop_delay_sec": 0, "max_nav_attempts": 3}
        cfg.update(agent_cfg or {})
        agent = LLMRobotAgent(
            backend=mock, llm=llm or FakeLLM([]), locations=lm,
            config={"agent": cfg}, verbose=False,
        )
        return agent, mock

    def test_disabled_tools_dict_form(self):
        """dict 形式：名字 -> 原因，原因应出现在被拒提示里。"""
        agent, _ = self._agent(agent_cfg={
            "disabled_tools": {"cancel_navigation": "急停需人工确认"},
        })
        assert not agent.tools.is_enabled("cancel_navigation")
        schema_names = {s["function"]["name"] for s in agent.tool_schemas}
        assert "cancel_navigation" not in schema_names

        result = agent.tools.invoke("cancel_navigation", {})
        assert not result.ok
        assert "急停需人工确认" in result.observation

    def test_disabled_tools_list_form(self):
        """list 形式：只给名字。"""
        agent, _ = self._agent(agent_cfg={
            "disabled_tools": ["cancel_navigation"],
        })
        assert not agent.tools.is_enabled("cancel_navigation")

    def test_typo_in_disabled_tools_does_not_crash(self):
        """配置里写错工具名只告警，不能让 Agent 起不来。"""
        agent, _ = self._agent(agent_cfg={
            "disabled_tools": ["no_such_tool_typo"],
        })
        # 正常构造出来了，且其它工具仍可用
        assert agent.tools.is_enabled("navigate_to")

    def test_no_disabled_tools_by_default(self):
        agent, _ = self._agent()
        assert len(agent.tools.enabled_names()) == len(agent.tools.names())

    def test_usage_is_accumulated(self):
        """LLM 返回的 usage 必须被累计，否则无法回答「花了多少 token」。"""
        llm = FakeLLM([
            _call("navigate_to", '{"location": "room_a"}'),
            _call("report_done", '{"summary": "完成"}'),
        ])
        # 给每次响应挂上 usage
        llm.usage = {"prompt_tokens": 120, "completion_tokens": 30}
        agent, _ = self._agent(llm)
        out = agent.run("去 room_a")

        assert out["llm_calls"] == 2
        assert out["total_tokens"] == 2 * (120 + 30)
        rec = out["record"]
        assert rec["prompt_tokens"] == 240
        assert rec["completion_tokens"] == 60
        assert rec["total_tokens"] == 300

    def test_usage_absent_does_not_break(self):
        """端点不返回 usage 时应保持 0，而不是抛异常。"""
        llm = FakeLLM([
            _call("report_done", '{"summary": "完成"}'),
        ])
        llm.usage = None
        agent, _ = self._agent(llm)
        out = agent.run("随便")
        assert out["llm_calls"] == 1
        assert out["total_tokens"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
