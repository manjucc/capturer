# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`capture.py` (workspace root, `__version__` 1.0.0) is a Python webcam-capture program for a Raspberry Pi 5:

- **Capture**: one photo every 5 s (`DEFAULT_INTERVAL`; `-i` sets any value) **natively via OpenCV's V4L2 backend**: the worker thread opens `/dev/video0` once and holds it for the whole run (MJPG, `BUFFERSIZE=1`, warm-up grabs at open), and each tick drops the single stale buffered frame and keeps the next (~0.1 s per shot). A failed read releases the handle and the next tick reopens it lazily — that frame just counts "failed"; a startup open failure is a setup error (exit 1). This cv2 build's V4L2 backend rejects the OPEN/READ timeout properties (passing them fails the whole open), so a wedged driver call is contained by the main-thread watchdog instead.
- **Person detection** (YOLOv5s, loaded once at startup from `yolov5-USB` via `torch.hub`, ~6 s load; ~0.64 s per inference at the default `--infer-size 480` — long side of the detection input only; saved photos stay 640x480, JPEG quality 85, ~17 KB/frame): a photo is saved only when a person is present. Detection runs on an in-memory BGR→RGB numpy view (the vendored AutoShape treats numpy input as RGB), so discarded frames never touch disk. "Person" = COCO class 0 — COCO has no "face" class, so it covers the intent as "anyone present". `--no-detect` saves every frame and never uploads; `-c/--conf` (default 0.25) tunes the threshold.
- **Disk floor**: self-terminate (exit code 2) when free space on the output filesystem drops below 2 GiB (`--min-free-gib`; 0 disables) — checked in the capture loop before every frame (statvfs of the output dir's filesystem, so it also fails fast at startup) and by the main-thread watchdog, so it holds even if the worker wedges in native code. With `--retain-days N` (default 0 = off) captures older than N days are purged at startup and as a self-rescue before the low-disk exit — only top-level files matching this program's `YYYY-MM-DD_HHMMSS[-N].jpg` naming are ever deleted, so the user's own files in `/home/pi/TF` are safe.
- **HTTPS upload** (the `HttpsUploader` class): every kept photo goes to a daemon thread with a bounded queue (128) fed by `submit()` from the capture loop, so a dead network never stalls captures or eats memory. Per photo it WebDAV-`PUT`s the bytes to a hidden `.name.jpg.part` URL on nginx's built-in dav module, then one `MOVE` puts it under its final name — a partial transfer never appears under its final name. TLS is verified against the pinned cert named in the upload config (self-signed is verified, not bypassed); every HTTP call has a 5 s connect/read timeout and every photo a `--upload-timeout` overall deadline (default 30 s, then the upload is abandoned). `capture-list.txt` is kept via a local mirror (`capture-list.txt` in the output dir): appended after each successful upload, re-uploaded whole on a dual threshold (≥8 new names and ≥30 s since the last push, or ≥5 min whichever comes first) and flushed at shutdown. Because the whole file is re-uploaded, the mirror is only pushed once synced with the server's copy — a mirror whose fetch failed never clobbers the server's history, and names added meanwhile are merged in once the server is reachable. Config: `--remote-url` (default `https://capture.manjucc.com`) and `--upload-config` (default `~/.config/capturer/upload.json`, JSON with `user`, `password`, `ca`, optionally `url`); a missing/broken config is a setup error (exit 1). `--no-detect` never uploads; `--no-upload` disables uploads.
- **Run semantics**: foreground; Ctrl+C or SIGTERM stops cleanly; self-terminates after 168 h = 7×24 (`-t`/`--max-runtime`). Exit codes: 0 normal end, 1 setup error, 2 low disk, 130 on a second stop signal.

`run_loop`'s max-runtime clock starts *before* the model load (aligned with the watchdog, which measures from thread start) while the capture grid anchors after it. The default weights path (`-w/--weights`) is script-relative — `yolov5-USB/yolov5s.pt` next to `capture.py` — and `load_detector` uses the weights file's parent as the `torch.hub` repo, so a custom `-w` must point at a `.pt` inside a yolov5 checkout.

Tests (both offline, no camera/torch/network needed): `python3 -I tests/local_upload_check.py` — end-to-end check of the uploader against a stub HTTPS+WebDAV server with fault injection, 26 assertions; `python3 -I tests/test_capture_logic.py` — logic unit tests (normalize_device/free_gib/parse_args/filename collisions/purge, 10 assertions).

Docs: `docs/https-upload.md` is the full design/implementation/deploy/usage write-up of the upload + gallery (in Chinese). READMEs: `README.md` (English) and `README_zh-CN.md` (简体中文).

## Conventions

- JPEG, 640x480, no timestamp banner. The camera supports only 1920x1080 / 1280x720 / 800x600 / 640x480 — there is no 640x360 mode; a requested-vs-negotiated mismatch is logged as a warning.
- Photos named `YYYY-MM-DD_HHMMSS.jpg`, extended with a `-2`, `-3`, … suffix within the same second.
- Default output dir `/home/pi/TF` (pre-existing dir that also holds the user's own `capture.py`); only files matching this program's naming are ever touched there.

## Hardware / environment

- Raspberry Pi 5 (aarch64), system Python 3.11.2
- The USB webcam is `/dev/video0` — `v4l2-ctl --list-devices` shows it as "WebCamera". All other `/dev/video*` nodes belong to the Pi's ISP/codec blocks (pispbe, rpivid); never iterate over `/dev/video*` blindly.
- `v4l2-ctl` is installed (camera inspection).
- ML stack installed system-wide: torch 2.1.2, torchvision 0.16.2, opencv-python 4.8.1.78, Pillow 10.1.0, numpy 1.24.2, requests. No virtualenv is in use.

## YOLOv5 checkout (yolov5-USB/)

A local checkout of ultralytics/yolov5 **v7.0** with `yolov5s.pt` (14,808,437 bytes) at its root. It is **gitignored — not part of the repo**; both READMEs document how to rebuild it from upstream (`git clone --depth 1 -b v7.0 https://github.com/ultralytics/yolov5.git yolov5-USB` plus the `yolov5s.pt` release asset). `capture.py` loads it via `torch.hub`; `detect.py` inside it works for CLI smoke tests:

```bash
python3 yolov5-USB/detect.py --weights yolov5-USB/yolov5s.pt --source <image> --classes 0  # 0 = person in COCO
```

`.pt` weights are pickle files and execute code on load — use only weights from a trusted source.

## Gotcha: SIGINT vs torch on this Pi

SIGINT delivered while the main thread is inside a torch/OpenMP native call can wedge the process (main thread spins in R state forever; KeyboardInterrupt never raises). `capture.py` therefore runs its capture/detect loop on a **worker thread** and stops via an event flag; the main thread only does short interruptible joins and force-exits (`os._exit`) if the worker fails to stop within `STOP_GRACE` seconds — this also keeps the 168 h self-termination reliable. Do not move the loop back onto the main thread or stop via KeyboardInterrupt.

When *testing* signal handling from a script: backgrounding a compound command (`rm -f log && python3 capture.py ... &`) runs it in a **bash subshell**, so `$!` is the subshell's pid and `kill -INT $!` never reaches python (verified the hard way — gdb on the "stuck" pid showed bash's `wait_for`). Background a single command, or `pgrep -f capture.py`, to signal the real process.

## Secrets (never commit any of these)

- manjucc: andy's sudo password lives in `/home/pi/.config/capturer/passwd.txt` on the Pi.
- Upload basic auth (user `Capturer`): plaintext only in `~/.config/capturer/upload.json` on the Pi; apr1 hash in `/etc/nginx/htpasswd-capture` on the server, mirrored as `CAP_HASH` in `server/deploy-v5.sh` — keep all three in sync on every rotation.

## Remote host: manjucc

`manjucc` is an ssh alias in `~/.ssh/config` (HostName manjucc.com; User andy, IdentityFile id_ed25519, Compression, ServerAliveInterval 60) for the Alibaba Cloud ECS server that hosts the gallery. `ssh manjucc` works non-interactively:

- nginx (Ubuntu 24.04): manjucc.com serves `/var/www/html` unchanged over HTTPS, and :80 301-redirects to https. TLS = self-signed `/etc/nginx/ssl/manjucc.{crt,key}` — CN manjucc.com, SANs manjucc.com, www.manjucc.com, capture.manjucc.com, IP 118.178.229.25, **395-day validity (browsers reject longer; current one expires 2027-12-11)**. `server/deploy-v5.sh` only re-signs the cert when <30 days remain (`openssl x509 -checkend`) — re-running it doesn't break the CA pinned on the Pi — and the original pre-V5 backups (`/etc/nginx/sites-available/default.pre-v5` + the first `~/nginx-config-backup-*.tar.gz`, minute-stamped) are never clobbered by a re-run.
- Photo retention: `capture-retention.timer` deletes `*.jpg` older than 90 days from `/var/www/captures` daily at 03:30 (as www-data, top level only). **`capture-list.txt` is never deleted there** — it is the historical record, and the Pi's merge semantics would push pruned lines back on the next whole-file PUT.
- Photos live in `/var/www/captures` (www-data-writable; nginx serves *and* writes it via dav PUT/MOVE); `capture-list.txt` sits next to the photos. `/home/andy/captures` is the frozen pre-HTTPS-era archive (its `captured.list` keeps the old name).
- Browsing **and** uploading require basic-auth user `Capturer` (password in upload.json; see "Secrets"). Gallery: `https://capture.manjucc.com/`, or `https://manjucc.com/captures/` (same content, read-only alias). Browsers will warn about the self-signed cert — accept once per device.
- capture.manjucc.com resolves publicly to 118.178.229.25. The Pi also carries an `/etc/hosts` line `118.178.229.25 capture.manjucc.com` — harmless while the IP is right, keep it in sync if the ECS IP ever changes.
- If outside browsing/uploads ever time out, check the Aliyun security group first (inbound TCP 443 must be open; the server's own ufw/iptables being open is not enough).
- `server/deploy-v5.sh` (idempotent and safe to re-run: the cert is only re-signed when <30 days remain, backups aren't clobbered, and it installs/refreshes the retention timer; `scp` it over, then `SUDO_PASS=... CAP_PASS=... bash /tmp/deploy-v5.sh`) re-creates all server-side state. Gotcha fixed in it: write root-owned files via `sudo install` from a staged file — `printf pass | sudo -S tee` makes sudo eat the stdin password and silently writes **empty files** (this briefly took the site down during the first deploy).

## Useful commands

```bash
# run: capture every 5 s into /home/pi/TF, save only person frames, upload
# them over HTTPS to https://capture.manjucc.com (PUT+MOVE, basic auth,
# pinned self-signed cert) with capture-list.txt maintenance, Ctrl+C to stop,
# exits after 168 h (7*24); add --no-detect to save every frame (no uploads),
# --no-upload to detect+save without uploading
python3 capture.py
# all options (interval, max-runtime, output dir, device, resolution, conf,
# weights, no-detect, disk floor, infer size, retain days, remote url,
# upload config, upload timeout, no-upload, version) via -i/-t/-o/-d/-r/-c/-w/
# --no-detect/--min-free-gib/--infer-size/--retain-days/--remote-url/
# --upload-config/--upload-timeout/--no-upload/--version
python3 capture.py --help

# offline end-to-end check of the uploader (stub HTTPS+WebDAV server)
python3 -I tests/local_upload_check.py   # 26 assertions
# offline logic unit tests (no camera/torch/network)
python3 -I tests/test_capture_logic.py   # 10 assertions

# one-shot frame grab (what capture.py does each tick)
python3 -c "import cv2; c=cv2.VideoCapture('/dev/video0', cv2.CAP_V4L2); print(c.read()[1].shape)"

# inspect camera formats/controls
v4l2-ctl -d /dev/video0 --list-formats-ext

# check the remote side (auth user 'Capturer', password in upload.json)
curl --cacert ~/.config/capturer/manjucc-capture.crt \
  -u "Capturer:$(python3 -c "import json; print(json.load(open('/home/pi/.config/capturer/upload.json'))['password'])")" \
  https://manjucc.com/captures/ | tail
ssh manjucc 'tail /var/www/captures/capture-list.txt; ls /var/www/captures | tail'
```
