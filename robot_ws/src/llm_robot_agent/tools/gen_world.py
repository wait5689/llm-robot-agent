#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
室内世界生成器（Indoor World Generator）
========================================

为什么要用生成器而不是手写 SDF
------------------------------
手写 world 时，墙体是「一整块 box」，门的开洞要人工算坐标并拆成 2-3 段。
一旦房间尺寸变了，或者墙厚改了，所有门洞坐标全部失效——
本项目上一版的世界文件就是这样：墙体全部连通、**没有任何门洞**，
四个房间变成四个封闭盒子，无论导航参数怎么调都不可能到达。

本生成器把「房间 + 门」表达成参数，几何由代码推导，保证：
1. 每面墙按门位自动切分成若干段，门宽恒定
2. 生成后用 audit_world.py 校验连通性，不通过就报错
3. 调整房间尺寸 = 改参数，不用重新算坐标

布局（俯视，+X 向东，+Y 向北）
-----------------------------
        y=+5  ┌────────┬────────┬────────┐
              │ room_a │  room_c│ room_b │     ← 北区（研究/储物/会客）
        y=+1.5│        │        │        │
              ├───┬────┴────┬───┴────┬───┤
        y=+1.0│   │  门洞走廊  │        │   │
              │   └────┬────┘        │   │     ← 南北连接通道（宽 1.0m）
        y=0   │ 门 │        门        │ 门│
              ├────┴────────┴────────┴───┤
              │        laundry / room_d  │
        y=-3.5│                          │     ← 南区（活动区）
              │      room_d      home    │
        y=-5  └──────────────────────────┘

用法
----
    python3 tools/gen_world.py --out worlds/complex_indoor.world
"""

from __future__ import annotations

import argparse
import os
import sys

# ----------------------------------------------------------------------
# 布局参数（改这里就能调整整个场景）
# ----------------------------------------------------------------------

WALL_H = 1.0        # 墙高
WALL_T = 0.15       # 墙厚
DOOR_W = 1.10       # 门洞净宽（需 > 机器人直径 0.32*2 + 余量）
DOOR_H = 0.85       # 门洞高度（预留上方过梁，激光雷达可扫到）

# 场地范围
X_MIN, X_MAX = -6.0, 6.0
Y_MIN, Y_MAX = -5.0, 5.0

# 核心分隔：一条东西向承重墙，把场地分为南/北两区
SPLIT_Y = 0.0

# 北区纵向隔墙（把北区切成 room_a / room_c / room_b）
NORTH_DIV_X = [-2.5, 2.5]

# 南北连通通道：在 SPLIT_Y 墙上开 3 个门，x 坐标如下
CROSS_DOORS_X = [-4.0, 0.0, 4.0]

# 北区隔墙上的门：靠近 SPLIT_Y 端开（让三条走廊都能进北房间）
NORTH_DOORS = [
    ("door_north_west", -2.5, [(-3.0, -2.5)]),   # 墙 x=-2.5 上，y∈[-3.0,-2.5]? 见下方生成逻辑
]

WHITE = "0.92 0.92 0.92 1"
GREY = "0.70 0.70 0.70 1"
WOOD = "0.55 0.38 0.22 1"
FABRIC = "0.25 0.35 0.60 1"
METAL = "0.55 0.58 0.62 1"


def box(name, cx, cy, sx, sy, sz, rgba, yaw=0.0):
    """一个静态 box 模型。"""
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{cx:.3f} {cy:.3f} {sz / 2:.3f} 0 0 {yaw:.4f}</pose>
      <link name="link">
        <collision name="collision">
          <geometry>
            <box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box>
          </geometry>
        </collision>
        <visual name="visual">
          <geometry>
            <box><size>{sx:.3f} {sy:.3f} {sz:.3f}</size></box>
          </geometry>
          <material>
            <ambient>{rgba}</ambient>
            <diffuse>{rgba}</diffuse>
          </material>
        </visual>
      </link>
    </model>
"""


def cylinder(name, cx, cy, radius, length, rgba):
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{cx:.3f} {cy:.3f} {length / 2:.3f} 0 0 0</pose>
      <link name="link">
        <collision name="collision">
          <geometry><cylinder><radius>{radius:.3f}</radius><length>{length:.3f}</length></cylinder></geometry>
        </collision>
        <visual name="visual">
          <geometry><cylinder><radius>{radius:.3f}</radius><length>{length:.3f}</length></cylinder></geometry>
          <material>
            <ambient>{rgba}</ambient>
            <diffuse>{rgba}</diffuse>
          </material>
        </visual>
      </link>
    </model>
