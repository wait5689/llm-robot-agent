cmake_minimum_required(VERSION 3.0.2)
project(llm_robot_agent)

find_package(catkin REQUIRED COMPONENTS
  rospy
  std_msgs
  geometry_msgs
  nav_msgs
  sensor_msgs
  actionlib
  actionlib_msgs
  move_base_msgs
  tf
)

catkin_package()

# 可执行脚本。
# 注意：catkin_install_python 遇到不存在的文件会直接让 catkin_make 失败，
# 因此这里列出的每个脚本都必须真实存在（历史上前端曾引用不存在的
# nav_bridge.py，导致整包编译不过）。
catkin_install_python(PROGRAMS
  scripts/agent_run.py
  scripts/web_server.py
  scripts/initial_pose.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

# 需要安装的目录。maps 目录可能暂时为空（尚未建图），
# 用 OPTIONAL 避免空目录导致安装失败。
install(DIRECTORY launch urdf worlds config rviz
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
  USE_SOURCE_PERMISSIONS
)

install(DIRECTORY maps
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
  USE_SOURCE_PERMISSIONS
  OPTIONAL
)

install(DIRECTORY tools
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
  USE_SOURCE_PERMISSIONS
)
