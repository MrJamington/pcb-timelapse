#!/usr/bin/env python3
"""
Render PCB Timelapse recordings into videos.

  python render.py SESSION_DIR [SESSION_DIR ...] [options]

Several sessions are joined into one continuous video (oldest first), written to
renders/joined_<first>_to_<last>/. Use --separate to render each
on its own instead.

Outputs (in renders/<recording>/ next to the recordings, unless --out):
  view.mp4      zoomed editor view
  board.mp4     whole board, re-rendered from snapshots (layers chosen here)
  combined.mp4  both together, side by side or board below (with --combined)

Needs: kicad-cli, ffmpeg, and `pip install pymupdf pillow`.
Layer SVGs are cached per snapshot, so re-running with different layers /
hidden layers / colours is quick.
"""

import argparse
import concurrent.futures as cf
import csv
import gzip
import hashlib
import io
import math
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter

try:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # older PyMuPDF
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
except ImportError:
    sys.exit("Missing packages: pip install pymupdf pillow")


# ------------------------------------------------------------------ helpers
def find_kicad_cli(user_path):
    if user_path:
        return user_path
    found = shutil.which("kicad-cli")
    if found:
        return found
    for base in (r"C:\Program Files\KiCad", r"C:\Program Files (x86)\KiCad"):
        if os.path.isdir(base):
            for ver in sorted(os.listdir(base), reverse=True):
                p = os.path.join(base, ver, "bin", "kicad-cli.exe")
                if os.path.exists(p):
                    return p
    mac = "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
    if os.path.exists(mac):
        return mac
    sys.exit("kicad-cli not found; pass --kicad-cli PATH")


def read_log(session):
    rows = []
    with open(os.path.join(session, "log.csv"), newline="") as f:
        for r in csv.DictReader(f):
            r["t_active"] = float(r["t_active"])
            r["active_layer"] = (r.get("active_layer") or "").strip()
            canvas = r.get("canvas")
            if canvas is None and r.get(None):   # continued session with an older header
                canvas = r[None][0]
            try:
                x, y, w, h = (int(v) for v in (canvas or "").split())
                r["canvas"] = (x, y, w, h) if w > 0 and h > 0 else None
            except ValueError:
                r["canvas"] = None              # older recordings: already canvas-only
            rows.append(r)
    rows.sort(key=lambda r: r["t_active"])
    for r in rows:
        r["path"] = os.path.join(session, "view" if r["stream"] == "view" else "board", r["file"])
    views = [r for r in rows if r["stream"] == "view"]
    boards = [r for r in rows if r["stream"] == "board"]
    return views, boards


def read_sessions(sessions):
    """Chain several sessions into one timeline, oldest first."""
    views, boards, offset = [], [], 0.0
    for s in sessions:
        v, b = read_log(s)
        for r in v + b:
            r["t_active"] += offset
        views += v
        boards += b
        end = max((r["t_active"] for r in v + b), default=offset)
        offset = end + 1.0
    return views, boards


def drop_broken_views(views):
    """
    A session that ended abruptly (crash, power cut) can leave its last few
    screenshots missing or half-written. Skip those instead of failing.
    """
    tail = {}
    for i, r in enumerate(views):
        tail.setdefault(os.path.dirname(r["path"]), []).append(i)
    suspect = {i for idx in tail.values() for i in idx[-5:]}   # only the end can be cut off
    good = []
    for i, r in enumerate(views):
        if not os.path.exists(r["path"]):
            print(f"  skipping missing frame {r['file']}")
            continue
        if i in suspect:
            try:
                with Image.open(r["path"]) as im:
                    im.load()
            except Exception:  # noqa: BLE001
                print(f"  skipping damaged frame {r['file']}")
                continue
        good.append(r)
    return good


RENDERS = "renders"   # same as recorder.RENDERS


class RenderError(Exception):
    pass


def parse_size(s):
    w, h = s.lower().split("x")
    return int(w) // 2 * 2, int(h) // 2 * 2


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


LAYER_RE = re.compile(r'\(\s*(\d+)\s+"([^"]+)"\s+(\w+)(?:\s+"([^"]*)")?\s*\)')
COPPER_TYPES = {"signal", "power", "mixed", "jumper"}


def board_layers(pcb_text):
    """alias -> (display name, is_copper) for every enabled layer in the board."""
    start = pcb_text.find("(layers")
    depth, end = 0, start
    for i in range(start, len(pcb_text)):
        c = pcb_text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    out = {}
    for _num, canon, ltype, user in LAYER_RE.findall(pcb_text[start:end + 1]):
        disp = user or canon
        info = (disp, ltype in COPPER_TYPES)
        out[canon] = info
        out[disp] = info
    return out


def default_stack(layers):
    """Bottom -> top drawing order."""
    copper = sorted({d for d, cu in layers.values() if cu and d not in ("F.Cu", "B.Cu")},
                    key=lambda n: int(re.sub(r"\D", "", n) or 0), reverse=True)
    stack = ["B.Silkscreen", "B.Cu", *copper, "F.Cu", "F.Silkscreen", "Edge.Cuts"]
    return [l for l in stack if l in layers]


