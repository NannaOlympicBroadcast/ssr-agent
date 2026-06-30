"""SSR robotics: drive an Isaac Lab / OpenArm robot by natural language over the bus.

This package is the *brain side* of the integration. The agent never touches the
simulator; it only publishes/consumes :mod:`ssr.bus` events. Given an arbitrary
instruction (e.g. "把苹果放到橘子上"), the agent perceives objects by looking at
the camera frame (it is never handed object positions; it targets by image pixel),
then for each step:

* dispatches a semantic command (pick / place / move) on ``arm.action.execute``
  and registers a bus handler, then **ends the turn** (suspending the session);
* the robot side executes and publishes ``arm.grasp.completed`` /
  ``arm.action.completed`` (grasp/proprioception result + camera frame);
* that event wakes a fresh *bus-handler agent turn* which verifies the result and
  either re-plans (loop) or moves to the next step / finishes.

Topic constants and command helpers live in :mod:`ssr.robotics.protocol`.
"""

from __future__ import annotations

from . import protocol
from .controller import ArmController
from .protocol import ArmActionRequest

__all__ = ["ArmController", "ArmActionRequest", "protocol"]
