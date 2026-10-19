#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LLM Robot Agent - 基于大语言模型的自主移动机器人 Agent
核心 Agent 类：接收自然语言指令，调用 LLM 理解任务，执行机器人控制
"""

import json
import rospy
from ros_tools import RobotController


class LLMRobotAgent:
    """LLM 机器人 Agent - 自然语言控制机器人"""
    
    def __init__(self, llm_client=None):
        """
        初始化 Agent
        
        参数:
            llm_client: LLM 客户端（OpenAI/Anthropic 等）
        """
        self.robot = RobotController()
        self.llm_client = llm_client
        
        # 工具定义 - 供 LLM 调用
        self.tools = {
            'move_forward': {
                'description': '向前移动指定距离',
                'parameters': {
                    'distance': {'type': 'float', 'description': '移动距离（米）'},
                    'speed': {'type': 'float', 'description': '移动速度（m/s），默认 0.2', 'default': 0.2}
                },
                'function': self.robot.move_forward
            },
            'rotate': {
                'description': '旋转指定角度',
                'parameters': {
                    'angle_degrees': {'type': 'float', 'description': '旋转角度（度），正数左转，负数右转'}
                },
                'function': self.robot.rotate
            },
            'stop': {
                'description': '停止机器人',
                'parameters': {},
                'function': self.robot.stop
            },
            'get_position': {
                'description': '获取机器人当前位置',
                'parameters': {},
                'function': self.robot.get_position
            }
        }
        
        rospy.loginfo("LLM Robot Agent 初始化完成")
    
    def parse_task(self, natural_language_input):
        """
        解析自然语言任务（调用 LLM）
        
        参数:
            natural_language_input: 用户的自然语言指令
        
        返回:
            list: 任务步骤列表
        """
        # TODO: 接入真实的 LLM API
        # 这里先用简单的规则解析作为示例
        
        task_steps = []
        
        # 简单的关键词匹配
        if '前进' in natural_language_input or '向前' in natural_language_input:
            # 提取距离
            import re
            distance_match = re.search(r'(\d+\.?\d*)\s*米', natural_language_input)
            distance = float(distance_match.group(1)) if distance_match else 1.0
            task_steps.append({
                'tool': 'move_forward',
                'parameters': {'distance': distance}
            })
        
        if '左转' in natural_language_input:
            import re
            angle_match = re.search(r'(\d+)\s*度', natural_language_input)
            angle = float(angle_match.group(1)) if angle_match else 90.0
            task_steps.append({
                'tool': 'rotate',
                'parameters': {'angle_degrees': angle}
            })
        
        if '右转' in natural_language_input:
            import re
            angle_match = re.search(r'(\d+)\s*度', natural_language_input)
            angle = float(angle_match.group(1)) if angle_match else -90.0
            task_steps.append({
                'tool': 'rotate',
                'parameters': {'angle_degrees': angle}
            })
        
        if '停' in natural_language_input:
            task_steps.append({
                'tool': 'stop',
                'parameters': {}
            })
        
        if '位置' in natural_language_input or '在哪' in natural_language_input:
            task_steps.append({
                'tool': 'get_position',
                'parameters': {}
            })
        
        return task_steps
    
    def execute_task(self, task_steps):
        """
        执行任务步骤
        
        参数:
            task_steps: 任务步骤列表
        
        返回:
            dict: 执行结果
        """
        results = []
        
        for i, step in enumerate(task_steps):
            tool_name = step['tool']
            params = step['parameters']
            
            rospy.loginfo(f"执行步骤 {i+1}/{len(task_steps)}: {tool_name}")
            
            if tool_name not in self.tools:
                results.append({
                    'step': i + 1,
                    'success': False,
                    'message': f'未知工具: {tool_name}'
                })
                continue
            
            try:
                # 调用工具
                tool_func = self.tools[tool_name]['function']
                result = tool_func(**params)
                
                results.append({
                    'step': i + 1,
                    'tool': tool_name,
                    'success': result.get('success', False),
                    'message': result.get('message', ''),
                    'data': result
                })
                
                rospy.loginfo(f"步骤 {i+1} 完成: {result.get('message', '')}")
                
            except Exception as e:
                results.append({
                    'step': i + 1,
                    'tool': tool_name,
                    'success': False,
                    'message': f'执行失败: {str(e)}'
                })
                rospy.logerr(f"步骤 {i+1} 失败: {str(e)}")
        
        return {
            'total_steps': len(task_steps),
            'completed_steps': sum(1 for r in results if r.get('success')),
            'results': results
        }
    
    def run(self, natural_language_input):
        """
        运行 Agent - 主入口
        
        参数:
            natural_language_input: 用户的自然语言指令
        
        返回:
            dict: 完整执行结果
        """
        rospy.loginfo(f"收到指令: {natural_language_input}")
        
        # 1. 解析任务
        task_steps = self.parse_task(natural_language_input)
        
        if not task_steps:
            return {
                'success': False,
                'message': '无法解析任务，请尝试更明确的指令'
            }
        
        rospy.loginfo(f"解析出 {len(task_steps)} 个任务步骤")
        
        # 2. 执行任务
        execution_result = self.execute_task(task_steps)
        
        # 3. 生成最终报告
        final_position = self.robot.get_position()
        
        return {
            'success': execution_result['completed_steps'] == execution_result['total_steps'],
            'input': natural_language_input,
            'task_steps': len(task_steps),
            'completed': execution_result['completed_steps'],
            'results': execution_result['results'],
            'final_position': final_position,
            'message': f"任务完成: {execution_result['completed_steps']}/{execution_result['total_steps']} 步"
        }


def main():
    """主函数 - 测试 Agent"""
    try:
        agent = LLMRobotAgent()
        
        rospy.loginfo("=" * 50)
        rospy.loginfo("LLM Robot Agent 已启动")
        rospy.loginfo("示例指令:")
        rospy.loginfo("  - 前进 1 米")
        rospy.loginfo("  - 左转 90 度")
        rospy.loginfo("  - 我的位置在哪")
        rospy.loginfo("=" * 50)
        
        # 测试示例
        test_commands = [
            "前进 0.5 米",
            "我的位置在哪",
            "左转 90 度"
        ]
        
        for cmd in test_commands:
            rospy.loginfo(f"\n测试指令: {cmd}")
            result = agent.run(cmd)
            rospy.loginfo(f"执行结果: {result['message']}")
            rospy.sleep(1)
        
    except rospy.ROSInterruptException:
        rospy.loginfo("Agent 已停止")


if __name__ == '__main__':
    main()
