#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web 控制界面（Web Control UI）
==============================

提供三个端点：
    GET  /               单页界面
    GET  /api/status     机器人状态 + 地点清单
    POST /api/execute    提交自然语言任务，同步返回完整执行轨迹

设计约束
--------
导航是**阻塞式**的，一次任务可能持续数十秒。因此：
- 使用后台线程执行任务，避免阻塞 Flask 请求
- 同一时刻只允许一个任务（机器人物理上只有一台，天然需要互斥锁）
- 执行过程中的轨迹实时写入 task_history，供前端轮询

这比旧版「直接在前端等待一个巨型 JSON」更符合真实系统形态。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Dict, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from flask import Flask, jsonify, request, render_template  # noqa: E402
from flask_cors import CORS  # noqa: E402

from agent import LLMRobotAgent, LocationMap, MockBackend, create_llm  # noqa: E402
from agent.backends.ros_backend import ROS_AVAILABLE, RosBackend  # noqa: E402
from agent.backends.http_backend import HttpBackend  # noqa: E402

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder=os.path.join(_HERE, "templates"))
CORS(app)

_pkg_root = os.path.dirname(_HERE)

# ---------------------------------------------------------------------------
# 全局状态
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_task_lock = threading.Lock()   # 机器人互斥：同时只允许一个任务
_backend: Any = None
_agent: Optional[LLMRobotAgent] = None
_locations: Optional[LocationMap] = None

_history: list = []
MAX_HISTORY = 50

_current = {
    "running": False,
    "instruction": "",
    "started_at": None,
    "elapsed": 0.0,
    "stage": "idle",
    "logs": [],
}


