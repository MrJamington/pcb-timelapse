"""PCB Timelapse - record KiCad PCB layout as a zoomed-view and whole-board timelapse."""

from .recorder import (PAUSE_NAME, TOOL_NAME, TimelapseAction, TimelapsePauseAction,
                       start_autostarter, toggle_pause)
from .control_window import open_control_window
from .render_dialog import RENDER_NAME, TimelapseRenderAction, open_render_dialog

TimelapseAction().register()
TimelapsePauseAction().register()
TimelapseRenderAction().register()

try:
    # Our own click handlers, so KiCad doesn't mark the board modified on every click
    start_autostarter({TOOL_NAME: open_control_window, PAUSE_NAME: toggle_pause,
                       RENDER_NAME: open_render_dialog})
except Exception as e:  # noqa: BLE001 - never break plugin loading
    print("pcb_timelapse: background watcher not started:", e)
