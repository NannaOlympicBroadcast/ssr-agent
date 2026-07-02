"""SSR robotics: drive an Isaac Lab / OpenArm robot by natural language over the bus.

This package holds both halves of the **big-brain / cerebellum (大脑/小脑)**
split. The big brain is the SSR agent itself: it never touches the simulator, it
only publishes/consumes :mod:`ssr.bus` events. Given an arbitrary instruction
(e.g. "把苹果放到橘子上"):

* for **grasping**, the brain's only vision job is cropping the target object out
  of the camera frame (``arm_grasp``) and publishing ``arm.grasp.request``; the
  **cerebellum** (:mod:`ssr.robotics.cerebellum`, hosted by the openarm plugin's
  bus handler) closes the realtime loop with the Om-Agent VLX-Flow streaming
  vision model — camera RTSP stream in, ``servo`` corrections out — and calls
  back on ``arm.grasp.result``;
* for other motion, the brain dispatches robot-advertised skills on
  ``arm.action.execute``, registers a bus handler and **ends the turn**
  (suspending the session); the robot's ``arm.action.completed`` wakes a fresh
  *bus-handler agent turn* which verifies the step and re-plans or advances.

Topic constants and payload dataclasses live in :mod:`ssr.robotics.protocol`.
"""

from __future__ import annotations

from . import protocol
from .controller import ArmController
from .protocol import ArmActionRequest, ArmGraspRequest

__all__ = ["ArmController", "ArmActionRequest", "ArmGraspRequest", "protocol"]
