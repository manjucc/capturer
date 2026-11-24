# Capturer

**English** | [简体中文](README_zh-CN.md)

Periodic webcam capture with person detection for a Raspberry Pi 5, backed by
a browsable HTTPS gallery on a remote server.

Once started, `capture.py` grabs a frame from the USB webcam every few
seconds, runs YOLOv5s person detection on it, and — only when a person is
present — saves the JPEG locally **and** uploads it asynchronously over HTTPS
to an nginx-served gallery you can browse from any PC or phone. It runs in the
foreground, exits cleanly on Ctrl+C, self-terminates after 7 days, and shuts
down early if free disk space drops below 2 GiB.

## Features

- **Periodic capture** from a V4L2 device (default `/dev/video0`) at a fixed
  interval, natively through OpenCV — no external capture tool
- **Person detection** with YOLOv5s: a photo is saved only when someone is in
  it (`--no-detect` saves every photo instead and never uploads)
- **Disk floor**: exits with code 2 when free space on the output filesystem
  drops below `--min-free-gib` (default 2 GiB; `0` disables), optionally
  purging this program's old captures first (`--retain-days`)
- **Asynchronous HTTPS upload** of kept photos via WebDAV (`PUT` + `MOVE`,
  basic auth, pinned self-signed certificate) — a dead network never stalls
  captures or eats memory; each upload is abandoned after `--upload-timeout`
- **Browsable gallery** served by nginx autoindex, with a `capture-list.txt`
  name list maintained next to the photos
- Clean shutdown on Ctrl+C/SIGTERM, `--max-runtime` self-termination,
  documented exit codes

## Requirements

- Raspberry Pi (developed on a Pi 5, aarch64) with Python 3.11
- USB webcam at `/dev/video0`
- `v4l2-ctl` (optional, for inspecting camera formats)
- Python packages, installed system-wide: `torch`, `torchvision`, `Pillow`,
  `numpy`, `opencv-python`, `requests`
- A YOLOv5 checkout named `yolov5-USB/` next to `capture.py` — **not included
  in this repository**, see the next section

## Prepare the YOLOv5 checkout

`capture.py` loads YOLOv5 locally via `torch.hub`: the default `--weights`
path is `yolov5-USB/yolov5s.pt` next to the script, and the weights file's
parent directory is used as the `torch.hub` repository. The folder is
intentionally not committed (it is 18 MB of third-party code plus weights,
freely obtainable upstream), so prepare it once after cloning this repo:

```bash
# 1. get the yolov5 source (release v7.0 — what this project is tested with)
git clone --depth 1 -b v7.0 https://github.com/ultralytics/yolov5.git yolov5-USB

# 2. get the yolov5s weights into it
wget -O yolov5-USB/yolov5s.pt \
    https://github.com/ultralytics/yolov5/releases/download/v7.0/yolov5s.pt
```

No `git`? Download the zipped source instead —
<https://github.com/ultralytics/yolov5/archive/refs/tags/v7.0.zip> — extract
it and rename the resulting `yolov5-7.0/` folder to `yolov5-USB/`, then
download `yolov5s.pt` from the v7.0 release assets as above.

Verify the weights file if you like (the one this project was built with):

```
size:   14,808,437 bytes
sha256: 8b3b748c1e592ddd8868022e8732fde20025197328490623cc16c6f24d0782ee
```

Notes:

- A custom `-w/--weights` may point at any `.pt` **inside** a yolov5
  checkout (its parent directory becomes the `torch.hub` repo) — the folder
  does not have to be named `yolov5-USB` in that case.
- Development and testing used upstream release **v7.0**; newer releases
  generally work too but are untested here.
- `.pt` weights are pickle files that execute code on load — only use
  weights from a source you trust.

## Quick start

```bash
python3 capture.py
```

Captures every 5 s from `/dev/video0` into `/home/pi/TF` at 640x480, saves
only frames with a person, and uploads them to
`https://capture.manjucc.com`. Stop with Ctrl+C; it also stops itself after
168 h (7×24).

Plain capture without detection (never uploads):

```bash
python3 capture.py --no-detect
```

Detect and save locally without uploading:

```bash
python3 capture.py --no-upload
```

### Options

