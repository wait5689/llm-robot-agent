#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
世界文件可导航性审计（World Navigability Audit）
================================================

用途
----
在把 Agent 送上 Gazebo 之前，先用纯几何方式回答一个问题：
**「从起点出发，世界里每个语义地点到底走不走得到？」**

为什么需要它
------------
move_base 的失败是「运行时」才知道的，而且一次要跑几十秒。
而绝大多数导航失败其实是**场景设计问题**，完全可以离线算出来：

- 墙没留门洞 → 房间是封闭盒子，目标必然 GOAL_UNREACHABLE
- 目标点落在障碍物里 → 规划器直接 abort
- 走廊被家具堵死 → 只有部分房间可达

这些问题在 Gazebo 里表现为「模型乱转」「报 aborted」，
排查要开 RViz、看 costmap、翻日志，非常耗时。
本工具把它们提前暴露成一张表。

算法
----
1. 解析 world 中的 `<model>`（box / cylinder），提取位姿与尺寸
2. 按机器人半径做圆形膨胀（近似 footprint 外接圆）
3. 用 BFS 在膨胀后的占据栅格上计算连通域
4. 检查每个语义地点：是否被占据、是否与起点同属一个连通域

用法
----
    python3 tools/audit_world.py --world worlds/complex_indoor.world \\
            --locations config/locations.yaml
    python3 tools/audit_world.py --world ... --json     # 机器可读输出

退出码：0 = 全部可达，1 = 有地点不可达，2 = 输入错误
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import deque
from typing import Dict, List, Optional, Tuple

# ----------------------------------------------------------------------
# world 解析
# ----------------------------------------------------------------------

_MODEL_RE = re.compile(r'<model\s+name="([^"]+)"[^>]*>(.*?)</model>', re.S)
_POSE_RE = re.compile(r'<pose[^>]*>([-\d.eE\s]+)</pose>')
_BOX_RE = re.compile(r"<box>\s*<size>([-\d.eE\s]+)</size>\s*</box>", re.S)
_CYL_RE = re.compile(r"<cylinder>\s*<radius>([-\d.eE]+)</radius>\s*<length>([-\d.eE]+)</length>\s*</cylinder>", re.S)
_STATIC_RE = re.compile(r"<static>\s*(true|false)\s*</static>", re.I)
_URI_RE = re.compile(r"<\s*uri>\s*model://([^<\s]+)\s*</uri>")


def _floats(text: str) -> List[float]:
    return [float(v) for v in text.split()]


class Footprint:
    """世界中的一个占据体，抽象成轴对齐矩形（斜置模型取外接框）。"""

    __slots__ = ("name", "cx", "cy", "hx", "hy", "kind", "exact", "zmin", "zmax")

    def __init__(self, name, cx, cy, hx, hy, kind="box", exact=True,
                 zmin=0.0, zmax=1.0):
        self.name = name
        self.cx = cx
        self.cy = cy
        self.hx = hx
        self.hy = hy
        self.kind = kind
        self.exact = exact   # False 表示用默认尺寸估算（如 model:// 引用的家具）
        self.zmin = zmin     # 占据体在 z 方向的底面
        self.zmax = zmax     # 顶面

    def overlaps_robot(self, robot_zmin: float, robot_zmax: float) -> bool:
        """是否与机器人本体在高度上相交。

        门洞上方的过梁（z 从 0.85 到 1.0）不与 0.2m 高的机器人相交，
        因此不应阻挡机器人通过——这正是「门洞」与「实心墙」的区别。
        """
        return not (self.zmax <= robot_zmin or self.zmin >= robot_zmax)

    def __repr__(self) -> str:
        return (f"<{self.name} {self.kind} @({self.cx:.2f},{self.cy:.2f}) "
                f"size={self.hx * 2:.2f}x{self.hy * 2:.2f} z=[{self.zmin:.2f},{self.zmax:.2f}]>")


