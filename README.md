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
│   │   ├── gazebo.launch               # 阶段一：高级机器人 + 多房间世界
│   │   ├── car.launch                  # 阶段一：基础小车 + 障碍物场景
│   │   ├── car_gazebo.launch           # 同上，命令式生成版本
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
│   ├── urdf/
│   │   ├── advanced_robot.urdf         # 四轮差速 + 激光 + 相机（导航用这个）
│   │   ├── car.urdf                    # 基础小车，仅相机无雷达
│   │   └── obstacles.urdf
│   ├── worlds/
│   │   ├── complex_indoor.world        # 21 个模型的多房间场景
│   │   └── car_world.world             # 3 个障碍物的基础场景
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
└── docs/
    ├── ARCHITECTURE.md                 # 架构说明
    └── MERGE_NOTES.md                  # four_car_sim 合并记录
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
# 编辑 config/.env，按 A 或 B 两种接法之一填写
```

**接法 A（DeepSeek 官方）**

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
```

**接法 B（OpenAI 兼容网关，本项目实测可用）**

```ini
DEEPSEEK_API_KEY=<网关下发的 Key>
DEEPSEEK_BASE_URL=https://amr-link.open-design.ai/v1
DEEPSEEK_MODEL=deepseek-v4.1-flash
```

配置生效的判定方式——启动横幅会直接打印实际使用的类：

```
[配置] 后端=MockBackend  LLM=LLMClient        ← 已接上真实模型
[配置] 后端=MockBackend  LLM=OfflinePlanner   ← 仍走降级规则引擎
```

`.env` 的查找顺序（先命中者优先，后命中只补齐缺失项）：

1. `robot_ws/src/llm_robot_agent/config/.env`（catkin 安装后的规范位置）
2. `robot_ws/src/llm_robot_agent/.env`
3. `<仓库根>/config/.env`
4. `<仓库根>/.env`

变量优先级：显式构造参数 > `LLM_*` > `DEEPSEEK_*`。
`config/agent.yaml` 中 `llm.base_url` / `llm.model` **默认为空**，
此时由环境变量决定；一旦在 YAML 里写了值，就会强制覆盖 `.env`。

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

# 阶段一：能看到机器人（两个场景任选）
roslaunch llm_robot_agent gazebo.launch        # 高级机器人 + 多房间
roslaunch llm_robot_agent car.launch           # 基础小车 + 障碍物
rosrun teleop_twist_keyboard teleop_twist_keyboard.py

# 阶段二：自主导航
roslaunch llm_robot_agent navigation.launch

# 阶段三：全系统
roslaunch llm_robot_agent full_system.launch \
    task:="去房间A，然后去房间B，最后回起点"
```

> **重要**：ROS 1 Noetic 官方仅支持 Ubuntu 20.04。README 早期版本写的
> 「Ubuntu 22.04 + ROS 1」是矛盾的组合，请按本说明使用 20.04。
> 若要上 22.04，应整体迁移到 ROS 2 Humble + Nav2。

### 3.5 导航系统（阶段二）

#### 3.5.1 地图

`maps/indoor.yaml` 已随仓库提供，**开箱即可跑导航**。它有两种来源：

| 方式 | 命令 | 用途 |
|---|---|---|
| 离线真值（已有） | `python3 tools/build_map.py --world worlds/complex_indoor.world --out maps/indoor` | 开发、回归、CI；秒级生成 |
| SLAM 实建（推荐验收） | 见下方 gmapping 流程 | 答辩演示；地图带真实建图误差 |

用 gmapping 重新建图（会覆盖现有地图）：

```bash
roslaunch llm_robot_agent gazebo.launch
rosrun gmapping slam_gmapping scan:=scan
rosrun teleop_twist_keyboard teleop_twist_keyboard.py   # 把每个房间走一遍
rosrun map_server map_saver -f $(rospack find llm_robot_agent)/maps/indoor
```

> 离线地图是「上帝视角抄答案」，坐标与 world 完全一致；
> SLAM 地图会有几厘米到十几厘米的累积误差，**建图后需用 RViz 微调
> `config/locations.yaml`**，否则目标点可能贴在墙上。

#### 3.5.2 一次跑完全部静态检查

```bash
python3 tools/accept_stage2.py
```

四项检查，任一失败即退出码非 0：

1. **世界可导航性** —— 每个语义地点是否真的走得到
2. **导航配置静态校验** —— launch / 参数 / 依赖引用是否自洽
3. **地图一致性** —— `indoor.pgm` 尺寸与 origin 是否覆盖全部地点
4. **A\* 路径可达性** —— 在膨胀栅格上真实求解从 home 到各房间的路径，并给出绕行系数

实测输出（2026-10-22 复测）：

```
==============================================================
阶段二验收（导航栈静态检查）
==============================================================
[PASS] 世界可导航性
        全部语义地点可从起点到达