def _load_locations() -> LocationMap:
    path = os.path.join(_pkg_root, "config", "locations.yaml")
    if os.path.exists(path):
        try:
            return LocationMap.from_yaml(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("地点表加载失败：%s", exc)
    from agent import default_location_map

    return default_location_map()


def _load_cfg() -> Dict[str, Any]:
    path = os.path.join(_pkg_root, "config", "agent.yaml")
    if not os.path.exists(path):
        return {}
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return {}


def _load_dotenv() -> None:
    for rel in ("config/.env", ".env"):
        path = os.path.join(_pkg_root, rel)
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def init_system(backend_name: str = "auto", bridge_url: str = "http://localhost:9090") -> None:
    """初始化后端与 Agent。auto = 有 ROS 用 ROS，否则用 Mock。"""
    global _backend, _agent, _locations

    _load_dotenv()
    _locations = _load_locations()
    cfg = _load_cfg()

    if backend_name == "auto":
        backend_name = "ros" if ROS_AVAILABLE else "mock"

    if backend_name == "ros" and ROS_AVAILABLE:
        nav = cfg.get("navigation", {})
        _backend = RosBackend(
            _locations,
            goal_tolerance=float(nav.get("goal_tolerance", 0.30)),
            default_timeout=float(nav.get("nav_timeout_sec", 120)),
        )
    elif backend_name == "http":
        nav = cfg.get("navigation", {})
        _backend = HttpBackend(
            _locations,
            bridge_url=bridge_url,
            timeout_sec=float(nav.get("nav_timeout_sec", 120)),
        )
    else:
        if backend_name == "ros":
            log.warning("未安装 ROS，自动降级为 Mock 后端")
        _backend = MockBackend(_locations, seed=42, step_delay=0.6)

    llm = create_llm(cfg.get("llm", {}), offline_fallback=True)
    _agent = LLMRobotAgent(_backend, llm, _locations, config=cfg, verbose=True)
    log.info("系统就绪：后端=%s，LLM=%s", _backend.name(), llm.__class__.__name__)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    state = _backend.get_state() if _backend else None
    return jsonify(
        {
            "ok": True,
            "backend": _backend.name() if _backend else "not_initialized",
            "ready": bool(_backend and _backend.is_ready()),
            "llm": _agent.llm.__class__.__name__ if _agent else "",
            "robot": state.to_dict() if state else {},
            "locations": [
                {"name": n, "description": _locations.describe(n)}
                for n in (_locations.known_names() if _locations else [])
            ],
            "current_task": _current,
            "history": _history[-10:],
        }
    )


@app.route("/api/execute", methods=["POST"])
def api_execute():
    payload = request.get_json(silent=True) or {}
    instruction = (payload.get("instruction") or "").strip()

    if not instruction:
        return jsonify({"ok": False, "error": "指令不能为空"}), 400

    # 先判断能否拿到锁，拿不到就直接拒绝
    acquired = _task_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({"ok": False, "error": "机器人正在执行其他任务，请稍候"}), 409

    try:
        _current.update(
            running=True,
            instruction=instruction,
            started_at=time.time(),
            elapsed=0.0,
            stage="planning",
        )
        _current["logs"] = [f"[{time.strftime('%H:%M:%S')}] 收到任务：{instruction}"]

        result = _agent.run(instruction)

        record = {
            "instruction": instruction,
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "turns": result.get("turns", 0),
            "tool_calls": result.get("tool_calls", 0),
            "replans": result.get("replans", 0),
            "elapsed_sec": result.get("elapsed_sec", 0),
            "steps": result.get("steps", []),
            "trace": (result.get("record") or {}).get("trace", []),
            "finished_at": time.time(),
        }
        with _lock:
            _history.append(record)
            del _history[:-MAX_HISTORY]

        return jsonify(
            {
                "ok": True,
                "result": record,
                "robot": _backend.get_state().to_dict(),
                "stats": getattr(_backend, "stats", None).to_dict()
                if hasattr(getattr(_backend, "stats", None), "to_dict")
                else {},
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("任务执行异常")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _current.update(running=False, stage="idle", elapsed=time.time() - (_current["started_at"] or time.time()))
        _task_lock.release()


@app.route("/api/stats")
def api_stats():
    stats = getattr(_backend, "stats", None)
    return jsonify({"ok": True, "stats": stats.to_dict() if hasattr(stats, "to_dict") else {}})


@app.route("/api/navigate", methods=["POST"])
def api_navigate():
    """导航到指定语义地点"""
    payload = request.get_json(silent=True) or {}
    target = (payload.get("target") or "").strip()

    if not target:
        return jsonify({"ok": False, "error": "目标地点不能为空"}), 400

    if not _locations:
        return jsonify({"ok": False, "error": "地点表未加载"}), 500

    resolved = _locations.resolve(target)
    if not resolved:
        return jsonify({"ok": False, "error": f"未知地点: {target}，可用: {_locations.known_names()}"}), 400

    pose = _locations.pose(resolved)
    if not pose:
        return jsonify({"ok": False, "error": f"地点 {resolved} 无坐标"}), 500

    instruction = f"前往{resolved}"
    desc = _locations.describe(resolved)

    # 复用执行锁
    acquired = _task_lock.acquire(blocking=False)
    if not acquired:
        return jsonify({"ok": False, "error": "机器人正在执行其他任务，请稍候"}), 409

    try:
        _current.update(
            running=True,
            instruction=instruction,
            started_at=time.time(),
            elapsed=0.0,
            stage="navigating",
        )
        _current["logs"] = [f"[{time.strftime('%H:%M:%S')}] 开始导航: {resolved} ({desc})"]

        result = _agent.run(instruction)

        record = {
            "instruction": instruction,
            "target": resolved,
            "description": desc,
            "success": result.get("success", False),
            "message": result.get("message", ""),
            "turns": result.get("turns", 0),
            "tool_calls": result.get("tool_calls", 0),
            "elapsed_sec": result.get("elapsed_sec", 0),
            "finished_at": time.time(),
        }
        with _lock:
            _history.append(record)
            del _history[:-MAX_HISTORY]

        return jsonify({
            "ok": True,
            "target": resolved,
            "description": desc,
            "pose": {"x": pose[0], "y": pose[1], "yaw": pose[2]},
            "result": record,
            "robot": _backend.get_state().to_dict(),
        })
    except Exception as exc:  # noqa: BLE001
        log.exception("导航执行异常")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        _current.update(running=False, stage="idle", elapsed=time.time() - (_current["started_at"] or time.time()))
        _task_lock.release()


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    """取消当前任务"""
    if not _current.get("running"):
        return jsonify({"ok": True, "message": "当前无运行中的任务"})

    _current.update(running=False, stage="cancelled", elapsed=time.time() - (_current["started_at"] or time.time()))
    _current["logs"].append(f"[{time.strftime('%H:%M:%S')}] 用户取消任务")

    if _backend and hasattr(_backend, "cancel"):
        _backend.cancel()

    return jsonify({"ok": True, "message": "任务已取消"})


@app.route("/api/locations")
def api_locations():
    """获取所有可用地点"""
    if not _locations:
        return jsonify({"ok": False, "error": "地点表未加载"}), 500

    locs = []
    for name in _locations.known_names():
        pose = _locations.pose(name)
        locs.append({
            "name": name,
            "description": _locations.describe(name),
            "x": pose[0] if pose else 0,
            "y": pose[1] if pose else 0,
            "yaw": pose[2] if pose else 0,
        })
    return jsonify({"ok": True, "locations": locs})


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="LLM Robot Agent Web UI")
    parser.add_argument("--backend", choices=["auto", "mock", "ros", "http"], default="auto")
    parser.add_argument("--bridge-url", default="http://localhost:9090",
                        help="WSL 侧 nav_bridge 的地址 (默认 http://localhost:9090)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s | %(message)s"
    )
    init_system(args.backend, bridge_url=args.bridge_url)

    print("=" * 60)
    print(f"  后端：{_backend.name()}")
    print(f"  访问：http://{args.host}:{args.port}")
    print("=" * 60)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
