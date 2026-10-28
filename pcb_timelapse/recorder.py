"""
PCB Timelapse recorder (KiCad action plugin, SWIG API - KiCad 7 to 10).

Click the toolbar button to start, click again to stop.
While recording, and only while the PCB Editor window is the active window:
  * view stream  - screenshot of the canvas every VIEW_INTERVAL_S
  * board stream - copies of your saves and KiCad's autosave file (see BOARD_SOURCE)

Output: <board folder>/pcb_timelapse/<recording timestamp>/
Render it afterwards with render.py (see README).
"""

import csv
import gzip
import hashlib
import json
import os
import shutil
import sys
import threading
import time

import re

import pcbnew
import wx

try:
    import wx.aui  # so KiCad's toolbars come back as AuiToolBar objects
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL_NAME = "PCB Timelapse: recorder"
PAUSE_NAME = "PCB Timelapse: pause/resume recording"

# ---------------------------------------------------------------- settings
VIEW_INTERVAL_S = 1.0      # screenshot period while the editor is active
BOARD_SOURCE = "safe"      # "safe" - board stream comes from files KiCad itself wrote:
                           #          your saves and KiCad's autosave. Never touches the
                           #          live board, so it can't upset KiCad mid-drag.
                           # "live" - also serialise the live board every BOARD_INTERVAL_S.
                           #          Finer-grained, but can crash KiCad if it lands
                           #          mid-operation (KiCad's own autosave waits for that;
                           #          a plugin can't tell).
BOARD_INTERVAL_S = 5.0     # live mode only: snapshot period
CAPTURE_MODE = "window"    # "window" = whole editor, and the drawing area's position is
                           #   logged so the renderer can crop to it (or not) later
                           # "canvas" = drawing area only (smaller files, UI can't be shown)
SKIP_DUPLICATE_VIEWS = True
AUTO_START = "marked"      # "off"    - only the toolbar button starts recording
                           # "marked" - auto-start boards you've ticked "auto-record" for
                           # "always" - auto-start every board you open
STORE_IN = "outside"       # "outside" - <STORE_DIR>/<board name> (<id>)/, outside the project
                           #   (KiCad's backup on save scans the whole project folder,
                           #   so thousands of screenshots in it slow every save down)
                           # "project" - <board folder>/pcb_timelapse/, next to the board
STORE_DIR = ""             # base folder for "outside"; empty = Documents/PCB Timelapse
ICON_BLINK = True          # blink the record button while capturing (False = steady red)
# --------------------------------------------------------------------------

AUTOSTART_MARKER = ".autostart"   # lives in <board folder>/pcb_timelapse/, wherever recordings are stored

# Settings changed in the PCB Timelapse window are saved here and override the
# defaults above
def settings_dir():
    """KiCad's user settings folder, so updating the plugin doesn't wipe your settings."""
    try:
        base = pcbnew.SETTINGS_MANAGER.GetUserSettingsPath()
    except Exception:  # noqa: BLE001
        base = os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "pcb_timelapse")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.dirname(os.path.abspath(__file__))
    return d


SETTINGS_FILE = os.path.join(settings_dir(), "recorder_settings.json")
SETTING_NAMES = ("AUTO_START", "CAPTURE_MODE", "VIEW_INTERVAL_S", "BOARD_SOURCE", "STORE_IN", "STORE_DIR", "ICON_BLINK")


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except Exception:  # noqa: BLE001 - missing or damaged: keep defaults
        return
    g = globals()
    for k in SETTING_NAMES:
        if k in saved and isinstance(saved[k], type(g[k])):
            g[k] = saved[k]


def save_settings(**changes):
    g = globals()
    for k, v in changes.items():
        if k in SETTING_NAMES:
            g[k] = v
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({k: g[k] for k in SETTING_NAMES}, f, indent=2)
    except OSError as e:
        print("pcb_timelapse: couldn't save settings:", e)


load_settings()


def window_icons(png):
    """Title bar / taskbar icons for our windows, from one of the plugin's PNGs."""
    bundle = wx.IconBundle()
    try:
        img = wx.Image(os.path.join(HERE, png))
        for size in (16, 24, 32, 48, 64, 96):
            bundle.AddIcon(wx.Icon(wx.Bitmap(img.Scale(size, size, wx.IMAGE_QUALITY_HIGH))))
    except Exception:  # noqa: BLE001 - an icon is never worth failing over
        pass
    return bundle


def _state():
    """
    Shared state, kept on the `sys` module so it survives 'Refresh Plugins'
    (which re-imports this file). Not on wx.GetApp(): inside KiCad that returns
    a fresh wrapper each call, so attributes set on it vanish.
    """
    st = getattr(sys, "_pcb_timelapse_state", None)
    if st is None:
        st = {"rec": None, "auto": None, "render_dlg": None}
        sys._pcb_timelapse_state = st
    return st