[PASS] 导航配置静态校验
        launch / 参数 / 依赖自洽
[PASS] 地图文件
        size: 284x244
        resolution: 0.05
        origin: [-7.095, -6.095]
        world_extent: [-7.09, -6.09, 7.11, 6.11]
        locations_outside_map: []
[PASS] A* 路径可达性
        corridor   路径   0.73 m (直线  0.68 m, 绕行 1.08x)
        hall       路径   3.28 m (直线  3.10 m, 绕行 1.06x)
        room_a     路径   7.23 m (直线  6.05 m, 绕行 1.20x)
        room_b     路径   6.04 m (直线  5.42 m, 绕行 1.11x)
        room_c     路径   4.87 m (直线  4.76 m, 绕行 1.02x)
        room_d     路径   3.99 m (直线  3.95 m, 绕行 1.01x)
==============================================================
结论：全部通过，导航栈配置可用于实车/仿真验证
```

绕行系数 1.01–1.20 说明路径确实是从门洞穿过去的，而不是被误判成直线可达。
（`room_d` 为 1.01x 是正常的：它就在南区西侧，从起点过去基本是直线。）

#### 3.5.3 世界布局

`worlds/complex_indoor.world` 由 `tools/gen_world.py` 生成，**不要手工修改**：

```
        y=+5  ┌────────┬────────┬────────┐
              │ room_a │ room_c │ room_b │   北区
        y=0   ├──┬─────┴───┬────┴──┬─────┤
              │  │  门洞×3  │       │     │   主分隔墙
        y=0   ├──┴─────────┴───────┴─────┤
              │   room_d      hall       │   南区
        y=-5  └─────────────────────────┘
