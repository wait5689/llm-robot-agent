#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
初始位姿发布器（Initial Pose Publisher）
========================================

仿真环境中机器人是程序化生成在已知位置的，因此可以跳过 RViz 里的
"2D Pose Estimate" 手动点击，直接把初始位姿喂给 AMCL。

为什么不能只发一次
------------------
AMCL 启动时要先加载地图、初始化粒子群，这段时间内它尚未订阅
`/initialpose`。如果发布器在 `roslaunch` 一开始就发一次，消息直接丢弃，
表现为「RViz 里粒子云撒满全图，怎么都不收敛」。

旧实现的三个缺陷：
  1. 只等 `pub.get_num_connections()`，但发布者刚创建时连接数恒为 0，
     而且没有检查 AMCL 是否真的活着；
  2. 发 5 次就退出，若 AMCL 启动慢于这 1.5 秒仍会丢失；
  3. 退出后如果 AMCL 因故重启，就再也没有初始位姿了。

现在的做法：
  - 先等 `/map` 话题存在（说明 map_server 已就绪）
  - 再等 `/amcl` 节点出现在 ROS master 上
  - 然后在最初 30 秒内持续以 2 Hz 重发（AMCL 对重复 initialpose
    是幂等的，重复设置同一个位姿不会让粒子群发散）
  - 之后退出，避免长期占用话题
"""

from __future__ import annotations

import sys

try:
    import rospy
    from geometry_msgs.msg import PoseWithCovarianceStamped
    from tf.transformations import quaternion_from_euler
    ROS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ROS_AVAILABLE = False


def _wait_for(condition, timeout: float, poll: float = 0.2) -> bool:
    """轮询等待条件成立。"""
    deadline = rospy.Time.now() + rospy.Duration(timeout)
    while not rospy.is_shutdown() and rospy.Time.now() < deadline:
        if condition():
            return True
        rospy.sleep(poll)
    return False


def build_msg(x: float, y: float, yaw: float,
              cov_xy: float = 0.25, cov_yaw: float = 0.06):
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = "map"
    msg.header.stamp = rospy.Time.now()
    msg.pose.pose.position.x = x
    msg.pose.pose.position.y = y
    msg.pose.pose.position.z = 0.0

    q = quaternion_from_euler(0, 0, yaw)
    msg.pose.pose.orientation.x = q[0]
    msg.pose.pose.orientation.y = q[1]
    msg.pose.pose.orientation.z = q[2]
    msg.pose.pose.orientation.w = q[3]

    # 协方差是 6x6 行主序：[x,y,z,roll,pitch,yaw]
    msg.pose.covariance[0] = cov_xy    # x
    msg.pose.covariance[7] = cov_xy    # y
    msg.pose.covariance[35] = cov_yaw  # yaw
    return msg


def publish_initial_pose(x: float, y: float, yaw: float,
                         settle_timeout: float = 30.0,
                         resend_sec: float = 30.0) -> bool:
    """发布初始位姿；返回是否成功发出过至少一次。"""
    pub = rospy.Publisher("/initialpose", PoseWithCovarianceStamped, queue_size=1)
    msg = build_msg(x, y, yaw)

    # 第一步：等地图就绪
    rospy.loginfo("等待 /map 话题 ...")
    if not _wait_for(lambda: pub.get_num_connections() > 0 or
                     rospy.get_published_topics() and
                     any(t == "/map" for t, _ in rospy.get_published_topics()),
                     settle_timeout):
        rospy.logwarn("/map 在 %.0fs 内未出现，仍尝试发布初始位姿", settle_timeout)
    else:
        rospy.loginfo("/map 已就绪")

    # 第二步：等 AMCL 订阅本话题
    rospy.loginfo("等待 AMCL 订阅 /initialpose ...")
    if not _wait_for(lambda: pub.get_num_connections() > 0, settle_timeout):
        rospy.logwarn("AMCL 未在 %.0fs 内连上 /initialpose", settle_timeout)
        return False

    # 第三步：持续重发一段时间，确保映射到粒子群
    rospy.loginfo("开始发布初始位姿 (%.2f, %.2f, yaw=%.2f)，持续 %.0fs",
                  x, y, yaw, resend_sec)
    rate = rospy.Rate(2)
    deadline = rospy.Time.now() + rospy.Duration(resend_sec)
    count = 0
    while not rospy.is_shutdown() and rospy.Time.now() < deadline:
        msg.header.stamp = rospy.Time.now()
        pub.publish(msg)
        count += 1
        rate.sleep()

    rospy.loginfo("初始位姿发布完成（共 %d 次）", count)
    return count > 0


def main() -> int:
    if not ROS_AVAILABLE:
        print("需要 ROS 1 环境（rospy）", file=sys.stderr)
        return 2

    rospy.init_node("initial_pose_publisher", anonymous=False)

    argv = [a for a in sys.argv[1:]]
    try:
        vals = [float(a) for a in argv[:3]]
    except ValueError:
        print("参数应为 x y yaw（三个浮点数）", file=sys.stderr)
        return 2
    while len(vals) < 3:
        vals.append(0.0)

    ok = publish_initial_pose(vals[0], vals[1], vals[2])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