def parse_world(path: str, default_size: float = 0.4) -> Tuple[List[Footprint], List[str]]:
    """返回 (占据体列表, 告警列表)。

    未声明几何的模型（常见于 `model://` 引用的家具库）按 `default_size` 的
    正方形保守计入——宁可误判为障碍，也不要漏判导致路径规划失败。
    """
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()

    obstacles: List[Footprint] = []
    warnings: List[str] = []

    for m in _MODEL_RE.finditer(src):
        name, body = m.group(1), m.group(2)
        if name.lower().startswith(("target", "marker", "goal", "wp_")):
            continue  # 标记类模型不是障碍

        pm = _POSE_RE.search(body)
        if not pm:
            warnings.append(f"{name}: 缺少 <pose>，跳过")
            continue
        p = _floats(pm.group(1))
        cx, cy = p[0], p[1]
        yaw = p[5] if len(p) >= 6 else 0.0

        box = _BOX_RE.search(body)
        cyl = _CYL_RE.search(body)

        if box:
            s = _floats(box.group(1))
            hx, hy = s[0] / 2.0, s[1] / 2.0
            sz = s[2] if len(s) > 2 else 1.0
            if abs(math.sin(yaw)) > 0.5:  # 绕 z 转 90° 附近，交换半轴
                hx, hy = hy, hx
            zmin = p[2] - sz / 2.0
            zmax = p[2] + sz / 2.0
            obstacles.append(Footprint(name, cx, cy, hx, hy, "box",
                                       zmin=zmin, zmax=zmax))
        elif cyl:
            r = float(cyl.group(1))
            length = float(cyl.group(2))
            obstacles.append(Footprint(name, cx, cy, r, r, "cylinder",
                                       zmin=p[2] - length / 2.0,
                                       zmax=p[2] + length / 2.0))
        else:
            uri = _URI_RE.search(body)
            hint = f" (引用 {uri.group(1)})" if uri else ""
            warnings.append(
                f"{name}: 未声明 box/cylinder 几何{hint}，"
                f"按 {default_size:.1f}m 正方形保守计入"
            )
            obstacles.append(Footprint(name, cx, cy,
                                       default_size / 2, default_size / 2,
                                       "assumed", exact=False,
                                       zmin=0.0, zmax=1.0))

    return obstacles, warnings


# ----------------------------------------------------------------------
# 栅格与连通性
# ----------------------------------------------------------------------

class Grid:
    def __init__(self, xmin, xmax, ymin, ymax, resolution):
        self.res = resolution
        self.xmin, self.ymin = xmin, ymin
        self.w = int(round((xmax - xmin) / resolution))
        self.h = int(round((ymax - ymin) / resolution))
        self.data = bytearray(self.w * self.h)  # 1 = 占据

    def to_cell(self, x: float, y: float) -> Tuple[int, int]:
        """世界坐标 -> 栅格索引。

        必须用 floor 而非 int()：int() 向零截断，对 -0.5 返回 0 而不是 -1，
        会让原点左侧的所有格子偏移一格，矩形填充会向左无限扩张。
        """
        return (math.floor((x - self.xmin) / self.res),
                math.floor((y - self.ymin) / self.res))

    def inside(self, i: int, j: int) -> bool:
        return 0 <= i < self.w and 0 <= j < self.h

    def get(self, i: int, j: int) -> int:
        return self.data[j * self.w + i]

    def set(self, i: int, j: int, v: int = 1) -> None:
        if self.inside(i, j):
            self.data[j * self.w + i] = v

    def stamp_rect(self, fp: Footprint, inflate: float = 0.0) -> None:
        """矩形 Minkowski 膨胀：对四个边分别外移 inflate 米。

        半开区间 [i0, i1) 遍历：闭区间会在外扩量正好落在格边界时多占
        一列/一行（例如 0.075 + 0.32 恰好是 0.05 的整数倍）。

        注意：**不能**用「矩形 + 外接圆」的方式膨胀。细长墙（如 0.15×6）
        若按外接圆处理，两端会鼓出半径 0.32 的圆盘，把墙外 0.32m 内的
        走廊整片吃掉——这正是第一版把 12m 走廊判成全堵的原因。
        """
        hx, hy = fp.hx + inflate, fp.hy + inflate
        i0, j0 = self.to_cell(fp.cx - hx, fp.cy - hy)
        i1, j1 = self.to_cell(fp.cx + hx, fp.cy + hy)
        for j in range(max(0, j0), min(self.h, j1)):
            base = j * self.w
            for i in range(max(0, i0), min(self.w, i1)):
                self.data[base + i] = 1

    def stamp_cylinder(self, fp: Footprint, inflate: float = 0.0) -> None:
        """圆柱体及其膨胀：以圆心为中心的圆盘。"""
        r = fp.hx + inflate
        ri = int(math.ceil(r / self.res))
        ci, cj = self.to_cell(fp.cx, fp.cy)
        r2 = (r / self.res) ** 2
        for dj in range(-ri, ri + 1):
            for di in range(-ri, ri + 1):
                if di * di + dj * dj <= r2:
                    self.set(ci + di, cj + dj, 1)

    def component_of(self, start: Tuple[int, int]) -> set:
        """四邻域 BFS，返回与起点连通的自由格集合。"""
        if not self.inside(*start) or self.get(*start):
            return set()
        seen = {start}
        q = deque([start])
        while q:
            i, j = q.popleft()
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ni, nj = i + di, j + dj
                if self.inside(ni, nj) and not self.get(ni, nj) and (ni, nj) not in seen:
                    seen.add((ni, nj))
                    q.append((ni, nj))
        return seen


