# Releasing PCB Timelapse

1. Commit and **push before creating the release**, so the tag lands on the right commit. Repo: `github.com/MrJamington/pcb-timelapse` (public). The KiCad store requires an issue tracker; GitHub Issues counts.
2. Bump `VERSION` in `pcb_timelapse/version.py`. The build copies it into `metadata.json` (once the plugin is in the KiCad store, later releases also need a new entry in `versions`).
3. Build, from the repo root: `python release/make_release.py pcb_timelapse`
4. Create a GitHub release (tag `v<ver>`) and attach `release/dist/pcb-timelapse-<ver>-pcm.zip` and `-manual.zip` as assets. Copy the asset's direct link (`https://github.com/MrJamington/pcb-timelapse/releases/download/v<ver>/pcb-timelapse-<ver>-pcm.zip`).
5. Re-run with the link: `python release/make_release.py pcb_timelapse --url <direct link>`. This reuses the zip you uploaded (it doesn't rebuild), so the SHA-256 in the submission matches it.
6. With a GitLab account (the store lives there even though the code is on GitHub), fork `gitlab.com/kicad/addons/metadata` and add `packages/com.github.mrjamington.pcb-timelapse/` containing `dist/submission/.../metadata.json` and `icon.png`. Open a merge request.

The `-manual.zip` is for people installing by hand: unzip it into the plugins folder.
