#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ROS 1 后端（ROS 1 Backend）
===========================

把导航栈真实能力包装成 `RobotBackend` 接口。

与旧实现的本质区别
------------------
| 旧实现 (ros_tools.py)          | 本实现                          |
|--------------------------------|---------------------------------|
| 直接发 /cmd_vel 开环控制         | 通过 move_base Action 闭环导航   |
| 无避障、无路径规划               | 全局+局部规划器，遇障自动重规划    |
| 按时间推进，不知道是否到达        | 监听 Action 反馈，确认到达       |
| 无法处理失败                     | 区分超时/受阻/不可达，可恢复      |

话题与 Action
-------------
- `/move_base/goal` / `/move_base/cancel` : move_base Action（Nav2 对应 `/navigate_to_pose`）
- `/move_base/status`                    : 动作状态
- `/amcl_pose`                           : 定位位姿
- `/tf` (map -> base_link)               : 位姿兜底来源
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

from ..contracts import ErrorCode, RobotState, ToolResult
from ..tools.locations import LocationMap
from .base import RobotBackend

log = logging.getLogger(__name__)

# ROS 相关导入集中在这里，未安装 ROS 时模块仍可被导入（供测试与文档生成）
try:
    import rospy
    import actionlib
    import tf
    from move_base_msgs.msg import MoveBaseAction, MoveBaseGoal
    from actionlib_msgs.msg import GoalStatus
    from geometry_msgs.msg import PoseWithCovarianceStamped
    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ROS_AVAILABLE = False
    rospy = None
    actionlib = None
    tf = None
    MoveBaseAction = object
    MoveBaseGoal = object

    class GoalStatus:  # type: ignore
        PENDING = 0
        ACTIVE = 1
        SUCCEEDED = 3
        ABORTED = 4
        REJECTED = 5
        PREEMPTED = 6
        LOST = 9


