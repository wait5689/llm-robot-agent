cmake_minimum_required(VERSION 3.0.2)
project(llm_robot_agent)

find_package(catkin REQUIRED COMPONENTS
  rospy
  std_msgs
  geometry_msgs
  nav_msgs
  sensor_msgs
  actionlib
  move_base_msgs
)

catkin_package()

catkin_install_python(PROGRAMS
  scripts/agent_run.py
  scripts/web_server.py
  scripts/nav_bridge.py
  DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

install(DIRECTORY launch urdf worlds config maps rviz
  DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}
  USE_SOURCE_PERMISSIONS
)
