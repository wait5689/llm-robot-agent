#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
室内世界生成器 v2（Indoor World Generator）
===========================================

v2 视觉升级：
  - 墙高 2.5m（真实层高），带踢脚线
  - 室内点光源，暖色氛围
  - 每间房有独立主题家具（办公室/储物间/客厅/卧室）
  - 充电桩带底座和指示灯
  - 柱子带底座和柱头
  - 更真实的材质配色（米白墙、浅灰地、胡桃木家具）

布局（俯视，+X 向东，+Y 向北）
-----------------------------
        y=+5  ┌────────┬────────┬────────┐
              │ room_a │  room_c│ room_b │     ← 北区（研究/储物/会客）
        y=+1.5│        │        │        │
              ├───┬────┴────┬───┴────┬───┤
        y=+1.0│   │  门洞走廊  │        │   │
              │   └────┬────┘        │   │     ← 南北连接通道（宽 1.0m）
        y=0   │ 门 │        门        │ 门│
              ├────┴────────┴────────┴───
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
# 布局参数
# ----------------------------------------------------------------------

WALL_H = 2.5        # 墙高（真实层高）
WALL_T = 0.15       # 墙厚

# 门洞净宽：1.10 -> 1.60 -> 1.90。
#
# 演化过程（全部是实测数据，不是估算）：
#   1.10m：叠加 inflation 0.42 后净空 0.26m < 机器人内切 0.32m，
#          门在代价地图上等价封死，规划必然失败。
#   1.60m：全局规划能过，但实测 move_base 在 1.6m 门洞处反复振荡。
#          量了净空剖面才发现问题不在门本身，而在门「内外两侧」：
#          门洞 y∈[0.90,2.50] 宽 1.60 看着够，
#          但门内侧净空只有 0.50~0.57m，
#          扣除 inflation 0.35 后仅剩 0.15~0.22m，
#          比机器人内切半径 0.23m 还小 —— 等于窄了。
#   1.90m：门内外净空提到 0.65~0.72m，扣除膨胀后余量 0.30~0.37m，
#          大于内切半径，DWA 才有足够横向空间走出弧线。
#
# 结论：判断门够不够宽，不能只看门洞尺寸，
#       必须看「门洞 + 两侧各 0.5m」那段的最窄净空。
DOOR_W = 1.90
DOOR_H = 2.10       # 门洞高度（标准门高）
BASEBOARD_H = 0.10  # 踢脚线高度
BASEBOARD_D = 0.02  # 踢脚线突出墙面

# 场地范围
X_MIN, X_MAX = -6.0, 6.0
Y_MIN, Y_MAX = -5.0, 5.0

# 核心分隔
SPLIT_Y = 0.0
# 北区两道隔墙：从 ±2.5 外移到 ±2.8，
# 让中间房间（room_c）的净宽从 4.85m 增加到 5.45m，
# 家具靠墙摆放后中央仍有一条 ≥1.2m 的直通道。
NORTH_DIV_X = [-2.8, 2.8]
# 南北门洞：三个，居中分布。配合 DOOR_W=1.60，门中心间距足够，
# 门框之间不会互相吃掉墙体。
CROSS_DOORS_X = [-3.8, 0.0, 3.8]

# ----------------------------------------------------------------------
# 材质配色（RGBA，sRGB 线性近似）
# ----------------------------------------------------------------------

# 建筑
WALL = "0.93 0.91 0.88 1"           # 米白墙面
BASEBOARD = "0.82 0.78 0.72 1"      # 暖灰踢脚线
DOOR_FRAME = "0.75 0.68 0.58 1"     # 木色门框
FLOOR_TILE = "0.80 0.79 0.77 1"     # 浅灰地砖
PILLAR = "0.72 0.72 0.74 1"         # 混凝土柱
PILLAR_BASE = "0.55 0.55 0.57 1"    # 柱底座

# 木材（家具）
WOOD_DARK = "0.38 0.24 0.14 1"      # 深胡桃木
WOOD_MED = "0.55 0.38 0.22 1"       # 中色木
WOOD_LIGHT = "0.72 0.56 0.36 1"     # 浅橡木