def _marker_path(board_path):
    """Small per-project files stay in the project; only recordings may live outside it."""
    return os.path.join(project_sessions_dir(board_path), AUTOSTART_MARKER)


def autostart_marked(board_path):
    return os.path.exists(_marker_path(board_path))


def _set_autostart(board_path, on):
    m = _marker_path(board_path)
    if on:
        os.makedirs(os.path.dirname(m), exist_ok=True)
        open(m, "w").close()
    elif os.path.exists(m):
        os.remove(m)


def _find_pcb_frame():
    wins = wx.GetTopLevelWindows()
    for w in wins:
        if w.GetName() == "PcbFrame":
            return w
    for w in wins:
        if "PCB Editor" in w.GetTitle():
            return w
    return None


def _is_foreground(frame):
    """
    True only if the PCB Editor is the window you're actually using.
    On Windows, ask the OS directly: wx's IsActive() can stay True on the lock
    screen or after wake, which would capture black frames.
    """
    if frame is None or frame.IsIconized():
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            fg = user32.GetForegroundWindow()
            if not fg:
                return False  # lock screen / secure desktop
            root = user32.GetAncestor(fg, 3)  # GA_ROOTOWNER: dialogs map to their owner
            return int(frame.GetHandle()) in (fg, root) and frame.IsActive()
        except Exception:  # noqa: BLE001
            pass
    return frame.IsActive()


class ToolbarIcon:
    """
    Swaps the record button's icon: dull dot when idle, bright red when
    recording, blinking while it's actually capturing. Best effort - if the
    button can't be found it just does nothing.
    """

    NAME = TOOL_NAME
    FILES = {"idle": "icon.png", "rec": "icon_rec.png", "rec_dim": "icon_rec_dim.png",
             "paused": "icon_rec_paused.png"}

    def __init__(self):
        self.found = None       # (toolbar, tool id)
        self.shown = None
        self.images = {}

    def _find(self, frame):
        stack = list(frame.GetChildren())
        while stack:
            w = stack.pop()
            if hasattr(w, "GetToolCount") and hasattr(w, "FindToolByIndex"):
                for i in range(w.GetToolCount()):
                    item = w.FindToolByIndex(i)
                    if item is not None and item.GetShortHelp() == self.NAME:
                        return w, item.GetId()
            stack.extend(w.GetChildren())
        return None

    def _bundle(self, state, size):
        key = (state, size)
        if key not in self.images:
            img = wx.Image(os.path.join(HERE, self.FILES[state]))
            b1 = wx.Bitmap(img.Scale(size, size, wx.IMAGE_QUALITY_HIGH))
            b2 = wx.Bitmap(img.Scale(size * 2, size * 2, wx.IMAGE_QUALITY_HIGH))
            self.images[key] = wx.BitmapBundle.FromBitmaps(b1, b2)
        return self.images[key]

    def set(self, state):
        frame = _find_pcb_frame()
        if frame is None:
            return
        for _attempt in range(2):
            try:
                if self.found is None:
                    self.found = self._find(frame)
                    self.shown = None
                    if self.found is None:
                        return
                tb, tool_id = self.found
                if state == self.shown and tb.FindTool(tool_id) is not None:
                    return
                size = tb.GetToolBitmapSize().GetHeight() or 24
                tb.SetToolBitmap(tool_id, self._bundle(state, size))
                tb.Refresh()
                self.shown = state
                return
            except Exception:  # noqa: BLE001 - toolbar rebuilt or destroyed: look again
                self.found = None


def _icon():
    st = _state()
    if st.get("icon") is None:
        st["icon"] = ToolbarIcon()
    return st["icon"]


def _find_canvas(frame):
    """The GAL canvas is the largest visible direct child of the frame."""
    best, best_area = None, 0
    for c in frame.GetChildren():
        if not c.IsShown() or isinstance(c, wx.StatusBar):
            continue
        w, h = c.GetClientSize()
        if w * h > best_area:
            best, best_area = c, w * h
    return best


def _enabled_layer_names(board):
    names = []
    for i in range(pcbnew.PCB_LAYER_ID_COUNT):
        try:
            if board.IsLayerEnabled(i):
                names.append(board.GetLayerName(i))
        except Exception:  # noqa: BLE001
            pass
    return names


def _match_layer(text, names):
    """Layer-box items look like 'F.Cu (PgUp)' or 'In1.Cu' - strip the extras."""
    text = text.strip()
    best = None
    for n in names:
        if (text == n or text.startswith(n + " ")) and (best is None or len(n) > len(best)):
            best = n
    return best