def build_grid(obstacles: List[Footprint], robot_radius: float,
               resolution: float = 0.05,
               bounds: Optional[Tuple[float, float, float, float]] = None,
               robot_zmin: float = 0.05, robot_zmax: float = 0.35) -> Grid:
    if bounds is None:
        xs = [o.cx - o.hx for o in obstacles] + [o.cx + o.hx for o in obstacles]
        ys = [o.cy - o.hy for o in obstacles] + [o.cy + o.hy for o in obstacles]
        if not xs:
            xs, ys = [-5.0, 5.0], [-5.0, 5.0]
        bounds = (min(xs) - 1.0, max(xs) + 1.0, min(ys) - 1.0, max(ys) + 1.0)

    g = Grid(bounds[0], bounds[1], bounds[2], bounds[3], resolution)
    for fp in obstacles:
        # 高度过滤：门洞过梁、悬空横梁不与机器人躯干相交，不构成阻挡。
        # 这一条是「有门洞的世界」能被正确判定的前提。
        if not fp.overlaps_robot(robot_zmin, robot_zmax):
            continue
        # 先画实体本身，再画膨胀区。两步都必需：
        # 只画膨胀区会让墙体内部变成"空洞"，机器人会以为可以穿墙。
        if fp.kind == "cylinder":
            g.stamp_cylinder(fp, 0.0)
            g.stamp_cylinder(fp, robot_radius)
        else:
            g.stamp_rect(fp, 0.0)
            g.stamp_rect(fp, robot_radius)
    return g


# ----------------------------------------------------------------------
# 地点表加载（不依赖 PyYAML，保持本工具零第三方依赖）
# ----------------------------------------------------------------------

def load_locations_minimal(path: str) -> Dict[str, Tuple[float, float]]:
    """解析 locations.yaml 中 `位置: [x, y]` 形式，够用即可。"""
    out: Dict[str, Tuple[float, float]] = {}
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current: Optional[str] = None
    in_locations = False
    for raw in lines:
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if re.match(r"^locations:\s*$", line):
            in_locations = True
            continue
        if re.match(r"^\S", line) and not line.startswith("locations:"):
            in_locations = False
            current = None
        indent = len(line) - len(line.lstrip())
        m = re.match(r"^\s*([\w\u4e00-\u9fa5]+):\s*$", line)
        if m and (in_locations or indent == 0):
            current = m.group(1)
            continue
        m = re.match(r"^\s*position:\s*\[([-\d.eE,\s]+)\]", line)
        if m and current:
            xy = _floats(m.group(1).replace(",", " "))
            if len(xy) >= 2:
                out[current] = (xy[0], xy[1])
    return out


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------

