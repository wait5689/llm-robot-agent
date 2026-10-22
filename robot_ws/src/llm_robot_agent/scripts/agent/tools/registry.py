#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具注册表（Tool Registry）
===========================

Agent 的「手」：把后端能力包装成 LLM 可调用、且经过校验的函数。

关键设计
--------
1. **JSON Schema 化**：每个工具自带 OpenAI function-calling 规范的 schema，
   由模型生成参数，由本地代码校验参数——不信任模型输出的类型。
2. **统一返回**：任何工具都返回 `ToolResult`，绝不抛异常给上层循环。
3. **后端可替换**：工具只依赖 `RobotBackend` 抽象接口，
   因此同一套工具既能驱动 Gazebo，也能驱动内存里的 Mock 机器人。
4. **安全阀门**：`navigate_to` 只接受语义地点名，坐标由 `locations.yaml` 决定，
   模型无法生成任意坐标把机器人开进墙里。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from ..contracts import ErrorCode, ToolResult

log = logging.getLogger(__name__)


class ToolSpec:
    """单个工具的定义。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        handler: Callable[..., ToolResult],
        required: Optional[List[str]] = None,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.handler = handler
        self.required = required or []

    def to_openai_schema(self) -> Dict[str, Any]:
        """转换成 OpenAI / DeepSeek function-calling 格式。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }


class ToolRegistry:
    """工具集合。负责 schema 导出、参数校验、调用与耗时统计。"""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self.call_count = 0

    # ---------------- 注册 ----------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"工具重复注册: {spec.name}")
        self._tools[spec.name] = spec
        log.debug("注册工具 %s", spec.name)

    def names(self) -> List[str]:
        return list(self._tools.keys())

    def schemas(self) -> List[Dict[str, Any]]:
        return [t.to_openai_schema() for t in self._tools.values()]

    # ---------------- 调用 ----------------
    def invoke(self, name: str, arguments: Dict[str, Any]) -> ToolResult:
        """按名称调用工具。

        对模型幻觉出的工具名、错误参数类型、执行期异常做统一兜底，
        保证调用方永远拿到 `ToolResult` 而不是炸掉的栈。
        """
        started = time.time()

        spec = self._tools.get(name)
        if spec is None:
            return ToolResult(
                ok=False,
                observation=f"工具 '{name}' 不存在。可用工具：{', '.join(self.names())}",
                error_code=ErrorCode.TOOL_NOT_FOUND,
                retryable=True,
                latency_ms=int((time.time() - started) * 1000),
            )

        if not isinstance(arguments, dict):
            return ToolResult(
                ok=False,
                observation=f"参数必须是 JSON 对象，收到 {type(arguments).__name__}",
                error_code=ErrorCode.PARAM_INVALID,
                retryable=True,
            )

        missing = [p for p in spec.required if p not in arguments]
        if missing:
            return ToolResult(
                ok=False,
                observation=f"缺少必填参数：{', '.join(missing)}",
                error_code=ErrorCode.PARAM_INVALID,
                retryable=True,
            )

        # 按 schema 校验类型与 enum。
        #
        # 只查 required 是不够的：schema 里明明写了
        # "enum": ["room_a", ...]，但模型完全可能返回
        # {"location": 3} 或拼错的地点名，这些会一路传到 handler。
        # 更糟的是 Python 的宽松性让 {"timeout_sec": "30"} 也能跑，
        # 直到深处才炸出难懂的错误。
        #
        # 在工具边界挡住，并给出**可操作的**提示（列出合法取值），
        # 模型看到 observation 就能自己改对，而不是盲试。
        bad = self._validate_arguments(spec, arguments)
        if bad:
            return ToolResult(
                ok=False,
                observation=bad,
                error_code=ErrorCode.PARAM_INVALID,
                retryable=True,
            )

        self.call_count += 1
        try:
            result = spec.handler(**arguments)
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, observation=str(result))
        except TypeError as exc:
            # 参数名或类型不匹配，通常是模型幻觉所致
            result = ToolResult(
                ok=False,
                observation=f"参数不匹配：{exc}",
                error_code=ErrorCode.PARAM_INVALID,
                retryable=True,
            )
        except Exception as exc:  # noqa: BLE001 - 工具边界必须兜住一切
            log.exception("工具 %s 执行异常", name)
            result = ToolResult(
                ok=False,
                observation=f"工具执行异常：{exc}",
                error_code=ErrorCode.BACKEND_ERROR,
                retryable=True,
            )

        result.latency_ms = int((time.time() - started) * 1000)
        return result

    @staticmethod
    def _validate_arguments(spec: "ToolSpec",
                            arguments: Dict[str, Any]) -> Optional[str]:
        """按工具 schema 校验参数，返回错误说明；通过则返回 None。

        只处理本项目的实际需求（string / number / integer / boolean /
        array），不追求完整的 JSON Schema 实现。

        关于 `enum`：这里**不**校验它。
        原因是 `navigate_to` 的 enum 只列了规范地点名，而本项目
        有意支持别名（`a` / `房间a` / `去房间A` / `Room_A` 等），
        由 `LocationMap.resolve()` 负责解析。若在 schema 层按字面量
        比对 enum，就会把这些合法写法全部误杀。

        所以 enum 只作为「给模型看的提示」写进 schema，
        真正的合法性判断由 handler 里的 resolve() 完成
        （解析失败会返回 UNKNOWN_LOCATION 并列出可用地点）。
        """
        for key, value in arguments.items():
            rule = spec.parameters.get(key)
            if not isinstance(rule, dict):
                # schema 未声明的多余参数：不拒绝（forward compatible），
                # 由 handler 的签名检查兜底。
                continue

            expected = rule.get("type")
            if expected is None or value is None:
                continue

            if not ToolRegistry._type_ok(expected, value):
                return (
                    f"参数 '{key}' 类型错误：期望 {expected}，"
                    f"实际收到 {type(value).__name__}（值 {value!r}）。"
                )
        return None

    @staticmethod
    def _type_ok(expected: str, value: Any) -> bool:
        """只做类型判断，不做隐式转换。"""
        if expected == "string":
            return isinstance(value, str)
        if expected == "boolean":
            # bool 必须先判，否则 True 会被 int 分支接受
            return isinstance(value, bool)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "array":
            return isinstance(value, list)
        if expected == "object":
            return isinstance(value, dict)
        return True  # 未知类型不作限制

    def invoke_json(self, name: str, raw_arguments: str) -> ToolResult:
        """处理模型返回的原始 JSON 字符串参数（可能带语法错误）。"""
        if not raw_arguments or not raw_arguments.strip():
            return self.invoke(name, {})
        try:
            args = json.loads(raw_arguments)
        except json.JSONDecodeError:
            # 常见退化情况：模型包了一层 ```json 或补了说明文字
            cleaned = raw_arguments.strip().strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
            try:
                args = json.loads(cleaned)
            except json.JSONDecodeError as exc:
                return ToolResult(
                    ok=False,
                    observation=f"参数 JSON 解析失败：{exc}",
                    error_code=ErrorCode.PARAM_INVALID,
                    retryable=True,
                )
        return self.invoke(name, args)