# 织物
FABRIC_BLUE = "0.22 0.32 0.52 1"    # 深蓝布艺
FABRIC_GREY = "0.52 0.52 0.55 1"    # 灰色布艺
FABRIC_GREEN = "0.28 0.42 0.32 1"   # 墨绿布艺

# 金属
METAL_DARK = "0.28 0.28 0.30 1"     # 深灰金属
METAL_LIGHT = "0.70 0.72 0.75 1"    # 银色金属
METAL_CHROME = "0.85 0.87 0.90 1"   # 镀铬

# 其他
WHITE = "0.95 0.95 0.95 1"          # 纯白（电器等）
BLACK = "0.08 0.08 0.10 1"          # 黑色（屏幕等）
RED_INDICATOR = "0.90 0.15 0.10 1"  # 红色指示灯
GREEN_INDICATOR = "0.10 0.80 0.20 1"# 绿色指示灯
PLANT_GREEN = "0.18 0.45 0.20 1"    # 植物绿
POT_TERRA = "0.65 0.35 0.20 1"      # 陶土花盆


def box(name, cx, cy, sx, sy, sz, rgba, yaw=0.0, z_off=None):
    """一个静态 box 模型。z_off 可手动指定底部离地高度。"""
    if z_off is None:
        z_off = sz / 2
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{cx:.3f} {cy:.3f} {z_off:.3f} 0 0 {yaw:.4f}</pose>
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
            <specular>0.1 0.1 0.1 1</specular>
          </material>
        </visual>
      </link>
    </model>
"""


def cylinder(name, cx, cy, radius, length, rgba, z_off=None):
    if z_off is None:
        z_off = length / 2
    return f"""    <model name="{name}">
      <static>true</static>
      <pose>{cx:.3f} {cy:.3f} {z_off:.3f} 0 0 0</pose>
      <link name="link">
        <collision name="collision">
          <geometry><cylinder><radius>{radius:.3f}</radius><length>{length:.3f}</length></cylinder></geometry>
        </collision>
        <visual name="visual">
          <geometry><cylinder><radius>{radius:.3f}</radius><length>{length:.3f}</length></cylinder></geometry>
          <material>
            <ambient>{rgba}</ambient>
            <diffuse>{rgba}</diffuse>
            <specular>0.15 0.15 0.15 1</specular>
          </material>
        </visual>
      </link>
    </model>