def arrange(stack, layers, active, companions=True):
    """
    Raise the active layer (matched by display name) to the top. Layers above it
    on the same side (e.g. F.Mask, F.Silkscreen over F.Cu) come along, so mask
    tint and silk don't flicker on and off as you switch layers. The board
    outline always stays on top. Returns (order, top layer, raised group).
    """
    top = next((l for l in stack if l in layers and layers[l][0] == active), None) if active else None
    if top is None:
        return list(stack), None, set()
    i = stack.index(top)
    side = layers[top][0][:2] if layers[top][0][:2] in ("F.", "B.") else None
    group = [top] + ([l for l in stack[i + 1:] if l in layers and layers[l][0].startswith(side)]
                     if side and companions else [])
    edge = [l for l in stack if l in layers and layers[l][0] == "Edge.Cuts" and l not in group]
    rest = [l for l in stack if l not in group and l not in edge]
    return rest + group + edge, top, set(group)


def file_suffix(name):
    bad = '\\/:*?"<>|%.'
    return "".join("_" if c in bad else c for c in name.strip())


# --------------------------------------------------------- board: SVG export
SNAP_NAME = "snap.pcbtxt"


def clean_old_caches(session):
    """Rename leftover cache .kicad_pcb copies from older versions (see snapshot_dir)."""
    n = 0
    for root, _dirs, files in os.walk(os.path.join(session, "cache")):
        if "snap.kicad_pcb" in files:
            old, new = os.path.join(root, "snap.kicad_pcb"), os.path.join(root, SNAP_NAME)
            try:
                if os.path.exists(new):
                    os.remove(old)
                else:
                    os.replace(old, new)
                n += 1
            except OSError:
                pass
    return n
class Exporter:
    def __init__(self, cli, cache_name, theme, drill_opt):
        self.cli, self.cache_name, self.theme, self.drill_opt = cli, cache_name, theme, drill_opt
        self.multi = True  # flipped off if this kicad-cli has no --mode-multi

    def base_args(self):
        a = [self.cli, "pcb", "export", "svg", "--exclude-drawing-sheet",
             "--drill-shape-opt", str(self.drill_opt)]
        if self.theme:
            a += ["--theme", self.theme]
        return a

    def snapshot_dir(self, snap_gz):
        name = os.path.basename(snap_gz).split(".")[0]
        session_root = os.path.dirname(os.path.dirname(os.path.abspath(snap_gz)))
        d = os.path.join(session_root, "cache", self.cache_name, name)
        os.makedirs(d, exist_ok=True)
        # Kept as .pcbtxt, not .kicad_pcb: KiCad's backup-on-save zips every
        # .kicad_pcb under the project folder, and hundreds of cached copies made
        # every save hitch. A .kicad_pcb copy only exists briefly during export.
        pcb = os.path.join(d, SNAP_NAME)
        old = os.path.join(d, "snap.kicad_pcb")       # caches from older versions
        if os.path.exists(old):
            if os.path.exists(pcb):
                os.remove(old)
            else:
                os.replace(old, pcb)
        if not os.path.exists(pcb):
            with gzip.open(snap_gz, "rb") as g, open(pcb + ".part", "wb") as f:
                f.write(g.read())
            os.replace(pcb + ".part", pcb)
        return d, pcb

    def svg_path(self, d, disp):
        return os.path.join(d, f"snap-{file_suffix(disp)}.svg")

    def export(self, snap_gz, wanted):
        """Make sure an SVG exists for every wanted layer. Returns (dir, layer map)."""
        d, pcb = self.snapshot_dir(snap_gz)
        with open(pcb, encoding="utf-8") as f:
            layers = board_layers(f.read())
        todo = [l for l in wanted if l in layers
                and not os.path.exists(self.svg_path(d, layers[l][0]))]
        if not todo:
            return d, layers

        tmp_pcb = os.path.join(d, "snap.kicad_pcb")  # kicad-cli wants the real extension
        shutil.copyfile(pcb, tmp_pcb)
        try:
            if self.multi:
                r = subprocess.run(self.base_args() + ["--mode-multi", "--layers", ",".join(todo),
                                                       "-o", d, tmp_pcb],
                                   capture_output=True, text=True)
                if r.returncode != 0 and "mode-multi" in (r.stderr + r.stdout):
                    self.multi = False  # older kicad-cli: fall back to one call per layer
                elif r.returncode != 0:
                    print(f"  kicad-cli failed on {os.path.basename(snap_gz)}:\n{r.stderr}")
            if not self.multi:
                for l in todo:
                    out = self.svg_path(d, layers[l][0])
                    subprocess.run(self.base_args() + ["--layers", l, "-o", out, tmp_pcb],
                                   capture_output=True, text=True)
        finally:
            try:
                os.remove(tmp_pcb)
            except OSError:
                pass
        return d, layers


# ----------------------------------------------------- board: rasterise + mix
SVG_SIZE_RE = re.compile(r'width="([\d.]+)mm" height="([\d.]+)mm" viewBox="([-\d. ]+)"')
PAGE_PAD = 1.0  # extra page-widths of canvas on each side


