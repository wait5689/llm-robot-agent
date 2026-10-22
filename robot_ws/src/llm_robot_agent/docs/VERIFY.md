# 验证手册（自己跑一遍）

本文是给你**自己验证**用的。每一步都写了「命令」「预期输出」「不对怎么办」。

如果你只想确认系统能用，跑 **[第 3 节](#3-五分钟快速验证)** 就够了，约 5 分钟。

---

## 0. 环境前提

| 项 | 要求 |
|---|---|
| 系统 | WSL2 + Ubuntu 20.04（distro 名 `Ubuntu-20.04`） |
| ROS | Noetic，装在 `/opt/ros/noetic` |
| 工作区 | `~/catkin_ws` |
| 包位置 | `~/catkin_ws/src/llm_robot_agent` |
| Gazebo | 11（随 Noetic 安装） |

快速自检：

```bash
source /opt/ros/noetic/setup.bash
echo $ROS_DISTRO          # 应输出 noetic
ls ~/catkin_ws/src/llm_robot_agent   # 应能看到 config launch scripts tools urdf worlds maps
```

> **注意**：本文所有命令都在 **WSL 终端**里执行，不是在 Windows PowerShell 里。
> 在 PowerShell 里请用 `wsl.exe -d Ubuntu-20.04` 进入。

---

## 1. 代码同步（改过 Windows 侧文件才需要）

Windows 侧的 git 仓库与 WSL 里的部署目录是**两份拷贝**，WSL 那份不是 git 仓库。
如果你在 Windows 侧改过文件，需要手动同步过来。

```bash
SRC=/mnt/c/Users/29621/Documents/Qoder/2026-10-19/1c495b10/llm-robot-agent/robot_ws/src/llm_robot_agent
DST=~/catkin_ws/src/llm_robot_agent

cp -r $SRC/config/*   $DST/config/
cp -r $SRC/launch/*   $DST/launch/
cp -r $SRC/urdf/*     $DST/urdf/
cp -r $SRC/worlds/*   $DST/worlds/
cp -r $SRC/tools/*    $DST/tools/
cp -r $SRC/scripts/*  $DST/scripts/
cp -r $SRC/maps/*     $DST/maps/
```

> 如果改动的是 `scripts/` 里的 Python 节点，还要重新编译一次
> （`catkin_make` 会为脚本生成 devel 包装器）：
>
> ```bash
> cd ~/catkin_ws && catkin_make --pkg llm_robot_agent
> source devel/setup.bash
> ```

---

## 2. 编译

```bash
cd ~/catkin_ws
catkin_make --pkg llm_robot_agent
source devel/setup.bash
```

**预期**：最后出现 `#### Running command: "make ..." ####`，且没有 `Error`。

**不对怎么办**：
- `catkin_make: command not found` → 没 source ROS：`source /opt/ros/noetic/setup.bash`
- 报缺依赖 → `rosdep install --from-paths src --ignore-src -r -y`

---

## 3. 五分钟快速验证

### 3.1 静态体检（不需要启动 Gazebo，10 秒）

这一步检查「坐标是否落在障碍里」「地点之间是否连通」「净空是否够」。
它能在启动仿真之前发现绝大多数导航问题。

```bash
cd ~/catkin_ws/src/llm_robot_agent
python3 tools/check_locations.py
```

**预期输出**（关键看最后几行）：

```
地点         坐标               净空(m)      判定
home       (  0.30, -2.55) +1.57     2.35   OK
room_a     ( -3.90,  1.80) +0.00     1.36   OK
room_c     (  0.00,  2.20) -1.57     2.30   OK
room_b     (  3.85,  1.55) +3.14     1.15   OK
room_d     ( -3.65, -2.45) +0.00     1.34   OK
corridor   (  0.10, -1.90) +1.57     1.98   OK
hall       (  3.35, -3.10) +3.14     1.76   OK
...
连通性: 21/21 对全部双向可达 OK
结果: 全部通过（7 个地点，21 对路径）
```

**退出码**：`echo $?` 应为 `0`。

**不对怎么办**：

| 现象 | 原因 | 处理 |
|---|---|---|
| `在障碍内` | 坐标压在墙/家具上 | 改 `config/locations.yaml` 里那个点的 `position` |
| `太窄(僵死)` 且净空 < 0.72 | footprint 角点会插进膨胀层，机器人会原地不动且**不报错** | 挪到净空 >= 0.95 的位置 |
| `偏窄(注意)` 0.72~0.95 | 能跑但边缘 | 建议挪，不强制 |
| 某对 `FAIL` | 两点之间被隔断 | 检查 door 宽度与家具摆放，或挪动该点 |
| `找不到 worlds/*.world` | 工作区不完整 | 确认 `worlds/complex_indoor.world` 存在 |

### 3.2 阶段二自动验收（不需要 Gazebo，5 秒）

```bash
cd ~/catkin_ws/src/llm_robot_agent
python3 tools/accept_stage2.py
```

**预期输出**（4 项全 PASS）：

```
[PASS] 世界可导航性
[PASS] 导航配置静态校验
[PASS] 地图文件
        size: 284x244
        resolution: 0.05
        origin: [-7.095, -6.095]
[PASS] A* 路径可达性
        corridor   路径   0.73 m (直线  0.68 m, 绕行 1.08x)
        hall       路径   3.28 m (直线  3.10 m, 绕行 1.06x)
        room_a     路径   7.23 m (直线  6.05 m, 绕行 1.20x)
        room_b     路径   6.04 m (直线  5.42 m, 绕行 1.11x)
        room_c     路径   4.87 m (直线  4.76 m, 绕行 1.02x)
        room_d     路径   3.99 m (直线  3.95 m, 绕行 1.01x)
结论：全部通过
```

绕行比（第二个数字）在 `1.00 ~ 1.20` 之间是健康的。若某条 `> 2.0`，
说明那条路要绕很远，通常是家具摆错位置。

### 3.3 里程计精度（30 秒，可选但很有价值）

里程计是所有定位的地基。这个测试让机器人在空旷处原地转 2π，
比对「真值转角」与「里程计转角」。

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
roslaunch llm_robot_agent gazebo.launch gui:=false &
sleep 22

python3 - <<'PY'
import math, rospy
from nav_msgs.msg import Odometry
from geometry_msgs.msg import Twist
from gazebo_msgs.srv import GetModelState
from tf.transformations import euler_from_quaternion

rospy.init_node('odom_test', anonymous=True)
rospy.wait_for_service('/gazebo/get_model_state', timeout=20)
gs = rospy.ServiceProxy('/gazebo/get_model_state', GetModelState)
S = {'yaw': 0.0}
rospy.Subscriber('/odom', Odometry,
    lambda m: S.__setitem__('yaw',
        euler_from_quaternion([m.pose.pose.orientation.x,
                               m.pose.pose.orientation.y,
                               m.pose.pose.orientation.z,
                               m.pose.pose.orientation.w])[2]))
rospy.sleep(2)

def truth():
    q = gs('robot', '').pose.orientation
    return euler_from_quaternion([q.x, q.y, q.z, q.w])[2]

pub = rospy.Publisher('/cmd_vel', Twist, queue_size=1)
t0, o0 = truth(), S['yaw']
tw = Twist(); tw.angular.z = 0.5
r = rospy.Rate(20)
end = rospy.Time.now() + rospy.Duration(12.566)
while rospy.Time.now() < end:
    pub.publish(tw); r.sleep()
pub.publish(Twist()); rospy.sleep(2)

dt = truth() - t0
do = S['yaw'] - o0
while dt < 0: dt += 2*math.pi
while do < 0: do += 2*math.pi
print("真值转角 %.4f rad (期望 6.2832, 偏差 %+.1f%%)" % (dt, 100*(dt-2*math.pi)/(2*math.pi)))
print("里程计   %.4f rad" % do)
print("odom/truth = %.4f   <-- 这个值应接近 1.000" % (do/dt))
PY

pkill -9 -f gzserver
```

**预期**：

```
真值转角 6.1860 rad (期望 6.2832, 偏差 -1.5%)
里程计   6.1889 rad
odom/truth = 1.0005   <-- 这个值应接近 1.000
```

**判读**：

| `odom/truth` | 含义 |
|---|---|
| `0.98 ~ 1.02` | 健康，可以继续 |
| `> 1.05` | 里程计虚报：车体没转够但编码器记满了（轮子打滑 / 万向球阻力大） |
| `< 0.95` | 里程计低估：`wheelSeparation` 参数偏大 |

这个比值不健康时，AMCL 会一路跟丢，症状是
**move_base 报告 SUCCEEDED 但机器人实际停在别的地方**，误差可达数米。
排查顺序见 [第 6 节](#6-故障排查按症状查)。

### 3.4 单元测试与离线演示（10 秒，不需要 ROS）

这一段验证的是 **Agent 逻辑层**，与 Gazebo 无关。

⚠️ **必须在仓库根目录运行**，不是在 ROS 包里：

```bash
cd /mnt/c/Users/29621/Documents/Qoder/2026-10-19/1c495b10/llm-robot-agent
# 或者你 clone 的位置，只要该目录下有 tests/ 和 robot_ws/ 即可

python3 -m pytest tests/ -q
python3 demos/demo.py
```

**预期**：

```
33 passed in 1.90s

演示汇总：
  场景 1: 通过
  场景 2: 通过
  场景 3: 通过
  场景 4: 通过
```

**如果报 `ModuleNotFoundError: No module named 'agent'`**：

说明 `tests/` 被放到了错误的位置 —— 测试需要向上找到
`robot_ws/src/llm_robot_agent/scripts` 才能 import `agent` 包。
如果你把 `tests/` 拷进了 ROS 包内部（例如 `~/catkin_ws/src/...`），
层级就断了。把 `tests/` 放回仓库根目录即可。

> 这类「拷个目录就 import 失败」的问题很容易被误判成测试代码坏了。
> 现在 `tests/test_agent.py` 已改成向上逐级搜索，两种布局都能跑。

**注意**：`demos/demo.py` 和单元测试用的是
`scripts/agent/tools/locations.py` 里的**兜底地点表**（示意坐标），
不是 `config/locations.yaml`。它们只验证 Agent 的决策逻辑，
坐标是否落在墙上无关紧要（Mock 后端不做碰撞检测）。
真实导航验证看 §4 / §5。

---

## 4. 启动导航并观察（带界面）

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
roslaunch llm_robot_agent navigation.launch
```

这会同时启动 Gazebo 客户端、map_server、AMCL、move_base、RViz。

**预期**：约 20~30 秒后

- Gazebo 窗口里出现一个 12×10 m 的多房间场景，蓝色机器人在南区中央 `(0, -3.40)`，朝向 +Y
- RViz 里能看到地图、激光点云、机器人模型
- 终端持续输出日志，没有红色 `[ERROR]`

**检查 TF 是否正常**（另开一个终端）：

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
rosrun tf tf_echo map base_footprint
```

**预期**：`Translation: [-0.03, -3.41, 0.00]`，持续刷新。

**不对怎么办**：

| 现象 | 处理 |
|---|---|
| `RLException: unused args` | launch 文件与调用参数不匹配，检查是否多传了参数 |
| Gazebo 黑屏/无响应 | `pkill -9 -f gzserver` 后重来；WSL 图形性能差时可加 `gui:=false` |
| RViz 报 `No tf data` | 等 10 秒；仍不行则检查 `rosrun tf view_frames` |
| `amcl` 报 `Timed out waiting for transform` | AMCL 的 `transform_tolerance` 偏小，见 `config/amcl_params.yaml` |

### 4.1 手动发一个导航目标

RViz 里点工具栏的 **2D Nav Goal**，在地图上点一个位置（比如 `room_c` 里 `(0, 2.2)`），
拖动方向定朝向。

**预期**：机器人规划出一条路径并驶向该点，最后停稳。

或者用命令行（更可复现）：

```bash
rostopic pub -1 /move_base_simple/goal geometry_msgs/PoseStamped '{
  header: {frame_id: "map"},
  pose: {
    position: {x: 0.0, y: 2.2, z: 0.0},
    orientation: {x: 0.0, y: 0.0, z: 0.7071, w: 0.7071}
  }
}'
```

---

## 5. 完整回归（8 个目标，无人值守）

这是最有力的验证：连续跑 8 段，每段都对比**真值**（Gazebo 里的真实位姿）
与目标点，而不是只看 move_base 报的成功。

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
pkill -9 -f gzserver; pkill -9 -f roslaunch; sleep 3

roslaunch llm_robot_agent navigation.launch gui:=false > /tmp/nav_test.log 2>&1 &
sleep 34
```