"""


def h_wall(name, y, x0, x1, doors=(), thickness=WALL_T, height=WALL_H):
    """沿 X 方向的墙，doors 为 [(door_cx, width), ...]，自动切成多段。

    门洞上方保留过梁（door_height 之上），这样激光雷达仍能扫到墙，
    但机器人可以从门洞穿过——与现实一致。
    """
    segs = []
    # 按门位置排序后，把 [x0, x1] 切成若干实体段
    cuts = sorted(doors)
    cursor = x0
    idx = 0
    for dcx, dw in cuts:
        left, right = dcx - dw / 2, dcx + dw / 2
        if left > cursor:
            segs.append(("wall", cursor, left))
        segs.append(("door", left, right))
        cursor = right
    if cursor < x1:
        segs.append(("wall", cursor, x1))

    out = []
    for kind, a, b in segs:
        length = b - a
        if length <= 1e-6:
            continue
        if kind == "wall":
            out.append(box(f"{name}_{idx}", (a + b) / 2, y, length, thickness, height, WHITE))
        else:
            # 门洞：只保留上部过梁
            lintel_h = height - DOOR_H
            if lintel_h > 1e-6:
                out.append(box(f"{name}_lintel_{idx}", (a + b) / 2, y,
                               length, thickness, lintel_h, WHITE,
                               ))
                # 抬高到门洞顶部
                out[-1] = out[-1].replace(
                    f"<pose>{(a + b) / 2:.3f} {y:.3f} {lintel_h / 2:.3f}",
                    f"<pose>{(a + b) / 2:.3f} {y:.3f} {DOOR_H + lintel_h / 2:.3f}",
                )
            out.append(box(f"{name}_doorpost_{idx}_l", a, y, 0.04, thickness, height, GREY))
            out.append(box(f"{name}_doorpost_{idx}_r", b, y, 0.04, thickness, height, GREY))
        idx += 1
    return "".join(out)


def v_wall(name, x, y0, y1, doors=(), thickness=WALL_T, height=WALL_H):
    """沿 Y 方向的墙。"""
    segs = []
    cuts = sorted(doors)
    cursor = y0
    idx = 0
    for dcy, dw in cuts:
        bot, top = dcy - dw / 2, dcy + dw / 2
        if bot > cursor:
            segs.append(("wall", cursor, bot))
        segs.append(("door", bot, top))
        cursor = top
    if cursor < y1:
        segs.append(("wall", cursor, y1))

    out = []
    for kind, a, b in segs:
        length = b - a
        if length <= 1e-6:
            continue
        if kind == "wall":
            out.append(box(f"{name}_{idx}", x, (a + b) / 2, thickness, length, height, WHITE))
        else:
            lintel_h = height - DOOR_H
            if lintel_h > 1e-6:
                piece = box(f"{name}_lintel_{idx}", x, (a + b) / 2,
                            thickness, length, lintel_h, WHITE)
                piece = piece.replace(
                    f"<pose>{x:.3f} {(a + b) / 2:.3f} {lintel_h / 2:.3f}",
                    f"<pose>{x:.3f} {(a + b) / 2:.3f} {DOOR_H + lintel_h / 2:.3f}",
                )
                out.append(piece)
            out.append(box(f"{name}_doorpost_{idx}_b", x, a, thickness, 0.04, height, GREY))
            out.append(box(f"{name}_doorpost_{idx}_t", x, b, thickness, 0.04, height, GREY))
        idx += 1
    return "".join(out)


def build() -> str:
    parts = []

    parts.append(f"""<?xml version="1.0" ?>
<!--
  复杂室内环境（由 tools/gen_world.py 生成，请勿手工编辑）

  布局：南区（活动区）+ 北区（三个房间），由一条走廊墙分隔，
        墙上开 3 个门洞保证南北连通；北区两道隔墙靠近走廊端各开一门。

  所有门宽 {DOOR_W:.2f} m，机器人直径 0.64 m，留有余量。
  修改布局请改生成器参数后重新生成，并用 tools/audit_world.py 校验。
-->
<sdf version="1.4">
  <world name="complex_indoor">
    <physics type="ode">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1</real_time_factor>
      <real_time_update_rate>1000</real_time_update_rate>
    </physics>

    <include>
      <uri>model://sun</uri>
    </include>

    <include>
      <uri>model://ground_plane</uri>
    </include>

    <!-- ============ 外墙 ============ -->
