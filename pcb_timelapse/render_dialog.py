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
               ("Cyan ring", "cyan"), ("Gold ring", "gold")]
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
    """A Python that isn't KiCad's own and has pymupdf + pillow."""
    kicad_dir = os.path.normcase(os.path.dirname(sys.executable))
    cands = []
    if shutil.which("py"):
        cands.append(["py", "-3"])
    for n in ("python", "python3"):
        p = shutil.which(n)
        if p and os.path.normcase(os.path.dirname(p)) != kicad_dir and "WindowsApps" not in p:
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
    def __init__(self, parent, board):
        super().__init__(parent, title="PCB Timelapse - Render",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.proc = None
        self.cfg = _load_settings()
        from . import recorder as rec_mod
        self.sp_dir = rec_mod.sessions_dir(board.GetFileName())
        self.out_dir = None

        p = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)

        # sessions
        sbox = wx.StaticBoxSizer(wx.VERTICAL, p, "Sessions (tick one or more)")
        self.session_paths = []
        self.sessions = wx.CheckListBox(p, size=(-1, 110))
        for d in self._sessions():
            self._add_session(d)
        if self.session_paths:
            self.sessions.Check(0, True)  # newest
        srow = wx.BoxSizer(wx.HORIZONTAL)
        browse = wx.Button(p, label="Add folder...")
        browse.Bind(wx.EVT_BUTTON, self._browse)
        sel_all = wx.Button(p, label="All")
        sel_all.Bind(wx.EVT_BUTTON, lambda e: self._check_all(True))
        sel_none = wx.Button(p, label="None")
        sel_none.Bind(wx.EVT_BUTTON, lambda e: self._check_all(False))
        self.join = wx.RadioButton(p, label="Join into one video", style=wx.RB_GROUP)
        self.separate = wx.RadioButton(p, label="Render each separately")
        (self.separate if self.cfg.get("separate") else self.join).SetValue(True)
        for w in (browse, sel_all, sel_none):
            srow.Add(w, 0, wx.RIGHT, 4)
        srow.AddStretchSpacer()
        srow.Add(self.join, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        srow.Add(self.separate, 0, wx.ALIGN_CENTER_VERTICAL)
        sbox.Add(self.sessions, 1, wx.ALL | wx.EXPAND, 4)
        sbox.Add(srow, 0, wx.ALL | wx.EXPAND, 4)
        main.Add(sbox, 0, wx.ALL | wx.EXPAND, 6)

        row = wx.BoxSizer(wx.HORIZONTAL)

        # layers
        lbox = wx.StaticBoxSizer(wx.VERTICAL, p, "Board layers (top of list = drawn on top)")
        self.layers = wx.CheckListBox(p, size=(220, 260))
        saved = self.cfg.get("layers")  # [[name, checked], ...] top-first
        names = list(reversed(_board_layers(board)))
        if saved:
            known = [n for n, _ in saved if n in names]
            names = known + [n for n in names if n not in known]
            checked = {n for n, c in saved if c}
        else:
            checked = DEFAULT_ON
        self.layers.Set(names)
        for i, n in enumerate(names):
            self.layers.Check(i, n in checked)
        ud = wx.BoxSizer(wx.HORIZONTAL)
        for lbl, d in (("Up", -1), ("Down", 1)):
            b = wx.Button(p, label=lbl, size=(60, -1))
            b.Bind(wx.EVT_BUTTON, lambda e, d=d: self._move(d))
            ud.Add(b, 0, wx.RIGHT, 4)
        reset = wx.Button(p, label="Reset", size=(60, -1))
        reset.Bind(wx.EVT_BUTTON, self._reset_layers)
        ud.Add(reset)
        lbox.Add(self.layers, 1, wx.ALL | wx.EXPAND, 4)
        lbox.Add(ud, 0, wx.ALL, 4)
        row.Add(lbox, 1, wx.ALL | wx.EXPAND, 6)

        # options
        obox = wx.StaticBoxSizer(wx.VERTICAL, p, "Options")
        g = wx.FlexGridSizer(0, 2, 6, 8)
        g.AddGrowableCol(1)

        def add(label, ctrl):
            g.Add(wx.StaticText(p, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctrl, 1, wx.EXPAND)
            return ctrl

        c = self.cfg
        self.streams = add("Render", wx.Choice(p, choices=["Both", "View only", "Board only"]))
        self.streams.SetSelection(c.get("streams", 0))
        self.view_ui = add("View video shows", wx.Choice(p, choices=["Drawing area only",
                                                                     "Whole editor (with UI)"]))
        self.view_ui.SetSelection(1 if c.get("view_ui") == "full" else 0)
        self.follow = add("Follow active layer", wx.CheckBox(p))
        self.follow.SetValue(c.get("follow", True))
        self.inactive = add("Inactive layer opacity %", wx.SpinCtrl(p, min=5, max=100,
                                                                     initial=c.get("inactive", 100)))
        self.bg = add("Background", wx.ColourPickerCtrl(p, colour=wx.Colour(c.get("bg", "#001023"))))
        self.holes = add("Pad / mounting holes", wx.Choice(p, choices=[lbl for lbl, _ in HOLE_STYLES]))
        hv = c.get("holes", "kicad")
        self.holes.SetSelection(next((i for i, (_, v) in enumerate(HOLE_STYLES) if v == hv), 0))
        self.via_layer = add("Vias in layer colour", wx.CheckBox(p))
        self.via_layer.SetValue(c.get("via_layer", True))
        self.vias = add("Vias", wx.Choice(p, choices=[lbl for lbl, _ in HOLE_STYLES]))
        vv = c.get("vias", "kicad")
        self.vias.SetSelection(next((i for i, (_, v) in enumerate(HOLE_STYLES) if v == vv), 0))
        self.crop = add("Frame", wx.Choice(p, choices=["Board outline", "Everything"]))
        self.crop.SetSelection(c.get("crop", 0))
        self.fill = add("Shade board area", wx.CheckBox(p))
        self.fill.SetValue(c.get("fill", True))
        self.outline = add("Outline width (px)", wx.SpinCtrl(p, min=1, max=12, initial=c.get("outline", 3)))
        self.glow = add("Glow %", wx.SpinCtrl(p, min=0, max=100, initial=c.get("glow", 20)))
        self.sat = add("Colour boost %", wx.SpinCtrl(p, min=50, max=200, initial=c.get("sat", 120)))
        self.size = add("Board video size", wx.ComboBox(p, choices=SIZES, value=c.get("size", SIZES[0])))
        self.fps = add("FPS", wx.SpinCtrl(p, min=1, max=120, initial=c.get("fps", 30)))
        self.hold = add("Hold last frame (s)", wx.SpinCtrl(p, min=0, max=30, initial=c.get("hold", 3)))
        self.sync = add("Match view timing", wx.CheckBox(p))
        self.sync.SetValue(c.get("sync", True))
        self.combined = add("Combined video", wx.Choice(p, choices=[lbl for lbl, _ in LAYOUTS]))
        cv = c.get("combined", "auto")
        cv = {True: "auto", False: "off"}.get(cv, cv)  # older settings were a checkbox
        self.combined.SetSelection(next((i for i, (_, v) in enumerate(LAYOUTS) if v == cv), 1))
        obox.Add(g, 0, wx.ALL | wx.EXPAND, 6)
        row.Add(obox, 1, wx.ALL | wx.EXPAND, 6)
        main.Add(row, 1, wx.EXPAND)

        # tools
        tbox = wx.StaticBoxSizer(wx.VERTICAL, p, "Tools")
        tg = wx.FlexGridSizer(0, 2, 6, 8)
        tg.AddGrowableCol(1)
        py = c.get("python") or " ".join(_find_python() or [])
        tg.Add(wx.StaticText(p, label="Python"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.python = wx.TextCtrl(p, value=py)
        tg.Add(self.python, 1, wx.EXPAND)
        tg.Add(wx.StaticText(p, label="kicad-cli"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.cli = wx.TextCtrl(p, value=c.get("cli") or _find_kicad_cli())
        tg.Add(self.cli, 1, wx.EXPAND)
        tbox.Add(tg, 0, wx.ALL | wx.EXPAND, 4)
        main.Add(tbox, 0, wx.ALL | wx.EXPAND, 6)

        # log + buttons
        self.log = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 120))
        main.Add(self.log, 0, wx.ALL | wx.EXPAND, 6)
        self.gauge = wx.Gauge(p, range=100, size=(-1, 8))
        main.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 6)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.open_btn = wx.Button(p, label="Open output folder")
        self.open_btn.Bind(wx.EVT_BUTTON, self._open_out)
        self.open_btn.Disable()
        self.run_btn = wx.Button(p, label="Render")
        self.run_btn.Bind(wx.EVT_BUTTON, self._run)
        close = wx.Button(p, label="Close")
        close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btns.Add(self.open_btn)
        btns.AddStretchSpacer()
        btns.Add(self.run_btn, 0, wx.RIGHT, 6)
        btns.Add(close)
        main.Add(btns, 0, wx.ALL | wx.EXPAND, 6)

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self.gauge.Pulse(), self.timer)

        p.SetSizer(main)
        main.Fit(self)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()

        if not py:
            self._say("Couldn't find a Python with PyMuPDF + Pillow.\n"
                      "Run:  pip install pymupdf pillow   then reopen this dialog,\n"
                      "or type the path to python.exe above.")

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