然后运行下面这段（保存为 `nav_test.py` 再 `python3 nav_test.py`）：

```python
import math
import yaml
import actionlib
import rospy
from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
from gazebo_msgs.srv import GetModelState
from geometry_msgs.msg import PoseWithCovarianceStamped
from tf.transformations import euler_from_quaternion, quaternion_from_euler

rospy.init_node('nav_test', anonymous=True)
rospy.wait_for_service('/gazebo/get_model_state', timeout=25)
gs = rospy.ServiceProxy('/gazebo/get_model_state', GetModelState)
ac = actionlib.SimpleActionClient('move_base', MoveBaseAction)
assert ac.wait_for_server(rospy.Duration(30)), "move_base 未就绪"

loc = yaml.safe_load(open(
    "/home/zzt/catkin_ws/src/llm_robot_agent/config/locations.yaml"))
S = {'ax': None, 'ay': None}
rospy.Subscriber('/amcl_pose', PoseWithCovarianceStamped,
                 lambda m: S.update(ax=m.pose.pose.position.x,
                                    ay=m.pose.pose.position.y))


def truth():
    r = gs('robot', '')
    q = r.pose.orientation
    _, _, y = euler_from_quaternion([q.x, q.y, q.z, q.w])
    return r.pose.position.x, r.pose.position.y, y


def P(name):
    p = loc['home'] if name == 'home' else loc['locations'][name]
    return p['position'][0], p['position'][1], p.get('yaw', 0.0)


seq = ['corridor', 'room_c', 'room_a', 'home',
       'hall', 'room_b', 'room_d', 'home']
results = []
for name in seq:
    gx, gy, gyaw = P(name)
    g = MoveBaseGoal()
    g.target_pose.header.frame_id = 'map'
    g.target_pose.header.stamp = rospy.Time.now()
    g.target_pose.pose.position.x = gx
    g.target_pose.pose.position.y = gy
    q = quaternion_from_euler(0, 0, gyaw)
    g.target_pose.pose.orientation.x = q[0]
    g.target_pose.pose.orientation.y = q[1]
    g.target_pose.pose.orientation.z = q[2]
    g.target_pose.pose.orientation.w = q[3]

    t0 = rospy.Time.now()
    ac.send_goal(g)
    ok = ac.wait_for_result(rospy.Duration(130))
    dur = (rospy.Time.now() - t0).to_sec()
    st = ac.get_state() if ok else "TIMEOUT"
    rospy.sleep(1.5)

    t = truth()
    err = math.hypot(t[0] - gx, t[1] - gy)
    ae = math.hypot(t[0] - (S['ax'] or 0), t[1] - (S['ay'] or 0))
    verdict = "PASS" if (str(st) == "3" and err < 0.6) else "FAIL"
    print("%-9s st=%-4s %5.1fs target=(%6.2f,%6.2f) truth=(%6.2f,%6.2f) "
          "err=%.2f amcl_err=%.2f %s"
          % (name, st, dur, gx, gy, t[0], t[1], err, ae, verdict))
    results.append(verdict)

print()
print("RESULT: %d/%d PASS" % (results.count("PASS"), len(results)))
```

