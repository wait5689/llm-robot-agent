#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航桥接服务（Navigation Bridge）
=================================

运行在 WSL / Ubuntu 侧，把 ROS 导航能力以 HTTP 接口暴露出来，
供 Windows 上的 web_server.py 远程调用。

用法
----
    # WSL 终端（需要先 source ROS 环境 & 启动导航栈）
    python3 nav_bridge.py --port 9090

架构
----
    Windows                           WSL / Ubuntu
    ┌──────────────┐    HTTP/JSON    ┌──────────────────┐
    │ web_server.py │ ──────────────> │ nav_bridge.py    │
    │ (LLM + WebUI) │                │   ↓ RosBackend   │
    └──────────────┘                 │   ↓ move_base    │
                                     └──────────────────┘
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Dict, Optional

# 确保能导入同包的 agent 模块
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

log = logging.getLogger("nav_bridge")

# ---------------------------------------------------------------------------
# 延迟初始化：第一次请求时才创建 RosBackend（给 ROS 启动时间）
# ---------------------------------------------------------------------------
_backend = None
_backend_lock = threading.Lock()
_locations = None


def _init_components():
    """初始化 LocationMap + RosBackend（幂等）。"""
    global _backend, _locations
    if _backend is not None:
        return

    with _backend_lock:
        if _backend is not None:
            return

        # 加载 locations.yaml
        import yaml
        pkg_root = os.path.dirname(_HERE)
        loc_path = os.path.join(pkg_root, "config", "locations.yaml")
        with open(loc_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        from agent.tools.locations import LocationMap
        _locations = LocationMap(data)

        from agent.backends.ros_backend import RosBackend
        _backend = RosBackend(_locations, node_name="nav_bridge")
        log.info("RosBackend 初始化完成")


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class BridgeHandler(BaseHTTPRequestHandler):
    """处理来自 Windows web_server 的导航请求。"""

    def _send_json(self, data: Dict[str, Any], status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"ok": True, "backend": "ros" if _backend else "initializing"})
            return

        if self.path == "/status":
            try:
                _init_components()
                state = _backend.get_state()
                self._send_json({
                    "ok": True,
                    "robot": state.to_dict(),
                    "ready": _backend.is_ready(),
                })
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, 500)
            return

        self._send_json({"error": "unknown path"}, 404)

    def do_POST(self):
        try:
            _init_components()
        except Exception as exc:
            self._send_json({"ok": False, "error": f"初始化失败: {exc}"}, 500)
            return

        body = self._read_body()

        if self.path == "/navigate":
            location = body.get("location", "")
            timeout_sec = body.get("timeout_sec")
            if not location:
                self._send_json({"ok": False, "error": "缺少 location"}, 400)
                return

            log.info("收到导航请求: %s", location)
            result = _backend.navigate_to(location, timeout_sec=timeout_sec)
            self._send_json({
                "ok": result.ok,
                "observation": result.observation,
                "error_code": result.error_code.value,
                "data": result.data,
            })
            return

        if self.path == "/cancel":
            result = _backend.cancel_nav()
            self._send_json({
                "ok": result.ok,
                "observation": result.observation,
            })
            return

        self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    def log_message(self, fmt, *args):
        log.info(fmt, *args)


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="导航桥接服务")
    ap.add_argument("--port", type=int, default=9090, help="监听端口 (默认 9090)")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 确保 ROS 环境
    if not os.environ.get("ROS_MASTER_URI"):
        log.warning("ROS_MASTER_URI 未设置，使用默认值 http://localhost:11311")

    server = HTTPServer((args.host, args.port), BridgeHandler)
    log.info("=" * 60)
    log.info("  导航桥接服务")
    log.info("  监听: http://%s:%d", args.host, args.port)
    log.info("  等待 Windows web_server 连接 ...")
    log.info("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("桥接服务已停止")
        if _backend:
            _backend.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
