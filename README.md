# 基于大语言模型的自主移动机器人 Agent 系统

**LLM-based Autonomous Mobile Robot Agent**

用自然语言下达任务，机器人在仿真环境中自主规划、导航、处理异常并汇报结果。

```
用户："去房间 A 检查一下，然后前往房间 B，最后回到起点。路上遇到障碍就重新规划。"
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│  LLM Agent（ReAct 循环）                                   │
│  观察 → 决策 → 调用工具 → 观察结果 → 再决策                  │
└───────────────────────────┬───────────────────────────────┘
                            │ ToolResult（统一错误码）
┌───────────────────────────┴───────────────────────────────┐
│  ToolRegistry（JSON Schema 校验、参数兜底、异常隔离）        │
└───────────────────────────┬───────────────────────────────┘
                            │ navigate_to / get_state / cancel
┌───────────────────────────┴───────────────────────────────┐
│  RobotBackend（可替换）                                    │
│    ├─ RosBackend   → move_base Action + AMCL + TF          │
│    └─ MockBackend  → 内存模拟，支持故障注入与确定性复现      │
└───────────────────────────┬───────────────────────────────┘
                            │
                    Gazebo 11 仿真环境
```

---

## 一、核心设计决策

### 1. 大模型不做底层控制

模型**永远无法**输出速度指令或坐标。它只能调用语义化工具
（`navigate_to("room_a")`），坐标映射由 `config/locations.yaml` 查表决定。
这是把「不可靠的生成能力」约束在「可靠查表」范围内的关键。

### 2. 后端抽象：Mock 与 ROS 等价

`RobotBackend` 是唯一的抽象层。由此获得三个实际收益：

| 收益 | 说明 |
|---|---|
| 无需 ROS 即可开发 | 全部 Agent 逻辑在 Windows/Mac 上可运行、可测试 |
| 故障可确定性复现 | 「遇到障碍」不必靠运气等它发生，用 `inject_fault` 精确触发 |
| 换平台零改动 | 迁移到 Nav2 或真机，只实现一个新的 backend 类 |

### 3. 失败是返回值，不是异常

所有工具返回 `ToolResult`，携带标准化 `error_code`。
细分错误码让模型能做**不同**决策，而不是无脑重试：

| 错误码 | 含义 | Agent 策略 |
|---|---|---|
| `BLOCKED` | 路径被临时占用 | 重试（通常 1–2 次成功） |
| `NAV_TIMEOUT` | 导航超时 | 重试一次，再失败则放弃该点 |
| `GOAL_UNREACHABLE` | 规划器无解 | **不重试**，跳过并汇报 |
| `ROBOT_NOT_READY` | 定位丢失 / 栈未就绪 | 等待后重试 |
| `UNKNOWN_LOCATION` | 地点不存在 | 调用 `list_locations` 修正 |
| `PARAM_INVALID` | 参数非法 | 修正参数重试 |

### 4. ReAct 而非一次性规划

见 `scripts/agent/core.py`。每一步工具结果都会回流到模型上下文，
模型据此重新判断。配套机制：

- **轮数上限**（`max_turns`）防止死循环烧 token
- **连续失败熔断**（5 次）防止无意义重复
- **上下文压缩**：超过 12 条消息后，历史折叠为进度摘要

---

## 二、目录结构

```
llm-robot-agent/
├── robot_ws/src/llm_robot_agent/       # ROS 1 catkin 包
│   ├── package.xml  CMakeLists.txt
│   ├── launch/
│   │   ├── gazebo.launch               # 阶段一：只看机器人和世界
│   │   ├── navigation.launch           # 阶段二：地图+AMCL+move_base
│   │   └── full_system.launch          # 阶段三：全系统 + Agent 节点
│   ├── config/
│   │   ├── locations.yaml              # 语义地点表（人维护）
│   │   ├── agent.yaml                  # Agent 运行时参数
│   │   ├── costmap_common_params.yaml  # 代价地图
│   │   ├── global_costmap_params.yaml
│   │   ├── local_costmap_params.yaml
│   │   ├── base_local_planner_params.yaml  # DWA 局部规划
│   │   └── move_base_params.yaml       # 含恢复行为
│   ├── urdf/advanced_robot.urdf        # 四轮差速 + 激光 + 相机
│   ├── worlds/complex_indoor.world     # 21 个模型的多房间场景
│   ├── maps/                           # 建图输出
│   └── scripts/
│       ├── agent_run.py                # CLI 入口
│       ├── web_server.py               # Flask 接口
│       ├── initial_pose.py             # 自动喂初始位姿给 AMCL
│       ├── templates/index.html        # 单页界面
│       └── agent/                      # ★ 核心，纯 Python，不依赖 ROS
│           ├── contracts.py            # 数据契约
│           ├── core.py                 # ReAct 主循环
│           ├── llm_client.py           # 模型客户端 + 离线规划器
│           ├── prompts.py              # 提示词
│           ├── tools/{registry,locations}.py
│           └── backends/{base,mock_backend,ros_backend}.py
├── tests/test_agent.py                 # 31 项测试
├── demos/demo.py                       # 四个演示场景
└── docs/ARCHITECTURE.md                # 架构说明
```