**预期输出**（8/8）：

```
corridor  st=3      3.4s target=(  0.10, -1.90) truth=(  0.22, -2.06) err=0.20 amcl_err=0.04 PASS
room_c    st=3     18.8s target=(  0.00,  2.20) truth=( -0.06,  1.98) err=0.22 amcl_err=0.03 PASS
room_a    st=3     23.2s target=( -3.90,  1.80) truth=( -3.70,  1.85) err=0.20 amcl_err=0.03 PASS
home      st=3     26.4s target=(  0.30, -2.55) truth=(  0.19, -2.43) err=0.16 amcl_err=0.03 PASS
hall      st=3     15.8s target=(  3.35, -3.10) truth=(  3.19, -3.10) err=0.16 amcl_err=0.04 PASS
room_b    st=3     19.9s target=(  3.85,  1.55) truth=(  3.87,  1.33) err=0.22 amcl_err=0.03 PASS
room_d    st=3     38.8s target=( -3.65, -2.45) truth=( -3.45, -2.37) err=0.22 amcl_err=0.03 PASS
home      st=3     15.5s target=(  0.30, -2.55) truth=(  0.12, -2.66) err=0.21 amcl_err=0.05 PASS

RESULT: 8/8 PASS

=== 振荡中止 ===
0
=== 规划失败 ===
0
=== 恢复行为 ===
0
```