def _expand_canvas(svg):
    """
    KiCad plots on the page (e.g. A4), and anything placed off the page would be
    clipped. Grow the canvas a page-width in every direction. Every snapshot gets
    the same expansion, so frames still line up.
    """
    m = SVG_SIZE_RE.search(svg)
    if not m:
        return svg
    w, h = float(m.group(1)), float(m.group(2))
    vx, vy, vw, vh = (float(v) for v in m.group(3).split())
    k = 1 + 2 * PAGE_PAD
    new = (f'width="{w * k:.4f}mm" height="{h * k:.4f}mm" '
           f'viewBox="{vx - vw * PAGE_PAD:.4f} {vy - vh * PAGE_PAD:.4f} {vw * k:.4f} {vh * k:.4f}"')
    return svg[:m.start()] + new + svg[m.end():]


HOLE_COLOURS = re.compile(r"#(FFFFFF|000000)\b", re.I)


FILL_RE = re.compile(r"fill:\s*#([0-9A-Fa-f]{6})")


def _recolour_vias(svg_text, vias):
    """
    KiCad's plotter paints vias in the theme's via colour on every layer; the
    editor draws them in each copper layer's own colour. Repaint the via copper
    in this layer's main colour (the commonest fill), like the editor.
    """
    import xml.etree.ElementTree as ET
    from collections import Counter
    ET.register_namespace("", SVG_NS)
    try:
        root = ET.fromstring(svg_text.encode())
    except ET.ParseError:
        return svg_text
    via_groups, counts = [], Counter()
    for g in root.iter(f"{{{SVG_NS}}}g"):
        m = FILL_RE.search(g.get("style", ""))
        shapes = [c for c in g if c.tag != f"{{{SVG_NS}}}g"]
        if not m or not shapes or m.group(1).upper() in ("FFFFFF", "000000"):
            continue
        if all(_is_via(c, vias) for c in shapes):
            via_groups.append((g, m.group(1)))
        else:
            counts[m.group(1).upper()] += len(shapes)
    if not via_groups or not counts:
        return svg_text  # no vias, or nothing else on this layer to take a colour from
    layer = counts.most_common(1)[0][0]
    for g, old in via_groups:
        g.set("style", re.sub(old, layer, g.get("style"), flags=re.I))
    return ET.tostring(root, encoding="unicode")


def load_svg(path, bg_hex, via_layer_colour=False):
    with open(path, encoding="utf-8") as f:
        svg = _expand_canvas(f.read())
    if via_layer_colour:
        vias = board_vias(os.path.join(os.path.dirname(path), SNAP_NAME))
        if vias:
            svg = _recolour_vias(svg, vias)
    # KiCad plots drill holes in pure white (plated) or black (non-plated) to knock
    # out the pad. Paint them background; the hole style is applied afterwards.
    svg = HOLE_COLOURS.sub(bg_hex, svg)
    return fitz.open(stream=svg.encode(), filetype="svg")


# Bump when the look of rendered frames changes, so cached frames are redrawn
RENDER_VERSION = 9

SVG_NS = "http://www.w3.org/2000/svg"


VIA_RE = re.compile(r"\(via\b[^(]*\(at\s+([-\d.]+)\s+([-\d.]+)")
_via_cache = {}


def board_vias(pcb_path):
    """Via centres (mm) from a snapshot .kicad_pcb, to tell via holes from pad holes."""
    if pcb_path not in _via_cache:
        try:
            with open(pcb_path, encoding="utf-8") as f:
                _via_cache[pcb_path] = [(float(x), float(y)) for x, y in VIA_RE.findall(f.read())]
        except OSError:
            _via_cache[pcb_path] = []
    return _via_cache[pcb_path]


def _is_via(shape, vias):
    if not vias or not shape.tag.endswith("circle"):
        return False
    try:
        cx, cy = float(shape.get("cx")), float(shape.get("cy"))
    except (TypeError, ValueError):
        return False
    return any(abs(cx - x) < 0.005 and abs(cy - y) < 0.005 for x, y in vias)


def holes_only_svg(svg_text, kind="all", vias=(), plating="any"):
    """
    A copy of a copper-layer SVG keeping only the drill-hole shapes (the pure
    white/black groups). Parsed properly: KiCad nests some groups.
    kind: "all", "via" (via holes only) or "pad" (pad and mounting holes only).
    plating: "any", "plated" (KiCad plots these white) or "npth" (plotted black).
    """
    import xml.etree.ElementTree as ET
    ET.register_namespace("", SVG_NS)
    root = ET.fromstring(svg_text.encode())
    out = ET.Element(root.tag, root.attrib)
    found = False
    for g in root.iter(f"{{{SVG_NS}}}g"):
        style = g.get("style", "")
        m = HOLE_COLOURS.search(style)
        if not m:
            continue
        if plating == "plated" and m.group(1).upper() != "FFFFFF":
            continue
        if plating == "npth" and m.group(1) != "000000":
            continue
        shapes = [c for c in g if c.tag != f"{{{SVG_NS}}}g"]
        if kind == "via":
            shapes = [c for c in shapes if _is_via(c, vias)]
        elif kind == "pad":
            shapes = [c for c in shapes if not _is_via(c, vias)]
        if shapes:
            ng = ET.SubElement(out, g.tag, {"style": style})
            ng.extend(shapes)
            found = True
    return ET.tostring(out, encoding="unicode") if found else None