| Option | Default | Meaning |
|--------|---------|---------|
| `-o, --output-dir` | `/home/pi/TF` | folder to save photos in |
| `-i, --interval` | `5` | seconds between captures |
| `-t, --max-runtime` | `604800` (168 h) | self-terminate after this many seconds |
| `-d, --device` | `/dev/video0` | V4L2 device to capture from |
| `-r, --resolution` | `640x480` | requested image resolution WxH (see note below) |
| `--no-detect` | off | save every photo, no detection, no uploads |
| `-c, --conf` | `0.25` | person-detection confidence threshold |
| `--infer-size` | `480` | long-side size of the detection input (smaller = faster; photos keep full resolution) |
| `-w, --weights` | `yolov5-USB/yolov5s.pt` | YOLOv5 weights (script-relative default) |
| `--min-free-gib` | `2` | exit when free space drops below this many GiB; `0` disables |
| `--retain-days` | `0` (off) | delete captures older than this from the output dir (only this program's files); tried before the low-disk exit |
| `--remote-url` | config / `https://capture.manjucc.com` | HTTPS gallery/upload base URL |
| `--upload-config` | `~/.config/capturer/upload.json` | upload credentials + pinned CA (below) |
| `--upload-timeout` | `30` | seconds each upload gets before it is abandoned |
| `--no-upload` | off | detect and save locally, skip uploads |
| `--version` | — | print the version and exit |

Full list: `python3 capture.py --help`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | normal end (Ctrl+C, SIGTERM, or `--max-runtime` elapsed) |
| 1 | setup error (camera/model/config problem, e.g. missing upload config) |
| 2 | free disk space dropped below `--min-free-gib` |
| 130 | a second stop signal arrived while stopping |

## How detection works

`capture.py` loads YOLOv5s once at startup (~6 s) and runs inference on each
frame on a downscaled copy (`--infer-size`, long side 480 by default: ~0.65 s
per frame on this Pi; ~1.1 s at 640) while photos are saved at full
resolution (JPEG quality 85, ~17 KB per 640x480 frame). "Person" is COCO
class 0 — YOLOv5's COCO classes have no "face" category, so it detects
"anyone present". Photos are named `YYYY-MM-DD_HHMMSS.jpg` (extended with a
`-2`, `-3`, … suffix within the same second so rapid frames never overwrite
each other).

**Resolution note:** the camera supports only 1920x1080 / 1280x720 / 800x600 /
640x480 — there is no 640x360 mode. `capture.py` requests 640x480 directly
and logs a warning if the camera negotiates a different size.

## The upload

Every kept photo is enqueued to a daemon thread with a bounded queue (128), so
a dead network never stalls captures or eats memory. Each photo is WebDAV
`PUT` to a hidden `.name.jpg.part` URL, then one `MOVE` into place — a partial
transfer never appears under its final name. TLS is verified against the
pinned self-signed certificate (verified, not bypassed); every HTTP call has a
5 s timeout and each photo a `--upload-timeout` overall deadline. A
`capture-list.txt` (one filename per line) is maintained on the server from a
local mirror: appended after each successful upload, re-uploaded whole once at
least 8 new names and 30 s have accumulated since the last push (or 5 minutes,
whichever comes first), flushed at shutdown, and merged rather than clobbered
so server-side history is never lost.

Uploads need a config file (permissions 600):

```json
{
  "url": "https://capture.manjucc.com",
  "user": "Capturer",
  "password": "…",
  "ca": "/home/pi/.config/capturer/manjucc-capture.crt"
}
```

A missing or broken config is a setup error (exit 1).

### Browsing the gallery

Photos are browsable at <https://capture.manjucc.com/> (or
<https://manjucc.com/captures/>, read-only). Basic-auth user `Capturer`; the
password is the one in `upload.json`. Browsers will warn about the self-signed
certificate — accept it once per device.

The server side (nginx over HTTPS, WebDAV, basic auth, the
`/var/www/captures` layout, a daily photo-retention timer) is (re)created by
`server/deploy-v5.sh` — see `docs/https-upload.md` for the full design,
deployment, and usage write-up (in Chinese).

## Testing

```bash
# offline end-to-end check of the uploader (stub HTTPS+WebDAV server)
python3 -I tests/local_upload_check.py

# offline logic unit tests (no camera/torch/network)
python3 -I tests/test_capture_logic.py

# CLI smoke test of the detector on a single image
python3 yolov5-USB/detect.py --weights yolov5-USB/yolov5s.pt \
    --source <image> --classes 0
```

## Project layout

```
capture.py        the program: capture, detection, disk floor, uploader
yolov5-USB/       not committed — prepare it from upstream (see above)
tests/            local_upload_check.py — offline uploader self-check
                  test_capture_logic.py — offline logic unit tests
server/           deploy-v5.sh — idempotent server-side (re)deploy for the
                  gallery host (installs the 90-day photo retention timer;
                  re-signs the TLS cert only when <30 days remain)
docs/             https-upload.md — full design/deploy/usage write-up (Chinese)
README_zh-CN.md   简体中文版 README
CLAUDE.md         workspace guide for Claude Code sessions
```

## Implementation notes

- **Why a worker thread:** a SIGINT that lands while the main thread is inside
  a torch/OpenMP native call can wedge the process on this Pi. The capture
  loop therefore runs on a worker thread and stops via an event flag; the main
  thread does short interruptible joins and force-exits if the worker fails to
  stop.
- **Capture:** the camera is opened once via OpenCV's V4L2 backend and held
  for the whole run (worker thread only); each tick drops the single stale
  buffered frame and keeps the next, so a shot costs ~0.1 s instead of a
  ~1 s process spawn. A failed read releases the handle and the next tick
  reopens it lazily, the missed frame counting as "failed". Frames go to
  detection as in-memory arrays — discarded frames never touch disk.
- The max-runtime clock starts before the model load; the capture grid anchors
  after it.
- The disk floor is checked in the capture loop *and* by a main-thread
  watchdog, so it holds even if the worker wedges in native code.

## License

AGPL-3.0-only (`LICENSE`).

This is deliberate, not a default choice: the project runs YOLOv5 code via
`torch.hub`, and YOLOv5 is licensed AGPL-3.0 (see
[ultralytics/yolov5](https://github.com/ultralytics/yolov5/blob/master/LICENSE))
— a strong copyleft that extends to combined works and to network use
(section 13). AGPL-3.0 is the only license consistent with that dependency.
