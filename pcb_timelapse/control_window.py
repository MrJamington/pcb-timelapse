"""
PCB Timelapse control window: start / pause / stop, recording options, and
managing this board's recordings. Opened by the record button.
"""

import os
import shutil
import subprocess
import sys

import pcbnew
import wx

from . import recorder as sp

AUTO_CHOICES = [("Off", "off"), ("Boards with auto-record ticked", "marked"), ("Every board I open", "always")]
CAPTURE_CHOICES = [("Whole editor (UI on/off chosen when rendering)", "window"),
                   ("Drawing area only (smaller files)", "canvas")]
SOURCE_CHOICES = [("Saves and autosaves (safe)", "safe"), ("Live board, every few s (can crash KiCad)", "live")]


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


class ControlWindow(wx.Frame):
    def __init__(self, parent, board):
        style = wx.DEFAULT_FRAME_STYLE & ~wx.MAXIMIZE_BOX | wx.FRAME_FLOAT_ON_PARENT
        super().__init__(parent, title="PCB Timelapse", style=style)
        self.board_path = board.GetFileName()
        self.paths = []

        p = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)

        # ---------------------------------------------------------- controls
        title = wx.StaticText(p, label=os.path.basename(self.board_path))
        title.SetFont(title.GetFont().Bold())
        main.Add(title, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.start_btn = wx.Button(p, label="●  Start")
        self.pause_btn = wx.Button(p, label="❚❚  Pause")
        self.stop_btn = wx.Button(p, label="■  Stop")
        self.start_btn.Bind(wx.EVT_BUTTON, self._start)
        self.pause_btn.Bind(wx.EVT_BUTTON, self._pause)
        self.stop_btn.Bind(wx.EVT_BUTTON, self._stop)
        for b in (self.start_btn, self.pause_btn, self.stop_btn):
            btns.Add(b, 1, wx.RIGHT, 6)
        main.Add(btns, 0, wx.ALL | wx.EXPAND, 10)

        self.status = wx.StaticText(p, label="")
        main.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        into = wx.BoxSizer(wx.HORIZONTAL)
        self.new_rb = wx.RadioButton(p, label="New recording", style=wx.RB_GROUP)
        self.cont_rb = wx.RadioButton(p, label="Continue:")
        self.cont_choice = wx.Choice(p)
        self.cont_choice.Bind(wx.EVT_CHOICE, lambda e: self.cont_rb.SetValue(True))
        into.Add(self.new_rb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        into.Add(self.cont_rb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        into.Add(self.cont_choice, 1, wx.ALIGN_CENTER_VERTICAL)
        main.Add(into, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 10)

        # ----------------------------------------------------------- options
        obox = wx.StaticBoxSizer(wx.VERTICAL, p, "Options")
        g = wx.FlexGridSizer(0, 2, 6, 8)
        g.AddGrowableCol(1)

        def add(label, ctrl):
            g.Add(wx.StaticText(p, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            g.Add(ctrl, 1, wx.EXPAND)
            return ctrl

        self.auto = add("Auto-start", wx.Choice(p, choices=[l for l, _ in AUTO_CHOICES]))
        self.auto.SetSelection(next((i for i, (_, v) in enumerate(AUTO_CHOICES)
                                     if v == sp.AUTO_START), 1))
        self.auto_this = add("", wx.CheckBox(p, label="Auto-record this board when it's opened"))
        self.auto_this.SetValue(os.path.exists(sp._marker_path(self.board_path)))
        self.capture = add("Capture", wx.Choice(p, choices=[l for l, _ in CAPTURE_CHOICES]))
        self.capture.SetSelection(next((i for i, (_, v) in enumerate(CAPTURE_CHOICES)
                                        if v == sp.CAPTURE_MODE), 0))
        self.interval = add("Screenshot every (s)",
                            wx.SpinCtrlDouble(p, min=0.25, max=30, inc=0.25,
                                              initial=float(sp.VIEW_INTERVAL_S)))
        self.source = add("Board snapshots from", wx.Choice(p, choices=[l for l, _ in SOURCE_CHOICES]))
        self.source.SetSelection(next((i for i, (_, v) in enumerate(SOURCE_CHOICES)
                                       if v == sp.BOARD_SOURCE), 0))
        for c in (self.auto, self.capture, self.source):
            c.Bind(wx.EVT_CHOICE, self._options_changed)
        self.auto_this.Bind(wx.EVT_CHECKBOX, self._options_changed)
        self.interval.Bind(wx.EVT_SPINCTRLDOUBLE, self._options_changed)
        obox.Add(g, 0, wx.ALL | wx.EXPAND, 6)
        self.opt_note = wx.StaticText(p, label="")
        obox.Add(self.opt_note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        main.Add(obox, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, 10)

        # ------------------------------------------------------- recordings
        rbox = wx.StaticBoxSizer(wx.VERTICAL, p, "Recordings")
        self.list = wx.CheckListBox(p, size=(-1, 150))
        rbox.Add(self.list, 1, wx.ALL | wx.EXPAND, 4)

        r1 = wx.BoxSizer(wx.HORIZONTAL)
        for label, fn in (("All", lambda e: self._check_all(True)),
                          ("None", lambda e: self._check_all(False))):
            b = wx.Button(p, label=label, style=wx.BU_EXACTFIT)
            b.Bind(wx.EVT_BUTTON, fn)
            r1.Add(b, 0, wx.RIGHT, 4)
        r1.AddSpacer(8)
        short = wx.Button(p, label="Tick ones under", style=wx.BU_EXACTFIT)
        short.Bind(wx.EVT_BUTTON, self._tick_short)
        self.short_n = wx.SpinCtrl(p, min=1, max=100000, initial=30, size=(80, -1))
        self.short_unit = wx.Choice(p, choices=["frames", "minutes"])
        self.short_unit.SetSelection(0)
        r1.Add(short, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        r1.Add(self.short_n, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        r1.Add(self.short_unit, 0, wx.ALIGN_CENTER_VERTICAL)
        rbox.Add(r1, 0, wx.ALL, 4)

        r2 = wx.BoxSizer(wx.HORIZONTAL)
        delete = wx.Button(p, label="Delete ticked")
        delete.Bind(wx.EVT_BUTTON, self._delete)
        caches = wx.Button(p, label="Clear render caches")
        caches.SetToolTip("Frees space; the next render re-exports the layers")
        caches.Bind(wx.EVT_BUTTON, self._clear_caches)
        folder = wx.Button(p, label="Open folder")
        folder.Bind(wx.EVT_BUTTON, self._open_folder)
        vids = wx.Button(p, label="Open renders")
        vids.Bind(wx.EVT_BUTTON, self._open_renders)
        render = wx.Button(p, label="Render...")
        render.Bind(wx.EVT_BUTTON, self._render)
        for b in (delete, caches, folder, vids):
            r2.Add(b, 0, wx.RIGHT, 4)
        r2.AddStretchSpacer()
        r2.Add(render)
        rbox.Add(r2, 0, wx.ALL | wx.EXPAND, 4)
        self.total = wx.StaticText(p, label="")
        rbox.Add(self.total, 0, wx.ALL, 4)
        main.Add(rbox, 1, wx.ALL | wx.EXPAND, 10)

        p.SetSizer(main)
        main.Fit(self)
        self.SetMinSize(self.GetSize())

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda e: self._refresh_status(), self.timer)
        self.timer.Start(1000)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._reload_sessions()
        self._refresh_status()
        self._options_changed(None)
        self.CentreOnParent()

    # ----------------------------------------------------------- recording
    def _rec(self):
        return sp._state()["rec"]

    def _refresh_status(self):
        if not self:
            return
        rec = self._rec()
        recording = rec is not None
        self.start_btn.Enable(not recording)
        self.stop_btn.Enable(recording)
        self.pause_btn.Enable(recording)
        self.new_rb.Enable(not recording)
        self.cont_rb.Enable(not recording and bool(self.paths))
        self.cont_choice.Enable(not recording and bool(self.paths))
        if not recording:
            self.pause_btn.SetLabel("❚❚  Pause")
            self.status.SetLabel("Not recording")
        else:
            self.pause_btn.SetLabel("▶  Resume" if rec.paused else "❚❚  Pause")
            state = "Paused" if rec.paused else "Recording"
            self.status.SetLabel(f"{state}: {os.path.basename(rec.root)} · "
                                 f"{rec.active_time / 60:.1f} min · {rec.n_view} frames · "
                                 f"{rec.n_board_saved} board snapshot{'s' if rec.n_board_saved != 1 else ''}"
                                 + ("" if rec.layer_seen or rec.n_view < 3
                                    else " · active layer not detected"))
        self.Layout()

    def _start(self, _e):
        resume = None
        if self.cont_rb.GetValue():
            i = self.cont_choice.GetSelection()
            if i < 0:
                self.status.SetLabel("Pick a recording to continue.")
                return
            resume = self.paths[i]
        err = sp.start_recording(resume=resume)
        if err:
            self.status.SetLabel(err)
            self.Layout()
            return
        self.Close()

    def _pause(self, _e):
        sp.toggle_pause()
        self._refresh_status()

    def _stop(self, _e):
        rec = sp.stop_recording()
        if rec is not None and not getattr(rec, "kept", True):
            wx.MessageBox("Nothing was captured, so that recording was discarded.", "PCB Timelapse")
        self.Close()

    # ------------------------------------------------------------- options
    def _options_changed(self, _e):
        sp.save_settings(
            AUTO_START=AUTO_CHOICES[self.auto.GetSelection()][1],
            CAPTURE_MODE=CAPTURE_CHOICES[self.capture.GetSelection()][1],
            VIEW_INTERVAL_S=float(self.interval.GetValue()),
            BOARD_SOURCE=SOURCE_CHOICES[self.source.GetSelection()][1],
        )
        sp._set_autostart(self.board_path, self.auto_this.GetValue())
        self.auto_this.Enable(sp.AUTO_START == "marked")
        notes = []
        if self._rec() is not None:
            notes.append("Capture changes apply from the next recording.")
        if sp.BOARD_SOURCE == "safe":
            notes.append("Board video updates on each save and autosave. Keep KiCad's\n"
                         "Project Backup format on 'Zip archive' and location on\n"
                         "'Project directory' (Preferences > Common) so autosaves are seen.")
        self.opt_note.SetLabel("\n".join(notes))
        self.Layout()
        self.Fit()

    # ---------------------------------------------------------- recordings
    def _reload_sessions(self):
        rec = self._rec()
        current = rec.root if rec else None
        self.paths = sp.list_sessions(self.board_path)
        self.infos = [sp.session_info(d, with_size=True) for d in self.paths]
        labels = []
        for d, info in zip(self.paths, self.infos):
            tag = "   ● recording" if d == current else ""
            if os.path.isdir(sp.renders_dir(os.path.dirname(d), os.path.basename(d))):
                tag += "   ▶ rendered"
            labels.append(f"{sp.session_label(d)}    {info['seconds'] / 60:.1f} min · "
                          f"{info['frames']} frame{'s' if info['frames'] != 1 else ''} · {_human(info['bytes'])}{tag}")
        self.list.Set(labels)
        self.cont_choice.Set([sp.session_label(d) for d in self.paths])
        if self.paths:
            self.cont_choice.SetSelection(0)
        else:
            self.new_rb.SetValue(True)
        total = sum(i["bytes"] for i in self.infos)
        self.total.SetLabel(f"{len(self.paths)} recordings · {_human(total)} on disk")
        self._refresh_status()

    def _check_all(self, on):
        for i in range(self.list.GetCount()):
            self.list.Check(i, on)

    def _tick_short(self, _e):
        n = self.short_n.GetValue()
        minutes = self.short_unit.GetSelection() == 1
        for i, info in enumerate(self.infos):
            value = info["seconds"] / 60 if minutes else info["frames"]
            self.list.Check(i, value < n)

    def _ticked(self):
        return [self.paths[i] for i in self.list.GetCheckedItems()]

    def _delete(self, _e):
        rec = self._rec()
        ticked = [d for d in self._ticked() if not (rec and d == rec.root)]
        if rec and rec.root in self._ticked():
            wx.MessageBox("The recording in progress can't be deleted; stop it first.", "PCB Timelapse")
        if not ticked:
            return
        size = sum(sp.session_info(d, with_size=True)["bytes"] for d in ticked)
        names = "\n".join(sp.session_label(d) for d in ticked[:12])
        more = f"\n...and {len(ticked) - 12} more" if len(ticked) > 12 else ""
        if wx.MessageBox(f"Permanently delete {len(ticked)} recording(s), {_human(size)}?\n\n"
                         f"{names}{more}\n\nTheir rendered videos are kept (in the renders folder).",
                         "PCB Timelapse", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return
        for d in ticked:
            shutil.rmtree(d, ignore_errors=True)
        self._reload_sessions()

    def _clear_caches(self, _e):
        targets = self._ticked() or self.paths
        freed = 0
        for d in targets:
            c = os.path.join(d, "cache")
            if os.path.isdir(c):
                freed += sp.session_info(c, with_size=True)["bytes"]
                shutil.rmtree(c, ignore_errors=True)
        root = sp.sessions_dir(self.board_path)  # joined renders keep caches too
        if not self._ticked() and os.path.isdir(root):
            for name in os.listdir(root):
                c = os.path.join(root, name, "cache")
                if name.startswith("joined_") and os.path.isdir(c):
                    freed += sp.session_info(c, with_size=True)["bytes"]
                    shutil.rmtree(c, ignore_errors=True)
        which = "ticked recordings" if self._ticked() else "all recordings"
        wx.MessageBox(f"Cleared render caches for {which}: {_human(freed)} freed.", "PCB Timelapse")
        self._reload_sessions()

    def _open_renders(self, _e):
        root = sp.sessions_dir(self.board_path)
        ticked = self._ticked()
        path = sp.renders_dir(root, os.path.basename(ticked[0])) if len(ticked) == 1 else \
            os.path.join(root, sp.RENDERS)
        if not os.path.isdir(path):
            path = os.path.join(root, sp.RENDERS)
        os.makedirs(path, exist_ok=True)
        self._open(path)

    def _open_folder(self, _e):
        ticked = self._ticked()
        self._open(ticked[0] if len(ticked) == 1 else sp.sessions_dir(self.board_path))

    @staticmethod
    def _open(path):
        if not os.path.isdir(path):
            return
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    def _render(self, _e):
        from .render_dialog import open_render_dialog
        open_render_dialog()

    def _on_close(self, _e):
        self.timer.Stop()
        sp._state()["control_win"] = None
        self.Destroy()


def open_control_window():
    st = sp._state()
    win = st.get("control_win")
    if win:
        win._reload_sessions()
        win.Raise()
        return
    board = pcbnew.GetBoard()
    if board is None or not board.GetFileName():
        wx.MessageBox("Save the board once before recording.", "PCB Timelapse")
        return
    parent = sp._find_pcb_frame()
    win = ControlWindow(parent, board)
    st["control_win"] = win
    win.Show()
