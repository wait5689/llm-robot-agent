#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导航配置静态校验（Nav Stack Lint）
==================================

为什么需要它
------------
ROS 的 launch / 参数错误绝大多数属于「启动后才报错」或「静默不生效」：
- `<node type="xxx.py">` 指向不存在的文件 → 启动即崩
- `<rosparam file="...">` 路径写错 → move_base 用默认参数跑，现象是乱转
- YAML 两个 costmap 共用同一份公共参数文件，若文件里带了 `global_costmap:`
  顶层键，参数会被塞进 `/move_base/global_costmap/global_costmap/`
  → 一切正常启动，但参数**永远不生效**（本项目旧版就是这个 bug）
- `$(find pkg)` 引用了不存在的包 → 只有运行到才报

这些问题每排查一次要开 Gazebo、等十几秒、翻终端。本脚本把它们提前
变成一次静态检查，且完全不需要 ROS 环境。

用法
----
    python3 tools/lint_launch.py
    python3 tools/lint_launch.py --json

退出码：0 = 通过，1 = 有问题
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List

PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_NAME = os.path.basename(PKG_ROOT)


class Issue:
    def __init__(self, level: str, file: str, msg: str):
        self.level = level
        self.file = file
        self.msg = msg

    def __str__(self) -> str:
        tag = "ERROR" if self.level == "error" else "WARN "
        return f"[{tag}] {self.file}: {self.msg}"


def _rel(path: str) -> str:
    try:
        return os.path.relpath(path, PKG_ROOT)
    except ValueError:
        return path


# ----------------------------------------------------------------------
# launch 检查
# ----------------------------------------------------------------------

def _resolve(path_expr: str) -> str:
    """把 $(find pkg)/x 解析成包内绝对路径；返回空串表示外部包。"""
    m = re.match(r"^\$\(find ([^)]+)\)(.*)$", path_expr.strip())
    if not m:
        return ""
    pkg, rest = m.group(1), m.group(2)
    if pkg != PKG_NAME:
        return ""  # 外部包的资源，本地无法检查
    return os.path.normpath(os.path.join(PKG_ROOT, rest.lstrip("/")))


def _resolve_arg(value: str, args: Dict[str, str]) -> str:
    """替换一层 $(arg xxx)。"""
    m = re.match(r"^\$\(arg ([^)]+)\)$", value.strip())
    if m:
        return args.get(m.group(1), "")
    return value