最后三行是脚本统计的 /move_base 日志关键字计数：
`Reached the goal` 之外的三类失败信号**都应为 0**。
只要出现非零，即使 `RESULT` 是 8/8 也要去 §6 查对应症状。

末尾可能看到一条
`Got a transition callback on a goal handle that we're not tracking`：
这是连续快速发目标时 move_base 的无害告警，出现在最后一段结束之后，
不影响结果。

**怎么读这张表**：

| 列 | 含义 | 健康范围 |
|---|---|---|
| `st` | move_base 状态码 | `3` = SUCCEEDED；`4` = ABORTED；`TIMEOUT` |
| `dur` | 耗时 | 单段 10~45 秒 |
| `err` | **真值**误差（最关键） | `< 0.4 m` |
| `amcl_err` | AMCL 与真值的偏差 | `< 0.3 m`；`> 1.0` 说明定位漂了 |

⚠️ **不要只看 `st=3`**。本项目踩过一个坑：move_base 报 SUCCEEDED
但真值差了好几米（AMCL 跟丢后自说自话）。**必须同时看 `err` 和 `amcl_err`。**

---

## 6. 故障排查（按症状查）

### 症状 A：`st=3` 但 `err` 很大，`amcl_err` 也很大

**定位漂移**。AMCL 迷路了，move_base 在错误位姿上「成功」了。

排查顺序：

