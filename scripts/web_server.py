#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 服务器 - 提供简单的 Web 界面控制机器人
"""

import os
import json
import threading
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import rospy
from ros_tools import RobotController
from deepseek_client import DeepSeekClient

app = Flask(__name__)
CORS(app)

# 全局变量
robot_controller = None
llm_client = None
task_history = []


def init_ros():
    """初始化 ROS 节点"""
    global robot_controller
    try:
        # 检查是否已经初始化
        if not rospy.core.is_initialized():
            rospy.init_node('web_robot_agent', anonymous=True)
            print("ROS 节点初始化成功")
        else:
            print("ROS 节点已存在，直接使用")
        
        robot_controller = RobotController()
        print("ROS 控制器初始化成功")
    except Exception as e:
        print(f"ROS 初始化失败：{e}")
        # 即使初始化失败，也尝试创建控制器
        try:
            robot_controller = RobotController()
            print("ROS 控制器创建成功（使用现有节点）")
        except Exception as e2:
            print(f"ROS 控制器创建也失败：{e2}")


def init_llm():
    """初始化 LLM 客户端"""
    global llm_client
    try:
        # 加载环境变量
        env_path = os.path.join(os.path.dirname(__file__), '..', 'config', '.env')
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if '=' in line and not line.startswith('#'):
                        key, value = line.strip().split('=', 1)
                        os.environ[key] = value
        
        llm_client = DeepSeekClient()
        print("LLM 客户端初始化成功")
    except Exception as e:
        print(f"LLM 初始化失败：{e}")


@app.route('/')
def index():
    """主页"""
    return render_template('index.html')


@app.route('/api/status')
def get_status():
    """获取机器人状态"""
    if robot_controller:
        position = robot_controller.get_position()
        return jsonify({
            'success': True,
            'position': position,
            'history': task_history[-10:]  # 最近 10 条历史
        })
    else:
        return jsonify({
            'success': False,
            'error': '机器人控制器未初始化'
        })


@app.route('/api/execute', methods=['POST'])
def execute_command():
    """执行自然语言指令"""
    data = request.json
    command = data.get('command', '')
    
    if not command:
        return jsonify({
            'success': False,
            'error': '指令为空'
        })
    
    if not robot_controller:
        return jsonify({
            'success': False,
            'error': '机器人控制器未初始化'
        })
    
    try:
        # 解析任务
        task_steps = []
        if llm_client:
            available_tools = ['move_forward', 'rotate', 'stop', 'get_position']
            task_steps = llm_client.parse_task(command, available_tools)
        
        if not task_steps:
            # 回退到规则解析
            task_steps = parse_with_rules(command)
        
        # 执行任务
        results = []
        for step in task_steps:
            tool_name = step['tool']
            params = step['parameters']
            
            if tool_name == 'move_forward':
                result = robot_controller.move_forward(**params)
            elif tool_name == 'rotate':
                result = robot_controller.rotate(**params)
            elif tool_name == 'stop':
                result = robot_controller.stop()
            elif tool_name == 'get_position':
                result = robot_controller.get_position()
            else:
                result = {'success': False, 'message': f'未知工具：{tool_name}'}
            
            results.append({
                'tool': tool_name,
                'params': params,
                'result': result
            })
        
        # 记录历史
        task_history.append({
            'command': command,
            'steps': len(task_steps),
            'results': results
        })
        
        return jsonify({
            'success': True,
            'command': command,
            'task_steps': task_steps,
            'results': results,
            'final_position': robot_controller.get_position()
        })
    
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })


def parse_with_rules(command):
    """规则解析（备用）"""
    import re
    task_steps = []
    
    if '前进' in command or '向前' in command:
        distance_match = re.search(r'(\d+\.?\d*)\s*米', command)
        distance = float(distance_match.group(1)) if distance_match else 1.0
        task_steps.append({
            'tool': 'move_forward',
            'parameters': {'distance': distance}
        })
    
    if '左转' in command:
        angle_match = re.search(r'(\d+)\s*度', command)
        angle = float(angle_match.group(1)) if angle_match else 90.0
        task_steps.append({
            'tool': 'rotate',
            'parameters': {'angle_degrees': angle}
        })
    
    if '右转' in command:
        angle_match = re.search(r'(\d+)\s*度', command)
        angle = float(angle_match.group(1)) if angle_match else -90.0
        task_steps.append({
            'tool': 'rotate',
            'parameters': {'angle_degrees': angle}
        })
    
    if '停' in command:
        task_steps.append({
            'tool': 'stop',
            'parameters': {}
        })
    
    if '位置' in command or '在哪' in command:
        task_steps.append({
            'tool': 'get_position',
            'parameters': {}
        })
    
    return task_steps


if __name__ == '__main__':
    # 初始化
    init_ros()
    init_llm()
    
    # 启动 Web 服务器
    print("\n" + "="*50)
    print("LLM Robot Agent Web 界面已启动")
    print("打开浏览器访问：http://localhost:5000")
    print("="*50 + "\n")
    
    app.run(host='0.0.0.0', port=5000, debug=False)