---

## 三、快速开始

### 3.1 无需 ROS，先跑起来（推荐从这里开始）

```bash
pip install pyyaml requests flask flask-cors pytest

# 跑测试
python -m pytest tests/ -q

# 跑四个演示场景（含故障注入）
python demos/demo.py

# 单条任务
python robot_ws/src/llm_robot_agent/scripts/agent_run.py \
    --backend mock \
    --task "去房间 A 检查一下，然后前往房间 B，最后回到起点" \
    --fault room_a:blocked
```

输出示例：

```
[行动] navigate_to(room_a) 第 1 次
[观察] [FAIL:BLOCKED] 前往 room_a 途中遇到障碍物，路径被占用，已退回安全位置并停止。
[行动] navigate_to(room_a) 第 2 次
[观察] [OK] 已到达 room_a，位置 (-3.50, 2.50)，行驶约 3.3 m。
[行动] navigate_to(room_b) 第 1 次
[观察] [OK] 已到达 room_b，位置 (3.50, 2.50)。
```

### 3.2 Web 界面

```bash
python robot_ws/src/llm_robot_agent/scripts/web_server.py --backend mock
# 浏览器打开 http://127.0.0.1:5000
```

### 3.3 接入真实 LLM

```bash
cp config/.env.example config/.env
# 编辑 config/.env，填入 DEEPSEEK_API_KEY
```

未配置 Key 时自动降级为 `OfflinePlanner`（规则引擎），
断网也能完整演示主干流程——这一点对答辩现场很关键。

### 3.4 ROS 1 环境（WSL2 / Ubuntu 20.04）

```bash
# 依赖
sudo apt install ros-noetic-desktop-full ros-noetic-move-base \
                 ros-noetic-amcl ros-noetic-map-server ros-noetic-teleop-twist-keyboard

# 编译
cd robot_ws
catkin_make
source devel/setup.bash

# 阶段一：能看到机器人
roslaunch llm_robot_agent gazebo.launch
rosrun teleop_twist_keyboard teleop_twist_keyboard.py

# 建图（阶段二前置，只需一次）
rosrun gmapping slam_gmapping scan:=scan
rosrun map_server map_saver -f $(rospack find llm_robot_agent)/maps/indoor

# 阶段二：自主导航
roslaunch llm_robot_agent navigation.launch

# 阶段三：全系统
roslaunch llm_robot_agent full_system.launch \
    task:="去房间A，然后去房间B，最后回起点"
```

> **重要**：ROS 1 Noetic 官方仅支持 Ubuntu 20.04。README 早期版本写的
> 「Ubuntu 22.04 + ROS 1」是矛盾的组合，请按本说明使用 20.04。
> 若要上 22.04，应整体迁移到 ROS 2 Humble + Nav2。

---

## 四、验收标准

| 阶段 | 验收动作 | 判定依据 | 状态 |
|---|---|---|---|
| 一、仿真环境 | `gazebo.launch` + 键盘遥控 | Gazebo 中机器人可移动 | 代码就绪，待实机验证 |
| 二、自主导航 | `navigation.launch` + RViz 点目标 | 机器人绕障到达 | 代码就绪，待建图 |
| 三、LLM Agent | `full_system.launch task:="..."` | 多地点任务自主完成 | 逻辑已验证（Mock） |
| 三、异常处理 | `--fault room_a:blocked` | 失败后自动重试成功 | 已验证 |
| 四、扩展 | `web_server.py` | 浏览器完成任务并看到轨迹 | 已验证（Mock） |

---

## 五、验证记录（本地实测）

```
$ python -m pytest tests/ -q
31 passed in 0.08s

$ python demos/demo.py
  场景 1: 通过   多地点连续任务        → room_a, room_b, home 全部到达
  场景 2: 通过   遇障自动重试          → BLOCKED 后第 2 次成功，重规划 1 次
  场景 3: 通过   目标不可达            → room_c 仅尝试 1 次即跳过，其余完成
  场景 4: 通过   混合故障长任务        → 2 次重试成功，1 个跳过，重规划 2 次
```

---

## 六、已知问题与后续工作

**当前阻塞项**

1. `maps/indoor.yaml` 尚未生成 —— 需在 ROS 环境中先建图，否则 `navigation.launch` 会失败。
2. `config/locations.yaml` 中的坐标是基于世界文件估算的，建图后需用 RViz 校准。
3. 尚未在真实 Gazebo 中端到端跑通（当前验证止于 Mock 后端）。

**阶段四路线**

- 摄像头图像理解：`camera_link` 已在 URDF 中，接入多模态模型即可
- 长期记忆：`memory.summary_file` 已预留配置项
- 动态障碍：在 world 中加入可移动模型 + 局部规划器调参
- Nav2 迁移：新增 `nav2_backend.py`，实现同一 `RobotBackend` 接口

---

## 七、许可证

MIT
