"""机器人后端：Mock、ROS 与 HTTP 远程。"""

from .base import RobotBackend
from .mock_backend import FaultSpec, MockBackend, MockStats
from .ros_backend import ROS_AVAILABLE, RosBackend
from .http_backend import HttpBackend

__all__ = [
    "RobotBackend", "MockBackend", "MockStats", "FaultSpec",
    "RosBackend", "ROS_AVAILABLE", "HttpBackend",
]
