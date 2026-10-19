# four_car_sim → llm_robot_agent 合并说明

## 背景

WSL 里原本存在独立的 `four_car_sim` 包（`~/catkin_ws/src/four_car_sim/`），
而 GitHub 仓库里的 launch 文件通过 `$(find four_car_sim)` 引用它，
但该包从未被提交到 Git，导致**仓库克隆下来后无法运行**。

本次合并把 `four_car_sim` 的所有有效资产并入 `llm_robot_agent`，
使仓库自包含：**clone 后 catkin_make 即可用，无外部包依赖**。

## 合并映射表

| four_car_sim 原路径 | 合并后 | 处理 |
|---|---|---|
| `urdf/advanced_robot.urdf` | `urdf/advanced_robot.urdf` | 已存在，内容一致（仅换行符差异） |
| `worlds/complex_indoor.world` | `worlds/complex_indoor.world` | 已存在，内容一致 |
| `urdf/car.urdf` | `urdf/car.urdf` | **新增**（基础小车，仅相机无雷达） |
| `urdf/obstacles.urdf` | `urdf/obstacles.urdf` | **新增** |
| `worlds/car_world.world` | `worlds/car_world.world` | **新增**（3 个障碍物场景） |
| `launch/car.launch` | `launch/car.launch` | **新增**，重写为标准形式并参数化 |
| `launch/car_gazebo.launch` | `launch/car_gazebo.launch` | **新增**，修复两处缺陷（见下） |
| `launch/advanced.launch` | `launch/gazebo.launch` | 已有等价实现，功能覆盖 |
| `launch/advanced_robot.launch` | `launch/gazebo.launch` | 已有等价实现，功能覆盖 |
| `package.xml` | `package.xml` | 已重写，依赖更完整 |
| `CMakeLists.txt` | `CMakeLists.txt` | 已重写，含 install 规则 |

## 修复的缺陷

**1. `car_gazebo.launch` 硬编码绝对路径**

```xml
<!-- 修复前：换一台机器必然失败 -->
<arg name="world_name" value="/home/zzt/catkin_ws/src/four_car_sim/worlds/car_world.world"/>

<!-- 修复后 -->
<arg name="world_name" value="$(find llm_robot_agent)/worlds/car_world.world"/>
```

**2. `car_gazebo.launch` 使用了错误的节点类型**

```xml
<!-- 修复前：spawn_entity.py 不是 gazebo_ros 的有效 type -->
<node name="spawn_car" pkg="gazebo_ros" type="spawn_entity.py" .../>

<!-- 修复后 -->
<node name="spawn_car" pkg="gazebo_ros" type="spawn_model" .../>
```

**3. `car.urdf` 的 `leftJoint` 重复声明**

原 `car.launch` 中 4 个轮子关节各自独立命名，与 `advanced_robot.urdf`
的「左右各一个 joint 名、多轮共享」写法不同。两者不能互换配置，
因此 `car_world` 场景**仅用于阶段一验证**，不能用于阶段二导航取证。

## 关键区分：两个机器人模型不可混用

| 项 | `car.urdf` | `advanced_robot.urdf` |
|---|---|---|
| 轮子 | 4 个独立 joint | 左右各 1 个（多轮共享） |
| 激光雷达 | **无** | 有（`/scan`） |
| 相机 | 有 | 有 |
| 用途 | 阶段一「车能开动」 | 阶段二建图与导航 |

**结论：建图、导航、Agent 验证必须使用 `advanced_robot`。**
`car` 没有激光，`gmapping` 和 `amcl` 都无法工作。

## 使用方式

```bash
cd ~/catkin_ws
# 1. 用合并后的包替换旧包（先备份）
mv src/four_car_sim src/four_car_sim.old
cp -r /path/to/llm-robot-agent/robot_ws/src/llm_robot_agent src/

# 2. 重新编译
catkin_make
source devel/setup.bash

# 3. 验证：两个场景都能启动
roslaunch llm_robot_agent car.launch            # 基础小车
roslaunch llm_robot_agent gazebo.launch         # 高级机器人（带雷达）
```

## 后续建议

合并完成后，`four_car_sim.old` 可以删除。`src_backup/` 目录同理，
其中只保留了 `car.urdf` 的早期版本，已被当前版本覆盖。

---

## 附：LLM 接入链路修复（2026-10-19 晚）

排查「API 明明配过却仍走离线模式」时定位到 4 处独立缺陷，均会导致链路静默降级：

| # | 缺陷 | 表现 | 修复 |
|---|---|---|---|
| 1 | `agent.yaml` 硬编码 `base_url`/`model` | `.env` 里的网关配置被永久覆盖，请求打到官方域名 | 两个字段默认留空，让环境变量生效；`create_llm` 把空串视为「未配置」 |
| 2 | `_load_dotenv` 只查包内 `config/.env` | 仓库根 `config/.env` 被忽略 | 增加 4 级查找，含仓库根 |
| 3 | `LLMClient` 不读 `DEEPSEEK_MODEL` | `.env` 按文档写的 `DEEPSEEK_MODEL` 不生效，退回 `deepseek-chat` | 优先级链加入 `DEEPSEEK_MODEL` |
| 4 | `run()` 只返回 `message` | 调用方按 `summary` 取值会 `KeyError` | 双键并存 |

**诊断口诀**：启动横幅打印的 `LLM=LLMClient` / `LLM=OfflinePlanner` 就是唯一判据。
看到 `OfflinePlanner` 时不必猜，直接按上表四层依次排查。