def holes_mask(path, zoom, clip, W, H, ox, oy, kind="all", vias=(), plating="any"):
    """'L' mask of just the drill holes in a copper-layer SVG."""
    with open(path, encoding="utf-8") as f:
        text = holes_only_svg(_expand_canvas(f.read()), kind, vias, plating)
    if text is None:
        return None
    doc = fitz.open(stream=text.encode(), filetype="svg")
    pix = doc[0].get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=True)
    a = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples).getchannel("A")
    full = Image.new("L", (W, H), 0)
    full.paste(a, (ox, oy))
    return full


HOLE_RINGS = {"white": (235, 235, 235), "cyan": (0, 215, 255), "gold": (222, 175, 60)}
HOLE_FILLS = {"cyan-solid": HOLE_RINGS["cyan"]}  # whole hole filled with the colour
# KiCad default theme: plated pad hole, via hole, hole wall, non-plated hole
KICAD_PAD_HOLE = (194, 194, 0)
KICAD_VIA_HOLE = (227, 183, 46)
KICAD_HOLE_WALL = (236, 236, 236)
KICAD_NPTH = (26, 196, 210)


def content_bbox(svg_paths, bg_hex):
    """Union bbox (page units) of everything drawn in the given SVGs."""
    box = None
    for p in svg_paths:
        doc = load_svg(p, bg_hex)
        page = doc[0]
        zoom = 1600 / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=True)
        img = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        bb = img.getchannel("A").getbbox()
        if bb:
            r = fitz.Rect(*bb) / zoom
            box = r if box is None else box | r
    return box


def _rasterise(svg, bg_hex, zoom, clip, W, H, ox, oy, via_layer_colour=False):
    page = load_svg(svg, bg_hex, via_layer_colour)[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=True)
    layer = Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    full.paste(layer, (ox, oy))
    return full