def audit(world: str, locations_yaml: Optional[str], robot_radius: float,
          start: Tuple[float, float] = (0.0, -3.0),
          resolution: float = 0.05) -> Dict:
    obstacles, warnings = parse_world(world)
    grid = build_grid(obstacles, robot_radius, resolution)

    start_cell = grid.to_cell(*start)
    if grid.inside(*start_cell) and grid.get(*start_cell):
        start_status = "occupied"
        reachable: set = set()
    else:
        start_status = "free"
        reachable = grid.component_of(start_cell)

    free_total = grid.w * grid.h - sum(grid.data)
    locs = load_locations_minimal(locations_yaml) if locations_yaml else {}
    if locations_yaml and not locs:
        warnings.append(f"{locations_yaml}: 未解析出任何位置条目")

    # home 有时写在顶层而非 locations 下
    if locations_yaml:
        with open(locations_yaml, "r", encoding="utf-8") as f:
            top = f.read()
        m = re.search(r"^home:\s*\n\s*position:\s*\[([-\d.eE,\s]+)\]", top, re.M)
        if m and "home" not in locs:
            xy = _floats(m.group(1).replace(",", " "))
            locs["home"] = (xy[0], xy[1])

    results = []
    for name, (x, y) in locs.items():
        i, j = grid.to_cell(x, y)
        if not grid.inside(i, j):
            results.append({"name": name, "x": x, "y": y,
                            "status": "out_of_bounds"})
        elif grid.get(i, j):
            results.append({"name": name, "x": x, "y": y,
                            "status": "occupied"})
        elif (i, j) in reachable:
            results.append({"name": name, "x": x, "y": y,
                            "status": "reachable"})
        else:
            results.append({"name": name, "x": x, "y": y,
                            "status": "unreachable"})

    return {
        "world": world,
        "robot_radius": robot_radius,
        "resolution": resolution,
        "start": {"x": start[0], "y": start[1], "status": start_status},
        "obstacle_count": len(obstacles),
        "reachable_area_m2": round(len(reachable) * resolution ** 2, 2),
        "free_area_m2": round(free_total * resolution ** 2, 2),
        "locations": results,
        "warnings": warnings,
        "all_reachable": all(r["status"] == "reachable" for r in results) and results,
    }


_ICON = {
    "reachable": "OK   ",
    "unreachable": "NO-PATH",   # 自由格，但与起点不连通
    "occupied": "OCCUPIED",     # 目标点本身落在障碍（含膨胀）内
    "out_of_bounds": "OOB   ",
}


def main() -> int:
    ap = argparse.ArgumentParser(description="世界文件可导航性审计")
    ap.add_argument("--world", required=True)
    ap.add_argument("--locations", default=None)
    ap.add_argument("--radius", type=float, default=0.32,
                    help="机器人外接圆半径（米），默认 0.32")
    ap.add_argument("--start", default="0,-3", help="起点 x,y")
    ap.add_argument("--resolution", type=float, default=0.05)
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    if not os.path.exists(args.world):
        print(f"错误：world 文件不存在 {args.world}", file=sys.stderr)
        return 2
    if args.locations and not os.path.exists(args.locations):
        print(f"错误：locations 文件不存在 {args.locations}", file=sys.stderr)
        return 2

    try:
        sx, sy = (float(v) for v in args.start.split(","))
    except ValueError:
        print("错误：--start 格式应为 x,y", file=sys.stderr)
        return 2

    report = audit(args.world, args.locations, args.radius,
                   (sx, sy), args.resolution)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["all_reachable"] else 1

    print(f"世界文件: {report['world']}")
    print(f"障碍体:   {report['obstacle_count']} 个 | "
          f"机器人半径: {report['robot_radius']}m | "
          f"栅格: {report['resolution']}m")
    print(f"起点状态: {report['start']['status']} "
          f"({report['start']['x']:.1f}, {report['start']['y']:.1f})")
    print(f"可达面积: {report['reachable_area_m2']} m² / "
          f"自由总面积 {report['free_area_m2']} m²")
    print()

    if not report["locations"]:
        print("（未提供 locations.yaml，仅输出几何统计）")
    else:
        print(f"{'地点':<12} {'坐标':<16} {'判定'}")
        print("-" * 44)
        for r in report["locations"]:
            print(f"{r['name']:<12} "
                  f"({r['x']:>6.2f},{r['y']:>6.2f})   "
                  f"{_ICON.get(r['status'], r['status'])}")

    if report["warnings"]:
        print("\n告警:")
        for w in report["warnings"]:
            print(f"  - {w}")

    print()
    if report["all_reachable"]:
        print("结论：全部语义地点均可从起点到达。")
        return 0

    bad = [r for r in report["locations"] if r["status"] != "reachable"]
    print(f"结论：{len(bad)} 个地点无法到达 —— 导航栈会在这些目标上必然失败。")
    print("常见原因：墙体未留门洞 / 目标点落在障碍内 / 走廊被堵死。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
