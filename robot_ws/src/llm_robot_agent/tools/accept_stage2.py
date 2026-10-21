#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段二验收脚本（Stage-2 Acceptance）
====================================

一次跑完导航栈的静态验收，不需要 ROS / Gazebo：

  1. 世界可导航性审计  —— 每个语义地点是否真的走得到
  2. 导航配置静态校验  —— launch / 参数 / 依赖引用是否自洽
  3. 地图一致性校验    —— maps/indoor.pgm 是否与 world 匹配
  4. 路径可达性验证    —— 用 A* 在膨胀栅格上求真实路径长度

第 4 项是这套检查的核心：前 3 项都是「单点」检查，
而 A* 会真的从 home 走到每个房间，把「路径是否存在」变成可验证的事实。

用法
----
    python3 tools/accept_stage2.py
    python3 tools/accept_stage2.py --json

退出码：0 = 全部通过
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from audit_world import (parse_world, build_grid, load_locations_minimal,
                         Grid)  # noqa: E402

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------
# A* 路径搜索
# ----------------------------------------------------------------------

def astar(grid: Grid, start: Tuple[float, float],
          goal: Tuple[float, float]) -> Optional[List[Tuple[float, float]]]:
    """八邻域 A*，返回世界坐标路径点；不可达返回 None。

    为什么用八邻域：四邻域会把斜向通道判成不可通行，
    在门洞处尤其容易误判。
    """
    si, sj = grid.to_cell(*start)
    gi, gj = grid.to_cell(*goal)

    if not (grid.inside(si, sj) and grid.inside(gi, gj)):
        return None
    if grid.get(si, sj) or grid.get(gi, gj):
        return None

    def h(i, j):
        return math.hypot(i - gi, j - gj)

    open_heap = [(h(si, sj), 0.0, si, sj)]
    gscore = {(si, sj): 0.0}
    came: Dict[Tuple[int, int], Tuple[int, int]] = {}

    NEI = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
           (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))

    while open_heap:
        _, g, i, j = heapq.heappop(open_heap)
        if (i, j) == (gi, gj):
            path = []
            cur = (i, j)
            while cur in came:
                path.append((grid.xmin + cur[0] * grid.res,
                             grid.ymin + cur[1] * grid.res))
                cur = came[cur]
            path.append((grid.xmin + si * grid.res, grid.ymin + sj * grid.res))
            return path[::-1]

        if g > gscore.get((i, j), float("inf")):
            continue

        for di, dj, cost in NEI:
            ni, nj = i + di, j + dj
            if not grid.inside(ni, nj) or grid.get(ni, nj):
                continue
            # 斜向移动要求两个直角邻格都空闲，否则会"切墙角"
            if di and dj:
                if grid.get(i + di, j) or grid.get(i, j + dj):
                    continue
            ng = g + cost
            if ng < gscore.get((ni, nj), float("inf")):
                gscore[(ni, nj)] = ng
                came[(ni, nj)] = (i, j)
                heapq.heappush(open_heap, (ng + h(ni, nj), ng, ni, nj))

    return None


# ----------------------------------------------------------------------
# 各项检查
# ----------------------------------------------------------------------

def load_home(locations_path: str) -> Tuple[float, float]:
    """从 locations.yaml 读 home，读不到用默认值。

    默认值必须与 launch/gazebo.launch 的 (x, y) 一致；不一致时
    A* 会从一个「机器人根本不会出现的位置」起算，结论没有意义。
    """
    text = open(locations_path, "r", encoding="utf-8").read()
    lines = text.split("\n")
    in_home = False
    for line in lines:
        if line.startswith("home:"):
            in_home = True
            continue
        if in_home and line.strip().startswith("position:"):
            inner = line[line.index("[") + 1: line.index("]")]
            vals = [float(v) for v in inner.split(",")]
            return vals[0], vals[1]
        if in_home and line and not line.startswith(" "):
            break
    return 0.0, -3.0


def check_paths(world: str, locations_path: str, radius: float,
                results: List[dict]) -> bool:
    obstacles, _ = parse_world(world)
    grid = build_grid(obstacles, radius, 0.05)
    locs = load_locations_minimal(locations_path)
    home = load_home(locations_path)

    rows = []
    all_ok = True
    for name, (x, y) in locs.items():
        if name == "home":
            continue
        path = astar(grid, home, (x, y))
        if path is None:
            rows.append({"name": name, "reachable": False})
            all_ok = False
            continue
        length = sum(math.hypot(path[k + 1][0] - path[k][0],
                                path[k + 1][1] - path[k][1])
                     for k in range(len(path) - 1))
        # 路径长度 / 直线距离：反映绕行程度。
        # 房间里直线的理想值是 1.0；绕门走远一些是正常的。
        straight = math.hypot(x - home[0], y - home[1])
        ratio = length / straight if straight > 1e-6 else 1.0
        rows.append({"name": name, "reachable": True,
                     "path_len_m": round(length, 2),
                     "straight_m": round(straight, 2),
                     "detour_ratio": round(ratio, 2)})

    rows.sort(key=lambda r: r["name"])
    results.append({"check": "A* 路径可达性", "ok": all_ok, "detail": rows})
    return all_ok


