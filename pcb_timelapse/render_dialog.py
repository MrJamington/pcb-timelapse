"""
Render dialog for PCB Timelapse. Runs render.py with your normal Python
(KiCad's bundled Python doesn't have PyMuPDF/Pillow) as a background process.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import pcbnew
import wx

HERE = os.path.dirname(os.path.abspath(__file__))
RENDER_PY = os.path.join(HERE, "render.py")
def _settings_path():
    from . import recorder
    return os.path.join(recorder.settings_dir(), "render_settings.json")


SETTINGS = _settings_path()

HOLE_STYLES = [("KiCad (gold plated)", "kicad"), ("Dark", "dark"), ("Board colour", "board"), ("White ring", "white"),
               ("Cyan ring", "cyan"), ("Cyan (solid)", "cyan-solid"), ("Gold ring", "gold")]
LAYOUTS = [("Off", "off"), ("Auto", "auto"), ("Side by side", "side"), ("Board below", "below")]
SIZES = ["auto", "1920x1080", "2560x1440", "3840x2160", "1080x1080", "1080x1920"]
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


# ------------------------------------------------------------------ helpers
def _clean_env():
    """
    KiCad points PYTHONHOME/PYTHONPATH at its own bundled Python. Passing that on
    would make your normal Python load KiCad's library instead of its own.
    """
    env = dict(os.environ)
    for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP",
              "__PYVENV_LAUNCHER__", "VIRTUAL_ENV"):
        env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _kill_tree(proc):
    """terminate() only stops render.py itself; take kicad-cli/ffmpeg/workers with it."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.terminate()


