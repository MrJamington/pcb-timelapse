# How PCB Timelapse works

A plain-English tour of the code, for understanding and maintaining it. It explains what each part does and why it's built that way, and points to where to look in the source. Function names are given so you can search for them (Ctrl+F) in the files.

---

## 1. The big picture

There are two halves that never run at the same time:

```
 RECORDING (inside KiCad)                     RENDERING (outside KiCad)
 ─────────────────────────                    ─────────────────────────
 every second, while the editor is in front:  when you click Render:
   • screenshot the editor  ──► view/*.png       • read the log
   • log the active layer   ──► log.csv          • crop/scale screenshots ──► view.mp4
 whenever KiCad saves or autosaves:              • turn each board snapshot into
   • copy the board file    ──► board/*.gz         layer images, stack and colour them ──► board.mp4
                                                 • put the two side by side ──► combined.mp4
```

The recording side has to be **light and safe**, because it runs inside KiCad while you work. The rendering side can be **slow and heavy**, because it runs in a separate program afterwards.

### Files

| File | Lines | Runs where | Job |
|---|---|---|---|
| `__init__.py` | ~20 | KiCad | Entry point. Registers the three toolbar buttons and starts the background watcher |
| `recorder.py` | ~1000 | KiCad | Everything about recording: capture, board snapshots, pause, auto-start, button hooks, settings |
| `control_window.py` | ~350 | KiCad | The PCB Timelapse window (Start/Pause/Stop, options, recordings list) |
| `render_dialog.py` | ~560 | KiCad | The Render window. Collects your choices and launches `render.py` |
| `render.py` | ~950 | Separate Python | Turns recordings into videos. Can also be run from the command line |

---

## 2. Python in 5 minutes (just enough to read this code)

If you've used C or TypeScript, most of it will look familiar. The main differences:

- **Indentation is the block structure.** There are no `{}`; whatever is indented under `if`, `for` or `def` belongs to it.
- **`def name(args):`** defines a function. **`class Name:`** defines a class, and methods inside it take `self` first (like `this`).
- **No type declarations.** `x = 5`, `names = []` (a list), `info = {"frames": 0}` (a dictionary, like a JS object or C struct by name).
- **`import x`** pulls in a module. **`from . import recorder`** means "the `recorder.py` next to this file".
- **`try: … except Exception: …`** is try/catch. This code uses it a lot on purpose: a failure inside a KiCad plugin must never crash KiCad, so risky steps are wrapped and the error is just printed.
- **`f"…{value}…"`** is a template string, like JS backticks.
- **`with open(path) as f:`** opens a file and closes it automatically afterwards.
- **`threading.Thread(target=fn).start()`** runs `fn` in the background.

Three libraries do most of the work:
- **`pcbnew`** is KiCad's Python interface, available only inside KiCad. It's used to get the open board, its file name, its layers, and so on.
- **`wx`** (wxPython) is the GUI toolkit KiCad is built on. It provides windows, buttons, timers and screenshots.
- **PyMuPDF (`fitz`) and Pillow (`PIL`)** are used only by the renderer: PyMuPDF draws SVG files into images, and Pillow edits and combines images.

---

## 3. Startup: `__init__.py`

KiCad imports every folder in its plugins directory when the PCB Editor opens. Our `__init__.py`:

1. **Registers three "action plugins"**, which become the toolbar buttons: `TimelapseAction` (record window), `TimelapsePauseAction` and `TimelapseRenderAction`. Each is a small class with a `defaults()` method (name, icon) and a `Run()` method (what happens on click).
2. **Starts the background watcher** (`start_autostarter`), described in §6.

---

## 4. Recording: `recorder.py`

### 4.1 Shared state

Several parts need to know "is something recording right now?". That's stored in one dictionary returned by `_state()`, kept on Python's `sys` module. It survives KiCad's *Refresh Plugins*, which re-imports our files.

*Why not a normal global variable?* Re-importing would reset it, and a recording already in progress would be "forgotten" while still running. The earliest version stored it on `wx.GetApp()`, which in KiCad returns a fresh object each time. That caused the "start button keeps showing Start" bug.

### 4.2 The `Recorder` class: one recording session

Created by `start_recording()`. In `__init__` it:

