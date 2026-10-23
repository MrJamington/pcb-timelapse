#!/usr/bin/env python3
"""
Build PCB Timelapse release archives.

  python make_release.py <path to pcb_timelapse folder> [--url DOWNLOAD_URL]

Writes to ./dist/:
  pcb-timelapse-<ver>-pcm.zip     for KiCad's Plugin and Content Manager
                                  (plugins/, resources/icon.png, metadata.json)
  pcb-timelapse-<ver>-manual.zip  a pcb_timelapse/ folder to drop into the plugins folder
  submission/<identifier>/        metadata.json (+ icon) for the merge request to
                                  gitlab.com/kicad/addons/metadata, with sha256 and sizes
"""
import argparse
import hashlib
import json
import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_TIME = (2020, 1, 1, 0, 0, 0)   # same bytes every build -> same SHA-256


def add(z, data_or_path, arcname):
    """Add a file with a fixed timestamp so rebuilding gives an identical archive."""
    info = zipfile.ZipInfo(arcname, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    if isinstance(data_or_path, bytes):
        z.writestr(info, data_or_path)
    else:
        with open(data_or_path, "rb") as f:
            z.writestr(info, f.read())
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_FILES = {"recorder_settings.json", "render_settings.json"}


def plugin_files(src):
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in sorted(files):
            if f in SKIP_FILES or f.endswith((".pyc", ".pcbtmp")):
                continue
            full = os.path.join(root, f)
            yield full, os.path.relpath(full, src).replace(os.sep, "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plugin_dir")
    ap.add_argument("--url", help="public download URL of the -pcm.zip (needed for submission). "
                                  "Reuses the existing build so the checksum matches what you uploaded")
    a = ap.parse_args()

    with open(os.path.join(HERE, "metadata.json")) as f:
        meta = json.load(f)
    ver = meta["versions"][0]["version"]
    dist = os.path.join(HERE, "dist")
    pcm = os.path.join(dist, f"pcb-timelapse-{ver}-pcm.zip")
    reuse = bool(a.url) and os.path.exists(pcm)
    if not reuse:
        shutil.rmtree(dist, ignore_errors=True)
        os.makedirs(dist)

    # The metadata inside the archive must not carry download_* fields
    inner = json.loads(json.dumps(meta))
    for v in inner["versions"]:
        for k in ("download_url", "download_sha256", "download_size", "install_size"):
            v.pop(k, None)

    manual = os.path.join(dist, f"pcb-timelapse-{ver}-manual.zip")
    if not reuse:
        with zipfile.ZipFile(pcm, "w") as z:
            for full, rel in plugin_files(a.plugin_dir):
                add(z, full, "plugins/" + rel)
            add(z, os.path.join(HERE, "resources", "icon.png"), "resources/icon.png")
            add(z, json.dumps(inner, indent=2).encode(), "metadata.json")
        with zipfile.ZipFile(manual, "w") as z:
            for full, rel in plugin_files(a.plugin_dir):
                add(z, full, "pcb_timelapse/" + rel)
    else:
        print("reusing", os.path.basename(pcm), "(not rebuilt, so its checksum is unchanged)")
    with zipfile.ZipFile(pcm) as z:
        install_size = sum(i.file_size for i in z.infolist())

    with open(pcm, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    v = meta["versions"][0]
    v["download_sha256"] = sha
    v["download_size"] = os.path.getsize(pcm)
    v["install_size"] = install_size
    v["download_url"] = a.url or "TODO: public URL of " + os.path.basename(pcm)
    sub = os.path.join(dist, "submission", meta["identifier"])
    shutil.rmtree(os.path.join(dist, "submission"), ignore_errors=True)
    os.makedirs(sub)
    with open(os.path.join(sub, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)
    shutil.copy(os.path.join(HERE, "resources", "icon.png"), os.path.join(sub, "icon.png"))

    print("package:", pcm, f"({v['download_size']} bytes, sha256 {sha[:16]}...)")
    print("manual install zip:", manual)
    print("submission metadata:", os.path.join(sub, "metadata.json"))
    if not a.url:
        print("note: re-run with --url once the -pcm.zip is uploaded somewhere public")


if __name__ == "__main__":
    main()
