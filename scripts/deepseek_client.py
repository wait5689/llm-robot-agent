#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek LLM 客户端
封装 DeepSeek API 调用，支持 function calling
"""

import os
import json
import requests


class DeepSeekClient:
    """DeepSeek API 客户端"""
    
    def __init__(self, api_key=None, base_url=None, model=None):
        """
        初始化 DeepSeek 客户端
        
        参数:
            api_key: API Key（可选，默认从环境变量读取）
            base_url: API 基础 URL
            model: 模型名称
        """
        self.api_key = api_key or os.getenv('DEEPSEEK_API_KEY')
        self.base_url = base_url or os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
        self.model = model or os.getenv('DEEPSEEK_MODEL', 'deepseek-chat')
        
        if not self.api_key:
            raise ValueError("请设置 DEEPSEEK_API_KEY 环境变量或传入 api_key 参数")
        
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        }
        
        print(f"DeepSeek 客户端初始化完成，模型: {self.model}")
    
    def chat(self, messages, tools=None, temperature=0.7):
        """
        发送聊天请求
        
        参数:
            messages: 消息列表
            tools: 工具定义列表（可选）
            temperature: 温度参数
        
        返回:
            dict: 响应结果
        """
        try:
            url = f"{self.base_url}/chat/completions"
            
            payload = {
                'model': self.model,
                'messages': messages,
                'temperature': temperature,
            }
            
            if tools:
                payload['tools'] = tools
                payload['tool_choice'] = 'auto'
            
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            choice = data['choices'][0]
            
            return {
                'success': True,
                'content': choice['message'].get('content', ''),
                'tool_calls': choice['message'].get('tool_calls', []),
                'finish_reason': choice.get('finish_reason', ''),
                'usage': data.get('usage', {})
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def parse_task(self, natural_language_input, available_tools):
        """
        解析自然语言任务
        
        参数:
            natural_language_input: 用户的自然语言指令
            available_tools: 可用工具列表
        
        返回:
            list: 任务步骤列表
        """
        system_prompt = """你是一个机器人任务规划助手。你的任务是将用户的自然语言指令转换为具体的机器人操作步骤。

可用的工具：
1. move_forward - 向前移动指定距离
   参数: distance (float, 米), speed (float, m/s, 默认 0.2)
2. rotate - 旋转指定角度
   参数: angle_degrees (float, 度, 正数左转，负数右转)
3. stop - 停止机器人
4. get_position - 获取机器人当前位置

请根据用户指令，输出一个 JSON 格式的任务步骤列表。每个步骤包含：
- tool: 工具名称
- parameters: 参数字典

示例输出：
[
  {"tool": "move_forward", "parameters": {"distance": 1.0, "speed": 0.2}},
  {"tool": "rotate", "parameters": {"angle_degrees": 90}},
  {"tool": "get_position", "parameters": {}}
]

只输出 JSON，不要其他内容。"""

        messages = [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': natural_language_input}
        ]
        
        response = self.chat(messages, temperature=0.1)
        
        if not response['success']:
            print(f"LLM 调用失败: {response['error']}")
            return []
        
        content = response['content']
        
        # 尝试解析 JSON
        try:
            # 提取 JSON 部分（可能被 markdown 代码块包裹）
            if '```json' in content:
                content = content.split('```json')[1].split('```')[0]
            elif '```' in content:
                content = content.split('```')[1].split('```')[0]
            
            task_steps = json.loads(content.strip())
            return task_steps
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}")
            print(f"原始内容: {content}")
            return []


def main():
    """测试 DeepSeek 客户端"""
    try:
        client = DeepSeekClient()
        
        # 测试简单对话
        print("\n测试 1: 简单对话")
        response = client.chat([
            {'role': 'user', 'content': '你好，你能做什么？'}
        ])
        print(f"响应: {response['content']}")
        
        # 测试任务解析
        print("\n测试 2: 任务解析")
        tools = ['move_forward', 'rotate', 'stop', 'get_position']
        task = client.parse_task("前进 1 米，然后左转 90 度", tools)
        print(f"解析结果: {json.dumps(task, indent=2, ensure_ascii=False)}")
        
    except Exception as e:
        print(f"测试失败: {e}")


if __name__ == '__main__':
    main()