- Works out the folder: `sessions_dir(board)` → `<board folder>/pcb_timelapse/<date-time>/`, with `view/` and `board/` subfolders. If *continuing* an earlier recording, it reuses that folder, carries on the clock from the last logged time, and continues the file numbering (`_next_index`).
- Opens `log.csv` (see §4.6) and writes `session.json` (board name, settings, start time).
- Takes a first board snapshot from the board file on disk.
- Starts a **`wx.Timer`** that calls `_on_tick` every `VIEW_INTERVAL_S` (1 s by default).

### 4.3 Every tick: `_on_tick`

This runs once a second on KiCad's own UI thread, so it must be quick. In order:

1. **Paused by you?** (`self.paused`) If so, show the paused icon and stop here.
2. **Is the PCB Editor actually in front?** `_is_foreground` asks Windows which window has focus (`GetForegroundWindow`). This catches alt-tab, minimised windows and the lock screen; while locked, Windows reports no window in front. If the editor isn't in front, the clock stops and the tick ends.
3. **Advance the "active time" clock.** If more than ~5 s passed since the last tick, the computer was asleep or KiCad froze, so that gap isn't counted.
4. **Make sure it's still the same board** (you might have opened a different one).
5. **Take a screenshot:** `_grab_view` (§4.4).
6. **Check for new board files:** `_check_files` (§4.5).