def _load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_settings(d):
    try:
        with open(SETTINGS, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _python_ok(cmd):
    try:
        r = subprocess.run(cmd + ["-c", "import fitz, PIL; print('ok')"], capture_output=True,
                           text=True, timeout=20, creationflags=NO_WINDOW, env=_clean_env())
        return r.returncode == 0 and "ok" in r.stdout
    except Exception:  # noqa: BLE001
        return False


def _find_python():
    """A Python that isn't KiCad's bundled one and has pymupdf + pillow."""
    kicad_dir = os.path.normcase(os.path.dirname(sys.executable))
    # On Linux KiCad uses the system Python, which lives next to kicad in /usr/bin,
    # so only skip KiCad's folder where it ships its own Python (Windows/macOS).
    skip_kicad_dir = not sys.platform.startswith("linux")
    cands = []
    if shutil.which("py"):
        cands.append(["py", "-3"])
    for n in ("python3", "python"):
        p = shutil.which(n)
        if not p or "WindowsApps" in p or [p] in cands:
            continue
        if skip_kicad_dir and os.path.normcase(os.path.dirname(p)) == kicad_dir:
            continue
        cands.append([p])
    for c in cands:
        if _python_ok(c):
            return c
    return None


def _find_kicad_cli():
    exe = "kicad-cli.exe" if sys.platform == "win32" else "kicad-cli"
    p = os.path.join(os.path.dirname(sys.executable), exe)  # KiCad's own bin folder
    if os.path.exists(p):
        return p
    return shutil.which("kicad-cli") or ""


# Rarely-used layers first, so they sink to the bottom of the dialog's list
BOTTOM_TO_TOP = ["User.Drawings", "User.Comments", "User.Eco1", "User.Eco2", "Margin",
                 "B.Fab", "B.Courtyard", "B.Silkscreen", "B.Mask", "B.Paste", "B.Adhesive",
                 "B.Cu", "F.Cu", "F.Adhesive", "F.Paste", "F.Mask", "F.Silkscreen",
                 "F.Courtyard", "F.Fab", "Edge.Cuts"]


def _layer_sort_key(name):
    """Bottom -> top drawing order, as seen from the front."""
    m = re.match(r"In(\d+)\.Cu$", name)
    if m:  # inner layers sit between B.Cu and F.Cu, In1 nearest the top
        return (BOTTOM_TO_TOP.index("B.Cu") + 0.5, -int(m.group(1)))
    if name in BOTTOM_TO_TOP:
        return (BOTTOM_TO_TOP.index(name), 0)
    return (-1, 0)  # User.1 etc / renamed layers: bottom of the pile


DEFAULT_ON = {"B.Silkscreen", "B.Cu", "F.Cu", "F.Silkscreen", "Edge.Cuts"}


def _board_layers(board):
    names = []
    for i in range(pcbnew.PCB_LAYER_ID_COUNT):
        try:
            if board.IsLayerEnabled(i):
                names.append(board.GetLayerName(i))
        except Exception:  # noqa: BLE001
            pass
    return sorted(names, key=_layer_sort_key)


# ------------------------------------------------------------------- dialog
class RenderDialog(wx.Dialog):
    """
    Layout: three sections stacked with draggable dividers (sessions / layers+options /
    tools+log), and a draggable divider between layers and options. Each section shrinks
    on its own (lists and options scroll inside themselves), and the divider positions
    keep their proportions when the window is resized. Sizes are remembered.
    """
    SASH_MIN = 80  # px, smallest a section can be dragged to

    def __init__(self, parent, board):
        super().__init__(parent, title="PCB Timelapse - Render",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.proc = None
        self.cfg = _load_settings()
        from . import recorder as rec_mod
        self.sp_dir = rec_mod.sessions_dir(board.GetFileName())
        self.out_dir = None
        c = self.cfg

        self.outer = outer = wx.Panel(self)
        sp = wx.SP_LIVE_UPDATE | wx.SP_3DSASH | wx.SP_NOBORDER
        self.split_top = wx.SplitterWindow(outer, style=sp)        # sessions | rest
        self.split_bot = wx.SplitterWindow(self.split_top, style=sp)  # middle | tools+log
        self.split_mid = wx.SplitterWindow(self.split_bot, style=sp)  # layers | options
        for s in (self.split_top, self.split_bot, self.split_mid):
            s.SetMinimumPaneSize(self.SASH_MIN)
            s.Bind(wx.EVT_SPLITTER_SASH_POS_CHANGED, self._sash_moved)

        # ---- sessions
        top = wx.Panel(self.split_top)
        sbox = wx.StaticBoxSizer(wx.VERTICAL, top, "Sessions (tick one or more)")
        self.session_paths = []
        self.sessions = wx.CheckListBox(top, size=(-1, 110))
        for d in self._sessions():
            self._add_session(d)
        if self.session_paths:
            self.sessions.Check(0, True)  # newest
        srow = wx.BoxSizer(wx.HORIZONTAL)
        browse = wx.Button(top, label="Add folder...")
        browse.Bind(wx.EVT_BUTTON, self._browse)
        sel_all = wx.Button(top, label="All")
        sel_all.Bind(wx.EVT_BUTTON, lambda e: self._check_all(True))
        sel_none = wx.Button(top, label="None")
        sel_none.Bind(wx.EVT_BUTTON, lambda e: self._check_all(False))
        self.join = wx.RadioButton(top, label="Join into one video", style=wx.RB_GROUP)
        self.separate = wx.RadioButton(top, label="Render each separately")
        (self.separate if c.get("separate") else self.join).SetValue(True)
        for w in (browse, sel_all, sel_none):
            srow.Add(w, 0, wx.RIGHT, 4)
        srow.AddStretchSpacer()
        srow.Add(self.join, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        srow.Add(self.separate, 0, wx.ALIGN_CENTER_VERTICAL)
        sbox.Add(self.sessions, 1, wx.ALL | wx.EXPAND, 4)
        sbox.Add(srow, 0, wx.ALL | wx.EXPAND, 4)
        ts = wx.BoxSizer(wx.VERTICAL)
        ts.Add(sbox, 1, wx.ALL | wx.EXPAND, 6)
        top.SetSizer(ts)

        # ---- layers
        left = wx.Panel(self.split_mid)
        lbox = wx.StaticBoxSizer(wx.VERTICAL, left, "Board layers (top of list = drawn on top)")
        self.layers = wx.CheckListBox(left, size=(220, 260))
        saved = c.get("layers")  # [[name, checked], ...] top-first
        names = list(reversed(_board_layers(board)))
        if saved:
            known = [n for n, _ in saved if n in names]
            names = known + [n for n in names if n not in known]
            checked = {n for n, on in saved if on}
        else:
            checked = DEFAULT_ON
        self.layers.Set(names)
        for i, n in enumerate(names):
            self.layers.Check(i, n in checked)
        ud = wx.BoxSizer(wx.HORIZONTAL)
        for lbl, d in (("Up", -1), ("Down", 1)):
            b = wx.Button(left, label=lbl, size=(60, -1))
            b.Bind(wx.EVT_BUTTON, lambda e, d=d: self._move(d))
            ud.Add(b, 0, wx.RIGHT, 4)
        reset = wx.Button(left, label="Reset", size=(60, -1))
        reset.Bind(wx.EVT_BUTTON, self._reset_layers)
        ud.Add(reset)
        lbox.Add(self.layers, 1, wx.ALL | wx.EXPAND, 4)
        lbox.Add(ud, 0, wx.ALL, 4)
        ls = wx.BoxSizer(wx.VERTICAL)
        ls.Add(lbox, 1, wx.LEFT | wx.TOP | wx.BOTTOM | wx.EXPAND, 6)
        left.SetSizer(ls)

        # ---- options (scrolls on its own when squeezed)
        right = wx.Panel(self.split_mid)
        obox = wx.StaticBoxSizer(wx.VERTICAL, right, "Options")
        self.opt_scroll = o = wx.ScrolledWindow(right, style=wx.VSCROLL | wx.HSCROLL)
        o.SetScrollRate(10, 10)
        g = wx.FlexGridSizer(0, 2, 6, 8)
        g.AddGrowableCol(1)

        def add(label, ctrl):
            g.Add(wx.StaticText(o, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctrl, 1, wx.EXPAND)
            return ctrl

        self.streams = add("Render", wx.Choice(o, choices=["Both", "View only", "Board only"]))
        self.streams.SetSelection(c.get("streams", 0))
        self.view_ui = add("View video shows", wx.Choice(o, choices=["Drawing area only",
                                                                     "Whole editor (with UI)"]))
        self.view_ui.SetSelection(1 if c.get("view_ui") == "full" else 0)
        self.follow = add("Follow active layer", wx.CheckBox(o))
        self.follow.SetValue(c.get("follow", True))
        self.inactive = add("Inactive layer opacity %", wx.SpinCtrl(o, min=5, max=100,
                                                                     initial=c.get("inactive", 100)))
        self.bg = add("Background", wx.ColourPickerCtrl(o, colour=wx.Colour(c.get("bg", "#001023"))))
        self.holes = add("Pad / mounting holes", wx.Choice(o, choices=[lbl for lbl, _ in HOLE_STYLES]))
        hv = c.get("holes", "kicad")
        self.holes.SetSelection(next((i for i, (_, v) in enumerate(HOLE_STYLES) if v == hv), 0))
        self.via_layer = add("Vias in layer colour", wx.CheckBox(o))
        self.via_layer.SetValue(c.get("via_layer", True))
        self.vias = add("Vias", wx.Choice(o, choices=[lbl for lbl, _ in HOLE_STYLES]))
        vv = c.get("vias", "kicad")
        self.vias.SetSelection(next((i for i, (_, v) in enumerate(HOLE_STYLES) if v == vv), 0))
        self.crop = add("Frame", wx.Choice(o, choices=["Board outline", "Everything"]))
        self.crop.SetSelection(c.get("crop", 0))
        self.fill = add("Shade board area", wx.CheckBox(o))
        self.fill.SetValue(c.get("fill", True))
        self.outline = add("Outline width (px)", wx.SpinCtrl(o, min=1, max=12, initial=c.get("outline", 3)))
        self.glow = add("Glow %", wx.SpinCtrl(o, min=0, max=100, initial=c.get("glow", 20)))
        self.sat = add("Colour boost %", wx.SpinCtrl(o, min=50, max=200, initial=c.get("sat", 120)))
        self.size = add("Board video size", wx.ComboBox(o, choices=SIZES, value=c.get("size", SIZES[0])))
        self.fps = add("FPS", wx.SpinCtrl(o, min=1, max=120, initial=c.get("fps", 30)))
        self.hold = add("Hold last frame (s)", wx.SpinCtrl(o, min=0, max=30, initial=c.get("hold", 3)))
        self.sync = add("Match view timing", wx.CheckBox(o))
        self.sync.SetValue(c.get("sync", True))
        self.combined = add("Combined video", wx.Choice(o, choices=[lbl for lbl, _ in LAYOUTS]))
        cv = c.get("combined", "auto")
        cv = {True: "auto", False: "off"}.get(cv, cv)  # older settings were a checkbox
        self.combined.SetSelection(next((i for i, (_, v) in enumerate(LAYOUTS) if v == cv), 1))
        og = wx.BoxSizer(wx.VERTICAL)
        og.Add(g, 0, wx.ALL | wx.EXPAND, 6)
        o.SetSizer(og)
        obox.Add(o, 1, wx.EXPAND)
        rs = wx.BoxSizer(wx.VERTICAL)
        rs.Add(obox, 1, wx.RIGHT | wx.TOP | wx.BOTTOM | wx.EXPAND, 6)
        right.SetSizer(rs)

        # ---- tools + log
        bottom = wx.Panel(self.split_bot)
        tbox = wx.StaticBoxSizer(wx.VERTICAL, bottom, "Tools")
        tg = wx.FlexGridSizer(0, 2, 6, 8)
        tg.AddGrowableCol(1)
        py = c.get("python") or " ".join(_find_python() or [])
        tg.Add(wx.StaticText(bottom, label="Python"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.python = wx.TextCtrl(bottom, value=py)
        tg.Add(self.python, 1, wx.EXPAND)
        tg.Add(wx.StaticText(bottom, label="kicad-cli"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cli = wx.TextCtrl(bottom, value=c.get("cli") or _find_kicad_cli())
        tg.Add(self.cli, 1, wx.EXPAND)
        tbox.Add(tg, 0, wx.ALL | wx.EXPAND, 4)
        self.log = wx.TextCtrl(bottom, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 100))
        self.gauge = wx.Gauge(bottom, range=100, size=(-1, 8))
        bs = wx.BoxSizer(wx.VERTICAL)
        bs.Add(tbox, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)
        bs.Add(self.log, 1, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)
        bs.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.TOP | wx.EXPAND, 6)
        bottom.SetSizer(bs)

        # ---- buttons (outside the splitters, always visible)
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = wx.Button(outer, label="Open output folder")
        self.open_btn.Bind(wx.EVT_BUTTON, self._open_out)
        self.open_btn.Disable()
        self.run_btn = wx.Button(outer, label="Render")
        self.run_btn.Bind(wx.EVT_BUTTON, self._run)
        close = wx.Button(outer, label="Close")
        close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btns.Add(self.open_btn)
        btns.AddStretchSpacer()
        btns.Add(self.run_btn, 0, wx.RIGHT, 6)
        btns.Add(close)

        osz = wx.BoxSizer(wx.VERTICAL)
        osz.Add(self.split_top, 1, wx.EXPAND)
        osz.Add(btns, 0, wx.ALL | wx.EXPAND, 6)
        outer.SetSizer(osz)

        self.split_mid.SplitVertically(left, right)
        self.split_bot.SplitHorizontally(self.split_mid, bottom)
        self.split_top.SplitHorizontally(top, self.split_bot)

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.gauge.Pulse(), self.timer)

        self._initial_layout(parent, top, left, right, bottom, btns)
        self.CentreOnParent()

        if not py:
            example = ("python.exe, e.g. C:\\Python312\\python.exe" if sys.platform == "win32"
                       else "your Python, e.g. python3 or /usr/bin/python3")
            self._say("Couldn't find a Python with PyMuPDF + Pillow.\n"
                      "Run:  pip install pymupdf pillow   then reopen this dialog,\n"
                      f"or type the path to {example} above.")

    # -------------------------------------------------------------- layout
    def _initial_layout(self, parent, top, left, right, bottom, btns):
        """Saved size and divider positions if any, else natural sizes shrunk to fit the screen."""
        idx = wx.Display.GetFromWindow(parent) if parent else wx.NOT_FOUND
        area = wx.Display(idx if idx != wx.NOT_FOUND else 0).GetClientArea()
        max_w, max_h = area.width - 40, area.height - 60
        sash = self.split_top.GetSashSize()
        chrome = self.GetSize() - self.GetClientSize()  # title bar + borders

        h_top = top.GetBestSize().height
        # The options panel scrolls, so its "best size" is tiny; measure the grid inside it
        need = self.opt_scroll.GetSizer().GetMinSize()
        box = right.GetBestSize() - self.opt_scroll.GetBestSize()  # static box + margins
        scrollbar = wx.SystemSettings.GetMetric(wx.SYS_VSCROLL_X)
        w_right = need.width + box.width + scrollbar
        h_mid = max(left.GetBestSize().height, need.height + box.height)
        h_bot = bottom.GetBestSize().height
        h_btn = btns.GetMinSize().height + 12
        w_left = left.GetBestSize().width
        nat_w = max(w_left + w_right + sash, top.GetBestSize().width) + chrome.width
        nat_h = h_top + h_mid + h_bot + 2 * sash + h_btn + chrome.height

        ui = self.cfg.get("ui") or {}
        try:
            w, h = (int(v) for v in ui["size"])
        except Exception:  # noqa: BLE001
            w, h = nat_w, nat_h
        w, h = max(min(w, max_w), 420), max(min(h, max_h), 360)

        # Divider fractions: saved, or natural sizes. If it doesn't fit, shrink the log
        # first, then the sessions list, and the options (the main part) last.
        avail = h - h_btn - chrome.height - 2 * sash
        over = max(0, h_top + h_mid + h_bot - avail)
        cut_bot = min(over, max(0, h_bot - 175))
        cut_top = min(over - cut_bot, max(0, h_top - 145))
        cut_mid = over - cut_bot - cut_top
        t, m, b = h_top - cut_top, h_mid - cut_mid, h_bot - cut_bot
        f_top = ui.get("f_top", t / max(1, t + m + b + sash))
        f_bot = ui.get("f_bot", m / max(1, m + b))
        f_mid = ui.get("f_mid", w_left / max(1, w_left + w_right))
        self._fracs = {self.split_top: f_top, self.split_bot: f_bot, self.split_mid: f_mid}

        # Measured; now let everything shrink below its natural size (lists scroll instead)
        self.split_top.SetMinSize(wx.Size(200, 200))
        self.sessions.SetMinSize(wx.Size(-1, 30))
        self.layers.SetMinSize(wx.Size(100, 40))
        self.log.SetMinSize(wx.Size(-1, 30))
        self.SetMinSize(wx.Size(min(w, 420), min(h, 360)))
        self.SetSize(wx.Size(w, h))
        self.Layout()
        self.outer.Layout()
        self._apply_fracs()
        wx.CallAfter(self._apply_fracs)  # again once the window manager has settled the size

    def _apply_fracs(self):
        if not self:
            return
        for s in (self.split_top, self.split_bot, self.split_mid):  # outer first
            f = min(max(self._fracs[s], 0.05), 0.95)
            total = s.GetSize().height if s.GetSplitMode() == wx.SPLIT_HORIZONTAL else s.GetSize().width
            s.SetSashGravity(f)  # resizing the window keeps this proportion
            s.SetSashPosition(int(total * f))
            s.UpdateSize()
        self.opt_scroll.FitInside()

    def _sash_moved(self, e):
        s = e.GetEventObject()
        total = s.GetSize().height if s.GetSplitMode() == wx.SPLIT_HORIZONTAL else s.GetSize().width
        if total > 0:
            self._fracs[s] = e.GetSashPosition() / total
            s.SetSashGravity(min(max(self._fracs[s], 0.0), 1.0))
        e.Skip()

    def _ui_state(self):
        sz = self.GetSize()
        return {"size": [sz.width, sz.height],
                "f_top": self._fracs[self.split_top], "f_bot": self._fracs[self.split_bot],
                "f_mid": self._fracs[self.split_mid]}

    # ------------------------------------------------------------- session
    def _sessions(self):
        dirs = [d for d in glob.glob(os.path.join(self.sp_dir, "*"))
                if os.path.exists(os.path.join(d, "log.csv"))]
        return sorted(dirs, reverse=True)

    @staticmethod
    def _describe(d):
        """'2026-10-20 15:42   12.3 min, 840 frames' from the folder name and log."""
        name = os.path.basename(d)
        label = name
        m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}(.*)", name)
        if m:  # 20261020-154200_2 -> 2026-10-20 15:42 (2)
            label = f"{m[1]}-{m[2]}-{m[3]} {m[4]}:{m[5]}" + (f" ({m[6][1:]})" if m[6] else "")
        try:
            n, t = 0, 0.0
            with open(os.path.join(d, "log.csv"), newline="") as f:
                next(f)
                for line in f:
                    parts = line.split(",")
                    if len(parts) > 2 and parts[2] == "view":
                        n += 1
                    t = max(t, float(parts[0]))
            label += f"    {t / 60:.1f} min, {n} frames"
        except Exception:  # noqa: BLE001
            pass
        return label

    def _add_session(self, d, checked=False):
        d = os.path.abspath(d)
        if d in self.session_paths:
            i = self.session_paths.index(d)
        else:
            self.session_paths.append(d)
            self.sessions.Append(self._describe(d))
            i = len(self.session_paths) - 1
        if checked:
            self.sessions.Check(i, True)

    def _check_all(self, on):
        for i in range(self.sessions.GetCount()):
            self.sessions.Check(i, on)

    def _browse(self, _e):
        dlg = wx.DirDialog(self, "Choose a recording folder",
                           defaultPath=self.sp_dir if os.path.isdir(self.sp_dir) else "")
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            if os.path.exists(os.path.join(path, "log.csv")):
                self._add_session(path, checked=True)
            else:  # maybe they picked the pcb_timelapse folder itself
                found = [d for d in sorted(glob.glob(os.path.join(path, "*")), reverse=True)
                         if os.path.exists(os.path.join(d, "log.csv"))]
                for d in found:
                    self._add_session(d)
                if not found:
                    wx.MessageBox("No sessions found there (looking for log.csv).", "PCB Timelapse")
        dlg.Destroy()

    # -------------------------------------------------------------- layers
    def _move(self, d):
        i = self.layers.GetSelection()
        j = i + d
        if i < 0 or not (0 <= j < self.layers.GetCount()):
            return
        items = list(self.layers.GetItems())
        checks = [self.layers.IsChecked(k) for k in range(len(items))]
        items[i], items[j] = items[j], items[i]
        checks[i], checks[j] = checks[j], checks[i]
        self.layers.Set(items)
        for k, c in enumerate(checks):
            self.layers.Check(k, c)
        self.layers.SetSelection(j)

    def _reset_layers(self, _e):
        names = list(reversed(_board_layers(self.board)))
        self.layers.Set(names)
        for i, n in enumerate(names):
            self.layers.Check(i, n in DEFAULT_ON)

    # ----------------------------------------------------------------- run
    def _collect(self):
        items = list(self.layers.GetItems())
        layers_cfg = [[n, self.layers.IsChecked(i)] for i, n in enumerate(items)]
        cfg = {
            "layers": layers_cfg,
            "streams": self.streams.GetSelection(),
            "view_ui": "full" if self.view_ui.GetSelection() == 1 else "canvas",
            "follow": self.follow.GetValue(),
            "inactive": self.inactive.GetValue(),
            "bg": self.bg.GetColour().GetAsString(wx.C2S_HTML_SYNTAX),
            "holes": HOLE_STYLES[max(0, self.holes.GetSelection())][1],
            "vias": HOLE_STYLES[max(0, self.vias.GetSelection())][1],
            "via_layer": self.via_layer.GetValue(),
            "crop": self.crop.GetSelection(),
            "size": self.size.GetValue(),
            "fps": self.fps.GetValue(),
            "hold": self.hold.GetValue(),
            "sync": self.sync.GetValue(),
            "combined": LAYOUTS[max(0, self.combined.GetSelection())][1],
            "python": self.python.GetValue().strip(),
            "cli": self.cli.GetValue().strip(),
            "separate": self.separate.GetValue(),
            "fill": self.fill.GetValue(),
            "outline": self.outline.GetValue(),
            "glow": self.glow.GetValue(),
            "sat": self.sat.GetValue(),
            "ui": self._ui_state(),
        }
        return cfg

    def _run(self, _e):
        if self.proc is not None:  # acting as Cancel
            _kill_tree(self.proc)
            return
        chosen = [self.session_paths[i] for i in self.sessions.GetCheckedItems()]
        if not chosen:
            wx.MessageBox("Tick at least one session.", "PCB Timelapse")
            return
        c = self._collect()
        if not c["python"]:
            wx.MessageBox("Set the Python path first.", "PCB Timelapse")
            return
        _save_settings(c)

        # list is top-first; render.py wants bottom-first
        stack = [n for n, on in reversed(c["layers"]) if on]
        if not stack and c["streams"] != 1:
            wx.MessageBox("Tick at least one layer.", "PCB Timelapse")
            return

        py = c["python"]
        args = [py] if os.path.exists(py) else py.split()   # path, or e.g. "py -3"
        args += ["-u", RENDER_PY, *chosen,
                 "--fps", str(c["fps"]), "--hold", str(c["hold"]),
                 "--board-size", c["size"], "--bg", c["bg"], "--holes", c["holes"], "--vias", c["vias"],
                 "--via-colour", "layer" if c["via_layer"] else "plot",
                 "--crop", "edge" if c["crop"] == 0 else "all",
                 "--layers", ",".join(stack), "--view-ui", c["view_ui"],
                 "--inactive-opacity", f"{c['inactive'] / 100:.2f}",
                 "--board-fill", "auto" if c["fill"] else "none",
                 "--outline", str(c["outline"]),
                 "--glow", f"{c['glow'] / 100:.2f}",
                 "--saturation", f"{c['sat'] / 100:.2f}"]
        if c["streams"] == 1:
            args += ["--only", "view"]
        elif c["streams"] == 2:
            args += ["--only", "board"]
        if not c["follow"]:
            args.append("--no-follow-active")
        if not c["sync"]:
            args.append("--no-sync")
        if c["combined"] != "off" and c["streams"] == 0:
            args += ["--combined", "--layout", c["combined"]]
        if c["cli"]:
            args += ["--kicad-cli", c["cli"]]
        if c["separate"] and len(chosen) > 1:
            args.append("--separate")

        # where the results land (matches render.py's naming)
        ordered = sorted(chosen)
        renders = os.path.join(os.path.dirname(ordered[0]), "renders")
        if len(ordered) == 1:
            self.out_dir = os.path.join(renders, os.path.basename(ordered[0]))
        elif c["separate"]:
            self.out_dir = renders  # one subfolder per recording
        else:
            names = [os.path.basename(x) for x in ordered]
            self.out_dir = os.path.join(renders, f"joined_{names[0]}_to_{names[-1]}")
        self.log.Clear()
        self._say("Rendering... (KiCad stays usable meanwhile)")
        self.run_btn.SetLabel("Cancel")
        self.open_btn.Disable()
        self.timer.Start(80)
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, creationflags=NO_WINDOW, env=_clean_env())
        except Exception as e:  # noqa: BLE001
            self._finish(f"Couldn't start Python: {e}", ok=False)
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        proc = self.proc
        for line in proc.stdout:
            wx.CallAfter(self._say, line.rstrip())
        rc = proc.wait()
        wx.CallAfter(self._finish, "Done." if rc == 0 else f"Stopped (exit code {rc}).", rc == 0)

    def _say(self, text):
        if self:  # dialog may have been closed
            self.log.AppendText(text + "\n")

    def _finish(self, msg, ok):
        if not self:
            return
        self.proc = None
        self.timer.Stop()
        self.gauge.SetValue(100 if ok else 0)
        self.run_btn.SetLabel("Render")
        self._say(msg)
        if self.out_dir and os.path.isdir(self.out_dir):
            self.open_btn.Enable()

    def _open_out(self, _e):
        if self.out_dir and os.path.isdir(self.out_dir):
            if sys.platform == "win32":
                os.startfile(self.out_dir)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", self.out_dir])

    def _on_close(self, _e):
        if self.proc is not None:
            if wx.MessageBox("A render is running. Stop it?", "PCB Timelapse",
                             wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
                return
            _kill_tree(self.proc)
        _save_settings(self._collect())
        self.timer.Stop()
        self.Destroy()


RENDER_NAME = "PCB Timelapse: render videos"


class TimelapseRenderAction(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = RENDER_NAME
        self.category = "Recording"
        self.description = "Render a PCB Timelapse recording to video"
        self.show_toolbar_button = True
        icon = os.path.join(HERE, "icon_render.png")
        self.icon_file_name = icon if os.path.exists(icon) else ""

    def Run(self):
        t = (getattr(sys, "_pcb_timelapse_state", None) or {}).get("intercepted", {}).get(RENDER_NAME, 0)
        if time.monotonic() - t < 1.5:
            return  # already handled by our click hook
        open_render_dialog()


def open_render_dialog():
    board = pcbnew.GetBoard()
    if board is None or not board.GetFileName():
        wx.MessageBox("Open a saved board first.", "PCB Timelapse")
        return
    st = getattr(sys, "_pcb_timelapse_state", None)
    if st is None:
        st = {"rec": None, "auto": None, "render_dlg": None}
        sys._pcb_timelapse_state = st
    old = st.get("render_dlg")
    if old:  # already open: bring it forward
        old.Raise()
        return
    parent = next((w for w in wx.GetTopLevelWindows() if w.GetName() == "PcbFrame"), None)
    dlg = RenderDialog(parent, board)
    st["render_dlg"] = dlg
    dlg.Show()