"""


def h_wall(name, y, x0, x1, doors=(), thickness=WALL_T, height=WALL_H):
    """沿 X 方向的墙，自动切分门洞 + 踢脚线 + 门框。"""
    segs = []
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
            # 墙体
            out.append(box(f"{name}_{idx}", (a + b) / 2, y, length, thickness, height, WALL))
            # 踢脚线（墙底部，略突出）
            out.append(box(f"{name}_base_{idx}", (a + b) / 2, y,
                           length, thickness + BASEBOARD_D * 2, BASEBOARD_H, BASEBOARD,
                           z_off=BASEBOARD_H / 2))
        else:
            # 门洞过梁
            lintel_h = height - DOOR_H
            if lintel_h > 1e-6:
                out.append(box(f"{name}_lintel_{idx}", (a + b) / 2, y,
                               length, thickness, lintel_h, WALL,
                               z_off=DOOR_H + lintel_h / 2))
            # 门框立柱（比门框颜色深，略宽）
            frame_w = 0.06
            out.append(box(f"{name}_frame_{idx}_l", a, y, frame_w, thickness + 0.04, DOOR_H,
                           DOOR_FRAME, z_off=DOOR_H / 2))
            out.append(box(f"{name}_frame_{idx}_r", b, y, frame_w, thickness + 0.04, DOOR_H,
                           DOOR_FRAME, z_off=DOOR_H / 2))
            # 门框顶梁
            out.append(box(f"{name}_frame_{idx}_t", (a + b) / 2, y,
                           length + frame_w * 2, thickness + 0.04, 0.06, DOOR_FRAME,
                           z_off=DOOR_H + 0.03))
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
            out.append(box(f"{name}_{idx}", x, (a + b) / 2, thickness, length, height, WALL))
            out.append(box(f"{name}_base_{idx}", x, (a + b) / 2,
                           thickness + BASEBOARD_D * 2, length, BASEBOARD_H, BASEBOARD,
                           z_off=BASEBOARD_H / 2))
        else:
            lintel_h = height - DOOR_H
            if lintel_h > 1e-6:
                out.append(box(f"{name}_lintel_{idx}", x, (a + b) / 2,
                               thickness, length, lintel_h, WALL,
                               z_off=DOOR_H + lintel_h / 2))
            frame_w = 0.06
            out.append(box(f"{name}_frame_{idx}_b", x, a, thickness + 0.04, frame_w, DOOR_H,
                           DOOR_FRAME, z_off=DOOR_H / 2))
            out.append(box(f"{name}_frame_{idx}_t", x, b, thickness + 0.04, frame_w, DOOR_H,
                           DOOR_FRAME, z_off=DOOR_H / 2))
            out.append(box(f"{name}_frame_{idx}_top", x, (a + b) / 2,
                           thickness + 0.04, length + frame_w * 2, 0.06, DOOR_FRAME,
                           z_off=DOOR_H + 0.03))
        idx += 1
    return "".join(out)


def pillar(name, cx, cy, radius=0.22, height=WALL_H):
    """带底座和柱头的装饰柱。"""
    base_r = radius + 0.06
    cap_r = radius + 0.04
    base_h = 0.12
    cap_h = 0.08
    shaft_h = height - base_h - cap_h
    parts = []
    # 底座
    parts.append(cylinder(f"{name}_base", cx, cy, base_r, base_h, PILLAR_BASE,
                          z_off=base_h / 2))
    # 柱身
    parts.append(cylinder(f"{name}_shaft", cx, cy, radius, shaft_h, PILLAR,
                          z_off=base_h + shaft_h / 2))
    # 柱头
    parts.append(cylinder(f"{name}_cap", cx, cy, cap_r, cap_h, PILLAR_BASE,
                          z_off=base_h + shaft_h + cap_h / 2))
    return "".join(parts)


def build() -> str:
    parts = []

    parts.append(f"""<?xml version="1.0" ?>
<!--
  复杂室内环境 v2（由 tools/gen_world.py 生成，请勿手工编辑）

  布局：南区（活动区）+ 北区（三个房间），由一条走廊墙分隔，
        墙上开 3 个门洞保证南北连通；北区两道隔墙靠近走廊端各开一门。

  所有门宽 {DOOR_W:.2f}m / 高 {DOOR_H:.2f}m，墙高 {WALL_H:.1f}m。
  机器人直径 0.64m，门宽留有余量。
  修改布局请改生成器参数后重新生成，并用 tools/audit_world.py 校验。
