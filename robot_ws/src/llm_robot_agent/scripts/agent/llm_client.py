#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM 客户端（LLM Client）
========================

面向 Agent 循环设计的模型客户端，具备三个生产级特性：

1. **多提供商**：DeepSeek / OpenAI 兼容端点 / Anthropic，接口统一。
2. **工具调用标准化**：把各家格式归一为 `{"id","name","arguments"}`。
3. **离线回退**：没有 API Key 时自动切到 `OfflinePlanner`（规则引擎），
   保证项目在任何机器上都能演示，这一点对答辩和面试非常关键。

安全提示：API Key 只从环境变量读取，不写入日志。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 消息与响应
# --------------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # 原始 JSON 字符串


@dataclass
class LLMResponse:
    ok: bool
    content: str = ""
    tool_calls: List[ToolCall] = None  # type: ignore[assignment]
    error: str = ""
    latency_ms: int = 0
    usage: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


# --------------------------------------------------------------------------
# 主客户端
# --------------------------------------------------------------------------
class LLMClient:
    """OpenAI 兼容协议的通用客户端（DeepSeek 默认）。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        # 显式参数 > LLM_* > DEEPSEEK_* >
        # .env 约定的 DEEPSEEK_MODEL（2026-10 新增，见 config/.env.example）> 默认值
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or os.getenv("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com/v1"
        ).rstrip("/")
        self.model = (
            model
            or os.getenv("LLM_MODEL")
            or os.getenv("DEEPSEEK_MODEL")
            or "deepseek-chat"
        )
        self.temperature = temperature
        self.timeout = timeout
        self.max_retries = max_retries

    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        if not self.api_key:
            return LLMResponse(ok=False, error="未配置 API Key")

        try:
            import requests
        except ImportError:
            return LLMResponse(ok=False, error="缺少 requests 库")

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        started = time.time()
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                resp.raise_for_status()
                data = resp.json()
                return self._parse_response(data, int((time.time() - started) * 1000))
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))

        return LLMResponse(
            ok=False,
            error=last_error,
            latency_ms=int((time.time() - started) * 1000),
        )

    @staticmethod
    def _parse_response(data: Dict[str, Any], latency_ms: int) -> LLMResponse:
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError):
            return LLMResponse(ok=False, error=f"响应格式异常: {str(data)[:200]}", latency_ms=latency_ms)

        message = choice.get("message", {})
        calls: List[ToolCall] = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function", {})
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments") or "{}",
                )
            )

        return LLMResponse(
            ok=True,
            content=message.get("content") or "",
            tool_calls=calls,
            latency_ms=latency_ms,
            usage=data.get("usage"),
        )


# --------------------------------------------------------------------------
# 离线规划器：无 API Key 时的确定性回退
# --------------------------------------------------------------------------
class OfflinePlanner:
    """基于规则的极简规划器。

    它不是「假 LLM」，而是有意做成**行为可预期**的兜底路径：
    在断网、无 Key、或 API 故障时仍然能演示「多地点连续任务 + 失败重试」。

    支持的表达：
      「去A，然后去B，最后回起点」「前往房间C，遇到障碍就重新规划」
    """

    # 中文/英文地点词 -> 规范地点名（与 locations.yaml 保持一致的子集）
    LOC_PATTERNS = [
        (r"房间\s*[aA]|room\s*_?a|\bab\b", "room_a"),
        (r"房间\s*[bB]|room\s*_?b", "room_b"),
        (r"房间\s*[cC]|room\s*_?c", "room_c"),
        (r"房间\s*[dD]|room\s*_?d", "room_d"),
        (r"起点|出发点|大本营|返回出发|回到起点|home", "home"),
        (r"走廊|corridor|中央", "corridor"),
    ]

    def plan(self, instruction: str, locations: Any) -> List[Dict[str, Any]]:
        """把指令切成有序的地点序列，生成导航步骤。"""
        segments = re.split(r"然后|接着|之后|再|最后|，|,|;|；|\bthen\b|\bafter\b", instruction)
        steps: List[Dict[str, Any]] = []
        seen: Dict[str, int] = {}

        for seg in segments:
            if not seg.strip():
                continue
            loc = self._extract_location(seg, locations)
            if loc:
                seen[loc] = seen.get(loc, 0) + 1
                steps.append(
                    {
                        "step_id": len(steps) + 1,
                        "action": "navigate",
                        "target": loc,
                        "purpose": seg.strip()[:40],
                    }
                )

        # 指代消解：「A 点」「B 点」这类只给字母的说法
        if not steps:
            for letter in re.findall(r"[A-Da-d]\s*点", instruction):
                loc = locations.resolve(letter[0])
                if loc:
                    steps.append(
                        {
                            "step_id": len(steps) + 1,
                            "action": "navigate",
                            "target": loc,
                            "purpose": f"{letter} 点",
                        }
                    )
        return steps

    def _extract_location(self, segment: str, locations: Any) -> Optional[str]:
        for pattern, name in self.LOC_PATTERNS:
            if re.search(pattern, segment):
                resolved = locations.resolve(name)
                if resolved:
                    return resolved
        # 兜底：直接把片段交给地点表解析
        return locations.resolve(segment.strip())

    # 让 OfflinePlanner 与 LLMClient 在调用侧可互换
    def available(self) -> bool:
        return True


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------
def create_llm(cfg: Optional[Dict[str, Any]] = None, offline_fallback: bool = True):
    """按配置创建 LLM；无 Key 时返回 OfflinePlanner。

    返回值统一具备 `.available()`，但只有 LLMClient 有 `.chat()`。
    Agent 通过 `isinstance` 或 `hasattr` 判断走哪条路径。
    """
    cfg = cfg or {}
    key_env = cfg.get("api_key_env", "DEEPSEEK_API_KEY")
    api_key = cfg.get("api_key") or os.getenv(key_env) or os.getenv("DEEPSEEK_API_KEY")

    if not api_key:
        if offline_fallback:
            log.warning(
                "未检测到 %s，启用离线规则规划器（能力受限但可完整演示）。", key_env
            )
            return OfflinePlanner()
        raise RuntimeError(f"缺少环境变量 {key_env}")

    # 空字符串 / None 一律视为「未配置」，交由环境变量决定
    base_url = (cfg.get("base_url") or "").strip() or None
    model = (cfg.get("model") or "").strip() or None

    return LLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=float(cfg.get("temperature", 0.1)),
        timeout=float(cfg.get("timeout_sec", 60)),
        max_retries=int(cfg.get("max_retries", 2)),
    )
