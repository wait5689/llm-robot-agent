#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语义地点表（Location Map）
==========================

把「房间 A」这种人话映射到地图坐标系下的 (x, y, yaw)。

为什么需要这一层
----------------
如果让 LLM 直接输出 `{"x": -3.5, "y": 2.5}`，它会编造坐标，
而且每次建图后坐标系都会变，模型更不可能知道。所以：
- 模型只输出**语义名称**
- 名称到坐标的映射放在 YAML 里，由人维护
这是把「不可靠的生成能力」限制在「可靠查表」范围内的典型做法。
"""

from __future__ import annotations

import logging
import math
import os
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class LocationMap:
    """地点查询与别名解析。"""

    def __init__(self, entries: Dict[str, Dict], home: Optional[Dict] = None,
                 aliases: Optional[Dict[str, str]] = None, frame_id: str = "map"):
        self.frame_id = frame_id
        self._entries = dict(entries)
        self._aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        if home:
            self._entries.setdefault("home", home)

    # ---------------- 构造 ----------------
    @classmethod
    def from_yaml(cls, path: str) -> "LocationMap":
        if yaml is None:
            raise RuntimeError("需要 PyYAML：pip install pyyaml")
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        entries = dict(raw.get("locations") or {})
        home = raw.get("home")
        if home:
            entries["home"] = home
        return cls(
            entries=entries,
            aliases=raw.get("aliases") or {},
            frame_id=raw.get("frame_id", "map"),
        )

    @classmethod
    def from_dict(cls, data: Dict) -> "LocationMap":
        entries = dict(data.get("locations") or {})
        if data.get("home"):
            entries["home"] = data["home"]
        return cls(entries, aliases=data.get("aliases"), frame_id=data.get("frame_id", "map"))

    # ---------------- 查询 ----------------
    def known_names(self) -> List[str]:
        """对外暴露的规范地点名（不含别名）。"""
        return sorted(self._entries.keys())

    def resolve(self, name: str) -> Optional[str]:
        """把用户/模型给的说法解析成规范地点名，失败返回 None。

        依次尝试：精确匹配 -> 别名 -> 大小写不敏感 -> 去除常见前后缀。
        """
        if not name:
            return None
        raw = str(name).strip()
        if raw in self._entries:
            return raw

        lower = raw.lower()
        if lower in self._aliases:
            return self._aliases[lower]
        if lower in self._entries:
            return lower

        # 常见变体：「去房间A」「前往 room A」「the room_b」
        for prefix in ("前往", "去", "到", "回到", "返回", "the", "go to", "to"):
            if lower.startswith(prefix):
                lower = lower[len(prefix):].strip()
        for suffix in ("号房间", "房间", "room"):
            if lower.endswith(suffix):
                lower = lower[: -len(suffix)].strip()
        compact = lower.replace(" ", "").replace("_", "")
        for candidate in (lower, compact):
            if candidate in self._entries:
                return candidate
            if candidate in self._aliases:
                return self._aliases[candidate]

        return None

    def describe(self, name: str) -> str:
        entry = self._entries.get(name) or {}
        return entry.get("description") or "无说明"

    def pose(self, name: str) -> Optional[Tuple[float, float, float]]:
        """返回 (x, y, yaw)，地点不存在返回 None。"""
        entry = self._entries.get(name)
        if not entry:
            return None
        pos = entry.get("position") or [0.0, 0.0]
        yaw = float(entry.get("yaw", 0.0))
        return float(pos[0]), float(pos[1]), yaw

    def nearest(self, x: float, y: float) -> Tuple[str, float]:
        """找出离 (x, y) 最近的地点名，用于汇报「你在哪个房间」。"""
        best_name, best_dist = "", float("inf")
        for name in self._entries:
            pose = self.pose(name)
            if pose is None:
                continue
            d = math.hypot(pose[0] - x, pose[1] - y)
            if d < best_dist:
                best_name, best_dist = name, d
        return best_name, best_dist

    def __len__(self) -> int:
        return len(self._entries)


def default_location_map() -> LocationMap:
    """不依赖外部 YAML 的兜底地点表，保证程序永远能启动。"""
    return LocationMap.from_dict(
        {
            "frame_id": "map",
            "home": {"position": [0.0, -3.0], "yaw": 0.0, "description": "起点 / 充电桩"},
            "locations": {
                "room_a": {"position": [-3.5, 2.5], "yaw": 1.57, "description": "房间 A，西侧"},
                "room_b": {"position": [3.5, 2.5], "yaw": -1.57, "description": "房间 B，东侧"},
                "room_c": {"position": [0.0, 5.0], "yaw": 3.14, "description": "房间 C，北侧"},
                "room_d": {"position": [-3.5, -2.5], "yaw": 0.0, "description": "房间 D，西南角"},
                "corridor": {"position": [0.0, 0.0], "yaw": 0.0, "description": "中央走廊"},
            },
            "aliases": {
                "a": "room_a", "房间a": "room_a", "房间A": "room_a",
                "b": "room_b", "房间b": "room_b", "房间B": "room_b",
                "c": "room_c", "房间c": "room_c",
                "d": "room_d", "房间d": "room_d",
                "起点": "home", "返回起点": "home",
                "走廊": "corridor",
            },
        }
    )