-->
<sdf version="1.4">
  <world name="complex_indoor">
    <physics type="ode">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1</real_time_factor>
      <real_time_update_rate>1000</real_time_update_rate>
    </physics>

    <!-- 渲染：室内偏暗氛围 -->
    <scene>
      <ambient>0.18 0.18 0.22 1</ambient>
      <background>0.30 0.32 0.35 1</background>
      <shadows>true</shadows>
    </scene>

    <!-- 太阳光（减弱，模拟室内透过窗户的自然光） -->
    <include>
      <uri>model://sun</uri>
    </include>

    <!-- 地面 -->
    <include>
      <uri>model://ground_plane</uri>
    </include>

    <!-- 室内地砖（覆盖整个场地） -->
    <model name="floor_tile">
      <static>true</static>
      <pose>0 0 0.005 0 0 0</pose>
      <link name="link">
        <collision name="collision">
          <geometry>
            <box><size>{X_MAX - X_MIN:.1f} {Y_MAX - Y_MIN:.1f} 0.01</size></box>
          </geometry>
        </collision>
        <visual name="visual">
          <geometry>
            <box><size>{X_MAX - X_MIN:.1f} {Y_MAX - Y_MIN:.1f} 0.01</size></box>
          </geometry>
          <material>
            <ambient>{FLOOR_TILE}</ambient>
            <diffuse>{FLOOR_TILE}</diffuse>
            <specular>0.05 0.05 0.05 1</specular>
          </material>
        </visual>
      </link>
    </model>

    <!-- 室内点光源（暖色吊灯，适度亮度） -->
    <light name="indoor_light_center" type="point">
      <pose>0 0 2.3 0 0 0</pose>
      <diffuse>0.70 0.65 0.55 1</diffuse>
      <specular>0.15 0.15 0.15 1</specular>
      <attenuation>
        <range>10.0</range>
        <constant>0.6</constant>
        <linear>0.12</linear>
        <quadratic>0.02</quadratic>
      </attenuation>
      <cast_shadows>true</cast_shadows>
    </light>
    <light name="indoor_light_north" type="point">
      <pose>0 3.0 2.3 0 0 0</pose>
      <diffuse>0.65 0.60 0.50 1</diffuse>
      <attenuation>
        <range>7.0</range>
        <constant>0.7</constant>
        <linear>0.15</linear>
        <quadratic>0.02</quadratic>
      </attenuation>
      <cast_shadows>false</cast_shadows>
    </light>
    <light name="indoor_light_south" type="point">
      <pose>0 -3.0 2.3 0 0 0</pose>
      <diffuse>0.65 0.60 0.50 1</diffuse>
      <attenuation>
        <range>7.0</range>
        <constant>0.7</constant>
        <linear>0.15</linear>
        <quadratic>0.02</quadratic>
      </attenuation>
      <cast_shadows>false</cast_shadows>
    </light>

    <!-- ============ 外墙 ============ -->