class ActiveLayerProbe:
    """
    The SWIG API has no 'get active layer', so read the editor's layer
    selector dropdown instead. Returns None if it can't be found.
    """

    def __init__(self):
        self.widget = None
        self.names = []
        self.retry_at = 0.0

    def _looks_like_layer_box(self, w):
        try:
            n = w.GetCount()
            if n < 2:
                return False
            hits = sum(1 for i in range(min(n, 6)) if _match_layer(w.GetString(i), self.names))
            return hits >= 2
        except Exception:  # noqa: BLE001
            return False

    def _search(self, frame):
        stack = list(frame.GetChildren())
        while stack:
            w = stack.pop()
            if hasattr(w, "GetCount") and hasattr(w, "GetString") and hasattr(w, "GetSelection"):
                if self._looks_like_layer_box(w):
                    return w
            stack.extend(w.GetChildren())
        return None

    def get(self, frame, board):
        self.names = _enabled_layer_names(board)
        for _attempt in range(2):
            if self.widget is None:
                if time.monotonic() < self.retry_at:
                    return None
                self.widget = self._search(frame)
                if self.widget is None:
                    self.retry_at = time.monotonic() + 10  # don't walk the UI every tick
                    return None
            try:
                idx = self.widget.GetSelection()
                if idx < 0:
                    return None
                return _match_layer(self.widget.GetString(idx), self.names)
            except Exception:  # noqa: BLE001 - widget was rebuilt/destroyed
                self.widget = None
        return None


def _save_board_copy(board, path):
    """Write the live board to `path` without touching the board's own file/project."""
    errors = []
    for attempt in (
        lambda: pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, path, board),  # v8+
        lambda: pcbnew.IO_MGR.Save(pcbnew.IO_MGR.KICAD_SEXP, path, board),          # v6/7
    ):
        try:
            attempt()
            return
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    raise RuntimeError("Could not save board snapshot: %r" % errors)


def _next_index(folder, prefix):
    best = -1
    try:
        for name in os.listdir(folder):
            m = re.match(re.escape(prefix) + r"(\d+)", name)
            if m:
                best = max(best, int(m.group(1)))
    except OSError:
        pass
    return best + 1


FOLDER_NAME = "pcb_timelapse"     # STORE_IN = "project": <board folder>/pcb_timelapse/
RENDERS = "renders"                 # <folder>/renders/<recording>/ - kept when recordings are deleted


def board_key(board_path):
    """'<board name> (<id>)': readable, and unique per board file."""
    full = os.path.normcase(os.path.abspath(board_path))
    stem = os.path.splitext(os.path.basename(board_path))[0]
    return f"{stem} ({hashlib.md5(full.encode('utf-8')).hexdigest()[:6]})"


def default_store_dir():
    try:
        docs = wx.StandardPaths.Get().GetDocumentsDir()
    except Exception:  # noqa: BLE001
        docs = ""
    return os.path.join(docs or os.path.join(os.path.expanduser("~"), "Documents"), "PCB Timelapse")


def store_dir():
    return STORE_DIR or default_store_dir()


def project_sessions_dir(board_path):
    return os.path.join(os.path.dirname(board_path), FOLDER_NAME)


def outside_sessions_dir(board_path):
    return os.path.join(store_dir(), board_key(board_path))


def sessions_dir(board_path):
    """Where new recordings for this board go (the current setting)."""
    if STORE_IN == "project":
        return project_sessions_dir(board_path)
    return outside_sessions_dir(board_path)


def session_roots(board_path):
    """Every folder this board's recordings may be in: the current one first."""
    roots = [sessions_dir(board_path), project_sessions_dir(board_path), outside_sessions_dir(board_path)]
    out = []
    for r in roots:
        if os.path.normcase(r) not in (os.path.normcase(x) for x in out):
            out.append(r)
    return out


def move_session(d, dest_root):
    """Move a recording, and its rendered videos, into another folder. Returns the new path."""
    src_root, name = os.path.dirname(d), os.path.basename(d)
    if os.path.normcase(src_root) == os.path.normcase(dest_root):
        return d
    dst = os.path.join(dest_root, name)
    if os.path.exists(dst):
        raise OSError(f"{dst} already exists")
    os.makedirs(dest_root, exist_ok=True)
    shutil.move(d, dst)
    vids = renders_dir(src_root, name)
    if os.path.isdir(vids) and not os.path.exists(renders_dir(dest_root, name)):
        os.makedirs(os.path.join(dest_root, RENDERS), exist_ok=True)
        shutil.move(vids, renders_dir(dest_root, name))
    return dst