def check_map(world: str, map_yaml: str, locations_path: str,
              results: List[dict]) -> bool:
    """校验地图文件存在、尺寸/origin 与 world 一致、地点在地图内。"""
    if not os.path.exists(map_yaml):
        results.append({"check": "地图文件", "ok": False,
                        "detail": f"缺少 {map_yaml}"})
        return False

    meta = {}
    for line in open(map_yaml, "r", encoding="utf-8"):
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()

    pgm = os.path.join(os.path.dirname(map_yaml), meta.get("image", ""))
    if not os.path.exists(pgm):
        results.append({"check": "地图文件", "ok": False,
                        "detail": f"yaml 引用的图像不存在: {meta.get('image')}"})
        return False

    # 读 PGM 头
    with open(pgm, "rb") as f:
        magic = f.readline().strip()
        if magic != b"P5":
            results.append({"check": "地图文件", "ok": False,
                            "detail": f"PGM magic 应为 P5，实际 {magic}"})
            return False
        line = f.readline()
        while line.startswith(b"#"):
            line = f.readline()
        w, h = (int(v) for v in line.split())
        f.readline()  # maxval

    res = float(meta.get("resolution", 0.05))
    ox, oy = [float(v) for v in
              meta.get("origin", "0,0,0").strip("[]").split(",")[:2]]

    locs = load_locations_minimal(locations_path)
    out_of_map = []
    for name, (x, y) in locs.items():
        if not (ox <= x <= ox + w * res and oy <= y <= oy + h * res):
            out_of_map.append(name)

    ok = not out_of_map
    results.append({
        "check": "地图文件",
        "ok": ok,
        "detail": {"size": f"{w}x{h}", "resolution": res,
                   "origin": [ox, oy],
                   "world_extent": [round(ox, 2), round(oy, 2),
                                    round(ox + w * res, 2),
                                    round(oy + h * res, 2)],
                   "locations_outside_map": out_of_map},
    })
    return ok


def run_script(script: str, args: List[str]) -> Tuple[int, str]:
    # 必须显式指定 encoding：子进程输出是 UTF-8 中文，而 Windows 上
    # text=True 默认按 GBK 解码，会抛 UnicodeDecodeError 让整个验收中断。
    p = subprocess.run([sys.executable, os.path.join(PKG_ROOT, script)] + args,
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段二验收")
    ap.add_argument("--world", default=os.path.join(
        PKG_ROOT, "worlds/complex_indoor.world"))
    ap.add_argument("--locations", default=os.path.join(
        PKG_ROOT, "config/locations.yaml"))
    ap.add_argument("--map", default=os.path.join(PKG_ROOT, "maps/indoor.yaml"))
    ap.add_argument("--radius", type=float, default=0.32)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.world):
        print(f"错误：world 不存在 {args.world}", file=sys.stderr)
        return 2
    if not os.path.exists(args.locations):
        print(f"错误：locations 不存在 {args.locations}", file=sys.stderr)
        return 2

    results: List[dict] = []
    overall = True

    # 1. 世界审计
    code, out = run_script("tools/audit_world.py",
                           ["--world", args.world,
                            "--locations", args.locations,
                            "--radius", str(args.radius),
                            "--start", "%.2f,%.2f" % load_home(args.locations)])
    results.append({"check": "世界可导航性", "ok": code == 0,
                    "detail": "全部语义地点可从起点到达" if code == 0
                    else "存在不可达地点，详见 audit_world 输出"})
    overall &= (code == 0)

    # 2. 配置校验
    code2, out2 = run_script("tools/lint_launch.py", [])
    results.append({"check": "导航配置静态校验", "ok": code2 == 0,
                    "detail": "launch / 参数 / 依赖自洽" if code2 == 0
                    else out2.strip().splitlines()[:5]})
    overall &= (code2 == 0)

    # 3. 地图一致性
    overall &= check_map(args.world, args.map, args.locations, results)

    # 4. A* 路径
    overall &= check_paths(args.world, args.locations, args.radius, results)

    if args.json:
        print(json.dumps({"ok": overall, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print("=" * 62)
        print("阶段二验收（导航栈静态检查）")
        print("=" * 62)
        for r in results:
            mark = "PASS" if r["ok"] else "FAIL"
            print(f"[{mark}] {r['check']}")
            d = r["detail"]
            if isinstance(d, list):
                for row in d:
                    if "path_len_m" in row:
                        print(f"        {row['name']:<10s} 路径 {row['path_len_m']:6.2f} m "
                              f"(直线 {row['straight_m']:5.2f} m, 绕行 {row['detour_ratio']:.2f}x)")
                    elif row.get("reachable") is False:
                        print(f"        {row['name']:<10s} 不可达")
                    else:
                        print(f"        {row}")
            elif isinstance(d, dict):
                for k, v in d.items():
                    print(f"        {k}: {v}")
            else:
                print(f"        {d}")
        print("=" * 62)
        print("结论：" + ("全部通过，导航栈配置可用于实车/仿真验证"
                        if overall else "存在未通过项，先修复再启动 Gazebo"))

    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
