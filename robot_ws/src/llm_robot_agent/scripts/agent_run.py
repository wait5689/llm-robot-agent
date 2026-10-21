#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CLI 入口（Agent Runner）
========================

两种运行模式：
    --backend mock   无需 ROS，直接在内存里跑完整 Agent（用于演示与测试）
    --backend ros    连接 Gazebo + move_base 真实导航栈

示例：
    # 离线演示，带故障注入（模拟「路上遇到障碍重新规划」）
    python3 agent_run.py --backend mock \\
        --task "去房间 A 检查一下，然后前往房间 B，最后回到起点" \\
        --fault room_a:blocked

    # 连接真实仿真
    python3 agent_run.py --backend ros --task "先去 A 再去 B 然后回起点"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

# 让 agent 包可被直接 import（既支持 catkin 安装，也支持源码运行）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from agent import LLMRobotAgent, LocationMap, MockBackend, create_llm, default_location_map  # noqa: E402
from agent.backends.ros_backend import ROS_AVAILABLE, RosBackend  # noqa: E402


def _pkg_root() -> str:
    """返回 llm_robot_agent 包根目录（scripts/ 的上一级）。"""
    return os.path.dirname(_HERE)


def load_locations(path: str | None) -> LocationMap:
    candidate = path or os.path.join(_pkg_root(), "config", "locations.yaml")
    if os.path.exists(candidate):
        try:
            lm = LocationMap.from_yaml(candidate)
            logging.info("已加载地点表 %s（%d 个地点）", candidate, len(lm))
            return lm
        except Exception as exc:  # noqa: BLE001
            logging.warning("地点表加载失败（%s），使用内置默认表", exc)
    return default_location_map()


def load_agent_config(path: str | None) -> dict:
    candidate = path or os.path.join(_pkg_root(), "config", "agent.yaml")
    if not os.path.exists(candidate):
        return {}
    try:
        import yaml

        with open(candidate, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        logging.warning("Agent 配置加载失败：%s", exc)
        return {}


def _load_dotenv() -> None:
    """极简 .env 加载：只支持 KEY=VALUE 与 # 注释。

    查找顺序（先命中者生效，后命中只补齐缺失项，不覆盖已有值）：
      1. 包内 config/.env        —— catkin 安装后的规范位置
      2. 包内 .env
      3. 仓库根 config/.env      —— git 仓库布局下的开发者位置
      4. 仓库根 .env
    """
    repo_root = os.path.dirname(os.path.dirname(_pkg_root()))
    candidates = [
        os.path.join(_pkg_root(), "config", ".env"),
        os.path.join(_pkg_root(), ".env"),
        os.path.join(repo_root, "config", ".env"),
        os.path.join(repo_root, ".env"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def parse_faults(specs) -> None:
    """解析 --fault room_a:blocked:2 形式的故障注入。"""
    return specs or []


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 自主移动机器人 Agent")
    parser.add_argument("--task", "-t", required=True, help="自然语言任务指令")
    parser.add_argument("--backend", choices=["mock", "ros"], default="mock",
                        help="mock=内存模拟（无需 ROS）；ros=连接 Gazebo+move_base")
    parser.add_argument("--locations", help="地点表 YAML 路径")
    parser.add_argument("--config", help="Agent 配置 YAML 路径")
    parser.add_argument("--fault", action="append", default=[],
                        help="注入故障 location:mode[:times]，可多次指定")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Mock 每次导航的模拟耗时（秒）")
    parser.add_argument("--seed", type=int, default=42, help="Mock 随机种子")
    parser.add_argument("--verbose", "-v", action="store_true", help="打印完整思考过程")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出最终结果")
    # 用 parse_known_args：作为 roslaunch 节点启动时，ROS 会追加
    # __name:= / __log:= / __ns:= 等保留参数，未知参数不能导致节点退出。
    args, _unknown = parser.parse_known_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    _load_dotenv()

    locations = load_locations(args.locations)
    cfg = load_agent_config(args.config)

    # ---------------- 后端 ----------------
    if args.backend == "ros":
        if not ROS_AVAILABLE:
            print("错误：当前环境未安装 ROS 1。请在 WSL2/Ubuntu 20.04 中运行，"
                  "或使用 --backend mock。", file=sys.stderr)
            return 2
        nav_cfg = cfg.get("navigation", {})
        backend = RosBackend(
            locations,
            goal_tolerance=float(nav_cfg.get("goal_tolerance", 0.30)),
            default_timeout=float(nav_cfg.get("nav_timeout_sec", 120)),
        )
    else:
        backend = MockBackend(locations, seed=args.seed, step_delay=args.delay)
        for spec in args.fault:
            parts = spec.split(":")
            if len(parts) < 2:
                print(f"忽略无效故障描述：{spec}", file=sys.stderr)
                continue
            loc, mode = parts[0], parts[1]
            times = int(parts[2]) if len(parts) > 2 else 1
            backend.inject_fault(loc, mode, times=times,
                                 retryable=(mode != "unreachable"))

    # ---------------- LLM ----------------
    llm = create_llm(cfg.get("llm", {}), offline_fallback=True)
    print(f"[配置] 后端={backend.name()}  LLM={llm.__class__.__name__}")
    print(f"[配置] 地点={', '.join(locations.known_names())}")

    agent = LLMRobotAgent(
        backend=backend,
        llm=llm,
        locations=locations,
        config=cfg,
        verbose=not args.json,
    )

    result = agent.run(args.task)
    backend.shutdown()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"\n耗时 {result['elapsed_sec']}s | "
              f"轮数 {result['turns']} | 工具调用 {result['tool_calls']} | "
              f"重规划 {result['replans']}")
        if isinstance(backend, MockBackend):
            print(f"Mock 统计：{json.dumps(backend.stats.to_dict(), ensure_ascii=False)}")

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
