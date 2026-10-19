# 架构说明

本文档解释系统各层的职责边界与关键设计取舍，供代码评审与答辩使用。

## 一、分层

```
┌──────────────────────────────────────────────────────────────┐
│ 接入层                                                        │
│   agent_run.py (CLI)      web_server.py (Flask)              │
└──────────────────────────┬───────────────────────────────────┘
                           │ 调用 agent.run(instruction) -> dict
┌──────────────────────────┴───────────────────────────────────┐
│ Agent 层（core.py）                                           │
│   ReAct 循环：LLM 决策 → 工具调用 → 观察注入 → 再决策          │
│   职责：编排、终止判定、成本控制、轨迹记录                      │
└──────────────────────────┬───────────────────────────────────┘
                           │ ToolRegistry.invoke(name, args)
┌──────────────────────────┴───────────────────────────────────┐
│ 工具层（tools/）                                              │
│   registry.py  参数校验、schema 导出、异常隔离、耗时统计        │
│   locations.py 语义名称 → 坐标（人维护的地图，模型不可修改）     │
│   职责：把「模型输出」转换成「可信的函数调用」                  │
└──────────────────────────┬───────────────────────────────────┘
                           │ RobotBackend 接口
┌──────────────────────────┴───────────────────────────────────┐
│ 后端层（backends/）                                           │
│   base.py          抽象接口（唯一契约）                        │
│   mock_backend.py  内存模拟 + 故障注入                         │
│   ros_backend.py   move_base Action + AMCL + TF               │
│   职责：屏蔽物理平台差异                                      │
└──────────────────────────────────────────────────────────────┘
```

**依赖方向严格单向向下。** 上层不知道下层的实现细节，
下层不知道上层的存在。这是「换后端零改动」的前提。

## 二、关键数据流

一次 `navigate_to("房间A")` 的完整链路：

```
1. 模型输出工具调用
   {"name": "navigate_to", "arguments": "{\"location\": \"房间A\"}"}

2. ToolRegistry.invoke_json 解析 JSON，失败则返回 PARAM_INVALID（不抛异常）

3. ToolSpec.handler 执行 _navigate_to
   ├─ locations.resolve("房间A") -> "room_a"      ← 别名解析
   ├─ 解析失败 -> UNKNOWN_LOCATION + 可用列表
   └─ 成功 -> backend.navigate_to("room_a")

4. MockBackend / RosBackend 执行导航
   ├─ 检查就绪状态      -> ROBOT_NOT_READY
   ├─ 查表取坐标        -> 无坐标则 UNKNOWN_LOCATION
   ├─ 发送目标并阻塞等待
   └─ 判定结果:
        SUCCEEDED    -> ok=True
        位姿在容差内  -> ok=True (approx)
        ABORTED      -> GOAL_UNREACHABLE, retryable=False
        超时          -> NAV_TIMEOUT, retryable=True
        长时间无进展  -> BLOCKED, retryable=True

5. ToolResult 返回上层
   ├─ brief() 写入 LLM 上下文（控制 token）
   └─ data 写入 trace（用于汇报与复盘）
```

## 三、为什么不用 OpenAI 官方 SDK

`llm_client.py` 直接用 `requests` 调 `/chat/completions`，原因有三：

1. **端点兼容性**：需要支持任意 OpenAI 兼容网关，官方 SDK 的 base_url 行为受限。
2. **错误可见性**：HTTP 状态码、重试次数、延迟需要自己掌控并记录到 trace。
3. **依赖轻量**：ROS 环境里装 SDK 容易与系统 Python 冲突。

代价是需要自己维护消息格式，但格式本身是稳定的协议，风险可控。

## 四、ReAct 的终止条件

一个 Agent 循环最容易被忽视的是「何时停」。本系统有四道闸门：

| 闸门 | 触发条件 | 返回值 |
|---|---|---|
| 正常完成 | 模型调用 `report_done` | success=True |
| 主动放弃 | 模型调用 `report_blocked` | success=False |
| 预算耗尽 | `turns > max_turns` 或超时 | BUDGET_EXCEEDED |
| 失败熔断 | 连续 5 次工具失败 | 中止 + 说明 |

缺任何一条，模型都可能无限循环或长时间空转。

## 五、测试策略

31 项测试分五组，覆盖从单元到集成的完整梯度：

| 组 | 数量 | 验证目标 |
|---|---|---|
| LocationMap | 6 | 别名、口语、非法输入 |
| ToolRegistry | 8 | schema、缺参、幻觉工具、异常隔离 |
| MockBackend | 6 | 四种故障语义 + 统计 |
| AgentOffline | 5 | 端到端重试、跳过、收敛 |
| AgentReact | 6 | 观察回流、失败反馈、轮数上限、幻觉恢复 |

其中 `test_observation_is_fed_back` 是最关键的一条：
它直接断言「工具结果出现在下一轮的模型上下文里」。
如果 ReAct 退化回一次性规划，这条测试会立刻失败。

## 六、已知技术债

1. `ros_backend.py` 的完成判定依赖轮询 `get_state()` + 位姿误差，
   而非 Action 的 `done_cb` 回调。功能正确，但在高频场景下不够优雅。
2. `locations.yaml` 坐标需人工校准，尚无自动从 SLAM 地图提取房间质心的能力。
3. 上下文压缩是启发式的（保留最近 N 条），未做语义摘要。
4. Web 界面无鉴权，仅适用于本地演示。