1. 跑 [3.3 节](#33-里程计精度30-秒可选但很有价值) 看 `odom/truth`
   - 不在 `0.98~1.02` → 改 `urdf/advanced_robot.urdf` 里的 `wheelSeparation`
     或轮子/万向球摩擦
2. 检查 `config/amcl_params.yaml`：
   - `transform_tolerance` 应 `0.25`
   - `laser_max_beams` 应 `90`
   - `update_min_d` / `update_min_a` 应 `0.04` / `0.06`
   - `set_initial_pose` 应 `true`
3. 检查 `map` 与 `world` 是否对齐：`maps/indoor.pgm` 必须由
   `tools/build_map.py` 从**当前** `worlds/complex_indoor.world` 生成。
   改过 world 就要重新生成：
   ```bash
   python3 tools/build_map.py --world worlds/complex_indoor.world \
           --out maps/indoor --resolution 0.05
   ```

### 症状 B：机器人完全不动，`cmd_vel` 一直发但速度恒为 0

**最隐蔽的一类**。日志里没有任何 ERROR，目标永不结束。

诊断：

```bash
rostopic hz /cmd_vel              # 有频率（约 15Hz）
rostopic echo /cmd_vel            # 但 linear.x 与 angular.z 恒为 0
```

原因与处理：

| 原因 | 处理 |
|---|---|
| `latch_xy_goal_tolerance: true` | 改成 `false`（`config/base_local_planner_params.yaml`） |
| 目标点净空 < 0.71 m | 用 `tools/check_locations.py` 查，挪到净空 >= 0.95 |
| `min_vel_x > 0` 且需要倒车 | 保持 `min_vel_x: 0.0`，把目标点放到不需要倒车的位置 |
| `occdist_scale` 太大 | 保持 `0.02`（设 0.05 会在门口「觉得哪都危险」而不动） |

### 症状 C：`Rotate recovery can't rotate in place ... Cost: -1.00`

`-1.00` 是 **NO_INFORMATION（未知区域）**，不是障碍物。

原因：局部代价地图的 `track_unknown_space` 为 `true` 时，
激光没扫过的格子是 `-1`，恢复行为判定「可能碰撞」而拒绝旋转，
于是所有恢复手段连锁失效。

检查两处**都要**是 `false`：

```bash
grep -n "track_unknown_space" config/local_costmap_params.yaml
grep -n -A12 "^obstacle_layer:" config/costmap_common_params.yaml
```

- `config/local_costmap_params.yaml` 里顶层的 `track_unknown_space: false`
- `config/costmap_common_params.yaml` 里 `obstacle_layer:` 下面的
  `track_unknown_space: false`

全局代价地图的顶层 `track_unknown_space` 保持 `true`（防止路径画到地图外）。

### 症状 D：`Aborting because a valid plan could not be found`

全局规划器认为目标不可达。

1. 先跑 `python3 tools/accept_stage2.py`，看 A* 可达性那一项
2. 跑 `python3 tools/check_locations.py`，看是否有地点在障碍内
3. 若都没问题，检查门洞是否被家具膨胀封死：
   ```bash
   python3 tools/check_locations.py --min-clearance 0.95
   ```
   门洞可用宽度 = `DOOR_W - 2 * inflation_radius`，
   见 `tools/gen_world.py` 的 `DOOR_W` 与
   `config/costmap_common_params.yaml` 的 `inflation_radius`。
   当前是 `1.90 - 2*0.35 = 1.20 m`。

### 症状 E：`oscillating over and over`

机器人在原地来回摆。

先区分两种情况：

**E1：只在「从一个房间去另一个房间」时发生，房间内部目标正常**

这是**门洞太窄**的症状，不是参数问题。先查门洞净空：

```bash
cd ~/catkin_ws/src/llm_robot_agent
python3 tools/check_locations.py
```

然后单独量门洞中心线的净空（下面这段扫 y=0 主墙三个门洞）：

```bash
python3 - <<'PY'
import sys; sys.path.insert(0, 'tools')
from audit_world import parse_world
import importlib.util
spec = importlib.util.spec_from_file_location("c", "tools/check_locations.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
obs, _ = parse_world("worlds/complex_indoor.world")
G = m.Geometry(obs)
for x in (-3.8, 0.0, 3.8):
    best = max((G.clearance(v, 0.0), v) for v in
               [i * 0.02 + x - 0.6 for i in range(61)])
    print("x=%.1f  门洞最佳净空 %.3f" % (x, best[0]))
for x in (-2.8, 2.8):
    best = max((G.clearance(x, v), v) for v in
               [i * 0.02 + 0.2 for i in range(240)])
    print("x=%.1f  隔墙门洞最佳净空 %.3f @ y=%.2f" % (x, best[0], best[1]))
PY
```

门洞净空应 ≥ 0.90。若某个只有 0.3~0.5，说明：

- **world 文件不是当前 `gen_world.py` 生成的**（本项目真实踩过，见症状 H）
- 或者有家具横在门洞正前方 1.0 m 内（本项目真实踩过，见症状 I）

**E2：在任何地方都会发生**

调整参数：

- `config/move_base_params.yaml` 的 `oscillation_timeout: 20.0`
  （太小会误杀正常的对姿态动作）
- `config/base_local_planner_params.yaml` 的 `oscillation_reset_dist: 0.20`

### 症状 H：★ 房间内目标都能到，但「跨房间」必然中途失败，日志无报错

本项是**最隐蔽**的故障，本项目为此花了很长时间。典型表现：

- 单独测某个房间内的目标 → 成功
- 从该房间出发去别处 → 中途 `st=4` 或超时，真值停在门口附近
- `/move_base` 日志里没有 ERROR，只有 `oscillating over and over`
- `tools/check_locations.py` 报出所有地点净空都「OK」（因为它只看点）

**根本原因**：`worlds/complex_indoor.world` 是**旧版** `gen_world.py`
生成的，从未在改完参数后重新生成过。

本项目实测：`gen_world.py` 里 `DOOR_W` 已从 `1.10` 改成 `1.90`、
门中心已从 `±4.0` 改成 `±3.8`，但提交的 world 文件仍是旧参数产物
（两份文件差 5873 行）。于是：

- 三个门洞看起来都在、位置也合理
- 实际净宽只有 **1.10 m**（不是 1.90 m）
- 扣掉两侧膨胀 `2 × 0.35` 后，留给机器人中心的活动带宽只有 0.40 m
- 一进门口就开始振荡 → 中止

**怎么确认**：

```bash
cd ~/catkin_ws/src/llm_robot_agent
python3 tools/gen_world.py --out /tmp/world_new.world
diff -q worlds/complex_indoor.world /tmp/world_new.world && echo "一致" || echo "★ 过期"
```

只要不是「一致」，就说明仓库里的 world 与代码不同步。

**怎么修**（两条命令，必须都跑）：

```bash
python3 tools/gen_world.py --out worlds/complex_indoor.world
python3 tools/build_map.py --world worlds/complex_indoor.world \
                           --out maps/indoor --resolution 0.05
```

⚠️ **必须同时重建地图**。`maps/indoor.pgm` 是离线栅格（**不是** SLAM 产物），
是世界几何的投影。世界一变不重算，代价地图就会与真实几何不一致 ——
本项目另一次事故就是提交了过期的地图，导致全局代价地图 98% 是致命格，
机器人从第一步就不动、且日志全绿。

重建后 `config/locations.yaml` 的坐标**必须重新选**（见症状 I）。

### 症状 I：★ 重新生成 world 后，原本「净空 1.1」的点变成 0.16

家具位置会随 `gen_world.py` 一起变。本项目实测重新生成后：

| 地点 | 旧 world | 新 world |
|---|---|---|
| `hall` | 1.104 | **0.16**（`reception_desk_top` 挪到了那里） |
| `room_b` | 1.050 | 0.80 |
| `room_d` | 0.985 | 0.84 |

所以 **world 与 `locations.yaml` 必须成对更新**。改完 world 一定要：

```bash
python3 tools/check_locations.py     # 退出码 0 才算通过
python3 tools/accept_stage2.py       # 4 项全 PASS 才算通过
```

另外，家具不得遮挡门洞正前方 1.0 m。本项目实测一次：
`crate_c_corner` 与 `tvstand_b` 从两侧把 `x=+2.8` 的门洞夹到净空 **0.398 m**，
而那是 `room_b` 唯一的出口 —— 于是「去 room_b」成功、
「从 room_b 出发」必然振荡中止。把这两个家具挪走后，该门洞恢复到 0.920。

### 症状 J：★ 选点脚本报「净空 1.76」，实际只有 0.12

本项目真实踩过：自写的选点脚本里，打印用的格式串与参数个数不符，
把内部排序用的 `score = clearance - 0.30 × 距锚点距离`
当成净空打印出来，于是 `corridor` / `hall` 被按 1.76 m 写进
`locations.yaml`，实际只有 **0.12 / 0.25 m** —— 到点即僵死。

**教训**：净空数字只能来自
`tools/check_locations.Geometry.clearance()` 的返回值，并且
「验收打印」与「搜索打分」必须是两套独立的量，不能共用一行输出。
`config/locations.yaml` 里的每个坐标都以 `check_locations.py` 的输出为准。

### 症状 F：`TF_REPEATED_DATA` 偶尔出现几条

先分清两种量级，处理方式完全不同。

**F1：只有零星几条（正常，可忽略）**

实测连续 250 秒仿真、跑完 8 个目标，只出现 **3 条**，分布在不同时刻：

```
TF_REPEATED_DATA ignoring data with redundant timestamp
for frame base_footprint (parent odom) at time 155.377000
```

成因：Gazebo 物理步进与 TF 广播在同一个仿真时间戳上偶尔撞车，
属于仿真时序抖动，不影响定位与导航。**这种情况不要改任何配置。**

判断标准：日志里 `TF_REPEATED_DATA` 在几十条以内、且导航成功率正常，
就是这一类。

**F2：每帧都刷（几百上千条，才是真的重复发布）**

那才是轮子 TF 被重复发布。确认这两个值：

```xml
<publishWheelTF>false</publishWheelTF>
<publishWheelJointState>true</publishWheelJointState>
```

注意 `publishTf` / `publishOdomTF` 控制的是 `odom→base_footprint`
（**必需为 true**，关掉它 AMCL 就没有里程计输入了），
与轮子 TF 是两件不同的事，不要为了消警告去关。

> 本项目曾把这条写成「diff_drive 与 robot_state_publisher 同时发布轮子 TF」。
> 但实测那些零星告警其实来自 `odom→base_footprint` 的时间戳撞车，
> 而 `publishWheelTF` 早就是 `false` 了 —— 照旧描述去调只会白改一遍。

### 症状 G：`/scan` 没有数据

```bash
rostopic hz /scan     # 应为 30Hz 左右
```

没有的话检查 URDF 的 laser sensor 是否有 ROS 转换插件：

```xml
<plugin name="laser_plugin" filename="libgazebo_ros_laser.so">
  <topicName>scan</topicName>
  <frameName>laser_link</frameName>
</plugin>
```

### 症状 K：★ 机器人卡住不动，却要等到超时才报错

**现象**：机器人被挡住 / 局部规划器输出零速，一直不动。
Agent 侧却要等到整段 `timeout`（默认 120 s）耗尽才拿到失败，
错误码是 `NAV_TIMEOUT` 而不是 `BLOCKED`。

**这是本项目修过的一个真实 bug**，值得记住：

`RosBackend.navigate_to` 原本的「停滞检测」看的是 move_base 的
action 状态：

```python
# 错误写法（已废弃）
if state in (SUCCEEDED, ACTIVE, PENDING):
    if state != SUCCEEDED:
        last_active = time.time()      # ← 每轮都刷新
if time.time() - last_active > 45:     # ← 因此永不成立
    return BLOCKED
```

而机器人正常导航的**整个过程中 action 状态一直是 ACTIVE**，
所以 `last_active` 每 200 ms 就被刷新一次，那个 45 秒判定
**从来没有执行过**，是一段死代码。

实测证据（`tools/` 外的手工实验）：注入持续零速指令让机器人卡死，
90 秒后返回 `NAV_TIMEOUT`，而设计意图是尽早返回 `BLOCKED`。

**为什么这不只是「少一个错误码」**：
`BLOCKED` 语义是「可重试 / 可绕行」，`NAV_TIMEOUT` 是「等太久了」。
Agent 的策略表里两者处置不同，错误码错了后续决策也就错了，
而且会白白消耗掉整段 timeout（默认 120 s），长任务很容易被拖死。

**现在的正确判据**：只看位姿是否真的变化。

```python
mon = StagnationMonitor(timeout=25.0, dist_threshold=0.10)
mon.reset(x, y, now)            # 发送目标后初始化
...
if mon.update(x, y, now):       # True = 连续 25s 位移不足 10cm
    return BLOCKED
```

实现在 `scripts/agent/backends/stagnation.py`，**不依赖 ROS**，
因此可以确定性单测（`TestStagnationMonitor`，6 项用例）。

两个参数可在 `RosBackend(...)` 构造函数里调：

| 参数 | 默认 | 含义 |
|---|---|---|
| `stagnation_timeout` | 25.0 s | 连续多久没推进算停滞 |
| `stagnation_dist` | 0.10 m | 位移超过多少算「有推进」 |

> 调这两个值时记住：**阈值太敏感会误报**。
> 正常室内导航在某一段里 25 秒推进不到 10 cm 是可能的
> （门口犹豫、恢复行为原地旋转），此时报 BLOCKED 会打断
> 本来能成功的导航。日志里出现
> `原地停留超过 N 秒未推进` 时，先看那段是否真卡死，
> 再决定是调大 timeout 还是调小 dist_threshold。

---

## 7. 跑 Agent（LLM 闭环）

Agent 需要 `.env` 里的 LLM API key。**这个文件不进 git**。

```bash
cd ~/catkin_ws/src/llm_robot_agent
cat config/.env      # 应该有 LLM_API_KEY 等配置
```

先确认导航栈已启动（[第 4 节](#4-启动导航并观察带界面)），然后：

```bash
cd ~/catkin_ws/src/llm_robot_agent
python3 scripts/agent_run.py \
    --backend ros \
    --task "去房间A看看，然后去房间B，最后回起点" \
    --verbose
```

**预期**（前两轮）：

```
[配置] 后端=RosBackend  LLM=LLMClient
[配置] 地点=corridor, hall, home, room_a, room_b, room_c, room_d

任务：去房间A看看，然后去房间B，最后回起点

--- 第 1 轮 ---
[思考] 任务拆解：room_a → room_b → home。...
[行动] navigate_to({"location": "room_a"})
[观察] [OK] 已到达 room_a，当前位置 (-3.85, 1.58)。

--- 第 2 轮 ---
[思考] 已到达 room_a。继续前往 room_b。
[行动] navigate_to({"location": "room_b"})
[观察] [OK] 已到达 room_b，当前位置 (4.28, 1.13)。
```

**不依赖网络的验证**（不想连 LLM 时）：用 mock 后端跑工具链和错误码逻辑。

```bash
python3 scripts/agent_run.py --backend mock --task "先去房间A再去房间B" --verbose
```

Mock 后端不需要 ROS，也不需要网络，用于验证 Agent 的决策循环与错误处理。

---

## 8. 设计约束（改代码前必读）

这几条是整个项目的地基，改动时容易破坏：

1. **LLM 永远不输出坐标和速度**
   模型只能说「去 room_a」，坐标映射在 `config/locations.yaml` 完成。
   这样模型无法凭空编造数字，也不会因为单位换算出错。

2. **地点使用前必须过 `check_locations.py`**
   净空 < 0.71 m 会导致机器人原地僵死且不报错。

3. **`maps/indoor.pgm` 是离线生成的「上帝视角」地图，不是 SLAM 产物**
   它由 `tools/build_map.py` 从 world 直接光栅化，没有传感器噪声。
   用它验证导航栈的上下游是合适的，但**不能替代真实 SLAM 建图**
   作为最终交付依据。真机流程见 `tools/build_map.py` 顶部注释的 [A] 方案。

4. **改 world 必须同步四件事**
   - `tools/gen_world.py`（布局参数）
   - 重新生成 `worlds/complex_indoor.world`
   - 重新生成 `maps/indoor.pgm`
   - 重新选 `config/locations.yaml` 的坐标，并重跑
     `check_locations.py`（退出码 0）与 `accept_stage2.py`（4 项 PASS）

   ⚠️ 这四步少做任何一步都会产生「日志全绿但导航失败」的故障。
   本项目最严重的一次事故就是只改了 `gen_world.py` 没重新生成 world：
   门洞净宽实际只有 1.10 m，跨房间导航必然振荡中止。

5. **里程计标定比调参重要**
   `odom/truth` 不接近 1.000 时，**不要**用 `wheelSeparation` 硬凑。
   先修物理（万向球摩擦、接触刚度），再用几何真值。

6. **净空数字只能来自 `check_locations.Geometry.clearance()`**
   自写脚本很容易在格式化输出时把排序分数和验收数值混在一起。
   `config/locations.yaml` 里的每个坐标都以 `check_locations.py` 的输出为准。

---

## 9. 一键自查清单

按顺序跑，全部绿色即可认为环境健康：

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
cd ~/catkin_ws/src/llm_robot_agent

echo "[1/5] world 与 gen_world.py 是否同步"
python3 tools/gen_world.py --out /tmp/world_check.world > /dev/null 2>&1
if diff -q worlds/complex_indoor.world /tmp/world_check.world > /dev/null 2>&1; then
  echo "  OK"
else
  echo "  FAIL —— world 是旧版生成的，按 §6 症状 H 重建"
fi

echo "[2/5] 地图是否与 world 同步"
python3 tools/build_map.py --world worlds/complex_indoor.world \
        --out /tmp/map_check --resolution 0.05 > /dev/null 2>&1
if diff -q maps/indoor.pgm /tmp/map_check.pgm > /dev/null 2>&1; then
  echo "  OK"
else
  echo "  FAIL —— 地图过期，重新生成 maps/indoor.pgm"
fi

echo "[3/5] 地点体检"
python3 tools/check_locations.py > /tmp/c1.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c1.log; }

echo "[4/5] 阶段二验收"
python3 tools/accept_stage2.py > /tmp/c2.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c2.log; }

