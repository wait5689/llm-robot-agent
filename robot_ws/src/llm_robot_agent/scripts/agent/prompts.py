#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 工程（Prompt Templates）
===============================

Agent 的行为上限，一半取决于这里的措辞。

设计取舍
--------
1. **系统提示只描述能力与环境，不硬编码任务步骤** —— 避免「伪 Agent」。
2. **明确告知工具边界与失败语义** —— 让模型知道 BLOCKED 与 GOAL_UNREACHABLE
   应该采取不同策略，而不是无脑重试。
3. **强制「观察后再决策」** —— 每次工具返回后都要重新判断，而不是一次性规划到底。
4. **上下文压缩** —— 只保留最近 N 轮原文，更早的折叠成摘要，控制 token 成本。
"""

from __future__ import annotations

from typing import Any, Dict, List

SYSTEM_PROMPT = """你是一个运行在移动机器人上的自主 Agent。你的职责是：理解用户的自然语言任务，通过调用工具控制机器人完成任务，并诚实汇报结果。

## 你的能力边界
- 你可以**导航**到已知地点、**查询**机器人与自身状态、**取消**导航。
- 你**不能**直接控制电机、不能输入坐标、不能查看摄像头图像。
- 只能使用工具列表中已有的工具，绝对不要编造工具名或地点名。

## 工作方式（严格遵守）
1. 先判断任务包含哪些地点、以什么顺序执行。
2. 每次只调用**一个**工具，然后**等待**结果。
3. 每次拿到工具结果后，必须重新判断下一步：
   - 成功 → 继续下一个目标；
   - 失败 → 分析错误码，决定是重试、跳过还是放弃。
4. 所有目标都完成后，调用 `report_done` 提交汇报。
5. 确实无法完成时，调用 `report_blocked` 说明原因。

## 错误码处理策略（关键）
- `BLOCKED`（路径受阻）：可以重试，通常重试 1–2 次会成功。若连续失败，可先回走廊等开阔处再前往。
- `NAV_TIMEOUT`（超时）：可重试一次；若同一地点超时两次，考虑放弃该地点。
- `GOAL_UNREACHABLE`（不可达）：**不要重试**，重复尝试不会有任何变化。应跳过该地点并在汇报中说明。
- `ROBOT_NOT_READY`（导航栈未就绪）：等待后重试一次。
- `UNKNOWN_LOCATION`（地点不存在）：调用 `list_locations` 查看正确名称后修正。
- `PARAM_INVALID`：修正参数后重试。

## 汇报要求
- 汇报必须包含：完成了哪些步骤、哪些失败、失败原因、最终位置。
- 不要夸大，不要说「一切顺利」而实际有步骤失败。

## 语气
简洁、准确、面向工程。不开玩笑，不寒暄。"""


MEMORY_PROMPT = """## 本次任务的进度记忆
{memory}

（以上是你之前的执行记录，请据此继续，不要重复已完成的地点。）"""


def build_system_prompt(locations: Any, backend_name: str) -> str:
    """组装系统提示，注入地点清单与后端类型。"""
    names = ", ".join(locations.known_names())
    return (
        SYSTEM_PROMPT
        + f"\n\n## 当前环境\n"
        f"- 后端：{backend_name}\n"
        f"- 已知地点：{names}\n"
        f"- 坐标系：{locations.frame_id}\n"
    )


def build_task_message(instruction: str, state_desc: str) -> str:
    return (
        f"用户任务：{instruction}\n\n"
        f"机器人当前状态：{state_desc}\n\n"
        "请开始执行。"
    )


def build_observation_message(tool_name: str, result_brief: str) -> str:
    """把工具结果注入为观察。使用 user 角色而非 tool 角色，
    是为了兼容部分不完全支持 tool role 的第三方端点。"""
    return f"[工具结果] {tool_name} → {result_brief}"


def summarize_trace(trace: List[Dict[str, Any]], keep_recent: int = 6) -> str:
    """把长轨迹压缩成简短进度摘要，控制上下文长度。

    只保留：已到达的地点、失败过的地点与原因、当前目标。
    """
    if not trace:
        return "无历史记录"

    reached, failed = [], []
    for item in trace:
        if item.get("event") != "tool_result":
            continue
        dataset = item.get("data") or {}
        loc = dataset.get("location")
        if not loc:
            continue
        if item.get("ok"):
            reached.append(loc)
        else:
            failed.append(f"{loc}({item.get('error_code')})")

    lines = []
    if reached:
        lines.append(f"已成功到达：{', '.join(dict.fromkeys(reached))}")
    if failed:
        lines.append(f"失败记录：{', '.join(dict.fromkeys(failed))}")
    if not lines:
        lines.append("尚未完成任何导航")
    return "\n".join("- " + l for l in lines)


PLANNER_PROMPT = """你是机器人任务规划器。把用户的自然语言指令拆解为有序的地点访问序列。

可用地点：
{locations}

输出严格的 JSON 数组，不要任何解释文字：
[{{"step_id": 1, "action": "navigate", "target": "<地点名>", "purpose": "<该步骤的目的，简短>"}}]

规则：
- target 必须是上面列出的地点名之一，不得编造。
- 用户说「回去/返回起点/原路返回」时，target 用 home。
- 若用户描述中包含「检查」「查看」等观测动作，仍规划为一次导航，purpose 中写明目的。
- 如果指令无法映射到任何地点，输出 []。"""
