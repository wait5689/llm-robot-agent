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
DOOR_W = 1.10       # 门洞净宽
DOOR_H = 2.10       # 门洞高度（标准门高）
BASEBOARD_H = 0.10  # 踢脚线高度
BASEBOARD_D = 0.02  # 踢脚线突出墙面

# 场地范围
X_MIN, X_MAX = -6.0, 6.0
Y_MIN, Y_MAX = -5.0, 5.0

# 核心分隔
SPLIT_Y = 0.0
NORTH_DIV_X = [-2.5, 2.5]
CROSS_DOORS_X = [-4.0, 0.0, 4.0]

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
    # ================================================================
    parts.append("    <!-- ============ 家具与装饰 ============ -->\n")

    # ---- room_a：办公室（西北角） ----
    # 办公桌（靠北墙）
    parts.append(box("desk_a_top", -4.4, 3.6, 1.4, 0.7, 0.04, WOOD_MED, z_off=0.74))
    parts.append(box("desk_a_leg_fl", -5.05, 3.28, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_fr", -3.75, 3.28, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_bl", -5.05, 3.92, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_a_leg_br", -3.75, 3.92, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    # 显示器
    parts.append(box("monitor_a_screen", -4.4, 3.7, 0.55, 0.03, 0.35, BLACK, z_off=0.92))
    parts.append(box("monitor_a_stand", -4.4, 3.65, 0.06, 0.12, 0.18, METAL_DARK, z_off=0.83))
    # 键盘
    parts.append(box("keyboard_a", -4.4, 3.45, 0.35, 0.12, 0.02, BLACK, z_off=0.75))
    # 办公椅
    parts.append(box("chair_a_seat", -4.4, 2.9, 0.45, 0.42, 0.06, FABRIC_GREY, z_off=0.48))
    parts.append(box("chair_a_back", -4.4, 3.1, 0.45, 0.06, 0.50, FABRIC_GREY, z_off=0.73))
    parts.append(cylinder("chair_a_pole", -4.4, 2.9, 0.03, 0.45, METAL_DARK, z_off=0.24))
    parts.append(cylinder("chair_a_base", -4.4, 2.9, 0.22, 0.04, METAL_DARK, z_off=0.02))
    # 书架（靠西墙）
    parts.append(box("shelf_a_frame", -5.7, 3.5, 0.35, 1.6, 1.8, WOOD_DARK))
    parts.append(box("shelf_a_s1", -5.7, 3.0, 0.33, 0.02, 1.76, WOOD_MED, z_off=0.45))
    parts.append(box("shelf_a_s2", -5.7, 3.5, 0.33, 0.02, 1.76, WOOD_MED, z_off=0.90))
    parts.append(box("shelf_a_s3", -5.7, 4.0, 0.33, 0.02, 1.76, WOOD_MED, z_off=1.35))
    # 书架上的书（彩色小方块）
    parts.append(box("book_a1", -5.6, 3.15, 0.20, 0.04, 0.28, "0.70 0.15 0.15 1", z_off=0.89))
    parts.append(box("book_a2", -5.55, 3.15, 0.18, 0.04, 0.26, "0.15 0.30 0.60 1", z_off=0.89))
    parts.append(box("book_a3", -5.75, 3.55, 0.22, 0.04, 0.25, "0.20 0.50 0.25 1", z_off=1.34))
    # 文件柜
    parts.append(box("filecab_a", -5.55, 2.4, 0.45, 0.50, 0.70, METAL_LIGHT))
    parts.append(box("filecab_a_drawer", -5.55, 2.4, 0.40, 0.46, 0.02, METAL_DARK, z_off=0.36))

    # ---- room_c：储物间（正北） ----
    # 金属货架 ×2
    parts.append(box("rack_c1", -1.2, 4.2, 0.40, 1.5, 2.0, METAL_LIGHT))
    parts.append(box("rack_c1_s1", -1.2, 3.7, 0.38, 0.02, 1.46, METAL_DARK, z_off=0.50))
    parts.append(box("rack_c1_s2", -1.2, 4.2, 0.38, 0.02, 1.46, METAL_DARK, z_off=1.00))
    parts.append(box("rack_c1_s3", -1.2, 4.7, 0.38, 0.02, 1.46, METAL_DARK, z_off=1.50))

    parts.append(box("rack_c2", 1.2, 4.2, 0.40, 1.5, 2.0, METAL_LIGHT))
    parts.append(box("rack_c2_s1", 1.2, 3.7, 0.38, 0.02, 1.46, METAL_DARK, z_off=0.50))
    parts.append(box("rack_c2_s2", 1.2, 4.2, 0.38, 0.02, 1.46, METAL_DARK, z_off=1.00))

    # 储物箱（货架上）
    parts.append(box("box_c1", -1.3, 3.7, 0.35, 0.30, 0.25, "0.75 0.65 0.40 1", z_off=0.62))
    parts.append(box("box_c2", -1.0, 4.2, 0.30, 0.35, 0.22, "0.60 0.60 0.65 1", z_off=0.62))
    parts.append(box("box_c3", 1.1, 3.8, 0.32, 0.28, 0.24, WOOD_LIGHT, z_off=0.62))
    parts.append(box("box_c4", 1.35, 4.5, 0.28, 0.32, 0.20, "0.70 0.55 0.35 1", z_off=1.12))

    # 地面堆放的箱子
    parts.append(box("crate_c1", 0.2, 2.8, 0.50, 0.50, 0.50, WOOD_LIGHT, yaw=0.3))
    parts.append(box("crate_c2", 1.0, 2.5, 0.45, 0.45, 0.45, WOOD_MED, yaw=-0.4))
    parts.append(box("crate_c3", 0.7, 3.2, 0.40, 0.40, 0.40, "0.65 0.55 0.40 1", yaw=0.7))

    # ---- room_b：客厅（东北角） ----
    # L 型沙发
    parts.append(box("sofa_b_seat", 4.5, 3.4, 1.8, 0.85, 0.42, FABRIC_BLUE, z_off=0.21))
    parts.append(box("sofa_b_back", 4.5, 3.8, 1.8, 0.12, 0.45, FABRIC_BLUE, z_off=0.64))
    parts.append(box("sofa_b_arm_l", 3.65, 3.4, 0.12, 0.85, 0.30, FABRIC_BLUE, z_off=0.57))
    parts.append(box("sofa_b_arm_r", 5.35, 3.4, 0.12, 0.85, 0.30, FABRIC_BLUE, z_off=0.57))
    # 沙发靠垫
    parts.append(box("cushion_b1", 4.1, 3.45, 0.40, 0.38, 0.10, "0.30 0.40 0.60 1", z_off=0.47))
    parts.append(box("cushion_b2", 4.9, 3.45, 0.40, 0.38, 0.10, "0.30 0.40 0.60 1", z_off=0.47))
    # 茶几
    parts.append(box("table_b_top", 4.5, 2.4, 0.90, 0.50, 0.04, WOOD_DARK, z_off=0.42))
    parts.append(box("table_b_leg1", 4.1, 2.2, 0.04, 0.04, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg2", 4.9, 2.2, 0.04, 0.04, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg3", 4.1, 2.6, 0.04, 0.04, 0.40, METAL_DARK, z_off=0.20))
    parts.append(box("table_b_leg4", 4.9, 2.6, 0.04, 0.04, 0.40, METAL_DARK, z_off=0.20))
    # 茶几上的物品
    parts.append(box("magazine_b", 4.4, 2.4, 0.22, 0.16, 0.02, "0.80 0.20 0.20 1", z_off=0.45))
    parts.append(cylinder("cup_b", 4.7, 2.35, 0.04, 0.10, WHITE, z_off=0.47))
    # 电视柜 + 电视
    parts.append(box("tvstand_b", 5.5, 3.4, 0.40, 1.2, 0.50, WOOD_DARK))
    parts.append(box("tv_b_screen", 5.5, 3.4, 0.04, 1.0, 0.60, BLACK, z_off=0.80))
    parts.append(box("tv_b_stand", 5.5, 3.4, 0.06, 0.20, 0.15, METAL_DARK, z_off=0.57))
    # 盆栽
    parts.append(cylinder("pot_b", 3.5, 2.2, 0.15, 0.30, POT_TERRA, z_off=0.15))
    parts.append(cylinder("plant_b", 3.5, 2.2, 0.20, 0.35, PLANT_GREEN, z_off=0.48))

    # ---- room_d：卧室（西南角） ----
    # 床架
    parts.append(box("bed_d_frame", -4.5, -3.8, 2.0, 1.4, 0.30, WOOD_DARK))
    # 床垫
    parts.append(box("bed_d_mattress", -4.5, -3.8, 1.90, 1.30, 0.22, WHITE, z_off=0.41))
    # 枕头
    parts.append(box("pillow_d1", -5.1, -3.5, 0.40, 0.25, 0.12, "0.90 0.90 0.92 1", z_off=0.58))
    parts.append(box("pillow_d2", -4.5, -3.5, 0.40, 0.25, 0.12, "0.90 0.90 0.92 1", z_off=0.58))
    # 被子
    parts.append(box("blanket_d", -4.2, -4.0, 1.30, 1.20, 0.08, FABRIC_GREEN, z_off=0.56))
    # 床头柜
    parts.append(box("nightstand_d", -5.55, -3.8, 0.45, 0.40, 0.50, WOOD_MED))
    parts.append(box("nightstand_d_drawer", -5.55, -3.8, 0.40, 0.36, 0.02, WOOD_DARK, z_off=0.26))
    # 台灯
    parts.append(cylinder("lamp_d_base", -5.55, -3.8, 0.06, 0.03, METAL_DARK, z_off=0.515))
    parts.append(cylinder("lamp_d_pole", -5.55, -3.8, 0.02, 0.25, METAL_DARK, z_off=0.64))
    parts.append(cylinder("lamp_d_shade", -5.55, -3.8, 0.10, 0.15, "0.95 0.90 0.80 1", z_off=0.84))
    # 衣柜（靠西墙）
    parts.append(box("wardrobe_d", -5.7, -2.2, 0.55, 1.0, 2.0, WOOD_MED))
    parts.append(box("wardrobe_d_door_l", -5.7, -2.5, 0.02, 0.48, 1.90, WOOD_DARK, z_off=1.0))
    parts.append(box("wardrobe_d_door_r", -5.7, -1.9, 0.02, 0.48, 1.90, WOOD_DARK, z_off=1.0))
    # 书桌（靠南墙）
    parts.append(box("desk_d_top", -4.5, -1.5, 1.0, 0.50, 0.04, WOOD_LIGHT, z_off=0.74))
    parts.append(box("desk_d_leg_fl", -4.95, -1.72, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_d_leg_fr", -4.05, -1.72, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_d_leg_bl", -4.95, -1.28, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    parts.append(box("desk_d_leg_br", -4.05, -1.28, 0.04, 0.04, 0.72, METAL_DARK, z_off=0.36))
    # 椅子
    parts.append(box("chair_d_seat", -4.5, -1.1, 0.42, 0.40, 0.05, BLACK, z_off=0.46))
    parts.append(box("chair_d_back", -4.5, -0.9, 0.42, 0.05, 0.45, BLACK, z_off=0.71))

    # ---- 南区大厅 ----
    # 装饰柱 ×2
    parts.append(pillar("pillar_1", 0.0, -2.0))
    parts.append(pillar("pillar_2", 2.6, -2.0))

    # 接待台 / 服务台
    parts.append(box("reception_desk", 3.0, -1.2, 1.6, 0.60, 1.05, WOOD_DARK))
    parts.append(box("reception_desk_top", 3.0, -1.2, 1.70, 0.70, 0.04, WOOD_MED, z_off=1.05))

    # 地面装饰箱
    parts.append(box("box_south_1", 3.2, -3.6, 0.50, 0.50, 0.50, WOOD_LIGHT, yaw=0.3))
    parts.append(box("box_south_2", -1.2, -4.2, 0.45, 0.45, 0.45, WOOD_MED, yaw=-0.5))

    # 盆栽（大厅）
    parts.append(cylinder("pot_hall_1", -0.8, -1.0, 0.18, 0.35, POT_TERRA, z_off=0.175))
    parts.append(cylinder("plant_hall_1", -0.8, -1.0, 0.25, 0.45, PLANT_GREEN, z_off=0.575))
    parts.append(cylinder("pot_hall_2", 4.5, -3.5, 0.15, 0.30, POT_TERRA, z_off=0.15))
    parts.append(cylinder("plant_hall_2", 4.5, -3.5, 0.22, 0.40, PLANT_GREEN, z_off=0.50))

    # ---- 充电桩（home 点） ----
    # 底座平台
    parts.append(cylinder("charger_base", 0.8, -3.6, 0.30, 0.06, METAL_DARK, z_off=0.03))
    # 充电柱
    parts.append(cylinder("charger_pillar", 0.8, -3.6, 0.12, 0.80, METAL_LIGHT, z_off=0.46))
    # 顶部
    parts.append(cylinder("charger_top", 0.8, -3.6, 0.18, 0.08, METAL_DARK, z_off=0.90))
    # 指示灯（绿色 = 可用）
    parts.append(cylinder("charger_led", 0.8, -3.48, 0.03, 0.04, GREEN_INDICATOR, z_off=0.70))
    # 充电接口
    parts.append(box("charger_port", 0.8, -3.45, 0.06, 0.04, 0.08, BLACK, z_off=0.55))

    # ---- 走廊装饰 ----
    # 走廊墙边装饰条（踢脚线已在墙函数中生成）
    # 走廊地面标识（home 点标记）
    parts.append(cylinder("floor_marker_home", 0.8, -3.6, 0.35, 0.01, "0.20 0.60 0.30 1", z_off=0.012))

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