def board_fill_mask(edge_svg, bg_hex, zoom, clip, W, H, ox, oy, cache_dir):
    """
    'L' mask of the area inside the board outline, or None if the outline isn't
    closed. Flood-fills the outside at quarter resolution; cached per outline
    because the outline rarely changes.
    """
    with open(edge_svg, "rb") as f:
        key = hashlib.md5(f.read() + repr((zoom, tuple(clip), W, H)).encode()).hexdigest()[:16]
    path = os.path.join(cache_dir, f"fill_{key}.png")
    if os.path.exists(path):
        try:
            m = Image.open(path)
            m.load()
            return None if m.getbbox() is None else m
        except Exception:  # noqa: BLE001 - damaged by an earlier crash: rebuild it
            pass

    alpha = _rasterise(edge_svg, bg_hex, zoom, clip, W, H, ox, oy).getchannel("A")
    k = 4
    small = alpha.resize((max(1, W // k), max(1, H // k)), Image.BOX)
    small = small.point(lambda v: 255 if v > 20 else 0).filter(ImageFilter.MaxFilter(3))
    padded = Image.new("L", (small.width + 2, small.height + 2), 0)
    padded.paste(small, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), 128)               # 128 = outside
    inside = padded.crop((1, 1, small.width + 1, small.height + 1)).point(
        lambda v: 255 if v == 0 else 0)
    area = sum(inside.histogram()[255:]) / max(1, inside.width * inside.height)
    if area < 0.01 or area > 0.98:   # outline open (fill leaked) or nothing there
        mask = Image.new("L", (W, H), 0)
    else:
        mask = inside.resize((W, H), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1))
    # Several worker processes may build the same mask at once: write to a
    # private temp file, then swap it in, so nobody reads a half-written PNG
    os.makedirs(cache_dir, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    mask.save(tmp, format="PNG")
    try:
        os.replace(tmp, path)
    except OSError:  # another worker won the race (Windows can refuse replace)
        try:
            os.remove(tmp)
        except OSError:
            pass
    return None if mask.getbbox() is None else mask


def render_board_frame(job):
    (svgs, opacities, copper, edge_svg, clip, size, bg, bg_hex, style, cache_dir, out_png) = job
    W, H = size
    clip = fitz.Rect(clip)
    zoom = min(W / clip.width, H / clip.height)
    ox = int((W - clip.width * zoom) / 2)
    oy = int((H - clip.height * zoom) / 2)

    # all layers onto a transparent sheet first, so glow/saturation only touch the artwork
    art = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    glow_src = Image.new("RGBA", (W, H), (0, 0, 0, 0))  # copper only: silk/fab glow = haze
    for svg, op, cu in zip(svgs, opacities, copper):
        if svg is None or not os.path.exists(svg):
            continue
        layer = _rasterise(svg, bg_hex, zoom, clip, W, H, ox, oy,
                           via_layer_colour=cu and style.get("via_colour", "layer") == "layer")
        a = layer.getchannel("A")
        if svg == edge_svg and style["outline"] > 1:   # thicker, clearer board outline
            a = a.filter(ImageFilter.MaxFilter(style["outline"] // 2 * 2 + 1))
            rgb = layer.convert("RGB").filter(ImageFilter.MaxFilter(style["outline"] // 2 * 2 + 1))
            layer = rgb.convert("RGBA")
        if op < 1:
            a = a.point(lambda v: int(v * op))
        layer.putalpha(a)
        art = Image.alpha_composite(art, layer)
        if cu:
            glow_src = Image.alpha_composite(glow_src, layer)

    if style["saturation"] != 1:
        a = art.getchannel("A")
        art = ImageEnhance.Color(art.convert("RGB")).enhance(style["saturation"]).convert("RGBA")
        art.putalpha(a)

    canvas = Image.new("RGBA", (W, H), bg + (255,))
    if style["fill"] is not None and edge_svg and os.path.exists(edge_svg):
        mask = board_fill_mask(edge_svg, bg_hex, zoom, clip, W, H, ox, oy, cache_dir)
        if mask is not None:
            canvas.paste(style["fill"] + (255,), (0, 0), mask)
    if style["glow"] > 0:
        glow = glow_src.filter(ImageFilter.GaussianBlur(max(2, H / 300)))
        ga = glow.getchannel("A").point(lambda v: int(min(255, v * 1.6) * style["glow"]))
        glow.putalpha(ga)
        canvas = Image.alpha_composite(canvas, glow)
    canvas = Image.alpha_composite(canvas, art)

    hole_src = next((svg for svg, cu in zip(svgs, copper) if cu and svg and os.path.exists(svg)), None)
    hs, vs = style.get("holes", "dark"), style.get("vias", "dark")
    if hole_src and (hs != "dark" or vs != "dark"):
        vias = board_vias(os.path.join(os.path.dirname(hole_src), SNAP_NAME))
        jobs = [("all", hs)] if hs == vs else [("pad", hs), ("via", vs)]
        k = 2 * max(1, round(min(W, H) / 1080)) + 1  # ring/wall width: ~1 px at 1080p
        for kind, hstyle in jobs:
            if hstyle == "dark":
                continue  # as plotted: copper ring, dark hole
            if hstyle == "kicad":
                # Like the editor: plated holes filled gold with a light wall,
                # non-plated holes outlined cyan
                plated = holes_mask(hole_src, zoom, clip, W, H, ox, oy, kind, vias, "plated")
                if plated is not None and plated.getbbox():
                    # whole hole in the wall colour, then the gold fill shrunk
                    # inside it, leaving a thin wall ring at the edge
                    canvas.paste(KICAD_HOLE_WALL + (255,), (0, 0), plated)
                    core = plated.filter(ImageFilter.MinFilter(3))  # wall: ~1 px, like the editor
                    if kind == "via":
                        canvas.paste(KICAD_VIA_HOLE + (255,), (0, 0), core)
                    else:
                        canvas.paste(KICAD_PAD_HOLE + (255,), (0, 0), core)
                        if kind == "all":  # vias get their own gold
                            vm = holes_mask(hole_src, zoom, clip, W, H, ox, oy, "via", vias, "plated")
                            if vm is not None:
                                canvas.paste(KICAD_VIA_HOLE + (255,), (0, 0),
                                             vm.filter(ImageFilter.MinFilter(3)))
                if kind != "via":
                    npth = holes_mask(hole_src, zoom, clip, W, H, ox, oy, kind, vias, "npth")
                    if npth is not None and npth.getbbox():
                        ring = ImageChops.subtract(npth.filter(ImageFilter.MaxFilter(k)), npth)
                        canvas.paste(KICAD_NPTH + (255,), (0, 0), ring)
                continue
            hm = holes_mask(hole_src, zoom, clip, W, H, ox, oy, kind, vias)
            if hm is None or not hm.getbbox():
                continue
            if hstyle == "board":
                canvas.paste((style["fill"] or bg) + (255,), (0, 0), hm)
            elif hstyle in HOLE_FILLS:
                canvas.paste(HOLE_FILLS[hstyle] + (255,), (0, 0), hm)
            elif hstyle in HOLE_RINGS:
                ring = ImageChops.subtract(hm.filter(ImageFilter.MaxFilter(k)), hm)
                canvas.paste(HOLE_RINGS[hstyle] + (255,), (0, 0), ring)
    canvas.convert("RGB").save(out_png)
    return out_png


# ------------------------------------------------------------ view frames
def normalise_view(job):
    src, dst, size, crop = job
    W, H = size
    im = Image.open(src).convert("RGB")
    if crop:
        x, y, w, h = crop
        im = im.crop((x, y, min(im.width, x + w), min(im.height, y + h)))
    scale = min(W / im.width, H / im.height)
    if scale != 1:
        im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))),
                       Image.LANCZOS)
    out = Image.new("RGB", (W, H), (0, 0, 0))
    out.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    out.save(dst)


# ------------------------------------------------------------------ ffmpeg
def video_size(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def encode_sequence(pattern, fps, out, hold, crf):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps), "-i", pattern,
                    "-vf", f"tpad=stop_mode=clone:stop_duration={hold}",
                    "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", out], check=True)