def renders_dir(root, name):
    """Where the videos for a recording (or a joined render) go."""
    return os.path.join(root, RENDERS, name)


def list_sessions(board_path):
    """This board's recordings, wherever they're stored, newest first."""
    out = []
    for root in session_roots(board_path):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        out += [os.path.join(root, n) for n in names
                if os.path.exists(os.path.join(root, n, "log.csv"))]
    return sorted(out, key=os.path.basename, reverse=True)


def session_info(d, with_size=False):
    """Frames, board snapshots and active seconds from a session's log."""
    info = {"path": d, "frames": 0, "boards": 0, "seconds": 0.0, "bytes": 0}
    try:
        with open(os.path.join(d, "log.csv"), newline="") as f:
            for r in csv.DictReader(f):
                try:
                    info["seconds"] = max(info["seconds"], float(r["t_active"]))
                except (KeyError, TypeError, ValueError):
                    continue
                if r.get("stream") == "view":
                    info["frames"] += 1
                elif r.get("stream") == "board":
                    info["boards"] += 1
    except OSError:
        pass
    if with_size:
        info["bytes"] = dir_size(d)
    return info


def dir_size(d):
    """Total bytes under a folder. scandir gets sizes from the directory listing
    on Windows, so this avoids one file-system call per screenshot."""
    total = 0
    stack = [d]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def session_label(d):
    name = os.path.basename(d)
    m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}(.*)", name)
    if m:
        return f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" + (f" ({m[6][1:]})" if m[6] else "")
    return name