class RosBackend(RobotBackend):
    """基于 move_base 的真实导航后端。"""

    def __init__(
        self,
        locations: LocationMap,
        node_name: str = "llm_robot_agent",
        move_base_namespace: str = "move_base",
        goal_tolerance: float = 0.30,
        default_timeout: float = 120.0,
        connect_timeout: float = 3.0,
    ):
        if not ROS_AVAILABLE:
            raise RuntimeError(
                "未检测到 ROS 1 环境。请在 WSL2 / Ubuntu 20.04 中 source "
                "/opt/ros/noetic/setup.bash 后再运行，或改用 MockBackend。"
            )

        self.loc = locations
        self.goal_tolerance = goal_tolerance
        self.default_timeout = default_timeout

        if not rospy.core.is_initialized():
            rospy.init_node(node_name, anonymous=True)

        self._lock = threading.Lock()
        self._pose = {"x": 0.0, "y": 0.0, "yaw": 0.0, "valid": False}
        self._nav_state = "idle"
        self._current_goal: Optional[str] = None

        # move_base Action 客户端
        self.client = actionlib.SimpleActionClient(move_base_namespace, MoveBaseAction)
        log.info("等待 move_base Action 服务器 ...")
        if not self.client.wait_for_server(rospy.Duration(connect_timeout)):
            log.warning(
                "move_base 未在 %.0fs 内响应，导航调用会返回 ROBOT_NOT_READY。"
                "请先启动导航 launch 文件。", connect_timeout,
            )
            self._server_ready = False
        else:
            self._server_ready = True
            log.info("move_base 已连接")

        # 位姿来源 1：AMCL
        self._amcl_sub = rospy.Subscriber(
            "/amcl_pose", PoseWithCovarianceStamped, self._amcl_cb, queue_size=1
        )
        # 位姿来源 2：TF 兜底（AMCL 未收敛时仍能读到 base_link）
        self._tf_listener = tf.TransformListener()
        self._tf_thread = threading.Thread(target=self._spin_tf, daemon=True)
        self._tf_thread.start()

    # ------------------------------------------------------------------
    # 位姿刷新
    # ------------------------------------------------------------------
    def _amcl_cb(self, msg) -> None:
        q = msg.pose.pose.orientation
        yaw = self._quat_to_yaw(q.x, q.y, q.z, q.w)
        with self._lock:
            self._pose.update(
                x=msg.pose.pose.position.x,
                y=msg.pose.pose.position.y,
                yaw=yaw,
                valid=True,
            )

    def _spin_tf(self) -> None:
        """独立线程持续尝试用 TF 刷新位姿（AMCL 不可用时的兜底）。"""
        rate = rospy.Rate(2)
        while not rospy.is_shutdown():
            try:
                (trans, rot) = self._tf_listener.lookupTransform(
                    "map", "base_link", rospy.Time(0)
                )
                with self._lock:
                    if not self._pose["valid"]:
                        self._pose.update(
                            x=trans[0], y=trans[1],
                            yaw=self._quat_to_yaw(*rot), valid=True,
                        )
            except Exception:  # noqa: BLE001 - TF 查询在系统未就绪时必然失败
                pass
            try:
                rate.sleep()
            except rospy.ROSInterruptException:
                break

    @staticmethod
    def _quat_to_yaw(x: float, y: float, z: float, w: float) -> float:
        siny_cosp = 2.0 * (w * z + x * y)
        cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
        return math.atan2(siny_cosp, cosy_cosp)

    # ------------------------------------------------------------------
    # RobotBackend 实现
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        return bool(self._server_ready)

    def navigate_to(self, location: str, timeout_sec: Optional[float] = None) -> ToolResult:
        if not self._server_ready:
            return ToolResult(
                ok=False,
                observation="move_base 未连接，导航栈可能尚未启动。",
                error_code=ErrorCode.ROBOT_NOT_READY,
                retryable=True,
            )

        pose = self.loc.pose(location)
        if pose is None:
            return ToolResult(
                ok=False,
                observation=f"地点 '{location}' 没有坐标定义。",
                error_code=ErrorCode.UNKNOWN_LOCATION,
                retryable=True,
            )

        x, y, yaw = pose
        timeout = float(timeout_sec or self.default_timeout)

        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = self.loc.frame_id
        goal.target_pose.header.stamp = rospy.Time.now()
        goal.target_pose.pose.position.x = x
        goal.target_pose.pose.position.y = y
        goal.target_pose.pose.position.z = 0.0
        q = tf.transformations.quaternion_from_euler(0, 0, yaw)
        goal.target_pose.pose.orientation.x = q[0]
        goal.target_pose.pose.orientation.y = q[1]
        goal.target_pose.pose.orientation.z = q[2]
        goal.target_pose.pose.orientation.w = q[3]

        self._nav_state = "navigating"
        self._current_goal = location
        log.info("发送导航目标 %s -> (%.2f, %.2f, %.2f)", location, x, y, yaw)
        self.client.send_goal(goal)

        deadline = time.time() + timeout
        last_active = time.time()
        while time.time() < deadline:
            if rospy.is_shutdown():
                break
            state = self.client.get_state()

            if state in (GoalStatus.SUCCEEDED, GoalStatus.ACTIVE, GoalStatus.PENDING):
                if state != GoalStatus.SUCCEEDED:
                    last_active = time.time()
                else:
                    break
            else:
                break

            # 超过 45 秒完全没有进展，视为卡死
            if time.time() - last_active > 45:
                self.client.cancel_goal()
                self._nav_state = "idle"
                self._current_goal = None
                return ToolResult(
                    ok=False,
                    observation=f"前往 {location} 时机器人长时间无进展，判定为受阻停滞。",
                    error_code=ErrorCode.BLOCKED,
                    retryable=True,
                )
            time.sleep(0.2)

        state = self.client.get_state()
        self._nav_state = "idle"
        self._current_goal = None

        if state == GoalStatus.SUCCEEDED:
            reached = self._pose
            return ToolResult(
                ok=True,
                observation=(
                    f"已到达 {location}，当前位置 "
                    f"({reached['x']:.2f}, {reached['y']:.2f})。"
                ),
                data={"location": location, "pose": list(pose)},
            )

        # 用位姿误差区分「差一点」和「根本不可达」
        dist_err = math.hypot(self._pose["x"] - x, self._pose["y"] - y)
        if dist_err <= self.goal_tolerance * 2:
            return ToolResult(
                ok=True,
                observation=(
                    f"已接近 {location}（距离约 {dist_err:.2f} m，在容差内视为到达）。"
                ),
                data={"location": location, "pose": list(pose), "approx": True},
            )

        if state == GoalStatus.ABORTED:
            code, note = ErrorCode.GOAL_UNREACHABLE, "全局规划器无法找到可行路径，目标不可达。"
            retryable = False
        elif state == GoalStatus.REJECTED:
            code, note = ErrorCode.PARAM_INVALID, "导航目标被拒绝，请检查目标位姿是否合法。"
            retryable = True
        elif time.time() >= deadline:
            code, note = ErrorCode.NAV_TIMEOUT, "导航超时，未能在规定时间内到达。"
            retryable = True
        else:
            code, note = ErrorCode.BACKEND_ERROR, f"导航异常终止（状态 {state}）。"
            retryable = True

        return ToolResult(
            ok=False,
            observation=f"前往 {location} 失败：{note} 当前距离目标 {dist_err:.2f} m。",
            error_code=code,
            retryable=retryable,
            data={"location": location, "dist_error": round(dist_err, 2)},
        )

    def cancel_nav(self) -> ToolResult:
        try:
            if self._server_ready:
                self.client.cancel_all_goals()
            self._nav_state = "idle"
            self._current_goal = None
            return ToolResult(
                ok=True, observation="已取消导航，机器人已停止。",
                data={"cancelled": True},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                ok=False, observation=f"取消失败：{exc}",
                error_code=ErrorCode.BACKEND_ERROR, retryable=True,
            )

    def get_state(self) -> RobotState:
        with self._lock:
            pose = dict(self._pose)
        nav_state = self._nav_state
        if not pose["valid"]:
            nav_state = "localization_lost"
        return RobotState(
            x=pose["x"], y=pose["y"], yaw=pose["yaw"],
            frame_id=self.loc.frame_id,
            nav_state=nav_state,
            current_goal=self._current_goal,
        )

    def shutdown(self) -> None:
        try:
            if self._server_ready:
                self.client.cancel_all_goals()
        except Exception:  # noqa: BLE001
            pass
        log.info("RosBackend 已关闭")
