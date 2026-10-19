#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
演示脚本（Demo）
===============

四个场景，按顺序覆盖项目的全部验收点。无需 ROS、无需 API Key 即可运行。

    python demos/demo.py            # 运行全部场景
    python demos/demo.py --scene 2  # 只运行第 2 个场景

场景设计意图
------------
1. 多地点连续任务   —— 证明「一句自然语言 → 多步执行」成立
2. 遇障自动重试     —— 证明失败反馈闭环成立（项目核心卖点）
3. 目标不可达       —— 证明系统能区分「可重试」与「不可重试」，不会死循环
4. 混合故障长任务   —— 证明多故障叠加下仍能完成可完成的部分并诚实汇报
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "robot_ws", "src", "llm_robot_agent", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from agent import LLMRobotAgent, MockBackend, default_location_map  # noqa: E402
from agent.llm_client import create_llm  # noqa: E402


def build_agent(faults=None, seed=42):
    lm = default_location_map()
    backend = MockBackend(lm, seed=seed, step_delay=0.4)
    for loc, mode, times in (faults or []):
        backend.inject_fault(loc, mode, times=times, retryable=(mode != "unreachable"))

    llm = create_llm({"api_key_env": "DEEPSEEK_API_KEY"}, offline_fallback=True)
    agent = LLMRobotAgent(
        backend=backend,
        llm=llm,
        locations=lm,
        config={"agent": {"max_turns": 15, "loop_delay_sec": 0, "max_nav_attempts": 3}},
        verbose=True,
    )
    return agent, backend


def banner(idx: int, title: str, note: str) -> None:
    print("\n" + "#" * 70)
    print(f"# 场景 {idx}：{title}")
    print(f"# {note}")
    print("#" * 70)


def run_scene(idx: int) -> bool:
    """返回该场景的验收结果。

    注意：场景 3、4 含**故意不可达**的目标，整体 `success=False` 是预期行为。
    这类场景的验收标准是「可完成的地点全部完成 + 不可达地点被正确跳过」，
    而不是任务全部成功。
    """
    if idx == 1:
        banner(1, "多地点连续任务", "验收点：一句自然语言完成多步导航")
        agent, backend = build_agent()
        task = "帮我去房间 A 检查一下，然后前往房间 B，最后回到起点。"
        result = agent.run(task)

    elif idx == 2:
        banner(2, "遇到障碍自动重试", "验收点：失败 → 观察 → 重新规划 → 成功")
        agent, backend = build_agent(faults=[("room_a", "blocked", 1)])
        task = "去房间 A 检查一下，然后前往房间 B。如果路上遇到障碍，就重新规划路线。"
        result = agent.run(task)

    elif idx == 3:
        banner(3, "目标不可达", "验收点：识别不可重试错误，跳过而非死循环")
        agent, backend = build_agent(faults=[("room_c", "unreachable", 9)])
        task = "去房间 C，然后去房间 A，最后回起点。"
        result = agent.run(task)
        # 预期：room_c 跳过，room_a 与 home 完成，且 room_c 只尝试 1 次
        steps = {s["target"]: s["status"] for s in result["steps"]}
        passed = (
            steps.get("room_c") == "skipped"
            and steps.get("room_a") == "done"
            and steps.get("home") == "done"
            and backend.stats.nav_calls == 3
        )
        _report(result, backend, passed,
                "预期：room_c 跳过（仅尝试 1 次），room_a / home 完成")
        return passed

    elif idx == 4:
        banner(4, "混合故障长任务", "验收点：多故障叠加下诚实汇报部分完成")
        agent, backend = build_agent(
            faults=[
                ("room_a", "blocked", 1),
                ("room_b", "timeout", 1),
                ("room_d", "unreachable", 9),
            ]
        )
        task = "先去房间 A，再去房间 B，然后去房间 D，最后回到起点。"
        result = agent.run(task)
        steps = {s["target"]: s["status"] for s in result["steps"]}
        passed = (
            steps.get("room_a") == "done"
            and steps.get("room_b") == "done"
            and steps.get("room_d") == "skipped"
            and steps.get("home") == "done"
            and result["replans"] >= 2
        )
        _report(result, backend, passed,
                "预期：A/B 重试后完成，D 跳过，home 完成，重规划 ≥ 2")
        return passed

    else:
        print(f"未知场景：{idx}")
        return False

    _report(result, backend, result["success"])
    return result["success"]


def _report(result, backend, passed: bool, note: str = "") -> None:
    print("\n" + "-" * 70)
    print(f"结果：{'通过' if passed else '未通过'}")
    if note:
        print(f"判定：{note}")
    print(f"汇报：{result['message'].splitlines()[0] if result['message'] else ''}")
    print(f"统计：轮数 {result['turns']} | 工具调用 {result['tool_calls']} | "
          f"重规划 {result['replans']} | 耗时 {result['elapsed_sec']}s")
    print(f"Mock：{backend.stats.to_dict()}")
    print("-" * 70)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LLM Robot Agent 演示")
    parser.add_argument("--scene", type=int, help="只运行指定场景（1-4）")
    args = parser.parse_args()

    scenes = [args.scene] if args.scene else [1, 2, 3, 4]
    results = {}
    for s in scenes:
        results[s] = run_scene(s)

    print("\n" + "=" * 70)
    print("演示汇总：")
    for s, ok in results.items():
        print(f"  场景 {s}: {'通过' if ok else '未通过'}")
    print("=" * 70)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