# --------------------------------------------------------------------------
# 工具构建器：把一个 RobotBackend 包装成完整的工具集
# --------------------------------------------------------------------------
def build_default_tools(backend: Any, locations: Any) -> ToolRegistry:
    """构建 Agent 的标准工具集。

    参数
    ----
    backend : RobotBackend
        实现 navigate_to / cancel_nav / get_state / list_locations 的对象。
    locations : LocationMap
        语义地点表，用于校验地点名并做别名解析。
    """

    registry = ToolRegistry()

    # ---------------- 导航 ----------------
    location_names = locations.known_names()
    location_desc = "；".join(
        f"{n}（{locations.describe(n)}）" for n in location_names
    )

    def _navigate_to(location: str, timeout_sec: Optional[float] = None) -> ToolResult:
        canonical = locations.resolve(location)
        if canonical is None:
            return ToolResult(
                ok=False,
                observation=(
                    f"未知地点 '{location}'。当前可去的地点：{', '.join(location_names)}"
                ),
                error_code=ErrorCode.UNKNOWN_LOCATION,
                retryable=True,
            )
        return backend.navigate_to(canonical, timeout_sec=timeout_sec)

    registry.register(
        ToolSpec(
            name="navigate_to",
            description=(
                "导航机器人前往一个具名地点，阻塞直到到达或失败。"
                "这是移动机器人的主要动作，只能使用已知的地点名。"
            ),
            parameters={
                "location": {
                    "type": "string",
                    "enum": location_names,
                    "description": f"目标地点名。可选值：{location_desc}",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "本次导航的超时秒数，省略则使用默认值",
                },
            },
            required=["location"],
            handler=_navigate_to,
        )
    )

    # ---------------- 状态查询 ----------------
    def _get_robot_state() -> ToolResult:
        state = backend.get_state()
        return ToolResult(
            ok=True,
            observation=f"机器人当前状态：{state.describe()}",
            data=state.to_dict(),
        )

    registry.register(
        ToolSpec(
            name="get_robot_state",
            description="读取机器人当前位姿、导航状态与当前目标。用于确认自己的处境。",
            parameters={},
            handler=_get_robot_state,
        )
    )

    # ---------------- 地点清单 ----------------
    def _list_locations() -> ToolResult:
        items = [
            {"name": n, "description": locations.describe(n)} for n in location_names
        ]
        return ToolResult(
            ok=True,
            observation="已知地点：" + "；".join(
                f"{i['name']}：{i['description']}" for i in items
            ),
            data={"locations": items},
        )

    registry.register(
        ToolSpec(
            name="list_locations",
            description="列出所有可以导航前往的地点及其说明。不确定去哪时先调用它。",
            parameters={},
            handler=_list_locations,
        )
    )

    # ---------------- 取消导航 ----------------
    def _cancel_navigation() -> ToolResult:
        return backend.cancel_nav()

    registry.register(
        ToolSpec(
            name="cancel_navigation",
            description="立即取消当前正在进行的导航任务并让机器人停下。",
            parameters={},
            handler=_cancel_navigation,
        )
    )

    # ---------------- 任务完成信号 ----------------
    def _report_done(summary: str) -> ToolResult:
        return ToolResult(
            ok=True,
            observation="任务已标记为完成。",
            data={"summary": summary, "final": True},
        )

    registry.register(
        ToolSpec(
            name="report_done",
            description=(
                "所有目标都已达成时调用，提交最终汇报。"
                "调用后本次任务结束，不要再调用其他工具。"
            ),
            parameters={
                "summary": {
                    "type": "string",
                    "description": "给用户的最终汇报，说明完成了哪些步骤、结果如何",
                }
            },
            required=["summary"],
            handler=_report_done,
        )
    )

    # ---------------- 主动放弃 ----------------
    def _report_blocked(reason: str) -> ToolResult:
        return ToolResult(
            ok=True,
            observation="任务已标记为失败并终止。",
            data={"reason": reason, "final": True, "success": False},
        )

    registry.register(
        ToolSpec(
            name="report_blocked",
            description=(
                "确实无法完成任务时调用（例如连续多次导航失败、目标不可达）。"
                "必须在已经尝试过合理恢复手段之后才使用。"
            ),
            parameters={
                "reason": {
                    "type": "string",
                    "description": "无法完成的具体原因，以及已尝试过的补救措施",
                }
            },
            required=["reason"],
            handler=_report_blocked,
        )
    )

    return registry