def encode_concat(frames_durs, out, fps, hold, crf, tmpdir):
    lst = os.path.join(tmpdir, "concat.txt")
    with open(lst, "w") as f:
        def q(p):  # forward slashes + escaped quotes keep ffmpeg's parser happy
            return "'" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'"
        for p, dur in frames_durs:
            f.write(f"file {q(p)}\nduration {dur:.6f}\n")
        f.write(f"file {q(frames_durs[-1][0])}\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-vf", f"fps={fps},tpad=stop_mode=clone:stop_duration={hold}",
                    "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", out], check=True)


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sessions", nargs="+", help="one or more session folders")
    ap.add_argument("--separate", action="store_true",
                    help="with several sessions: render each on its own instead of joining")
    ap.add_argument("--out", help="output folder (default: renders/<recording> next to the recordings)")
    ap.add_argument("--only", choices=["view", "board"], help="render one stream only")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--hold", type=float, default=3, help="seconds to hold the last frame")
    ap.add_argument("--crf", type=int, default=18, help="x264 quality (lower = better)")
    ap.add_argument("--view-size", help="e.g. 1920x1080 (default: most common capture size)")
    ap.add_argument("--view-ui", choices=["canvas", "full"], default="canvas",
                    help="view video: crop to the drawing area, or show the whole editor UI "
                         "(needs a recording made with the whole editor captured)")
    ap.add_argument("--board-size", default="auto",
                    help="e.g. 1920x1080, or auto (1920x1080 or 1080x1920 to suit the board)")
    ap.add_argument("--board-fill", default="auto",
                    help="colour inside the board outline, 'auto' (a shade lighter than --bg) or 'none'")
    ap.add_argument("--outline", type=int, default=3, help="board outline thickness in px")
    ap.add_argument("--glow", type=float, default=0.2, help="soft glow around copper, 0 = off")
    ap.add_argument("--saturation", type=float, default=1.2, help="colour boost, 1 = as KiCad plots")
    ap.add_argument("--layers", help="board layers, bottom to top, e.g. B.Cu,F.Cu,Edge.Cuts "
                                     "(default: B.Silkscreen,B.Cu,inner,F.Cu,F.Silkscreen,Edge.Cuts)")
    ap.add_argument("--hide", default="", help="layers to drop from the stack, e.g. B.Silkscreen")
    ap.add_argument("--opacity", action="append", default=[],
                    help="LAYER=0..1, repeatable, e.g. --opacity B.Cu=0.5")
    ap.add_argument("--bg", default="#001023", help="board background colour")
    ap.add_argument("--theme", help="KiCad colour theme name (default: your editor theme)")
    ap.add_argument("--holes", default="kicad",
                    choices=["kicad", "dark", "board", "white", "cyan", "cyan-solid", "gold",
                             "actual", "small", "none"],
                    help="pad and mounting holes: kicad (like the editor: gold plated holes, "
                         "cyan non-plated), dark, board (board colour), cyan-solid, or a white/cyan/gold ring")
    ap.add_argument("--via-colour", choices=["layer", "plot"], default="layer",
                    help="via copper in each layer's colour like the editor, or KiCad's plotted via colour")
    ap.add_argument("--vias", default="kicad", choices=["kicad", "dark", "board", "white", "cyan", "cyan-solid", "gold"],
                    help="via holes, same choices as --holes")
    ap.add_argument("--crop", choices=["edge", "all"], default="edge",
                    help="frame the board outline, or everything on the final board")
    ap.add_argument("--margin", type=float, default=0.05, help="crop margin (fraction)")
    ap.add_argument("--no-sync", action="store_true",
                    help="board video: one frame per snapshot instead of following view timing")
    ap.add_argument("--board-fps", type=float, default=8, help="snapshots/sec with --no-sync")
    ap.add_argument("--no-follow-active", action="store_true",
                    help="board video: keep a fixed layer order instead of raising the "
                         "layer you were working on")
    ap.add_argument("--inactive-opacity", type=float, default=1.0,
                    help="fade non-active layers, e.g. 0.5 (like KiCad high-contrast)")
    ap.add_argument("--combined", action="store_true", help="also write a combined video")
    ap.add_argument("--layout", choices=["auto", "side", "below"], default="auto",
                    help="combined video: side by side, board below, or auto (closest to 16:9)")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("--kicad-cli")
    a = ap.parse_args()

    sessions = sorted(os.path.abspath(s) for s in a.sessions)  # timestamp names sort by date
    groups = [[s] for s in sessions] if a.separate else [sessions]
    failed = 0
    for i, group in enumerate(groups):
        if len(groups) > 1:
            print(f"\n=== {i + 1}/{len(groups)}: {os.path.basename(group[0])}")
        try:
            render(group, a)
        except (RenderError, subprocess.CalledProcessError) as e:
            print("  ERROR:", e)
            failed += 1
    if failed:
        sys.exit(f"{failed} of {len(groups)} render(s) failed")