The toolbar icon blinks while ticks are capturing (`ToolbarIcon`, which swaps the button's image).

### 4.4 Screenshots: `_grab_view`

- Finds the **drawing canvas** (`_find_canvas`: the largest child area of the editor window) and the **active layer** (`ActiveLayerProbe`, below).
- Copies the editor window's pixels from the screen (`wx.ScreenDC` + `Blit`), the same as a screenshot.
- Records **where the canvas sits inside the screenshot** (x, y, width, height), so the renderer can later crop away the toolbars, or keep them.
- Skips the frame if it's **all black** (screen off or locked) or **identical to the last one** (an MD5 hash of the canvas area only). The status bar is ignored because its cursor coordinates change constantly.
- Saves `view/view_000123.png` and writes a line to the log.

**`ActiveLayerProbe`.** KiCad's Python interface has no "which layer is active?" function. Instead, the plugin finds the layer drop-down on KiCad's toolbar (a widget whose entries look like layer names) and reads its current selection. If that can't be found, it just records nothing, and the renderer falls back to a fixed layer order.

### 4.5 Board snapshots: `_check_files`, `_grab_file`, `_finish_board`

This is the most safety-critical part.

**The rule: the plugin never reads the board out of KiCad's memory** (in the default "safe" mode). Asking KiCad to serialise the board while you're halfway through dragging something can crash it. KiCad's own autosave waits for that reason, but a Python plugin can't tell when it's safe.

Instead, it watches two files that **KiCad itself writes when it is safe**:
- your board file `testboardv4.kicad_pcb`, which changes when you press save
- KiCad's autosave file `_autosave-testboardv4.kicad_pcb`, written about 5 s after you stop editing

Every tick, `_check_files` compares their modification times with what it last saw. When one changes, `_grab_file` starts a **background thread** that:

1. waits until the file stops changing, in case KiCad is still writing it
2. copies it and checks it ends like a complete board file
3. hands it to `_finish_board`, which hashes it (skipping exact duplicates) and saves it gzip-compressed as `board/board_000042.kicad_pcb.gz`, then logs it

The heavy work (copying, hashing, compressing) runs on a background thread so KiCad's UI never stutters.

*"Live" mode* (`BOARD_SOURCE = "live"`) is the older behaviour: it also asks KiCad for the board every few seconds via `_grab_board` / `_save_board_copy`. It's kept as an option, but it carries the crash risk described above.

### 4.6 The log: `log.csv`

Each recording keeps a simple spreadsheet-style log, one row per captured thing:

```
t_active, t_wall,        stream, file,                       active_layer, canvas
12.034,   1792472907.3,  view,   view_000010.png,            B.Cu,         120 0 1800 950
15.000,   1792472910.1,  board,  board_000003.kicad_pcb.gz,  ,
```

- `t_active`: seconds of *active editing* so far (pauses excluded). The renderer uses this to line up the two videos.
- `t_wall`: real clock time.
- `stream`: `view` (screenshot) or `board` (snapshot).
- `active_layer` and `canvas`: from §4.4.

The file is flushed after every row, so if KiCad or the PC crashes, everything up to that moment is safe.

### 4.7 Stopping: `stop`

Stops the timer, takes one last snapshot if a board file changed, waits for background threads to finish, and closes the log. If a *new* recording captured nothing at all, its folder is deleted.

### 4.8 Pause: `toggle_pause`

Flips `rec.paused` and swaps the icons. `_on_tick` checks the flag at the top.

---

## 5. The PCB Timelapse window: `control_window.py`

A normal wx window (`ControlWindow`), opened by the record button via `open_control_window()`. Only one can be open at a time; clicking again brings it to the front.

- **Buttons** call `start_recording()`, `toggle_pause()` and `stop_recording()` from `recorder.py`. `_refresh_status` runs every second to grey out the right buttons and update the status line.
- **Options** are saved immediately by `save_settings()` into `recorder_settings.json`, in KiCad's user settings folder (`settings_dir()`), so updating the plugin doesn't wipe them. They're loaded when the plugin starts (`load_settings()`).
- **Recordings list:** `list_sessions()` finds folders containing a `log.csv`, and `session_info()` reads each log to count frames and minutes and adds up file sizes. *Delete* removes the recording folder; videos are in a separate `renders/` folder, so they survive.

---

## 6. The background watcher: `AutoStarter`

Started once when KiCad loads the plugin, it runs every 2 seconds. It does two jobs.

### 6.1 Auto-start

If the open board is marked for auto-record (a `.autostart` file in its `pcb_timelapse` folder), or auto-start is set to "every board", it starts a recording. When the board closes or changes, it stops it.

### 6.2 Button hooks (why clicks don't mark the board as modified)

When KiCad runs *any* old-style Python plugin, it saves an undo copy of the whole board before and after, then marks the board as modified (`*`), even if the plugin changed nothing.

To avoid that, the watcher finds the plugin's own toolbar buttons and menu entries (`_ids_for`, by their tooltip names) and attaches its own click handler (`_hook`). That handler runs first and doesn't pass the click on, so KiCad's plugin machinery never runs, and there's no `*` and no pointless undo step. The `Run()` methods still exist as a fallback; `_recently_intercepted` makes sure a click never runs twice.

---

## 7. The Render window: `render_dialog.py`

`RenderDialog` collects your choices (recordings, layers and their order, look options) and then **launches `render.py` as a separate program** (`_run`).

*Why separate?* Rendering needs PyMuPDF and Pillow, which aren't part of KiCad's bundled Python, and it's slow, so KiCad must stay usable meanwhile. So:

- **`_find_python`** looks for your normal Python that has those packages installed.
- **`_clean_env`** removes KiCad's own Python settings from the environment before launching. Otherwise your Python would try to load KiCad's library files and fail.
- **`_pump`** reads the program's output on a background thread and shows it in the log box.
- **Cancel** uses `_kill_tree`, which also stops the programs `render.py` itself started (kicad-cli, ffmpeg).

Settings are remembered in `render_settings.json`.

---

## 8. Rendering: `render.py`

It can run on its own: `python render.py <recording folder> [options]`. The main flow is `main()` → `render()`.

### 8.1 Read and tidy the logs

- `read_log` / `read_sessions` load one or more `log.csv` files. When joining several recordings, each one's times are shifted to continue after the previous.
- `drop_broken_views` skips missing or half-written screenshots, which can happen after a crash.

### 8.2 The view video

For each screenshot (`normalise_view`): crop to the canvas (unless "whole editor" is chosen), scale to a common size, and save into a cache folder. ffmpeg then stitches them into `view.mp4` (`encode_sequence`).

### 8.3 The board video: the clever part

KiCad can't "screenshot" a board that isn't open, so each snapshot is re-drawn from scratch:

1. **Export layers with KiCad's own tool.** `Exporter` runs `kicad-cli pcb export svg` on each snapshot, producing one SVG (vector image) per layer: `snap-F_Cu.svg`, `snap-B_Cu.svg`, and so on. These are **cached**, which is why only the first render is slow. The board copy kept in the cache is named `snap.pcbtxt`, not `.kicad_pcb`, because KiCad's backup feature zips every `.kicad_pcb` in the project folder and hundreds of cached copies made saving slow.
2. **Decide the layer order for this moment.** `default_stack` gives the base order (bottom to top), or uses your order from the dialog. `arrange` then raises the copper layer you were last working on (from the log), together with its mask and silk, so the video "follows" you like the editor does. A non-copper active layer (e.g. silkscreen) is raised on its own, without reshuffling the copper.
3. **Draw each layer into an image** (`_rasterise`, using PyMuPDF) and stack them (`render_board_frame`, using Pillow). Along the way:
   - `_expand_canvas` enlarges the drawing area, so boards placed outside the page aren't cut off
   - `_recolour_vias` paints vias in each copper layer's colour, like the editor
   - `board_fill_mask` shades the inside of the board outline: the outline is drawn, then everything *outside* it is flood-filled, and what's left is the board
   - glow (copper only) and the colour boost are applied
   - **holes:** KiCad plots plated holes white and non-plated holes black. `holes_only_svg` / `holes_mask` pick those shapes out, then they're painted in the chosen style (KiCad gold with a light wall, rings, etc.). Via holes are told apart from pad holes by matching positions against the vias listed in the snapshot (`board_vias`)
4. **Timing.** By default each board frame is shown for as long as the matching stretch of the view video, so the two stay in sync. `encode_concat` gives ffmpeg a list of images with durations.

Finished frames are cached too. The cache key includes all the look settings plus `RENDER_VERSION`, which is bumped whenever the drawing code changes, so old cached frames aren't reused by mistake.

### 8.4 The combined video

`video_size` reads both videos' sizes. "Auto" layout puts them side by side or stacked, whichever gives the less extreme overall shape: a tall board goes beside, a wide board goes below. ffmpeg does the stacking (`hstack` / `vstack`).

### 8.5 Where files go

```
<board folder>/pcb_timelapse/
    20261024-153000/            a recording
        view/  board/  log.csv  session.json
        cache/                  render working files (safe to delete)
    joined_A_to_B/cache/        working files for joined renders
    renders/
        20261024-153000/        view.mp4, board.mp4, combined.mp4
```

---

## 9. Design decisions worth knowing

| Decision | Why |
|---|---|
| Never touch the live board (safe mode) | A snapshot during a drag can crash KiCad; KiCad's autosave already waits for a safe moment |
| Never write to your `.kicad_pcb` | Everything is written inside `pcb_timelapse/`, plus settings in KiCad's settings folder |
| No `.kicad_pcb` files in the cache | KiCad's backups zip every one, which slowed saves badly |
| Rendering in a separate program | Needs packages KiCad doesn't have, and must not freeze KiCad |
| Everything risky wrapped in `try/except` | A plugin error should print a message, never take KiCad down |
| Log flushed every row; broken frames skipped | A crash or power cut loses at most the last second |
| Our own click handlers | Stops KiCad marking the board modified on every button press |
| Settings in KiCad's settings folder | Survive plugin updates |

---

## 10. Questions someone might ask, and where to find the answer

| Question | Where to look |
|---|---|
| Does it modify my board? | No. Search the `.py` files for `open(`: everything written goes into `pcb_timelapse/` next to the board, apart from the two settings files in KiCad's settings folder |
| Does it send anything over the internet? | No. There's no networking code anywhere. The only programs it launches are `kicad-cli`, `ffmpeg`, `ffprobe` and `render.py` itself, plus Explorer when you click *Open folder* |
| What runs in the background inside KiCad? | Two `wx.Timer`s (the recorder's tick and the 2 s watcher) and short-lived file-copy threads |
| Can it crash KiCad? | In safe mode it only takes screenshots, reads one drop-down and copies files KiCad already wrote. Live mode is the risky one (§4.5) |
| What happens if KiCad crashes mid-recording? | The log is flushed every row; at most the last frame is lost, and the renderer skips damaged files (§8.1) |
| Why does rendering need a separate Python? | §7 |
| Which KiCad versions? | Uses the old SWIG Python API (`import pcbnew`, `ActionPlugin`): KiCad 7–10. KiCad 11 is expected to remove it; a port would use KiCad's new "IPC API" |

---

## 11. Good first places to experiment

- **Change the screenshot interval or defaults:** the constants at the top of `recorder.py` (`VIEW_INTERVAL_S`, `CAPTURE_MODE`, …).
- **Change a colour:** in `render.py`, `HOLE_RINGS` and the `KICAD_*` colours near the hole code.
- **Add a render option:** follow how `--glow` is done. It's defined in `main()` (`ap.add_argument`), stored in the `style` dictionary, used in `render_board_frame`, and given a control in `render_dialog.py` (`self.glow`) that's passed on the command line in `_run`.
- **See what a recording contains:** open a recording's `log.csv` in Excel, and unzip a `board_*.gz` to get a normal `.kicad_pcb` you can open in KiCad.
