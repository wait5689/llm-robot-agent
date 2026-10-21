#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语义地点体检工具
================================================================

把 locations.yaml 里每个地点与 worlds/*.world 的实际几何做交叉校验，
在启动 Gazebo 之前就把「目标点不可达 / 太窄」这类问题找出来。

为什么需要这个工具
----------------------------------------------------------------
几个典型的、排查成本极高的故障，本工具都能提前发现：

1. 地点落在障碍物里
   home 曾写成 (0.80,-3.60)，正好压在半径 0.30m 的充电桩上。
   现象是「所有地点都不可达」，因为 home 同时是 A* 起点与生成位姿。

2. 地点离墙/家具太近（DWA 原地僵死）
   净空低于 inflation_radius + footprint 外接半径时，
   机器人自身 footprint 的角点就落进膨胀层高代价区，
   DWA 认为所有采样轨迹都会碰撞，于是「选不动」：
   以 15Hz 持续发布 cmd_vel 但速度恒为 0，目标永不结束，
   日志里没有任何报错 —— 这是本项目最难定位的一类故障。
   实测阈值约 0.71m，建议留到 0.95m 以上。

3. 地点之间不连通
   门口被家具挡死、房间被墙隔断等情况，用膨胀后的栅格做 A* 判定。

用法
----------------------------------------------------------------
    cd ~/catkin_ws/src/llm_robot_agent

    # 基本检查（自动找 worlds/*.world 与 config/locations.yaml）
    python3 tools/check_locations.py

    # 指定文件
    python3 tools/check_locations.py \\
        --world worlds/complex_indoor.world \\
        --locations config/locations.yaml

    # 调整判定阈值
    python3 tools/check_locations.py --inflation 0.35 --min-clearance 0.95

退出码
----------------------------------------------------------------
    0  全部通过
    1  有地点不合格（位置被占 / 净空不足 / 互不可达）

作者备注
----------------------------------------------------------------
判定用的膨胀是「几何膨胀」: 把每个障碍物的包围盒向外扩 inflation_radius,
这与 costmap_2d 的 InflationLayer 在障碍物附近的行为一致。
代价地图真正的代价衰减（cost_scaling_factor）不参与判定，
因为我们只关心「能不能走」，不关心「走得多贵」。
"""

import argparse
import glob
import math
import os
import sys

try:
    import yaml
except ImportError:
    sys.stderr.write("需要 PyYAML: pip3 install pyyaml\n")
    raise SystemExit(1)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from audit_world import parse_world
except ImportError:
    sys.stderr.write(
        "无法导入 audit_world。请确认本脚本与 audit_world.py 在同一目录。\n"
    )
    raise SystemExit(1)


# ---------------------------------------------------------------- 几何查询
class Geometry(object):
    """封装障碍物集合，提供「到最近障碍的距离」查询。"""

    def __init__(self, obstacles, z_lo=0.05, z_hi=1.2):
        # 只保留会挡住机器人的障碍：地面以上到机身高度之间的
        self.obs = [o for o in obstacles if o.overlaps_robot(z_lo, z_hi)]

    def clearance(self, x, y):
        """(x,y) 到最近障碍物表面的距离。

        在障碍内部时返回负值（穿透深度），便于区分「贴边」与「压上」。
        """
        best = 99.0
        for o in self.obs:
            if o.kind == "cylinder":
                d = math.hypot(x - o.cx, y - o.cy) - o.hx
            else:
                dx = max(abs(x - o.cx) - o.hx, 0.0)
                dy = max(abs(y - o.cy) - o.hy, 0.0)
                if dx > 0 or dy > 0:
                    d = math.hypot(dx, dy)
                else:
                    # 在盒内：取到最近边的距离（负）
                    d = -min(o.hx - abs(x - o.cx), o.hy - abs(y - o.cy))
            if d < best:
                best = d
        return best

    def nearest(self, x, y):
        """返回 (距离, 物体名)，用于报错时指出「被谁挡住」。"""
        best = (99.0, None)
        for o in self.obs:
            if o.kind == "cylinder":
                d = math.hypot(x - o.cx, y - o.cy) - o.hx
            else:
                dx = max(abs(x - o.cx) - o.hx, 0.0)
                dy = max(abs(y - o.cy) - o.hy, 0.0)
                if dx > 0 or dy > 0:
                    d = math.hypot(dx, dy)
                else:
                    d = -min(o.hx - abs(x - o.cx), o.hy - abs(y - o.cy))
            if d < best[0]:
                best = (d, o.name)
        return best


# ---------------------------------------------------------------- 可达性
class Grid(object):
    """膨胀后的占据栅格 + A*，用于判定两点是否连通。"""

    def __init__(self, geometry, inflation, bounds, resolution=0.05):
        self.res = resolution
        self.xmin, self.xmax, self.ymin, self.ymax = bounds
        self.w = int(round((self.xmax - self.xmin) / resolution))
        self.h = int(round((self.ymax - self.ymin) / resolution))
        self.data = bytearray(self.w * self.h)  # 0=free 1=blocked
        self._rasterize(geometry, inflation)

    def _cell(self, x, y):
        return (int((x - self.xmin) / self.res),
                int((y - self.ymin) / self.res))

    def _center(self, i, j):
        return (self.xmin + (i + 0.5) * self.res,
                self.ymin + (j + 0.5) * self.res)

    def _rasterize(self, geometry, inflation):
        W, H, res = self.w, self.h, self.res

        # ★ 必须按「到障碍表面的真实欧氏距离」判断是否被膨胀覆盖，
        #   不能对矩形做「hx+inflation, hy+inflation」的轴对齐扩张。
        #
        #   矩形扩张会把四个角也扩成障碍（相当于外接正方形膨胀），
        #   比真实的 Minkowski 和保守得多：一个 0.5x0.5 的箱子
        #   在膨胀 0.35 后，矩形扩张的角部会比真实圆角多覆盖 0.15m。
        #   本项目因此误报过 corridor 与 hall「在膨胀栅格上是占据格」，
        #   而这两个点用 clearance() 量出来是 1.76m —— 远高于安全线。
        #   反过来，如果坐标真的紧贴家具，clearance() 也会一致地报小。
        #   所以这里直接复用 geometry.clearance()，保证
        #   「单点净空」与「栅格连通性」两套判断口径完全一致。
        for o in geometry.obs:
            if o.kind == "cylinder":
                hx = hy = o.hx + inflation
            else:
                # 包围盒外扩 amount 后再逐个格子用真实距离判定，
                # amount 只是候选范围，不参与最终判断。
                hx, hy = o.hx + inflation, o.hy + inflation

            i0, j0 = self._cell(o.cx - hx, o.cy - hy)
            i1, j1 = self._cell(o.cx + hx, o.cy + hy)
            for j in range(max(0, j0), min(H, j1 + 1)):
                y = self.ymin + (j + 0.5) * res
                base = j * W
                for i in range(max(0, i0), min(W, i1 + 1)):
                    x = self.xmin + (i + 0.5) * res
                    # 到该障碍表面的真实距离
                    if o.kind == "cylinder":
                        d = math.hypot(x - o.cx, y - o.cy) - o.hx
                    else:
                        dx = max(abs(x - o.cx) - o.hx, 0.0)
                        dy = max(abs(y - o.cy) - o.hy, 0.0)
                        if dx > 0 or dy > 0:
                            d = math.hypot(dx, dy)
                        else:
                            # 在盒内
                            d = -min(o.hx - abs(x - o.cx),
                                     o.hy - abs(y - o.cy))
                    if d < inflation:
                        self.data[base + i] = 1

        # 场地边界外不可通行（留一点余量，模拟墙体）
        #
        # ★ 边界要用 bounds 的实际四条边，不能写成
        #   abs(x) > max(|xmin|,|xmax|) - 0.95
        #   那种写法在原点不对称的 bounds 下会把一侧的正常区域裁掉。
        for j in range(H):
            y = self.ymin + (j + 0.5) * res
            base = j * W
            for i in range(W):
                x = self.xmin + (i + 0.5) * res
                if (x < self.xmin + 0.95 or x > self.xmax - 0.95 or
                        y < self.ymin + 0.95 or y > self.ymax - 0.95):
                    self.data[base + i] = 1

    def is_free(self, x, y):
        i, j = self._cell(x, y)
        if 0 <= i < self.w and 0 <= j < self.h:
            return self.data[j * self.w + i] == 0
        return False

    def astar(self, start, goal):
        """A*，8 邻域。返回路径长度；不可达返回 None。"""
        import heapq

        si, sj = self._cell(*start)
        gi, gj = self._cell(*goal)
        W, H = self.w, self.h
        if not (0 <= si < W and 0 <= sj < H and 0 <= gi < W and 0 <= gj < H):
            return None
        if self.data[sj * W + si] or self.data[gj * W + gi]:
            return None

        target = gj * W + gi
        dist = {sj * W + si: 0.0}
        prev = {}
        seen = set()
        pq = [(0.0, sj * W + si)]
        moves = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                 (1, 1, 1.41421), (1, -1, 1.41421),
                 (-1, 1, 1.41421), (-1, -1, 1.41421))

        while pq:
            d, cur = heapq.heappop(pq)
            if cur in seen:
                continue
            seen.add(cur)
            if cur == target:
                # 回溯求长度
                total = 0.0
                c = cur
                while c in prev:
                    p = prev[c]
                    cj, ci = divmod(c, W)
                    pj, pi = divmod(p, W)
                    total += math.hypot(ci - pi, cj - pj)
                    c = p
                return total * self.res

            cj, ci = divmod(cur, W)
            for di, dj, cost in moves:
                ni, nj = ci + di, cj + dj
                if not (0 <= ni < W and 0 <= nj < H):
                    continue
                nidx = nj * W + ni
                if self.data[nidx] or nidx in seen:
                    continue
                nd = d + cost
                if nd < dist.get(nidx, 1e18):
                    dist[nidx] = nd
                    prev[nidx] = cur
                    h = math.hypot(gi - ni, gj - nj)
                    heapq.heappush(pq, (nd + h, nidx))
        return None


# ---------------------------------------------------------------- 主流程
def load_locations(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    frame = doc.get("frame_id", "map")
    home = doc["home"]
    items = [("home", home["position"][0], home["position"][1], home.get("yaw"))]
    for name, spec in (doc.get("locations") or {}).items():
        items.append((name, spec["position"][0], spec["position"][1],
                      spec.get("yaw")))
    return frame, items


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    pkg = os.path.dirname(here)

    ap = argparse.ArgumentParser(
        description="校验 locations.yaml 与世界几何是否自洽",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--world", default=None,
                    help="世界文件；默认找 %s/worlds/*.world" % pkg)
    ap.add_argument("--locations",
                    default=os.path.join(pkg, "config", "locations.yaml"),
                    help="locations.yaml 路径")
    ap.add_argument("--inflation", type=float, default=0.35,
                    help="与 costmap_common_params.yaml 的 inflation_radius 一致")
    ap.add_argument("--min-clearance", type=float, default=0.95,
                    help="每个地点要求的最小净空（默认 0.95m）")
    ap.add_argument("--resolution", type=float, default=0.05,
                    help="可达性判定的栅格分辨率")
    args = ap.parse_args()

    world = args.world
    if world is None:
        # 优先用导航实际使用的那张图对应的世界，
        # 而不是目录里按字母序排第一的文件 —— 否则会拿
        # 早期 demo 的 car_world.world 去校验 v3 布局的坐标，
        # 得到的净空数值完全无意义（实测会报出 3.44m 这种假象）。
        preferred = os.path.join(pkg, "worlds", "complex_indoor.world")
        if os.path.isfile(preferred):
            world = preferred
        else:
            cands = sorted(glob.glob(os.path.join(pkg, "worlds", "*.world")))
            if not cands:
                sys.stderr.write("找不到 worlds/*.world，请用 --world 指定\n")
                return 1
            world = cands[0]
            sys.stderr.write(
                "警告: 未找到 worlds/complex_indoor.world，"
                "退回使用 %s\n" % os.path.basename(world))

    print("=" * 68)
    print("语义地点体检")
    print("=" * 68)
    print("世界文件 : %s" % world)
    print("地点文件 : %s" % args.locations)
    print("膨胀半径 : %.2f m" % args.inflation)
    print("净空下限 : %.2f m" % args.min_clearance)
    print()

    obstacles, _ = parse_world(world)
    geo = Geometry(obstacles)
    frame, items = load_locations(args.locations)

    print("坐标系   : %s" % frame)
    print("地点数量 : %d" % len(items))
    print()

    # --- 逐点检查 ---
    print("-" * 68)
    print("%-10s %-16s %-10s %-10s %s" %
          ("地点", "坐标", "净空(m)", "判定", "最近物体"))
    print("-" * 68)

    # footprint 外接半径用于理论阈值提示
    foot_circum = math.hypot(0.28, 0.23)
    theory_min = args.inflation + foot_circum

    bad = []
    for name, x, y, yaw in items:
        c = geo.clearance(x, y)
        dist, who = geo.nearest(x, y)

        if c < 0:
            verdict = "在障碍内"
            bad.append(name)
        elif c < theory_min:
            verdict = "太窄(僵死)"
            bad.append(name)
        elif c < args.min_clearance:
            verdict = "偏窄(注意)"
        else:
            verdict = "OK"

        yaw_s = "-" if yaw is None else "%+.2f" % yaw
        print("%-10s (%6.2f,%6.2f) %5s %8.2f   %-10s %s" % (
            name, x, y, yaw_s, c, verdict,
            who if (who and c < args.min_clearance) else ""))

    print("-" * 68)
    print("理论下限: inflation %.2f + footprint外接 %.3f = %.3f m"
          % (args.inflation, foot_circum, theory_min))
    print("低于理论下限时，机器人 footprint 角点会落进膨胀层高代价区，")
    print("DWA 会持续输出零速且不报错（原地僵死）。")
    print()

    # --- 可达性 ---
    bounds = (-7.0, 7.0, -6.0, 6.0)
    grid = Grid(geo, args.inflation, bounds, args.resolution)

    print("-" * 68)
    print("两两可达性（膨胀 %.2f m，栅格 %dx%d @ %.2fm）"
          % (args.inflation, grid.w, grid.h, args.resolution))
    print("-" * 68)

    # 先检查各点是否在栅格上自由
    trapped = []
    for name, x, y, yaw in items:
        if not grid.is_free(x, y):
            trapped.append(name)
            print("  %-10s 在膨胀栅格上是占据格 -> 无法作为目标" % name)
    if trapped:
        bad.extend(trapped)
        print()

    fail_pairs = 0
    total_pairs = 0
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            n1, x1, y1, _ = items[i]
            n2, x2, y2, _ = items[j]
            total_pairs += 1
            d1 = grid.astar((x1, y1), (x2, y2))
            d2 = grid.astar((x2, y2), (x1, y1))
            if d1 is None or d2 is None:
                fail_pairs += 1
                print("  FAIL  %-10s <-> %-10s  (%s)" % (
                    n1, n2,
                    "去程不通" if d1 is None else "回程不通"))

    print()
    if fail_pairs == 0 and not trapped:
        print("连通性: %d/%d 对全部双向可达 OK" % (total_pairs, total_pairs))
    else:
        print("连通性: %d/%d 对不通" % (fail_pairs, total_pairs))

    # --- 结论 ---
    print()
    print("=" * 68)
    if bad:
        print("结果: 不通过 —— %d 个地点有问题: %s" % (len(bad), ", ".join(bad)))
        print("=" * 68)
        return 1

    print("结果: 全部通过（%d 个地点，%d 对路径）" % (len(items), total_pairs))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
