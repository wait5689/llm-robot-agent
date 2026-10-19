#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
初始位姿发布器（Initial Pose Publisher）
========================================

仿真环境中机器人是程序化生成在已知位置的，因此可以跳过 RViz 里的
"2D Pose Estimate" 手动点击，直接把初始位姿喂给 AMCL。

这一步没有技术含量，但能显著减少演示时的操作失误——
答辩时少一次「第一次跑没定位上」的尴尬。
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


def publish_initial_pose(x: float, y: float, yaw: float,
                         topic: str = "/initialpose", repeat: int = 5) -> None:
    pub = rospy.Publisher(topic, PoseWithCovarianceStamped, queue_size=1)

    # 等待 AMCL 订阅者建立连接，否则首条消息会丢失
    deadline = rospy.Time.now() + rospy.Duration(5.0)
    while pub.get_num_connections() == 0 and rospy.Time.now() < deadline:
        rospy.sleep(0.1)

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

    # 协方差：中等置信度，让 AMCL 有空间做修正
    for i in (0, 7, 35):
        msg.pose.covariance[i] = 0.25

    for _ in range(repeat):
        msg.header.stamp = rospy.Time.now()
        pub.publish(msg)
        rospy.sleep(0.3)

    rospy.loginfo("已发布初始位姿 (%.2f, %.2f, yaw=%.2f)", x, y, yaw)


def main() -> int:
    if not ROS_AVAILABLE:
        print("需要 ROS 1 环境（rospy）", file=sys.stderr)
        return 2

    rospy.init_node("initial_pose_publisher", anonymous=True)
    args = [float(a) for a in sys.argv[1:4]] if len(sys.argv) >= 4 else [0.0, -3.0, 0.0]
    publish_initial_pose(*args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