def render(sessions, a):
    if len(sessions) == 1:
        work = sessions[0]
    else:
        names = [os.path.basename(s) for s in sessions]
        work = os.path.join(os.path.dirname(sessions[0]), f"joined_{names[0]}_to_{names[-1]}")
        boards_seen = set()
        for s in sessions:
            try:
                with open(os.path.join(s, "session.json")) as f:
                    boards_seen.add(json.load(f).get("board"))
            except Exception:  # noqa: BLE001
                pass
        if len(boards_seen) > 1:
            print("  warning: these sessions are from different boards:", ", ".join(map(str, boards_seen)))
        print(f"joining {len(sessions)} sessions")
    # Videos go to <folder>/renders/<recording>/, outside the recording itself,
    # so deleting a recording keeps its renders
    default_out = os.path.join(os.path.dirname(work), RENDERS, os.path.basename(work))
    out_dir = os.path.abspath(a.out or default_out)
    if a.out and a.separate:
        out_dir = os.path.join(out_dir, os.path.basename(work))
    cache = os.path.join(work, "cache")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache, exist_ok=True)
    for s in sessions:
        n = clean_old_caches(s)
        if n:
            print(f"  tidied {n} old cache files in {os.path.basename(s)}")
    views, boards = read_sessions(sessions)
    views = drop_broken_views(views)
    print(f"{len(views)} view frames, {len(boards)} board snapshots")
    if not views and not boards:
        raise RenderError("nothing recorded in this session")

    # ----------------------------------------------------------- view stream
    view_mp4 = os.path.join(out_dir, "view.mp4")
    if a.only != "board" and views:
        paths = [r["path"] for r in views]
        crops = [r["canvas"] if a.view_ui == "canvas" else None for r in views]
        if a.view_ui == "full" and not any(r["canvas"] for r in views):
            print("view: this recording only has the drawing area (no UI captured)")
        if a.view_size:
            vsize = parse_size(a.view_size)
        else:
            sizes = [(c[2], c[3]) if c else Image.open(p).size for p, c in zip(paths, crops)]
            w, h = Counter(sizes).most_common(1)[0][0]
            vsize = (w // 2 * 2, h // 2 * 2)
        sel = hashlib.md5(("|".join(paths) + a.view_ui).encode()).hexdigest()[:8]
        vdir = os.path.join(cache, f"view_{vsize[0]}x{vsize[1]}_{sel}")
        os.makedirs(vdir, exist_ok=True)
        jobs = [(p, os.path.join(vdir, f"{i:06d}.png"), vsize, c)
                for i, (p, c) in enumerate(zip(paths, crops))
                if not os.path.exists(os.path.join(vdir, f"{i:06d}.png"))]
        print(f"view: normalising {len(jobs)} frames to {vsize[0]}x{vsize[1]}")
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            list(ex.map(normalise_view, jobs, chunksize=16))
        encode_sequence(os.path.join(vdir, "%06d.png"), a.fps, view_mp4, a.hold, a.crf)
        print("  ->", view_mp4)

    # ---------------------------------------------------------- board stream
    board_mp4 = os.path.join(out_dir, "board.mp4")
    if a.only != "view" and boards:
        cli = find_kicad_cli(a.kicad_cli)
        drill = 2  # always actual size; --holes now picks the look
        if a.holes in ("actual", "small", "none"):  # old option values
            a.holes = "kicad"
        exp = Exporter(cli, f"svg_d{drill}_{a.theme or 'default'}", a.theme, drill)
        good = []
        for r in boards:
            path = r["path"]
            try:
                with gzip.open(path, "rb") as g:
                    while g.read(1 << 20):
                        pass
                good.append(r)
            except Exception:  # noqa: BLE001 - partial file, e.g. still recording
                print(f"  skipping unreadable snapshot {r['file']}")
        boards = good
        if not boards:
            raise RenderError("no readable board snapshots")
        snaps = [r["path"] for r in boards]

        # Work out the layer stack from the final board
        final_dir, final_layers = exp.export(snaps[-1], [])
        if a.layers:
            stack = [l.strip() for l in a.layers.split(",") if l.strip()]
        else:
            stack = default_stack(final_layers)
        hide = {h.strip() for h in a.hide.split(",") if h.strip()}
        stack = [l for l in stack if l not in hide]
        missing = [l for l in stack if l not in final_layers]
        if missing:
            print("  not on this board, skipping:", ", ".join(missing))
            stack = [l for l in stack if l in final_layers]
        if not stack:
            raise RenderError("no layers to render")
        opac = {}
        for o in a.opacity:
            k, v = o.split("=")
            opac[k.strip()] = float(v)
        print("board layers (bottom -> top):", ", ".join(stack))

        need = list(stack) + ["Edge.Cuts"]  # outline: framing and board fill
        need = list(dict.fromkeys(need))
        print(f"board: exporting SVGs ({len(snaps)} snapshots, cached after first run)")
        with cf.ThreadPoolExecutor(max(1, a.jobs // 2)) as ex:
            results = list(ex.map(lambda s: exp.export(s, need), snaps))

        # Fixed crop from the final board
        fd, fl = results[-1]
        clip = None
        if a.crop == "edge" and "Edge.Cuts" in fl:
            clip = content_bbox([exp.svg_path(fd, fl["Edge.Cuts"][0])], a.bg)
            if clip is None:
                print("  no board outline drawn yet, framing everything instead")
        if clip is None:
            clip = content_bbox([exp.svg_path(fd, fl[l][0]) for l in stack if l in fl], a.bg)
        if clip is None:
            raise RenderError("final board looks empty; nothing to frame")
        m = max(clip.width, clip.height) * a.margin
        clip = fitz.Rect(clip.x0 - m, clip.y0 - m, clip.x1 + m, clip.y1 + m)

        if a.board_size == "auto":
            bsize = (1920, 1080) if clip.width >= clip.height * 0.9 else (1080, 1920)
        else:
            bsize = parse_size(a.board_size)
        if a.board_fill == "none":
            fill = None
        elif a.board_fill == "auto":
            fill = tuple(min(255, int(c * 1.35 + 10)) for c in hex_rgb(a.bg))
        else:
            fill = hex_rgb(a.board_fill)
        style = {"fill": fill, "outline": a.outline, "glow": a.glow, "saturation": a.saturation,
                 "holes": a.holes, "vias": a.vias, "via_colour": a.via_colour}
        bg = hex_rgb(a.bg)
        key = hashlib.md5(json.dumps([RENDER_VERSION, stack, opac, list(clip), bsize, a.bg, drill,
                                      a.theme, a.inactive_opacity, snaps, style]).encode()).hexdigest()[:10]
        fdir = os.path.join(cache, f"board_frames_{key}")
        os.makedirs(fdir, exist_ok=True)

        # Timeline of (snapshot index, active layer), one entry per output step
        synced = bool(views) and not a.no_sync
        follow = synced and not a.no_follow_active and any(v["active_layer"] for v in views)
        # Copper order follows the last *copper* layer you used (inner layers too),
        # so hopping onto silkscreen etc. doesn't reshuffle the copper or via colours
        copper_names = {disp for disp, cu in final_layers.values() if cu}
        if synced:
            timeline, bi, last_cu = [], 0, ""
            for v in views:
                while bi + 1 < len(boards) and boards[bi + 1]["t_active"] <= v["t_active"]:
                    bi += 1
                act = v["active_layer"] if follow else ""
                if act in copper_names:
                    last_cu = act
                timeline.append((bi, act, last_cu))
        else:
            timeline = [(i, "", "") for i in range(len(results))]
        if follow:
            print("board: following the active layer")
        elif views and not a.no_follow_active and not a.no_sync:
            print("board: no active-layer data in this session, using a fixed layer order")

        jobs, seen, frames = [], set(), []
        for bi, active, last_cu in timeline:
            d, layers = results[bi]
            order, top_cu, raised = arrange(stack, layers, last_cu)
            top = top_cu
            if active and active != last_cu:   # a non-copper layer: raise it over the copper
                order, top_other, raised_other = arrange(order, layers, active, companions=False)
                if top_other:
                    top, raised = top_other, raised | raised_other
            png = os.path.join(fdir, f"{bi:06d}_{file_suffix(top_cu or 'fixed')}"
                                     f"_{file_suffix(top if top != top_cu else '')}.png")
            frames.append(png)
            if png in seen or os.path.exists(png):
                continue
            seen.add(png)
            svgs = [exp.svg_path(d, layers[l][0]) if l in layers else None for l in order]
            ops = [opac.get(l, 1.0) * (a.inactive_opacity
                                       if top and l not in raised and l in layers
                                       and layers[l][0] != "Edge.Cuts" else 1.0)
                   for l in order]
            edge = exp.svg_path(d, layers["Edge.Cuts"][0]) if "Edge.Cuts" in layers else None
            cu = [bool(l in layers and layers[l][1]) for l in order]
            jobs.append((svgs, ops, cu, edge, tuple(clip), bsize, bg, a.bg, style,
                         os.path.join(cache, "fills"), png))
        print(f"board: rendering {len(jobs)} frames at {bsize[0]}x{bsize[1]}")
        with cf.ProcessPoolExecutor(a.jobs) as ex:
            list(ex.map(render_board_frame, jobs))

        # Timing: synced = same length as the view video (side-by-side ready)
        step = 1 / a.fps if synced else 1 / a.board_fps
        durs = []
        for p in frames:
            if durs and durs[-1][0] == p:
                durs[-1][1] += step
            else:
                durs.append([p, step])
        frames_durs = [tuple(x) for x in durs]
        encode_concat(frames_durs, board_mp4, a.fps, a.hold, a.crf, cache)
        print("  ->", board_mp4)

    # -------------------------------------------------------------- combined
    if a.combined and os.path.exists(view_mp4) and os.path.exists(board_mp4):
        comb = os.path.join(out_dir, "combined.mp4")
        vw, vh = video_size(view_mp4)
        bw, bh = video_size(board_mp4)
        layout = a.layout
        if layout == "auto":
            # whichever arrangement avoids the more extreme shape: a portrait board
            # sits nicely beside the view, a wide board stacks into a near-square
            side = vw / vh + bw / bh                  # same height, side by side
            below = 1 / (vh / vw + bh / bw)           # same width, stacked
            target = 1.4                              # between square and 16:9
            layout = "side" if abs(math.log(side / target)) <= abs(math.log(below / target)) else "below"
        if layout == "side":
            fc = "[0:v]scale=-2:1080[a];[1:v]scale=-2:1080[b];[a][b]hstack=shortest=1"
        else:
            fc = "[0:v]scale=1920:-2[a];[1:v]scale=1920:-2[b];[a][b]vstack=shortest=1"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", view_mp4, "-i", board_mp4,
                        "-filter_complex", fc,
                        "-c:v", "libx264", "-crf", str(a.crf), "-pix_fmt", "yuv420p", comb],
                       check=True)
        print(f"  -> {comb} ({'side by side' if layout == 'side' else 'board below'})")


if __name__ == "__main__":
    main()