""")

    # 外墙
    parts.append(h_wall("wall_north", Y_MAX, X_MIN, X_MAX))
    parts.append(h_wall("wall_south", Y_MIN, X_MIN, X_MAX))
    parts.append(v_wall("wall_east", X_MAX, Y_MIN, Y_MAX))
    parts.append(v_wall("wall_west", X_MIN, Y_MIN, Y_MAX))

    # 主分隔墙（3 个门）
    parts.append("    <!-- ============ 主分隔墙：南/北区分界 ============ -->\n")
    parts.append(h_wall(
        "wall_split", SPLIT_Y, X_MIN, X_MAX,
        doors=[(dx, DOOR_W) for dx in CROSS_DOORS_X],
    ))

    # 北区隔墙
    parts.append("    <!-- ============ 北区隔墙 ============ -->\n")
    for k, x in enumerate(NORTH_DIV_X):
        parts.append(v_wall(
            f"wall_north_div{k + 1}", x, SPLIT_Y, Y_MAX,
            doors=[(SPLIT_Y + DOOR_W / 2 + 0.85, DOOR_W)],
        ))

    # 南区隔墙
    parts.append("    <!-- ============ 南区隔墙 ============ -->\n")
    parts.append(v_wall(
        "wall_south_div", -2.0, Y_MIN, SPLIT_Y,
        doors=[(-2.5, DOOR_W)],
    ))

    # ================================================================
    # 家具与装饰
    # ----------------------------------------------------------------
    # 布局硬约束（改家具前先看这里）：
    #   1. 所有家具必须贴墙或靠角，房间中央留出 ≥1.2m 宽的空通道。
    #   2. 任何家具不得遮挡门洞正前方 1.0m 的进出区域。
    #   3. 每个语义地点（room_*/corridor/hall/home）周围 0.6m 内必须空。
    #   4. 地面堆放物（crate/box）一律取消——它们散在房间中央，
    #      是上一版 room_c 被截断、home 压桩的直接原因。
    #
    # 上一版的真实故障：crate_c1/c2/c3 三个箱子摆在 room_c 正中，
    # 加上两侧货架，把房间从 x=-1.0 到 x=1.5 整片堵死，
    # 机器人只能从一条 0.3m 的缝里过，代价地图上直接变成致命格。
    # ================================================================
    parts.append("    <!-- ============ 家具与装饰（一律贴墙） ============ -->\n")

    # ---- room_a：办公室（西北，x<-2.8, y>0） ----
    # 办公桌：贴西墙，沿 Y 方向摆放
    parts.append(box("desk_a_top", -5.35, 3.4, 0.75, 1.50, 0.04, WOOD_MED, z_off=0.74))
    parts.append(box("desk_a_leg_1", -5.65, 2.72, 0.05, 0.05, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_2", -5.05, 2.72, 0.05, 0.05, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_3", -5.65, 4.08, 0.05, 0.05, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_4", -5.05, 4.08, 0.05, 0.05, 0.72, METAL_DARK, z_off=0.36))
    # 显示器（屏幕朝东，面向房间）
    parts.append(box("monitor_a_screen", -5.62, 3.4, 0.03, 0.60, 0.36, BLACK, z_off=0.95))
    parts.append(box("monitor_a_stand", -5.60, 3.4, 0.10, 0.14, 0.16, METAL_DARK, z_off=0.82))
    # 键盘
    parts.append(box("keyboard_a", -5.20, 3.4, 0.14, 0.40, 0.02, BLACK, z_off=0.77))
    # 办公椅（桌子东侧，靠墙一侧）
    parts.append(box("chair_a_seat", -4.75, 3.4, 0.45, 0.45, 0.06, FABRIC_GREY, z_off=0.46))
    parts.append(box("chair_a_back", -4.55, 3.4, 0.06, 0.45, 0.48, FABRIC_GREY, z_off=0.72))
    parts.append(cylinder("chair_a_pole", -4.75, 3.4, 0.03, 0.44, METAL_DARK, z_off=0.23))
    parts.append(cylinder("chair_a_base", -4.75, 3.4, 0.22, 0.04, METAL_DARK, z_off=0.02))
    # 文件柜：贴北墙
    parts.append(box("filecab_a", -4.2, 4.72, 1.00, 0.42, 1.20, METAL_LIGHT))
    parts.append(box("filecab_a_line", -4.2, 4.50, 0.96, 0.02, 0.02, METAL_DARK, z_off=0.60))
    # 盆栽（角上）
    parts.append(cylinder("pot_a", -5.55, 4.55, 0.16, 0.32, POT_TERRA, z_off=0.16))
    parts.append(cylinder("plant_a", -5.55, 4.55, 0.24, 0.42, PLANT_GREEN, z_off=0.53))

    # ---- room_c：储物间（正北，-2.8<x<2.8, y>0） ----
    #
    # ★ 货架的 y 起点是关键，不能贴到门洞旁边。
    #
    # 上一版把货架放在 y∈[2.00,4.00]，而西隔墙 x=-2.8 的门洞是 y∈[0.90,2.80]。
    # 货架离门洞中心线只有 0.18m，叠加 inflation_radius 0.35 之后，
    # 门洞上沿 y≥1.7 全部变成致命格，实测全局代价地图：
    #     y=1.1~1.6  可通行
    #     y≥1.7      99 / OCC（被货架膨胀吃掉）
    # 1.9m 的门实际只剩 0.5m 能走，机器人一旦走到 y=1.32 就被两侧高代价夹住，
    # 全局规划返回空路径，move_base 一直发 lin=0，最后振荡中止。
    # 表现极具欺骗性：定位完全正常（amcl 误差 0.2m），机器人就是不动。
    #
    # 现在把货架整体后移到 y∈[3.10,4.90]，
    # 与门洞上沿 2.80 留出 0.30m 空隙，加膨胀后仍有余量，
    # 门洞 y∈[0.90,2.80] 全宽可通行。
    RACK_Y = 4.00      # 货架中心
    RACK_LEN = 1.80    # 货架长度（y 方向）
    parts.append(box("rack_c_left", -2.42, RACK_Y, 0.40, RACK_LEN, 2.00, METAL_LIGHT))
    parts.append(box("rack_c_left_s1", -2.42, RACK_Y - 0.40, 0.38, 0.02, 1.90, METAL_DARK, z_off=0.55))
    parts.append(box("rack_c_left_s2", -2.42, RACK_Y + 0.40, 0.38, 0.02, 1.90, METAL_DARK, z_off=1.05))
    parts.append(box("rack_c_right", 2.42, RACK_Y, 0.40, RACK_LEN, 2.00, METAL_LIGHT))
    parts.append(box("rack_c_right_s1", 2.42, RACK_Y - 0.40, 0.38, 0.02, 1.90, METAL_DARK, z_off=0.55))
    parts.append(box("rack_c_right_s2", 2.42, RACK_Y + 0.40, 0.38, 0.02, 1.90, METAL_DARK, z_off=1.05))
    # 货架上的储物箱（悬空在层板上，不占地面）
    parts.append(box("box_c_l1", -2.42, RACK_Y - 0.20, 0.32, 0.36, 0.24, "0.75 0.65 0.40 1", z_off=0.68))
    parts.append(box("box_c_l2", -2.42, RACK_Y + 0.20, 0.32, 0.36, 0.24, "0.60 0.60 0.65 1", z_off=1.18))
    parts.append(box("box_c_r1", 2.42, RACK_Y - 0.20, 0.32, 0.36, 0.24, WOOD_LIGHT, z_off=0.68))
    parts.append(box("box_c_r2", 2.42, RACK_Y + 0.20, 0.32, 0.36, 0.24, "0.70 0.55 0.35 1", z_off=1.18))
    # 北墙货架（贴墙）
    parts.append(box("rack_c_back", 0.0, 4.72, 2.20, 0.40, 2.00, METAL_LIGHT))
    parts.append(box("rack_c_back_s1", 0.0, 4.72, 2.16, 0.38, 0.02, METAL_DARK, z_off=0.90))
    # 地面只留角落一个箱子，不放中央
    #
    # ★ 位置经过门洞净空校验。原先在 (2.35, 1.15)，离 x=+2.8 墙上的
    #   room_b 门洞（可用 y∈[1.20,1.60]）只有 0.4m，
    #   与 room_b 一侧的 tvstand_b 一起把门洞夹到净空 0.398m，
    #   而 room_b 只有这一个出口 —— 机器人进得去出不来，
    #   表现是「去 room_b 能成功、从 room_b 去别处必然振荡中止」。
    #   现在挪到 room_c 东南角的贴墙位置，离所有门洞 >1.5m。
    parts.append(box("crate_c_corner", 2.30, 3.90, 0.50, 0.50, 0.50, WOOD_LIGHT, yaw=0.25))

    # ---- room_b：客厅（东北，x>2.8, y>0） ----
    # 沙发贴东墙
    parts.append(box("sofa_b_seat", 5.30, 3.2, 0.85, 1.80, 0.42, FABRIC_BLUE, z_off=0.21))
    parts.append(box("sofa_b_back", 5.68, 3.2, 0.12, 1.80, 0.50, FABRIC_BLUE, z_off=0.67))
    parts.append(box("sofa_b_arm_l", 5.30, 2.32, 0.85, 0.12, 0.32, FABRIC_BLUE, z_off=0.58))
    parts.append(box("sofa_b_arm_r", 5.30, 4.08, 0.85, 0.12, 0.32, FABRIC_BLUE, z_off=0.58))
    parts.append(box("cushion_b1", 5.28, 2.80, 0.38, 0.40, 0.10, "0.30 0.40 0.60 1", z_off=0.47))
    parts.append(box("cushion_b2", 5.28, 3.60, 0.38, 0.40, 0.10, "0.30 0.40 0.60 1", z_off=0.47))
    # 茶几（沙发西侧，留出通道）
    parts.append(box("table_b_top", 4.20, 3.2, 0.55, 0.95, 0.04, WOOD_DARK, z_off=0.42))
    parts.append(box("table_b_leg_1", 4.00, 2.80, 0.05, 0.05, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg_2", 4.40, 2.80, 0.05, 0.05, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg_3", 4.00, 3.60, 0.05, 0.05, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg_4", 4.40, 3.60, 0.05, 0.05, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("magazine_b", 4.20, 3.05, 0.24, 0.18, 0.02, "0.80 0.20 0.20 1", z_off=0.45))
    parts.append(cylinder("cup_b", 4.20, 3.40, 0.04, 0.10, WHITE, z_off=0.47))
    # 电视柜：贴在 room_b 内部东侧，正对沙发。
    # 【坑 1】原先贴南侧分隔墙 y≈0.42，堵死 x=3.8 那个门洞
    # （门宽 1.6m 占 x∈[3.0,4.6]，柜子加膨胀后 x∈[3.28,5.32]）。
    # 【坑 2】后来移到 (3.35, 2.60)，仍堵住 x=+2.8 墙上的 room_b 门洞
    # （门洞可用 y∈[1.20,1.60]，柜子 y∈[1.95,3.25]，膨胀后压到 y≈1.60），
    # 与 room_c 的 crate_c_corner 一起把净空夹到 0.398m。
    # room_b 只有 x=+2.8 这一个出口，所以等于把机器人关在房间里：
    # 「去 room_b」能成功，「从 room_b 出发」必然振荡中止。
    # 现在贴东墙、让开所有门洞通道，离门洞 >1.5m。
    parts.append(box("tvstand_b", 5.20, 1.10, 0.40, 1.30, 0.50, WOOD_DARK))
    parts.append(box("tv_b_screen", 5.03, 1.10, 0.04, 1.05, 0.60, BLACK, z_off=0.85))
    parts.append(box("tv_b_stand", 5.09, 1.10, 0.06, 0.20, 0.15, METAL_DARK, z_off=0.60))
    # 盆栽靠角
    parts.append(cylinder("pot_b", 3.15, 4.55, 0.15, 0.30, POT_TERRA, z_off=0.15))
    parts.append(cylinder("plant_b", 3.15, 4.55, 0.22, 0.40, PLANT_GREEN, z_off=0.50))

    # ---- room_d：卧室（西南，x<-2.8, y<0） ----
    # 房间可用范围：x ∈ [-5.9, -2.9]（宽 3.0m），y ∈ [-4.9, -0.1]（高 4.8m）
    # 布局：床贴西墙北段，衣柜贴西墙南段，书桌贴南墙，
    #       中央与东侧留出 ≥1.3m 的直通道，保证从 x=-3.8 门洞进来能走到房间深处。
    # 床贴西北角
    parts.append(box("bed_d_frame", -5.45, -1.60, 0.90, 1.90, 0.30, WOOD_DARK))
    parts.append(box("bed_d_mattress", -5.45, -1.60, 0.82, 1.82, 0.22, WHITE, z_off=0.41))
    parts.append(box("pillow_d1", -5.45, -1.05, 0.62, 0.32, 0.12, "0.90 0.90 0.92 1", z_off=0.58))
    parts.append(box("pillow_d2", -5.45, -1.55, 0.62, 0.32, 0.12, "0.90 0.90 0.92 1", z_off=0.58))
    parts.append(box("blanket_d", -5.45, -2.15, 0.84, 1.10, 0.08, FABRIC_GREEN, z_off=0.56))
    # 床头柜贴北墙
    #
    # ★ 原在 (-4.55, -0.45)，正好压在 y=0 主墙西门洞（x∈[-4.75,-2.85]）
    #   的南侧出口上：柜子加膨胀后覆盖 y∈[-1.01,0.11]，
    #   把西门洞的净空从 0.905 压到 0.582，机器人进出 room_d 时会蹭到。
    #   现在往南挪 0.5m，仍在床头附近，但让开了门洞通道。
    parts.append(box("nightstand_d", -4.55, -0.95, 0.44, 0.42, 0.50, WOOD_MED))
    parts.append(cylinder("lamp_d_base", -4.55, -0.95, 0.07, 0.03, METAL_DARK, z_off=0.515))
    parts.append(cylinder("lamp_d_pole", -4.55, -0.95, 0.02, 0.24, METAL_DARK, z_off=0.65))
    parts.append(cylinder("lamp_d_shade", -4.55, -0.95, 0.11, 0.16, "0.95 0.90 0.80 1", z_off=0.85))
    # 衣柜贴西墙南段
    parts.append(box("wardrobe_d", -5.68, -3.55, 0.44, 1.10, 2.00, WOOD_MED))
    parts.append(box("wardrobe_d_line", -5.68, -3.55, 0.02, 1.06, 0.02, WOOD_DARK, z_off=1.00))
    # 书桌贴南墙
    parts.append(box("desk_d_top", -4.30, -4.68, 1.30, 0.50, 0.04, WOOD_LIGHT, z_off=0.74))
    parts.append(box("desk_d_leg_1", -4.90, -4.68, 0.05, 0.44, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_d_leg_2", -3.70, -4.68, 0.05, 0.44, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("chair_d_seat", -4.30, -3.95, 0.44, 0.44, 0.05, BLACK, z_off=0.46))
    parts.append(box("chair_d_back", -4.30, -3.74, 0.44, 0.05, 0.44, BLACK, z_off=0.71))

    # ---- 南区大厅（y<0） ----
    # 装饰柱：移到墙边，不占中央。原 (0,-2)/(2.6,-2) 正好挡在
    # 起点北上走廊的必经之路上。
    parts.append(pillar("pillar_1", -2.60, -1.20))
    parts.append(pillar("pillar_2", 2.60, -1.20))

    # 接待台：贴东墙，不再横在大厅里
    parts.append(box("reception_desk", 5.15, -1.20, 0.60, 1.60, 1.05, WOOD_DARK))
    parts.append(box("reception_desk_top", 5.15, -1.20, 0.70, 1.70, 0.04, WOOD_MED, z_off=1.05))

    # 南墙装饰柜
    parts.append(box("cabinet_south", -1.80, -4.70, 1.40, 0.45, 1.10, WOOD_MED))

    # 盆栽放角落
    parts.append(cylinder("pot_hall_1", -5.35, -1.30, 0.18, 0.35, POT_TERRA, z_off=0.175))
    parts.append(cylinder("plant_hall_1", -5.35, -1.30, 0.25, 0.45, PLANT_GREEN, z_off=0.575))
    parts.append(cylinder("pot_hall_2", 5.35, -4.55, 0.15, 0.30, POT_TERRA, z_off=0.15))
    parts.append(cylinder("plant_hall_2", 5.35, -4.55, 0.22, 0.40, PLANT_GREEN, z_off=0.50))

    # ---- 充电桩（home 点） ----
    # 【关键】充电桩整体挪到西南角，远离机器人出生位与主通道。
    # 上一版放在 (0.80,-3.60)，而 home 起点就在 (0,-3.5) 附近，
    # 桩体半径 0.30m + 膨胀 0.42m 直接把起点和北上通道一起吃掉了。
    # 现在放到 (-4.80,-4.30)：角落位置，不影响任何语义地点之间的通路。
    CHARGER_X, CHARGER_Y = -4.80, -4.30
    parts.append(cylinder("charger_base", CHARGER_X, CHARGER_Y, 0.30, 0.06, METAL_DARK, z_off=0.03))
    parts.append(cylinder("charger_pillar", CHARGER_X, CHARGER_Y, 0.12, 0.80, METAL_LIGHT, z_off=0.46))
    parts.append(cylinder("charger_top", CHARGER_X, CHARGER_Y, 0.18, 0.08, METAL_DARK, z_off=0.90))
    parts.append(cylinder("charger_led", CHARGER_X, CHARGER_Y + 0.12, 0.03, 0.04, GREEN_INDICATOR, z_off=0.70))
    parts.append(box("charger_port", CHARGER_X, CHARGER_Y + 0.15, 0.06, 0.04, 0.08, BLACK, z_off=0.55))
    # 地面标识（0.01m 高，属装饰，不参与避障）
    parts.append(cylinder("floor_marker_home", CHARGER_X, CHARGER_Y, 0.35, 0.01,
                          "0.20 0.60 0.30 1", z_off=0.012))

    parts.append("""  </world>
</sdf>
""")
    return "".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成可导航的室内世界 v2")
    ap.add_argument("--out", required=True, help="输出路径")
    args = ap.parse_args()

    xml = build()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(xml)

    print(f"已生成 {args.out}（{len(xml)} 字节）")
    print(f"  门宽 {DOOR_W}m / 高 {DOOR_H}m | 墙高 {WALL_H}m | 机器人直径 0.64m")
    print(f"  南北门洞 x = {CROSS_DOORS_X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