```

改布局 = 改 `gen_world.py` 顶部参数后重新生成，再用 `accept_stage2.py` 校验。

> **历史坑**：上一版 world 的墙体全部连通、**没有任何门洞**，四个房间是
> 四个封闭盒子。导航参数怎么调都不可能到达，现象是模型原地乱转、
> move_base 报 ABORTED。这类问题在 Gazebo 里排查要开 RViz、看 costmap、
> 翻日志，一轮十几分钟；而 `audit_world.py` 用纯几何 3 秒就能算出来。

#### 3.5.4 工具清单

| 工具 | 作用 |
|---|---|
| `tools/gen_world.py` | 按参数生成带门洞的室内世界 |
| `tools/audit_world.py` | 栅格化 + BFS，判断每个地点是否可达；输出自由栅格用于选坐标 |
| `tools/build_map.py` | 从 world 生成 ROS 地图（PGM + YAML） |
| `tools/lint_launch.py` | 静态校验 launch / YAML / CMake 引用 |
| `tools/accept_stage2.py` | 以上四者的总集成，阶段二验收入口 |


### 3.6 从旧的 four_car_sim 迁移

本包已合并原 `four_car_sim` 的全部有效资产，**不再依赖外部包**。
若你的 WSL 里还有旧包，请按 `docs/MERGE_NOTES.md` 的步骤替换：

```bash
cd ~/catkin_ws
mv src/four_car_sim src/four_car_sim.old    # 先备份，不要直接删
cp -r <本仓库>/robot_ws/src/llm_robot_agent src/
catkin_make && source devel/setup.bash
```

注意：`car.urdf` **没有激光雷达**，只能用于「车能开动」的验证；
建图与导航必须用 `advanced_robot.urdf`。

---

## 四、验收标准

| 阶段 | 验收动作 | 判定依据 | 状态 |
|---|---|---|---|
| 一、仿真环境 | `gazebo.launch` + 键盘遥控 | Gazebo 中机器人可移动 | 已验证（里程计 odom/truth 0.996~1.000） |
| 二、自主导航 | `navigation.launch` 连续 8 目标 | 真值误差 < 0.6m | **已验证：8/8 PASS**（三轮独立复现） |
| 三、LLM Agent | `full_system.launch task:="..."` | 多地点任务自主完成 | 真实模型已验证（Mock 后端） |
| 三、异常处理 | `--fault room_a:blocked` | 失败后自动重试成功 | 已验证（真实模型自主重试） |
| 四、扩展 | `web_server.py` | 浏览器完成任务并看到轨迹 | 已验证（Mock） |

### 阶段二修正记录

导航系统本次做了以下实质性修正（此前存在多处必然导致失败的缺陷）：

| # | 缺陷 | 后果 | 修正 |
|---|---|---|---|
| 1 | 世界中所有墙体连通，**没有门洞** | 四个房间是封闭盒子，任何目标都 ABORTED | 用 `gen_world.py` 重建成带门洞布局 |
| 2 | `room_c` 坐标 (0, 5.0) 落在北墙体内 | 该目标必然失败 | 坐标改为 (0, 2.15)，并建立自动校验 |
| 3 | `rviz/nav.rviz` 被引用但文件不存在 | rviz 启动即报错退出 | 补齐 RViz 配置 |
| 4 | AMCL 用官方示例 launch | 雷达量程/坐标系不匹配，定位发散 | 改用包内 `amcl_params.yaml` |
| 5 | `CMakeLists.txt` 引用不存在的 `nav_bridge.py` | `catkin_make` 直接失败 | 移除并补上 `initial_pose.py` |
| 6 | `initial_pose.py` 只发 5 次就退出 | AMCL 未就绪时消息全部丢失 | 改为等就绪后持续发布 |
| 7 | `maps/indoor.yaml` 缺失 | `navigation.launch` 无法启动 | 提供离线真值地图 + SLAM 建图说明 |

### 阶段二修正记录（续，2026-10-21）

上一轮「四项 PASS」的结论在复查时**无法复现**（实测 2/4 FAIL），根因是三处
坐标/位姿不一致。这三处都会让 Gazebo 端到端测试必然失败：

| # | 缺陷 | 后果 | 修正 |
|---|---|---|---|
| 8 | `home` 坐标 (0.80, -3.60) 正压在充电桩本体上（桩半径 0.30m） | 验收脚本用它作 A\* 起点，起点在障碍内 → **所有地点都判为不可达**；Gazebo 里机器人被刷进桩体，AMCL 初始位姿也是错的 | `home` 改为 (0.00, -3.00)，障碍间隙 0.70m |
| 9 | `hall` 坐标 (3.00, -1.20) 落进 `reception_desk`（1.6×0.6m）桌体内 | 该目标单独必然 ABORTED | 改为 (1.80, -1.20)，桌子西侧 0.40m |
| 10 | `gazebo.launch` 生成位姿 (0.0,-3.0) 与 `navigation.launch` 的 (0.8,-3.6) 不一致 | 两套 launch 说的不是同一个起点 | 统一为 (0.00, -3.00)，并在 `locations.yaml` 注明必须三者同步 |
| 11 | `accept_stage2.py` 的 `subprocess.run(text=True)` 未指定 encoding | Windows 上子进程 UTF-8 中文输出被按 GBK 解码，抛 `UnicodeDecodeError` 使验收中断 | 显式 `encoding="utf-8", errors="replace"` |

**Agent 层另有一处会导致长任务必然中断的缺陷：**

| # | 缺陷 | 后果 | 修正 |
|---|---|---|---|
| 12 | `core.py::_compress` 按固定条数从尾部切分消息 | 切点落在 `tool` 消息上，其父 `assistant(tool_calls)` 已被裁掉 → 孤立 `tool` 消息 → OpenAI 兼容接口返回 **400 Bad Request**。实测任何超过 6 步的任务必然中断 | 切点前移到完整对话单元边界（跳过孤立 `tool`），并丢弃末尾悬空的 `tool_calls`；新增 2 项回归测试 |

> 这个 bug 之所以长期未被发现，是因为验收用例「去 A 再去 B 回起点」只有 3 步，
> 恰好不触发压缩阈值（12 条）。现已用 7 地点长任务覆盖。


---

## 五、验证记录（本地实测）

**Gazebo 端到端导航验证**（连续 8 个目标，对比 Gazebo 真值，2026-10-22）：

```
corridor  st=3   3.3s target=( 0.10,-1.90) truth=( 0.20,-2.05) err=0.18 amcl_err=0.04 PASS
room_c    st=3  19.4s target=( 0.00, 2.20) truth=( 0.09, 2.02) err=0.20 amcl_err=0.04 PASS
room_a    st=3  18.1s target=(-3.90, 1.80) truth=(-3.71, 1.85) err=0.20 amcl_err=0.05 PASS
home      st=3  44.7s target=( 0.30,-2.55) truth=( 0.17,-2.37) err=0.22 amcl_err=0.05 PASS
hall      st=3  15.7s target=( 3.35,-3.10) truth=( 3.15,-3.06) err=0.20 amcl_err=0.01 PASS
room_b    st=3  21.3s target=( 3.85, 1.55) truth=( 3.80, 1.37) err=0.19 amcl_err=0.01 PASS
room_d    st=3  85.8s target=(-3.65,-2.45) truth=(-3.45,-2.25) err=0.28 amcl_err=0.12 PASS
home      st=3  15.5s target=( 0.30,-2.55) truth=( 0.12,-2.65) err=0.20 amcl_err=0.03 PASS