def check_launch(path: str, issues: List[Issue]) -> None:
    rel = _rel(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        issues.append(Issue("error", rel, f"XML 解析失败: {exc}"))
        return

    root = tree.getroot()

    # 收集 arg 默认值
    args: Dict[str, str] = {}
    for a in root.findall("arg"):
        name = a.get("name")
        if name and a.get("default") is not None:
            args[name] = a.get("default")

    seen_args = set()
    for a in root.findall("arg"):
        n = a.get("name")
        if n in seen_args:
            issues.append(Issue("warn", rel, f"arg '{n}' 重复定义"))
        seen_args.add(n)

    scripts_dir = os.path.join(PKG_ROOT, "scripts")

    # node 检查
    for node in root.findall("node"):
        pkg = node.get("pkg", "")
        ntype = node.get("type", "")
        nname = node.get("name", "?")

        if pkg == PKG_NAME:
            # 本包节点：type 必须是 scripts/ 下真实存在的文件
            if ntype.endswith(".py"):
                p = os.path.join(scripts_dir, ntype)
                if not os.path.exists(p):
                    issues.append(Issue(
                        "error", rel,
                        f"node '{nname}' 的 type='{ntype}' 在 scripts/ 下不存在"))
            else:
                issues.append(Issue(
                    "warn", rel,
                    f"node '{nname}' 的 type='{ntype}' 不是脚本，"
                    f"若这是 C++ 节点可忽略"))

        # rosparam / param 文件路径
        for rp in node.findall("rosparam"):
            f = rp.get("file", "")
            resolved = _resolve(f)
            if not resolved:
                if "$(find" in f:
                    issues.append(Issue(
                        "warn", rel,
                        f"node '{nname}' 引用了外部包资源 {f}，跳过检查"))
                continue
            if not os.path.exists(resolved):
                issues.append(Issue(
                    "error", rel,
                    f"node '{nname}' 的 rosparam file 不存在: {_rel(resolved)}"))

    # 顶层 rosparam
    for rp in root.findall("rosparam"):
        f = rp.get("file", "")
        resolved = _resolve(f)
        if resolved and not os.path.exists(resolved):
            issues.append(Issue("error", rel, f"rosparam file 不存在: {_rel(resolved)}"))

    # include 检查
    for inc in root.findall("include"):
        f = inc.get("file", "")
        resolved = _resolve(f)
        if resolved and not os.path.exists(resolved):
            issues.append(Issue("error", rel, f"include 的文件不存在: {_rel(resolved)}"))

    # rviz 配置
    for node in root.findall("node"):
        if node.get("type") != "rviz":
            continue
        a = node.get("args", "")
        m = re.search(r"-d\s+(\S+)", a)
        if m:
            resolved = _resolve(m.group(1))
            if resolved and not os.path.exists(resolved):
                issues.append(Issue(
                    "error", rel, f"rviz 配置不存在: {_rel(resolved)}"
                    "（rviz 会直接报错退出）"))

        # 检查 rviz 的 arg 是否在顶层定义
        for m2 in re.finditer(r"\$\(arg ([^)]+)\)", a):
            if m2.group(1) not in args:
                issues.append(Issue(
                    "error", rel, f"引用了未定义的 arg '{m2.group(1)}'"))

    # 全文件扫描未定义 arg
    raw = open(path, "r", encoding="utf-8").read()
    raw_nc = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
    for m3 in re.finditer(r"\$\(arg ([^)]+)\)", raw_nc):
        name = m3.group(1)
        if name in args:
            continue
        # 可能是从 include 传入的，这里只警告
        if f'<arg name="{name}"' not in raw_nc:
            issues.append(Issue(
                "warn", rel, f"可能引用了未定义 arg '{name}'"))

    # 检查 use_sim_time
    if "gazebo" in rel and "use_sim_time" not in raw_nc:
        issues.append(Issue("warn", rel, "未设置 use_sim_time，TF 时间戳会与仿真不一致"))


# ----------------------------------------------------------------------
# YAML 检查
# ----------------------------------------------------------------------

def _yaml_top_keys(path: str) -> List[str]:
    keys = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                continue
            m = re.match(r"^([A-Za-z_][\w]*):", line)
            if m:
                keys.append(m.group(1))
            elif line.strip() and not line.startswith(" ") and not line.startswith("-"):
                pass
    return keys


def check_nav_configs(issues: List[Issue]) -> None:
    cfg = os.path.join(PKG_ROOT, "config")

    common = os.path.join(cfg, "costmap_common_params.yaml")
    if not os.path.exists(common):
        issues.append(Issue("error", "config", "缺少 costmap_common_params.yaml"))
    else:
        keys = _yaml_top_keys(common)
        # 这是本项目曾经踩过的坑
        for bad in ("global_costmap", "local_costmap"):
            if bad in keys:
                issues.append(Issue(
                    "error", _rel(common),
                    f"公共参数文件不应包含顶层键 '{bad}:'。"
                    f"它会被加载到 {bad}/ 命名空间下，导致参数路径变成 "
                    f"/move_base/{bad}/{bad}/xxx，**静默失效**"))

    for name, expect in (
        ("global_costmap_params.yaml", "global_costmap"),
        ("local_costmap_params.yaml", "local_costmap"),
    ):
        p = os.path.join(cfg, name)
        if not os.path.exists(p):
            issues.append(Issue("error", "config", f"缺少 {name}"))
            continue
        keys = _yaml_top_keys(p)
        if expect not in keys:
            issues.append(Issue(
                "error", _rel(p),
                f"缺少顶层键 '{expect}:'（该文件会被整体 load 到 /move_base 下）"))

    # 关键参数存在性
    required = {
        "amcl_params.yaml": ["laser_model_type", "tf_broadcast", "odom_frame_id",
                             "global_frame_id", "base_frame_id"],
        "move_base_params.yaml": ["controller_frequency", "recovery_behaviors"],
        "base_local_planner_params.yaml": ["TrajectoryPlannerROS"],
        "locations.yaml": ["frame_id", "locations"],
    }
    for fname, keys in required.items():
        p = os.path.join(cfg, fname)
        if not os.path.exists(p):
            issues.append(Issue("error", "config", f"缺少 {fname}"))
            continue
        text = open(p, "r", encoding="utf-8").read()
        for k in keys:
            if not re.search(rf"^\s*{re.escape(k)}\s*:", text, re.M):
                issues.append(Issue(
                    "error", _rel(p), f"缺少关键字段 '{k}'"))

    # AMCL 的 tf_broadcast 必须是 true
    p = os.path.join(cfg, "amcl_params.yaml")
    if os.path.exists(p):
        text = open(p, "r", encoding="utf-8").read()
        m = re.search(r"^\s*tf_broadcast:\s*(\w+)", text, re.M)
        if m and m.group(1).lower() != "true":
            issues.append(Issue(
                "error", _rel(p),
                "tf_broadcast 必须为 true，否则 move_base 拿不到 map->odom，"
                "全局规划器完全不工作"))

    # 膨胀半径与门宽的匹配
    common_p = os.path.join(cfg, "costmap_common_params.yaml")
    world_p = os.path.join(PKG_ROOT, "worlds", "complex_indoor.world")
    if os.path.exists(common_p) and os.path.exists(world_p):
        text = open(common_p, "r", encoding="utf-8").read()
        m = re.search(r"^\s*inflation_radius:\s*([\d.]+)", text, re.M)
        if m:
            inflate = float(m.group(1))
            # 门宽不再写死：从 tools/gen_world.py 读 DOOR_W。
            # 之前这里硬编码 1.10，门宽改成 1.90 后校验就成了摆设，
            # 会让「膨胀把门封死」这个真实故障漏过去。
            door_w = None
            gen_p = os.path.join(PKG_ROOT, "tools", "gen_world.py")
            if os.path.exists(gen_p):
                gtext = open(gen_p, "r", encoding="utf-8").read()
                gm = re.search(r"^DOOR_W\s*=\s*([\d.]+)", gtext, re.M)
                if gm:
                    door_w = float(gm.group(1))
            if door_w is None:
                issues.append(Issue(
                    "warning", _rel(common_p),
                    "无法从 tools/gen_world.py 读到 DOOR_W，跳过门宽匹配校验"))
            elif inflate * 2 >= door_w:
                issues.append(Issue(
                    "error", _rel(common_p),
                    f"inflation_radius={inflate} 过大：2×{inflate} ≥ 门宽 {door_w}，"
                    f"所有门洞会被封死，机器人无法进房间"))
            else:
                # 留一点余量提示：实际可通行宽度小于 0.6m 时对 DWA 很不友好
                usable = door_w - 2 * inflate
                if usable < 0.60:
                    issues.append(Issue(
                        "warning", _rel(common_p),
                        f"门洞可用宽度仅 {usable:.2f}m（= {door_w} - 2×{inflate}），"
                        f"小于 0.60m 时局部规划器容易在门口反复犹豫"))

    # ★ 规划器选择与参数文件必须一致
    #
    # 这是本项目踩过的最贵的一个坑：
    # navigation.launch 加载了 dwa_local_planner_params.yaml，
    # 但 move_base_params.yaml 里 base_local_planner 指的是
    # TrajectoryPlannerROS。结果 TPR 一个参数都没读到，全部走默认值，
    # 而 TPR 默认 holonomic_robot = true，它会输出 linear.y，
    # 差速轮不吃横向分量 —— 机器人一步不动，8 次恢复行为全部无效，
    # 日志里没有任何报错。8 个目标全失败但看不出原因。
    #
    # 所以这里强制把「选中的规划器」和「launch 加载的参数文件」对齐检查。
    mb_p = os.path.join(cfg, "move_base_params.yaml")
    nav_launch_p = os.path.join(PKG_ROOT, "launch", "navigation.launch")
    if os.path.exists(mb_p) and os.path.exists(nav_launch_p):
        mb_text = open(mb_p, "r", encoding="utf-8").read()
        pm = re.search(r"^\s*base_local_planner\s*:\s*[\"']?([^\"'\n#]+)",
                       mb_text, re.M)
        if not pm:
            issues.append(Issue(
                "error", _rel(mb_p),
                "缺少 base_local_planner，move_base 会回退到默认规划器"))
        else:
            planner = pm.group(1).strip()
            # 插件类名 -> 预期的参数文件顶层键
            if "DWAPlannerROS" in planner:
                want_file = "dwa_local_planner_params.yaml"
                want_key = "DWAPlannerROS"
            elif "TrajectoryPlannerROS" in planner:
                want_file = "base_local_planner_params.yaml"
                want_key = "TrajectoryPlannerROS"
            else:
                want_file, want_key = None, None

            if want_file:
                launch_text = open(nav_launch_p, "r", encoding="utf-8").read()
                # 先去掉 XML 块注释，再找实际生效的 rosparam 行。
                # 必须去注释：本项目 launch 里刻意保留了被注释掉的备选
                # planner 参数行作说明，不剔除会误判成「加载了两个」。
                uncommented = re.sub(r"<!--.*?-->", "", launch_text, flags=re.S)
                active = []
                for line in uncommented.splitlines():
                    if re.search(
                            r"rosparam.*config/(base_local_planner_params|dwa_local_planner_params)\.yaml",
                            line):
                        fm = re.search(
                            r"config/(base_local_planner_params|dwa_local_planner_params)\.yaml",
                            line)
                        active.append(fm.group(1) + ".yaml")

                if want_file not in active:
                    issues.append(Issue(
                        "error", _rel(nav_launch_p),
                        f"move_base_params.yaml 选了 {planner}，"
                        f"但 launch 没有加载 {want_file}"
                        f"（实际加载：{active or '无'}）。"
                        f"规划器会拿不到任何参数而全部走默认值，"
                        f"典型表现是 holonomic_robot 默认为 true、"
                        f"输出 linear.y，差速轮不响应 → 机器人一步不动且无报错"))
                for other in active:
                    if other != want_file:
                        issues.append(Issue(
                            "warning", _rel(nav_launch_p),
                            f"同时加载了 {other}，与选中的规划器 {planner} 不匹配，"
                            f"该文件不会起作用（可保留作对照，但别指望它生效）"))

                # 参数文件里必须有对应顶层键
                pf = os.path.join(cfg, want_file)
                if os.path.exists(pf):
                    if want_key not in _yaml_top_keys(pf):
                        issues.append(Issue(
                            "error", _rel(pf),
                            f"缺少顶层键 '{want_key}:'，"
                            f"所选规划器 {planner} 将读不到参数"))
                else:
                    issues.append(Issue(
                        "error", "config", f"缺少 {want_file}"))

    # 到达容差一致性
    agent_p = os.path.join(cfg, "agent.yaml")
    local_p = os.path.join(cfg, "base_local_planner_params.yaml")
    if os.path.exists(agent_p) and os.path.exists(local_p):
        a = open(agent_p, "r", encoding="utf-8").read()
        l = open(local_p, "r", encoding="utf-8").read()
        ma = re.search(r"^\s*goal_tolerance:\s*([\d.]+)", a, re.M)
        ml = re.search(r"^\s*xy_goal_tolerance:\s*([\d.]+)", l, re.M)
        if ma and ml and float(ml.group(1)) > float(ma.group(1)):
            issues.append(Issue(
                "error", _rel(local_p),
                f"xy_goal_tolerance={ml.group(1)} 大于 agent 侧 "
                f"goal_tolerance={ma.group(1)}，会出现"
                f"「move_base 已到达但 agent 判定未到达」的矛盾"))


def main() -> int:
    ap = argparse.ArgumentParser(description="导航配置静态校验")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    issues: List[Issue] = []

    launch_dir = os.path.join(PKG_ROOT, "launch")
    if not os.path.isdir(launch_dir):
        print("错误：找不到 launch 目录", file=sys.stderr)
        return 2
    for f in sorted(os.listdir(launch_dir)):
        if f.endswith(".launch"):
            check_launch(os.path.join(launch_dir, f), issues)

    check_nav_configs(issues)

    # CMakeLists 里 catkin_install_python 引用的脚本必须存在
    cmake = os.path.join(PKG_ROOT, "CMakeLists.txt")
    if os.path.exists(cmake):
        text = open(cmake, "r", encoding="utf-8").read()
        m = re.search(r"catkin_install_python\(PROGRAMS(.*?)DESTINATION",
                      text, re.S)
        if m:
            for line in m.group(1).splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    p = os.path.join(PKG_ROOT, s)
                    if not os.path.exists(p):
                        issues.append(Issue(
                            "error", "CMakeLists.txt",
                            f"catkin_install_python 引用了不存在的文件 '{s}'"
                            f"（会导致 catkin_make 直接失败）"))

    if args.json:
        print(json.dumps([{"level": i.level, "file": i.file, "msg": i.msg}
                          for i in issues], ensure_ascii=False, indent=2))
    else:
        errors = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level != "error"]
        for i in issues:
            print(i)
        print()
        if not issues:
            print("✓ 导航配置校验通过：launch / 参数 / 依赖引用均自洽")
        else:
            print(f"共 {len(errors)} 个错误，{len(warns)} 个警告")

    return 1 if any(i.level == "error" for i in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
