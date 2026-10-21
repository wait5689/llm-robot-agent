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
home       (  0.00, -3.40) +1.57     1.51   OK
room_a     ( -3.90,  1.80) +1.57     1.36   OK
room_c     (  0.00,  2.20) +1.57     2.25   OK
room_b     (  4.45,  1.20) +0.00     1.14   OK
room_d     ( -3.65, -2.45) +0.00     1.34   OK
corridor   (  0.00, -1.20) +1.57     1.44   OK
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
        corridor   路径   2.20 m (直线  2.20 m, 绕行 1.00x)
        hall       路径   3.47 m (直线  3.36 m, 绕行 1.03x)
        room_a     路径   7.58 m (直线  6.50 m, 绕行 1.17x)
        room_b     路径   6.88 m (直线  6.40 m, 绕行 1.08x)
        room_c     路径   5.60 m (直线  5.60 m, 绕行 1.00x)
        room_d     路径   4.04 m (直线  3.77 m, 绕行 1.07x)
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
corridor  st=3      7.9s target=(  0.00, -1.20) truth=(  0.05, -1.38) err=0.18 amcl_err=0.05 PASS
room_c    st=3     12.5s target=(  0.00,  2.20) truth=(  0.02,  2.03) err=0.17 amcl_err=0.06 PASS
room_a    st=3     17.1s target=( -3.90,  1.80) truth=( -3.67,  1.82) err=0.23 amcl_err=0.06 PASS
home      st=3     32.1s target=(  0.00, -3.40) truth=( -0.11, -3.34) err=0.12 amcl_err=0.08 PASS
hall      st=3     16.8s target=(  3.35, -3.10) truth=(  3.25, -3.18) err=0.13 amcl_err=0.08 PASS
room_b    st=3     18.5s target=(  4.45,  1.20) truth=(  4.58,  0.99) err=0.24 amcl_err=0.22 PASS
room_d    st=3     40.5s target=( -3.65, -2.45) truth=( -3.31, -2.31) err=0.37 amcl_err=0.19 PASS
home      st=3     15.2s target=(  0.00, -3.40) truth=( -0.08, -3.37) err=0.08 amcl_err=0.11 PASS

RESULT: 8/8 PASS
```

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

- `config/move_base_params.yaml` 的 `oscillation_timeout: 20.0`
  （太小会误杀正常的对姿态动作）
- `config/base_local_planner_params.yaml` 的 `oscillation_reset_dist: 0.20`

### 症状 F：`TF_REPEATED_DATA` 刷屏

diff_drive 与 robot_state_publisher 同时发布轮子 TF。

确认 URDF 里：

```xml
<publishWheelTF>false</publishWheelTF>
<publishWheelJointState>true</publishWheelJointState>
```

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

4. **改 world 必须同步三件事**
   - `tools/gen_world.py`（布局参数）
   - 重新生成 `worlds/complex_indoor.world`
   - 重新生成 `maps/indoor.pgm`，并重跑 `check_locations.py`

5. **里程计标定比调参重要**
   `odom/truth` 不接近 1.000 时，**不要**用 `wheelSeparation` 硬凑。
   先修物理（万向球摩擦、接触刚度），再用几何真值。

---

## 9. 一键自查清单

按顺序跑，全部绿色即可认为环境健康：

```bash
source /opt/ros/noetic/setup.bash
source ~/catkin_ws/devel/setup.bash
cd ~/catkin_ws/src/llm_robot_agent

echo "[1/3] 地点体检"
python3 tools/check_locations.py > /tmp/c1.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c1.log; }

echo "[2/3] 阶段二验收"
python3 tools/accept_stage2.py > /tmp/c2.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c2.log; }

echo "[3/3] 编译"
cd ~/catkin_ws && catkin_make --pkg llm_robot_agent > /tmp/c3.log 2>&1 \
  && echo "  OK" || { echo "  FAIL"; tail -20 /tmp/c3.log; }
```

---

## 附：本次大改都动了什么

对照上一版，这些问题都已修复（细节写在各文件的注释里）：

| 问题 | 文件 | 现象 |
|---|---|---|
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