RESULT: 8/8 PASS
振荡中止 0 | 规划失败 0 | ERROR 行 0
```

三轮独立复现（两次冷启动 + 一次带协方差跟踪）均为 **8/8**，
真值误差稳定在 0.18~0.29m，AMCL 误差 0.01~0.12m。

两点需要如实说明：

1. **单段耗时有抖动**（同一段在 15s~86s 之间波动，且变慢的段每次不同）。
   已排查过累计漂移假设并排除：AMCL 协方差在运行中单调**下降**
   （0.0121 → 0.0002），`map→odom` 偏移稳定在 0.19~0.31m，
   单独压测 room_d 路径只需 19~28s。结论是仿真器自身的调度抖动，
   不影响成功率。

2. **偶发恢复行为**：约半数运行会在最长的那段触发 1~2 次
   保守重置或原地旋转，最终仍能到达，只是该段耗时拉长。
   日志里 `Recovery behavior will clear layer 'obstacles'`（t≈1.8s）
   是 move_base 启动时的一次性初始化，不是失败信号。

```
$ python -m pytest tests/ -q
33 passed in 1.90s

$ python demos/demo.py
  场景 1: 通过   多地点连续任务        → room_a, room_b, home 全部到达
  场景 2: 通过   遇障自动重试          → BLOCKED 后第 2 次成功，重规划 1 次
  场景 3: 通过   目标不可达            → room_c 仅尝试 1 次即跳过，其余完成
  场景 4: 通过   混合故障长任务        → 2 次重试成功，1 个跳过，重规划 2 次
```

**长任务回归验证**（7 个地点，跨过上下文压缩阈值，2026-10-21）：

```
$ python agent_run.py --backend mock \
      --task "依次去房间A、房间B、房间C、房间D、走廊、大厅，最后回起点"

[配置] 后端=MockBackend  LLM=LLMClient
--- 第 1 轮 --- navigate_to(room_a)  [OK]
...
--- 第 7 轮 --- navigate_to(home)    [OK]
--- 第 8 轮 --- report_done

