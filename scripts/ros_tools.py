#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ROS 工具接口模块
封装 ROS 1 接口，供 LLM Agent 调用
"""

import rospy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Image
import tf
import math


class RobotController:
    """机器人控制器 - 提供 ROS 接口工具"""
    
    def __init__(self):
        """初始化 ROS 节点和发布者/订阅者"""
        # 检查是否已经初始化，避免重复初始化
        if not rospy.core.is_initialized():
            rospy.init_node('llm_robot_agent', anonymous=True)
        
        # 速度发布者
        self.vel_pub = rospy.Publisher('/cmd_vel', Twist, queue_size=10)
        
        # 里程计订阅者
        self.odom_sub = rospy.Subscriber('/odom', Odometry, self.odom_callback)
        
        # 当前位置
        self.current_position = {'x': 0.0, 'y': 0.0, 'theta': 0.0}
        
        # 速率控制
        self.rate = rospy.Rate(10)  # 10 Hz
        
        rospy.loginfo("RobotController 初始化完成")
    
    def odom_callback(self, msg):
        """里程计回调函数，更新当前位置"""
        self.current_position['x'] = msg.pose.pose.position.x
        self.current_position['y'] = msg.pose.pose.position.y
        
        # 从四元数提取偏航角
        quaternion = (
            msg.pose.pose.orientation.x,
            msg.pose.pose.orientation.y,
            msg.pose.pose.orientation.z,
            msg.pose.pose.orientation.w
        )
        euler = tf.transformations.euler_from_quaternion(quaternion)
        self.current_position['theta'] = euler[2]
    
    def move(self, linear_speed=0.0, angular_speed=0.0, duration=1.0):
        """
        控制机器人移动
        
        参数:
            linear_speed: 线速度 (m/s)，正数前进，负数后退
            angular_speed: 角速度 (rad/s)，正数左转，负数右转
            duration: 持续时间 (秒)
        
        返回:
            dict: 执行结果
        """
        try:
            twist = Twist()
            twist.linear.x = linear_speed
            twist.angular.z = angular_speed
            
            start_time = rospy.Time.now()
            while (rospy.Time.now() - start_time).to_sec() < duration:
                self.vel_pub.publish(twist)
                self.rate.sleep()
            
            # 停止机器人
            self.stop()
            
            return {
                'success': True,
                'message': f'移动完成: 线速度={linear_speed}m/s, 角速度={angular_speed}rad/s, 持续={duration}s',
                'final_position': self.get_position()
            }
        except Exception as e:
            return {
                'success': False,
                'message': f'移动失败: {str(e)}'
            }
    
    def stop(self):
        """停止机器人"""
        twist = Twist()
        twist.linear.x = 0.0
        twist.angular.z = 0.0
        self.vel_pub.publish(twist)
        return {'success': True, 'message': '机器人已停止'}
    
    def get_position(self):
        """
        获取机器人当前位置
        
        返回:
            dict: 包含 x, y, theta 的位置信息
        """
        return {
            'x': round(self.current_position['x'], 3),
            'y': round(self.current_position['y'], 3),
            'theta': round(self.current_position['theta'], 3)
        }
    
    def rotate(self, angle_degrees):
        """
        旋转指定角度
        
        参数:
            angle_degrees: 旋转角度（度），正数左转，负数右转
        
        返回:
            dict: 执行结果
        """
        angle_radians = math.radians(angle_degrees)
        angular_speed = 0.5  # rad/s
        duration = abs(angle_radians / angular_speed)
        
        angular_vel = angular_speed if angle_degrees > 0 else -angular_speed
        
        return self.move(linear_speed=0.0, angular_speed=angular_vel, duration=duration)
    
    def move_forward(self, distance, speed=0.2):
        """
        向前移动指定距离
        
        参数:
            distance: 移动距离 (米)
            speed: 移动速度 (m/s)
        
        返回:
            dict: 执行结果
        """
        duration = distance / speed
        return self.move(linear_speed=speed, angular_speed=0.0, duration=duration)


# 测试代码
if __name__ == '__main__':
    try:
        controller = RobotController()
        rospy.loginfo("测试: 获取当前位置")
        pos = controller.get_position()
        rospy.loginfo(f"当前位置: {pos}")
        
        rospy.loginfo("测试: 向前移动 0.5 米")
        result = controller.move_forward(0.5, speed=0.2)
        rospy.loginfo(f"移动结果: {result}")
        
    except rospy.ROSInterruptException:
        rospy.loginfo("节点已停止")