class Recorder:
    def __init__(self, board, auto=False, resume=None):
        """resume: an existing session folder to continue instead of starting a new one."""
        if board is None:
            raise RuntimeError("No board is open.")
        board_path = board.GetFileName()
        if not board_path:
            raise RuntimeError("Save the board once before recording.")

        self.board_name = os.path.splitext(os.path.basename(board_path))[0]
        if resume:
            if not os.path.exists(os.path.join(resume, "log.csv")):
                raise RuntimeError("That isn't a recording folder (no log.csv).")
            self.root = resume
        else:
            base = os.path.join(sessions_dir(board_path),
                                time.strftime("%Y%m%d-%H%M%S"))
            self.root, n = base, 1
            while os.path.exists(self.root):     # two sessions in the same second
                n += 1
                self.root = f"{base}_{n}"
        self.view_dir = os.path.join(self.root, "view")
        self.board_dir = os.path.join(self.root, "board")
        os.makedirs(self.view_dir, exist_ok=True)
        os.makedirs(self.board_dir, exist_ok=True)

        self.board_path = board_path
        self.auto = auto
        self.n_view = 0
        self.n_board = 0               # snapshots attempted (used for file indices)
        self.n_board_saved = 0         # snapshots actually kept (changed boards)
        self.workers = []
        self.layer_probe = ActiveLayerProbe()
        self.layer_seen = False
        self.active_time = 0.0          # seconds the editor was actually active
        self.last_tick = None
        self.last_view_hash = None
        self.last_board_hash = None
        self.last_board_stamp = None
        self.last_board_save = 0.0
        self.next_board_at = 0.0
        self.paused = False
        self.save_cost = 0.0            # how long serialising the board takes (s)
        self.views_since_board = 0      # canvas changes since the last board snapshot
        self.lock = threading.Lock()
        self.resumed = bool(resume)

        log_path = os.path.join(self.root, "log.csv")
        if resume:
            # carry on the clock and file numbering from where the session stopped
            info = session_info(self.root)
            self.active_time = info["seconds"] + 1.0
            self.n_view = _next_index(self.view_dir, "view_")
            self.n_board = _next_index(self.board_dir, "board_")
            self.next_board_at = self.active_time
            self.log_f = open(log_path, "a", newline="")
            self.log = csv.writer(self.log_f)
        else:
            self.log_f = open(log_path, "w", newline="")
            self.log = csv.writer(self.log_f)
            self.log.writerow(["t_active", "t_wall", "stream", "file", "active_layer", "canvas"])

        meta_path = os.path.join(self.root, "session.json")
        meta = {}
        if resume:
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except Exception:  # noqa: BLE001
                meta = {}
            meta.setdefault("resumed", []).append(time.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            meta = {
                "board": self.board_name,
                "board_path": board_path,
                "kicad_version": pcbnew.GetBuildVersion(),
                "view_interval_s": VIEW_INTERVAL_S,
                "board_source": BOARD_SOURCE,
                "board_interval_s": BOARD_INTERVAL_S,
                "capture_mode": CAPTURE_MODE,
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        with open(meta_path, "w") as f:
            json.dump(meta, f, indent=2)

        self.frame = _find_pcb_frame()
        self.autosave_path = os.path.join(os.path.dirname(board_path),
                                          "_autosave-" + os.path.basename(board_path))
        self.seen_mtimes = {}
        for pth in (board_path, self.autosave_path):
            try:
                self.seen_mtimes[pth] = os.path.getmtime(pth)
            except OSError:
                self.seen_mtimes[pth] = None

        self.blink = False
        _icon().set("rec")
        if BOARD_SOURCE == "safe":
            # starting point: the board as last saved (or autosaved, if newer)
            src = max((p for p in (board_path, self.autosave_path) if os.path.exists(p)),
                      key=os.path.getmtime, default=None)
            if src:
                self._grab_file(src)

        self.timer = wx.Timer()
        self.timer.Bind(wx.EVT_TIMER, self._on_tick)
        self.timer.Start(int(VIEW_INTERVAL_S * 1000))

    # ---------------------------------------------------------------- loop
    def _on_tick(self, _evt):
        now = time.monotonic()
        if self.paused:
            self.last_tick = None       # paused by you: clock stopped, nothing captured
            _icon().set("paused")
            return
        frame = _find_pcb_frame()
        if not _is_foreground(frame):
            self.last_tick = None       # pause the active-time clock
            _icon().set("rec")          # solid = recording, but paused
            return
        self.blink = not self.blink if ICON_BLINK else True  # blinking = capturing right now
        _icon().set("rec" if self.blink else "rec_dim")

        # A long gap means the timer didn't run (laptop asleep, KiCad frozen):
        # don't count that time as editing
        if self.last_tick is not None and now - self.last_tick < max(5.0, 3 * VIEW_INTERVAL_S):
            self.active_time += now - self.last_tick
        self.last_tick = now

        board = pcbnew.GetBoard()
        if board is None or board.GetFileName() != self.board_path:
            return  # a different board is open; don't mix it in

        try:
            self._grab_view(frame, board)
        except Exception as e:  # noqa: BLE001
            print("pcb_timelapse: view capture failed:", e)

        # Files KiCad wrote itself (your save, or its autosave) are always a
        # consistent board: copy them off the UI thread
        if self._check_files(board):
            return

        if BOARD_SOURCE == "live" and self.active_time >= self.next_board_at:
            if wx.GetMouseState().LeftIsDown():
                return  # mid-drag: wait for the mouse to let go, so it never hitches
            # Big boards take longer to serialise: space snapshots out so the
            # editor spends at most ~5% of its time on them
            self.next_board_at = self.active_time + max(BOARD_INTERVAL_S, 20 * self.save_cost)
            try:
                self._grab_board(board)
            except Exception as e:  # noqa: BLE001
                print("pcb_timelapse: board snapshot failed:", e)

    def _log(self, stream, fname, t_active=None, layer="", canvas=""):
        t = self.active_time if t_active is None else t_active
        with self.lock:
            self.log.writerow([f"{t:.3f}", f"{time.time():.3f}", stream, fname, layer or "", canvas])
            self.log_f.flush()

    # ------------------------------------------------------------- streams
    def _grab_view(self, frame, board):
        layer = self.layer_probe.get(frame, board)
        if layer:
            self.layer_seen = True
        canvas = _find_canvas(frame) or frame
        win = frame if CAPTURE_MODE == "window" else canvas
        x, y = win.ClientToScreen((0, 0))
        w, h = win.GetClientSize()
        if w <= 0 or h <= 0:
            return

        bmp = wx.Bitmap(w, h)
        mem = wx.MemoryDC(bmp)
        mem.Blit(0, 0, w, h, wx.ScreenDC(), x, y)
        mem.SelectObject(wx.NullBitmap)

        # where the drawing area sits inside this frame, so the renderer can crop
        cx, cy = canvas.ClientToScreen((0, 0))
        cw, ch = canvas.GetClientSize()
        rect = (max(0, cx - x), max(0, cy - y), min(cw, w), min(ch, h))
        rect_s = "%d %d %d %d" % rect if win is frame and canvas is not frame else ""

        img = bmp.ConvertToImage()
        # judge black frames and duplicates on the drawing area only: the status
        # bar's cursor coordinates change constantly and would defeat de-duplication
        area = img.GetSubImage(wx.Rect(*rect)) if rect_s and rect[2] > 0 and rect[3] > 0 else img
        data = bytes(area.GetData())
        if max(data[::997] or b"\0") < 8:
            return  # pure black: screen locked/off or the capture failed
        if SKIP_DUPLICATE_VIEWS:
            digest = hashlib.md5(data + (layer or "").encode()).hexdigest()
            if digest == self.last_view_hash:
                return
            self.last_view_hash = digest

        fname = f"view_{self.n_view:06d}.png"
        img.SaveFile(os.path.join(self.view_dir, fname), wx.BITMAP_TYPE_PNG)
        self.n_view += 1
        self.views_since_board += 1
        self._log("view", fname, layer=layer, canvas=rect_s)

    def _grab_board(self, board, force=False):
        # Cheap change check first, if this KiCad version exposes it
        stamp = None
        try:
            stamp = board.GetTimeStamp()
        except Exception:  # noqa: BLE001
            pass
        # Undo/redo may not bump the stamp. Only re-check an "unchanged" board if
        # the canvas has actually changed since, and at most once a minute.
        if (not force and stamp is not None and stamp == self.last_board_stamp
                and (self.views_since_board == 0
                     or time.monotonic() - self.last_board_save < 60)):
            return
        self.last_board_save = time.monotonic()
        self.views_since_board = 0

        idx = self.n_board
        tmp = os.path.join(self.board_dir, f"_tmp_{idx:06d}.pcbtmp")
        t0 = time.perf_counter()
        _save_board_copy(board, tmp)          # must run on the GUI thread
        cost = time.perf_counter() - t0
        self.save_cost = cost if not self.save_cost else 0.7 * self.save_cost + 0.3 * cost
        self.last_board_stamp = stamp
        self._queue_finish(tmp, idx, self.active_time)

    def _check_files(self, board=None):
        """Snapshot any of the watched files that changed. True if one did."""
        newest = None
        for pth in (self.board_path, self.autosave_path):
            try:
                m = os.path.getmtime(pth)
            except OSError:
                m = None  # autosave file is deleted again when you save
            if m is not None and m != self.seen_mtimes.get(pth):
                self.seen_mtimes[pth] = m
                if newest is None or m > newest[0]:
                    newest = (m, pth)
        if newest is None:
            return False
        try:
            self._grab_file(newest[1], board)
        except Exception as e:  # noqa: BLE001
            print("pcb_timelapse: file snapshot failed:", e)
        return True

    def _grab_file(self, src, board=None):
        """Snapshot by copying a board file KiCad wrote (off the UI thread)."""
        idx = self.n_board
        tmp = os.path.join(self.board_dir, f"_tmp_{idx:06d}.pcbtmp")
        try:  # only touch the live board if the caller knows the editor is open
            if board is not None:
                self.last_board_stamp = board.GetTimeStamp()
        except Exception:  # noqa: BLE001
            pass
        self.last_board_save = time.monotonic()
        self.views_since_board = 0
        self.next_board_at = self.active_time + max(BOARD_INTERVAL_S, 20 * self.save_cost)
        t_active = self.active_time

        def copy_then_finish(prev):
            if prev is not None:
                prev.join()
            # KiCad may still be writing it: wait until it stops changing, and
            # only keep a copy that ends like a complete board file
            for _ in range(10):
                try:
                    st1 = os.stat(src)
                    time.sleep(0.3)
                    st2 = os.stat(src)
                    if (st1.st_mtime, st1.st_size) != (st2.st_mtime, st2.st_size):
                        continue
                    shutil.copyfile(src, tmp)
                    with open(tmp, "rb") as f:
                        f.seek(max(0, os.path.getsize(tmp) - 64))
                        if f.read().rstrip().endswith(b")"):
                            break
                except OSError:
                    time.sleep(0.3)
            else:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return
            self._finish_board(tmp, idx, t_active, None)

        prev = self.workers[-1] if self.workers else None
        t = threading.Thread(target=copy_then_finish, args=(prev,), daemon=True)
        t.start()
        self.workers = [t]
        self.n_board += 1

    def _queue_finish(self, tmp, idx, t_active):
        # Hashing + gzip off the GUI thread so the editor doesn't stutter.
        # Workers run one at a time (join previous) so snapshots stay in order.
        prev = self.workers[-1] if self.workers else None
        t = threading.Thread(target=self._finish_board, args=(tmp, idx, t_active, prev),
                             daemon=True)
        t.start()
        self.workers = [t]
        self.n_board += 1

    def _finish_board(self, tmp, idx, t_active, prev):
        if prev is not None:
            prev.join()
        with open(tmp, "rb") as f:
            data = f.read()
        os.remove(tmp)
        digest = hashlib.md5(data).hexdigest()
        if digest == self.last_board_hash:
            return
        self.last_board_hash = digest
        fname = f"board_{idx:06d}.kicad_pcb.gz"
        with gzip.open(os.path.join(self.board_dir, fname), "wb", compresslevel=6) as g:
            g.write(data)
        self.n_board_saved += 1
        self._log("board", fname, t_active)

    # ---------------------------------------------------------------- stop
    def stop(self, editor_open=True):
        self.timer.Stop()
        _icon().set("idle")
        _pause_icon().set("pause")
        try:
            # final state, so the video ends on the finished board
            got = self._check_files()
            if not got and BOARD_SOURCE == "live" and editor_open and self.n_view:
                board = pcbnew.GetBoard()
                if board is not None and board.GetFileName() == self.board_path:
                    self._grab_board(board, force=True)  # you clicked stop: no tool mid-drag
        except Exception as e:  # noqa: BLE001
            print("pcb_timelapse: final snapshot failed:", e)
        for t in self.workers:   # let the last gzip finish
            t.join(timeout=30)
        with self.lock:
            self.log_f.close()
        if self.n_view == 0 and self.n_board_saved == 0 and not self.resumed:
            shutil.rmtree(self.root, ignore_errors=True)  # never actually used
            return False
        return True


def _recently_intercepted(name):
    """True if our own click hook already handled this plugin a moment ago."""
    t = _state().setdefault("intercepted", {}).get(name, 0)
    return time.monotonic() - t < 1.5


class AutoStarter:
    """
    Background watcher (every 2 s):
      * starts/stops recording per AUTO_START when boards open/close
      * hooks our toolbar buttons and menu entries so clicks run our code
        directly. When KiCad runs an action plugin itself, it snapshots the
        whole board into the undo list and marks the board modified (the '*'),
        even if the plugin changed nothing. Our buttons never touch the board,
        so we take the click before KiCad does.
    """

    def __init__(self, handlers=None):
        self.declined = set()   # boards you stopped by hand this KiCad session
        self.handlers = handlers or {}
        self.bound = {}         # frame handle -> ids we've hooked
        self.next_scan = 0.0
        self.timer = wx.Timer()
        self.timer.Bind(wx.EVT_TIMER, self._on_tick)
        self.timer.Start(2000)
        wx.CallAfter(self._on_tick, None)

    # ------------------------------------------------------------- hooks
    @staticmethod
    def _ids_for(frame, name):
        ids = set()
        stack = list(frame.GetChildren())
        while stack:            # toolbar buttons (short help = plugin name)
            w = stack.pop()
            if hasattr(w, "GetToolCount") and hasattr(w, "FindToolByIndex"):
                for i in range(w.GetToolCount()):
                    item = w.FindToolByIndex(i)
                    if item is not None and item.GetShortHelp() == name:
                        ids.add(item.GetId())
            stack.extend(w.GetChildren())
        mb = frame.GetMenuBar()  # Tools > External Plugins entries
        menus = [mb.GetMenu(i) for i in range(mb.GetMenuCount())] if mb else []
        while menus:
            m = menus.pop()
            for item in m.GetMenuItems():
                if item.GetSubMenu() is not None:
                    menus.append(item.GetSubMenu())
                elif item.GetItemLabelText() == name:
                    ids.add(item.GetId())
        return ids

    def _hook(self, frame):
        if frame is None or not self.handlers:
            return
        key = int(frame.GetHandle())
        now = time.monotonic()
        if key in self.bound and now < self.next_scan:
            return  # walking KiCad's UI is cheap but not free; rescan every 30 s
        self.next_scan = now + 30
        try:
            bound = self.bound.setdefault(key, set())
            for name, fn in self.handlers.items():
                for wid in self._ids_for(frame, name) - bound:
                    frame.Bind(wx.EVT_MENU, lambda e, n=name, f=fn: self._fire(n, f), id=wid)
                    bound.add(wid)
        except Exception as e:  # noqa: BLE001 - never break the editor over this
            print("pcb_timelapse: couldn't hook buttons:", e)

    @staticmethod
    def _fire(name, fn):
        # Not calling Skip() stops KiCad's own plugin handler from running.
        _state().setdefault("intercepted", {})[name] = time.monotonic()
        wx.CallAfter(fn)

    # -------------------------------------------------------------- tick
    def _on_tick(self, _evt):
        st = _state()
        board = pcbnew.GetBoard()
        path = board.GetFileName() if board is not None else ""
        frame = _find_pcb_frame()
        self._hook(frame)
        self.declined &= {path}   # reopening a board later can auto-start again

        rec = st["rec"]
        if rec is not None and (frame is None or rec.board_path != path):
            st["rec"] = None          # board closed or switched: wrap up quietly
            rec.stop(editor_open=False)
            rec = None

        if AUTO_START == "off":
            return
        if rec is None and path and frame is not None and path not in self.declined:
            if AUTO_START == "always" or (AUTO_START == "marked"
                                          and autostart_marked(path)):
                try:
                    st["rec"] = Recorder(board, auto=True)
                except Exception as e:  # noqa: BLE001
                    print("pcb_timelapse: auto-start failed:", e)
                    self.declined.add(path)


def start_autostarter(handlers=None):
    """Start the background watcher. handlers: {plugin name: function to run on click}."""
    st = _state()
    if st["auto"] is not None:
        st["auto"].timer.Stop()
    st["auto"] = AutoStarter(handlers)


def toggle_pause():
    rec = _state()["rec"]
    if rec is None:
        wx.MessageBox("Not recording. Start recording with the record button first.",
                      "PCB Timelapse")
        return
    rec.paused = not rec.paused
    rec.last_tick = None
    _icon().set("paused" if rec.paused else "rec")
    _pause_icon().set("resume" if rec.paused else "pause")


class PauseIcon(ToolbarIcon):
    NAME = PAUSE_NAME
    FILES = {"pause": "icon_pause.png", "resume": "icon_resume.png"}


def _pause_icon():
    st = _state()
    if st.get("pause_icon") is None:
        st["pause_icon"] = PauseIcon()
    return st["pause_icon"]


class TimelapsePauseAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = PAUSE_NAME
        self.category = "Recording"
        self.description = "Pause or resume the current PCB Timelapse recording"
        self.show_toolbar_button = True
        icon = os.path.join(HERE, "icon_pause.png")
        self.icon_file_name = icon if os.path.exists(icon) else ""

    def Run(self):
        if _recently_intercepted(PAUSE_NAME):
            return  # already handled by our click hook
        toggle_pause()


class TimelapseAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = TOOL_NAME
        self.category = "Recording"
        self.description = "Record a zoomed-view and whole-board timelapse of PCB layout"
        self.show_toolbar_button = True
        icon = os.path.join(os.path.dirname(__file__), "icon.png")
        self.icon_file_name = icon if os.path.exists(icon) else ""

    def Run(self):
        if _recently_intercepted(TOOL_NAME):
            return  # already handled by our click hook
        from .control_window import open_control_window
        open_control_window()


def start_recording(resume=None):
    """Start (or continue) recording. Returns None on success, else an error message."""
    st = _state()
    if st["rec"] is not None:
        return "Already recording."
    try:
        st["rec"] = Recorder(pcbnew.GetBoard(), resume=resume)
    except Exception as e:  # noqa: BLE001
        st["rec"] = None
        return str(e)
    return None


def stop_recording():
    """Stop recording. Returns the stopped Recorder (or None)."""
    st = _state()
    rec, st["rec"] = st["rec"], None
    if rec is None:
        return None
    rec.kept = rec.stop()
    if st["auto"] is not None:
        st["auto"].declined.add(rec.board_path)  # not again until it's reopened
    return rec


def toggle_recording():
    """Old popup-based toggle, kept for reference; the record button now opens the window."""
    st = _state()
    if st["rec"] is None:
        try:
            st["rec"] = Recorder(pcbnew.GetBoard())
        except Exception as e:  # noqa: BLE001
            wx.MessageBox(str(e), "PCB Timelapse", wx.ICON_ERROR)
            st["rec"] = None
            return
        wx.MessageBox(
            "Recording started.\n\nOutput:\n" + st["rec"].root +
            "\n\nOnly captures while the PCB Editor is the active window.\n"
            "Click the button again to stop.", "PCB Timelapse")
        return

    rec, st["rec"] = st["rec"], None
    kept = rec.stop()
    if st["auto"] is not None:
        st["auto"].declined.add(rec.board_path)  # not again until it's reopened

    if kept:
        summary = (f"Recording stopped.\n\n{rec.n_view} view frames, "
                   f"{rec.n_board_saved} board snapshots\n"
                   f"{rec.active_time / 60:.1f} min of active editing\n"
                   f"Active layer: {'tracked' if rec.layer_seen else 'NOT detected'}"
                   f"\n\n{rec.root}")
    else:
        summary = "Recording stopped. Nothing was captured, so the session was discarded."

    if AUTO_START == "marked":
        on = autostart_marked(rec.board_path)
        ans = wx.MessageBox(summary + "\n\nAuto-record this board whenever it's opened?"
                            + ("\n(currently ON)" if on else ""),
                            "PCB Timelapse", wx.YES_NO | wx.ICON_QUESTION)
        _set_autostart(rec.board_path, ans == wx.YES)
    else:
        wx.MessageBox(summary, "PCB Timelapse")