耗时 53.6s | 轮数 8 | 工具调用 8 | 重规划 0
```

修复前该任务在第 7 轮崩溃（`400 Client Error: Bad Request`），
原因是压缩产生了孤立 `tool` 消息；修复后 7 个地点全部到达。

**真实 LLM 闭环验证**（`deepseek-v4.1-flash` @ `amr-link.open-design.ai`，
Mock 后端 + 故障注入，2026-10-19）：

```
$ python agent_run.py --backend mock \
      --task "去房间D检查一下，然后回起点" --fault room_d:blocked:1

[配置] 后端=MockBackend  LLM=LLMClient
--- 第 1 轮 --- [行动] navigate_to({"location": "room_d"})
                [观察] [FAIL:BLOCKED] 途中遇到障碍物……
--- 第 2 轮 --- [行动] navigate_to({"location": "room_d"})
                [观察] [OK] 已到达 room_d
--- 第 3 轮 --- [行动] navigate_to({"location": "home"})
                [观察] [OK] 已到达 home
--- 第 4 轮 --- [行动] report_done({...})

耗时 10.0s | 轮数 4 | 工具调用 4 | 重规划 1
```

关键观察：第 2 轮的重试**不是**由代码硬编码触发的，而是模型读到
`BLOCKED` 观察结果后自行决定的；第 4 轮的汇报里模型主动说明
「本机无摄像头能力，未做视觉检查」，没有虚构视觉结论。

反向验证（不可达场景，`room_a:unreachable`）：

```
耗时 7.5s | 轮数 3
navigate_to → GOAL_UNREACHABLE
get_robot_state → 确认位置未变
report_blocked → "错误码明确表示重复尝试不会改变结果，故未重试"
```

两条不对称策略均生效：`BLOCKED` 重试、`GOAL_UNREACHABLE` 立即放弃。

---

## 六、已知问题与后续工作

**当前阻塞项**

阶段二的静态验收已全部通过（`tools/accept_stage2.py`，四项 PASS），
剩余阻塞项均属「需要 ROS 运行环境」的实机环节：

| # | 阻塞项 | 影响 | 解除方式 |
|---|--------|------|----------|
| 1 | 未在 Gazebo 中端到端跑通 | 导航栈的动态行为（DWA 避障、恢复行为、AMCL 收敛）未经运行验证 | 在 Ubuntu 20.04（Noetic）中 `catkin_make` 后 `roslaunch llm_robot_agent navigation.launch`，RViz 手动给 3 个目标点 |
| 2 | 地图为离线真值图，非 SLAM 实建 | `maps/indoor.pgm` 由 world 文件直接光栅化，不含建图误差与未知区域，定位过于理想 | 跑 `slam_gmapping` 建图后替换 `maps/`，再重跑 `accept_stage2.py` |
| 3 | `locations.yaml` 未经 RViz 校准 | 坐标为世界真值，与实建地图可能有 10–20 cm 偏差；导航到达容差 0.20 m 可吸收部分误差 | 建图后在 RViz 中用 `Publish Point` 逐点核对 |

三项的关键差别：第 1 项是**能否跑起来**，第 2、3 项是**跑得准不准**。
当前 `locations.yaml` 的坐标是拿 `audit_world.py` 输出的自由栅格逐个挑的，
保证落点不在障碍物膨胀区内，因此即便直接用于第 1 项验证也不会因目标点非法而失败。

**安全提示（重要）**

`config/.env.example` 在早期提交（`1551921` / `b50d773`）中曾携带真实的 API Key，
该值已随 Git 历史留存。当前版本已替换为占位符 `your_api_key_here`。
该仓库为私有仓库，且 `config/.env` 已被 `.gitignore` 排除，日常使用中
**不要把 Key 写进任何被 git 跟踪的文件**，只放在 `config/.env` 里即可。

如需彻底清除历史中的明文，可重写 Git 历史（`git filter-repo`），
代价是全部 commit SHA 变更；当前不做此操作，避免影响已有记录。

**阶段四路线**

- 摄像头图像理解：`camera_link` 已在 URDF 中，接入多模态模型即可
- 长期记忆：`memory.summary_file` 已预留配置项
- 动态障碍：在 world 中加入可移动模型 + 局部规划器调参
- Nav2 迁移：新增 `nav2_backend.py`，实现同一 `RobotBackend` 接口

---

## 七、许可证

MIT