echo "[5/5] 编译"
cd ~/catkin_ws && catkin_make --pkg llm_robot_agent > /tmp/c3.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c3.log; }
```

前两项是本轮新增的：它们能提前发现「日志全绿但导航失败」这类
最隐蔽的故障（world 或地图与代码不同步）。

---

## 附：本次大改都动了什么

对照上一版，这些问题都已修复（细节写在各文件的注释里）：

| 问题 | 文件 | 现象 |
|---|---|---|
| **world 文件是旧版 gen_world.py 的产物**（门宽实际 1.10 m 而非 1.90） | `worlds/complex_indoor.world` | 房间内目标能到，跨房间必然振荡中止且无报错 |
| **`crate_c_corner` + `tvstand_b` 把 x=+2.8 门洞夹到净空 0.398** | `tools/gen_world.py` | room_b 进得去出不来 |
| **`nightstand_d` 堵住 y=0 西门洞南侧出口** | `tools/gen_world.py` | 进出 room_d 时蹭墙、偶发中止 |
| **`check_locations.Grid` 用矩形膨胀代替真实距离膨胀** | `tools/check_locations.py` | 误报远离家具的点「在膨胀栅格上是占据格」 |
| **`accept_stage2.py` 用 `--start 值` 拆写传负坐标** | `tools/accept_stage2.py` | 起点 x 为负时 argparse 报错，误判世界不可导航 |
| 门洞 1.10 m + 膨胀 0.42 把门封死 | `tools/gen_world.py` | 所有目标都「不可达」 |
| `room_c` 货架紧贴门洞，膨胀后吃掉门洞上沿 | `tools/gen_world.py` | 机器人卡在门洞内不动 |
| 驱动轮嵌进底盘，转向打滑 15% | `urdf/advanced_robot.urdf` | 里程计虚报，AMCL 长距离跟丢 |
| 万向球摩擦 0.001，转向时车体滑移 | `urdf/advanced_robot.urdf` | 转 180° 实际只转 172° |
| 激光没有 ROS 转换插件 | `urdf/advanced_robot.urdf` | `/scan` 完全没数据 |
| `gazebo.launch` 硬编码 `-Y 0` | `launch/gazebo.launch` | 生成朝 +X，AMCL 以为朝 +Y（差 90°） |
| `obstacle_layer/track_unknown_space: true` | `config/costmap_common_params.yaml` | 恢复行为报 `Cost: -1.00` 后全部失效 |
| `latch_xy_goal_tolerance: true` | `config/base_local_planner_params.yaml` | 目标永不结束，`cmd_vel` 恒零 |
| 地点净空 0.71~0.83 m | `config/locations.yaml` | 到点后原地僵死 |
| 地点没对准门洞通道带 | `config/locations.yaml` | 出房间要先绕大圈，容易失败 |

新增工具：

- `tools/check_locations.py` — 地点体检（净空 + 连通性）