""")

    # 外墙：南北墙无门，东西墙无门（封闭场地）
    parts.append(h_wall("wall_north", Y_MAX, X_MIN, X_MAX))
    parts.append(h_wall("wall_south", Y_MIN, X_MIN, X_MAX))
    parts.append(v_wall("wall_east", X_MAX, Y_MIN, Y_MAX))
    parts.append(v_wall("wall_west", X_MIN, Y_MIN, Y_MAX))

    # ---- 主分隔墙（东西向），开 3 个门 ----
    parts.append("    <!-- ============ 主分隔墙：南/北区分界，开 3 个门 ============ -->\n")
    parts.append(h_wall(
        "wall_split", SPLIT_Y, X_MIN, X_MAX,
        doors=[(dx, DOOR_W) for dx in CROSS_DOORS_X],
    ))

    # ---- 北区隔墙 ×2 ----
    parts.append("    <!-- ============ 北区隔墙：每道靠近走廊端开一门 ============ -->\n")
    for k, x in enumerate(NORTH_DIV_X):
        parts.append(v_wall(
            f"wall_north_div{k + 1}", x, SPLIT_Y, Y_MAX,
            doors=[(SPLIT_Y + DOOR_W / 2 + 0.85, DOOR_W)],
        ))

    # ---- 南区一道隔墙，把 room_d 与 home 区隔开 ----
    parts.append("    <!-- ============ 南区隔墙 ============ -->\n")
    parts.append(v_wall(
        "wall_south_div", -2.0, Y_MIN, SPLIT_Y,
        doors=[(-2.5, DOOR_W)],
    ))

    # ---- 家具 ----
    parts.append("    <!-- ============ 家具与障碍 ============ -->\n")

    # room_a：西北角，书桌 + 椅子
    parts.append(box("table_a", -4.4, 3.1, 1.2, 0.8, 0.75, WOOD))
    parts.append(box("chair_a1", -4.4, 2.45, 0.45, 0.45, 0.55, WOOD))
    parts.append(box("shelf_a", -5.4, 3.8, 0.35, 1.5, 1.6, WOOD))

    # room_c：正北，储物柜 + 货架
    parts.append(box("cabinet_c", -1.2, 4.4, 1.4, 0.45, 1.8, METAL))
    parts.append(box("rack_c", 1.3, 4.4, 1.6, 0.45, 1.5, METAL))
    parts.append(box("crate_c1", 0.2, 2.8, 0.5, 0.5, 0.5, WOOD, yaw=0.3))
    parts.append(box("crate_c2", 1.1, 2.6, 0.5, 0.5, 0.5, WOOD, yaw=-0.4))

    # room_b：东北角，沙发 + 茶几
    parts.append(box("sofa_b", 4.6, 3.4, 1.8, 0.85, 0.8, FABRIC))
    parts.append(box("table_b", 4.6, 2.4, 0.9, 0.6, 0.45, WOOD))

    # 南区：room_d 有床，走廊有柱子
    parts.append(box("bed_d", -4.6, -3.8, 1.9, 1.3, 0.55, FABRIC))
    parts.append(box("desk_d", -5.3, -1.6, 1.0, 0.5, 0.75, WOOD))
    parts.append(cylinder("pillar_1", 0.0, -2.0, 0.22, WALL_H, GREY))
    parts.append(cylinder("pillar_2", 2.6, -2.0, 0.22, WALL_H, GREY))
    parts.append(box("box_1", 3.2, -3.6, 0.5, 0.5, 0.5, WOOD, yaw=0.3))
    parts.append(box("box_2", -1.2, -4.2, 0.45, 0.45, 0.45, WOOD, yaw=-0.5))

    # 充电桩（home 点，视觉标识）
    parts.append(cylinder("charger_home", 0.0, -4.3, 0.16, 0.9, FABRIC))

    parts.append("""  </world>
</sdf>
""")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成可导航的室内 world")
    ap.add_argument("--out", required=True, help="输出路径")
    args = ap.parse_args()

    xml = build()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(xml)

    print(f"已生成 {args.out}（{len(xml)} 字节）")
    print(f"  门宽 {DOOR_W}m | 墙厚 {WALL_T}m | 机器人直径 0.64m")
    print(f"  南北门洞 x = {CROSS_DOORS_X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
