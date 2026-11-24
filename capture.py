#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Periodic webcam capture with person detection for the Raspberry Pi.

Capture: a photo is taken every --interval seconds (default 5) from the
V4L2 device --device (default /dev/video0), natively via OpenCV's V4L2
backend: the worker thread opens the camera once and holds it for the
whole run, dropping the single stale buffered frame each tick so the
keeper is fresh; a failed read releases the handle and the next tick
reopens it lazily.  Photos are JPEG at --resolution (default 640x480,
quality 85) named YYYY-MM-DD_HHMMSS[-N].jpg — seconds precision, with
a -N suffix when a second photo lands in the same second — saved into
--output-dir (default /home/pi/TF).

Person detection: each frame is run through YOLOv5s (loaded once at
startup from the local yolov5-USB checkout via torch.hub) and only
saved when a person is present.  "Person" is COCO class 0 — COCO has
no face class, so it covers "anyone present".  Detection runs on a
downscaled copy of the frame (--infer-size, default 480 = the long
side of the detection input); saved photos keep the full resolution.
-c/--conf (default 0.25) tunes the confidence threshold, and
--no-detect gives plain capture-and-save-everything behavior.

Disk floor: the free space on the filesystem holding the output dir
is watched; when it drops below --min-free-gib (default 2 GiB,
0 disables) the program exits with code 2 — checked before every
frame by the capture loop and enforced by the main-thread watchdog
even if the worker is wedged in native code.  With --retain-days N
(default 0 = off) captures older than N days are purged at startup
and as a self-rescue before the low-disk exit; only top-level files
matching this program's YYYY-MM-DD_HHMMSS[-N].jpg naming are ever
deleted.

Upload: each photo kept because a person was detected is also
uploaded to --remote-url (default https://capture.manjucc.com) by a
background uploader thread, so a slow network never stalls the
capture loop.  The transfer is WebDAV over HTTPS: a PUT of the bytes
to a hidden ".part" URL, then one MOVE into place (both served by
nginx's built-in dav module) — a partial transfer never appears under
its final name.  The same URL is the browsable gallery.
capture-list.txt (one name per line, served next to the photos) is
kept via a local mirror: appended to after each successful upload,
re-uploaded whole once enough new names and time have accumulated,
and flushed once at shutdown; a torn push heals on the next one.
Every photo gets --upload-timeout seconds (default 30) before its
upload is abandoned.  Credentials and the pinned server certificate
live in --upload-config (default ~/.config/capturer/upload.json);
uploads only run in detection mode, and --no-upload disables them.

Runs in the foreground: Ctrl+C (or SIGTERM) stops it cleanly, and it
self-terminates after --max-runtime (default 168 hours = 7*24).
Exit codes: 0 = done, 1 = setup error, 2 = low disk; a second stop
signal exits immediately with code 130.

Signal handling runs the capture loop on a worker thread and stops via an
event flag: on this torch/OpenMP stack, a SIGINT raised into the main
thread mid-inference can wedge the process in native code, so the main
thread stays in short interruptible joins and force-exits if the worker
ever fails to stop within STOP_GRACE seconds.
"""

import argparse
import json
import logging
import os
import queue
import re
import shutil
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import requests  # V5 uploader transport (installed system-wide)

__version__ = "1.0.0"

DEFAULT_INTERVAL = 5.0  # seconds between captures
DEFAULT_MAX_RUNTIME = 7 * 24 * 60 * 60  # self-terminate after 168 h (7*24)
DEFAULT_DEVICE = "/dev/video0"  # "WebCamera" on this Pi
DEFAULT_OUTPUT_DIR = Path("/home/pi/TF")
DEFAULT_RESOLUTION = "640x480"  # camera has no 640x360 mode; a negotiated mismatch is logged
DEFAULT_CONF = 0.25
DEFAULT_INFER_SIZE = 480  # detection input's long side (AutoShape size=); photos save at full size
DEFAULT_WEIGHTS = Path(__file__).resolve().parent / "yolov5-USB" / "yolov5s.pt"
DEFAULT_MIN_FREE_GIB = 2.0  # exit when free disk drops below this
DEFAULT_RETAIN_DAYS = 0  # local capture retention; 0 = keep everything
DEFAULT_REMOTE_URL = "https://capture.manjucc.com"  # gallery + upload endpoint
DEFAULT_UPLOAD_CONFIG = Path.home() / ".config/capturer/upload.json"  # creds + pinned cert
CAPTURE_LIST = "capture-list.txt"  # list of stored photo names, served next to them
DEFAULT_UPLOAD_TIMEOUT = 30.0  # per-photo deadline, then the upload is abandoned
UPLOAD_QUEUE_MAX = 128  # bound the queue so a dead network can't eat memory
LIST_PUSH_INTERVAL = 30.0  # min seconds between full capture-list.txt re-uploads
LIST_PUSH_MIN_NAMES = 8   # ...and at least this many new names since the last push
LIST_PUSH_MAX_DELAY = 300.0  # never leave the list dirty longer than this, though
UPLOAD_CALL_TIMEOUT = 5.0  # per HTTP request (connect, read) — bounds a hung peer
PERSON_CLASS = 0  # COCO class id for "person"
STOP_GRACE = 10.0  # seconds a stuck worker gets before the main thread force-exits
UPLOADER_STOP_BUDGET = 7.0  # uploader stop deadline: fits inside STOP_GRACE with room for worker teardown
EXIT_LOW_DISK = 2  # low-disk exit code; 0 = done, 1 = setup error
GIB = 1024 ** 3

# OpenCV/V4L2 capture tuning.
CAMERA_WARMUP_GRABS = 5   # frames discarded at open so auto-exposure settles (~1 s)
CAMERA_STALE_GRABS = 1    # buffered frames dropped per tick so the keeper is fresh
JPEG_QUALITY = 85         # cv2.imencode quality; ~17 KB per 640x480 frame

log = logging.getLogger("capture")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Capture a photo from the webcam every few seconds, "
                    "save it only when YOLOv5 detects a person, upload what "
                    "is saved to a remote HTTPS gallery, and exit when free "
                    "disk space drops below a floor."
    )
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + __version__)
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="folder to save photos in (default: %(default)s)")
    parser.add_argument("-i", "--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between captures (default: %(default)s)")
    parser.add_argument("-t", "--max-runtime", type=float, default=DEFAULT_MAX_RUNTIME,
                        help="self-terminate after this many seconds (default: 604800 = 168 h = 7*24)")
    parser.add_argument("-d", "--device", default=DEFAULT_DEVICE,
                        help="V4L2 device to capture from (default: %(default)s)")
    parser.add_argument("-r", "--resolution", default=DEFAULT_RESOLUTION,
                        help="image resolution WxH (default: %(default)s)")
    parser.add_argument("--no-detect", action="store_true",
                        help="save every photo without person detection or uploads")
    parser.add_argument("-c", "--conf", type=float, default=DEFAULT_CONF,
                        help="person-detection confidence threshold (default: %(default)s)")
    parser.add_argument("--infer-size", type=int, default=DEFAULT_INFER_SIZE,
                        help="long-side pixel size of the detection input; "
                             "smaller is faster, saved photos keep full "
                             "resolution (default: %(default)s)")
    parser.add_argument("-w", "--weights", type=Path, default=DEFAULT_WEIGHTS,
                        help="YOLOv5 weights file (default: %(default)s)")
    parser.add_argument("--min-free-gib", type=float, default=DEFAULT_MIN_FREE_GIB,
                        help="exit when free disk space on the output "
                             "filesystem drops below this many GiB; 0 disables "
                             "the check (default: %(default)s)")
    parser.add_argument("--retain-days", type=float, default=DEFAULT_RETAIN_DAYS,
                        help="delete captures older than this many days from "
                             "the output dir (only files this program named; "
                             "tried first when the disk floor is hit); "
                             "0 keeps everything (default: %(default)s)")
    parser.add_argument("--remote-url", default=None,
                        help="HTTPS gallery/upload URL for person-detected "
                             "photos (default: %(default)s = DEFAULT_REMOTE_URL, "
                             "or 'url' in --upload-config)")
    parser.add_argument("--upload-config", type=Path, default=DEFAULT_UPLOAD_CONFIG,
                        help="JSON file with the upload 'user', 'password' and "
                             "pinned 'ca' certificate "
                             "(default: %(default)s)")
    parser.add_argument("--upload-timeout", type=float, default=DEFAULT_UPLOAD_TIMEOUT,
                        help="seconds each photo upload gets before it is "
                             "abandoned, e.g. on network trouble "
                             "(default: %(default)s)")
    parser.add_argument("--no-upload", action="store_true",
                        help="do not upload detected photos to the gallery")
    args = parser.parse_args(argv)
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.max_runtime <= 0:
        parser.error("--max-runtime must be positive")
    try:
        parse_resolution(args.resolution)
    except ValueError as exc:
        parser.error(str(exc))
    if not 0 < args.conf < 1:
        parser.error("--conf must be between 0 and 1")
    if args.min_free_gib < 0:
        parser.error("--min-free-gib must be >= 0 (0 disables the check)")
    if args.infer_size <= 0:
        parser.error("--infer-size must be positive")
    if args.retain_days < 0:
        parser.error("--retain-days must be >= 0 (0 keeps everything)")
    if args.upload_timeout <= 0:
        parser.error("--upload-timeout must be positive")
    return args


def free_gib(path):
    """Free space, in GiB, on the filesystem containing *path*."""
    return shutil.disk_usage(path).free / GIB


def parse_resolution(resolution):
    """'WxH' -> (width, height); raises ValueError for anything else."""
    m = re.fullmatch(r"(\d{1,5})x(\d{1,5})", resolution.strip())
    if m is None:
        raise ValueError(f"resolution must be WxH, not {resolution!r}")
    return int(m.group(1)), int(m.group(2))


def normalize_device(device):
    """fswebcam accepted a bare index ("-d 0"); map it to /dev/videoN."""
    return f"/dev/video{device}" if device.isdigit() else device


def load_detector(weights, conf):
    """Load YOLOv5 from the local yolov5-USB clone via torch.hub."""
    import torch  # deferred: keeps --help and --no-detect runs light

    t0 = time.monotonic()
    # verbose= silences torch.hub itself; _verbose= is yolov5's own flag
    # (hubconf.custom) — both are needed for a quiet load.
    model = torch.hub.load(str(weights.parent), "custom", path=str(weights),
                           source="local", verbose=False, _verbose=False)
    model.conf = conf
    model.classes = [PERSON_CLASS]  # NMS keeps only person detections
    log.info("loaded %s in %.1fs (person = COCO class %d, conf >= %.2f)",
             weights, time.monotonic() - t0, PERSON_CLASS, conf)
    return model


def detect_persons(model, frame_rgb, size=DEFAULT_INFER_SIZE):
    """Count person detections in one RGB frame; return (count, best confidence).

    *size* is the inference input's long side (AutoShape letterboxes down
    to it); the saved photo keeps the frame's full resolution.
    """
    preds = model(frame_rgb, size=size).xyxy[0]  # rows: x1 y1 x2 y2 conf cls
    if len(preds) == 0:
        return 0, 0.0
    return len(preds), float(preds[:, 4].max())


class CameraError(Exception):
    """The camera could not be opened or configured."""


class Camera:
    """Persistent V4L2 capture handle via OpenCV — replaces fswebcam.

    Opened once and kept open for the whole run: the driver streams into
    a 1-frame buffer and each tick drops the stale buffered frame and
    keeps the next one, instead of paying a process spawn + device open
    per shot.  Only the worker thread may touch it (the SIGINT/torch
    gotcha); on a mid-run failure (USB hiccup) grab() releases the
    handle and lazily reopens on a later tick, so only that frame is
    counted "failed" and the process keeps running.  This cv2 build's
    V4L2 backend rejects the OPEN/READ timeout properties (passing them
    fails the whole open), so a wedged driver call is contained by the
    main-thread watchdog instead.
    """

    def __init__(self, device, resolution):
        import cv2  # deferred: keeps --help light and cv2 off the main thread

        self._cv2 = cv2
        self.device = normalize_device(device)  # fswebcam-style "-d 0" keeps working
        self.width, self.height = parse_resolution(resolution)
        self._cap = None

    def open(self):
        """Open + configure + warm up; raise CameraError on failure."""
        self.release()
        cv2 = self._cv2
        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"cannot open camera {self.device} via V4L2 "
                              f"(disconnected or not a capture device?)")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # best effort; drain makes it moot
        got = (round(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
               round(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
        if got != (self.width, self.height):
            log.warning("camera %s delivers %dx%d, not the requested %dx%d; "
                        "continuing (fswebcam used to adjust silently)",
                        self.device, got[0], got[1], self.width, self.height)
        for _ in range(CAMERA_WARMUP_GRABS):  # let auto-exposure settle
            if not cap.grab():
                cap.release()
                raise CameraError(f"{self.device} stopped delivering frames "
                                  f"during warm-up")
        self._cap = cap
        log.info("camera %s open: %dx%d MJPG (warm-up done)",
                 self.device, got[0], got[1])

    def grab(self):
        """Return the freshest BGR frame, or None if unavailable.

        Reopens first if the handle was lost; a failed reopen logs and
        returns None, so the missed frame counts as "failed" and the
        next tick retries — the interval grid is the backoff.
        """
        if self._cap is None:
            try:
                self.open()
            except CameraError as exc:
                log.warning("cannot reopen %s (%s); skipping frame",
                            self.device, exc)
                return None
        for _ in range(CAMERA_STALE_GRABS):  # drop frame buffered since last tick
            if not self._cap.grab():
                self._fail("camera stopped delivering frames")
                return None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self._fail("camera read failed")
            return None
        return frame

    def encode_jpeg(self, frame):
        """Encode one BGR frame as JPEG bytes at JPEG_QUALITY, or None."""
        ok, buf = self._cv2.imencode(".jpg", frame,
                                     [int(self._cv2.IMWRITE_JPEG_QUALITY),
                                      JPEG_QUALITY])
        return buf.tobytes() if ok else None

    def _fail(self, why):
        log.warning("capture failed (%s); skipping frame", why)
        self.release()  # next grab() reopens from scratch

    def release(self):
        """Close the handle if open; idempotent."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def load_upload_config(args):
    """Resolve the V5 upload settings from --upload-config + --remote-url.

    Returns {"url", "user", "password", "ca"} (ca may be None = verify
    against the system CAs) or raises ValueError with a user-facing reason.
    """
    path = args.upload_config
    try:
        cfg = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError(f"cannot read upload config {path} ({exc})")
    except ValueError as exc:
        raise ValueError(f"{path} is not valid JSON ({exc})")
    user, password = cfg.get("user"), cfg.get("password")
    if not user or not password:
        raise ValueError(f"{path} must define non-empty 'user' and 'password'")
    url = args.remote_url or cfg.get("url") or DEFAULT_REMOTE_URL
    parts = urlsplit(url)
    if (parts.scheme not in ("http", "https") or not parts.netloc
            or parts.query or parts.fragment):
        raise ValueError(f"upload URL must be a plain http(s)://host[:port]"
                         f"[/prefix] address, not {url!r}")
    ca = cfg.get("ca")
    if ca and not Path(ca).is_file():
        raise ValueError(f"'ca' named in {path} does not exist: {ca}")
    url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return {"url": url, "user": user, "password": password, "ca": ca}


class HttpsUploader:
    """Asynchronous, best-effort HTTPS upload of saved photos.

    The capture thread only queues a path (never blocks); this daemon
    thread does the work one photo at a time over WebDAV, which nginx's
    built-in dav module serves at the gallery URL: PUT the bytes to a
    hidden ".part" URL, then MOVE them into place — an interrupted
    transfer never leaves a partial file under its final name (the
    same atomic rename an scp-plus-mv would give, without ssh).
    PUT/MOVE and browsing sit
    behind HTTP basic auth (credentials in --upload-config), and TLS is
    pinned to the certificate named there, so the self-signed server
    cert is actually verified, not switched off.

    capture-list.txt (one name per line, served next to the photos)
    is kept by appending to a local mirror after each successful
    upload and re-uploading the whole list — once at least
    LIST_PUSH_MIN_NAMES new names and LIST_PUSH_INTERVAL seconds have
    accumulated since the last push (or LIST_PUSH_MAX_DELAY seconds,
    whichever comes first), flushed once at shutdown; a torn push
    heals on the next one.  Because the whole file is re-uploaded, the
    mirror is only pushed once synced with the server's copy: a mirror
    that could not be fetched (network down, 5xx) never clobbers the
    server's history, and names appended while unsynced are merged in
    once the server's list is reachable again.

    Each HTTP call gets UPLOAD_CALL_TIMEOUT seconds to connect/read,
    and each photo gets `timeout` seconds overall (default 30 s, then
    the upload is abandoned).  A stop request is
    noticed between calls and within one call's read timeout, and the
    whole stop stays inside UPLOADER_STOP_BUDGET — itself inside the
    main-thread watchdog's STOP_GRACE force-exit, so the final
    capture-list.txt flush is never cut off by os._exit.
    """

    def __init__(self, config, timeout, stop_event=None, list_dir=None):
        self.url = config["url"].rstrip("/")
        self.timeout = timeout
        self.list_cache = (list_dir or Path(".")) / CAPTURE_LIST
        self.queue = queue.Queue(maxsize=UPLOAD_QUEUE_MAX)
        self.counts = {"uploaded": 0, "failed": 0, "dropped": 0}
        self._stop = threading.Event()
        # The capture loop's stop event: shutting down captures means
        # shutting down uploads too, even mid-preflight.
        self._ext_stop = stop_event if stop_event is not None else threading.Event()
        self._dirty = False  # capture-list.txt mirror has changes not on the server
        self._list_synced = False  # mirror is known to match the server's list
        self._last_push = 0.0
        self._pending_names = 0  # names appended since the last successful push
        self._session = requests.Session()
        self._session.auth = (config["user"], config["password"])
        if config.get("ca"):
            self._session.verify = config["ca"]  # pin the self-signed cert
        self._session.trust_env = False  # no env proxies: talk to the host directly
        self.thread = threading.Thread(target=self._run, name="uploader",
                                       daemon=True)

    # ---- called from the capture thread ----

    def start(self):
        """Seed the capture-list.txt mirror if absent, then start the thread."""
        if self.list_cache.exists():
            self._list_synced = True
        else:
            self._seed_list()
        self.thread.start()

    def submit(self, path):
        """Queue one saved photo; never blocks, drops when the queue is full."""
        if self.stopping:
            return
        try:
            self.queue.put_nowait(path)
        except queue.Full:
            self.counts["dropped"] += 1
            log.warning("upload queue full (%d pending): dropping %s",
                        UPLOAD_QUEUE_MAX, path.name)

    def stop(self):
        """Stop the uploader; whatever is still pending is abandoned."""
        self._stop.set()
        # One shared deadline inside STOP_GRACE: there is no child process
        # to kill, an in-flight call ends within its read timeout
        # (UPLOAD_CALL_TIMEOUT), and the main-thread watchdog force-exits
        # at STOP_GRACE — a stop budget beyond that would be dead code and
        # could cut off the final capture-list.txt flush.
        deadline = time.monotonic() + UPLOADER_STOP_BUDGET
        self.thread.join(timeout=max(0.0, min(2.0, deadline - time.monotonic())))
        if self.thread.is_alive():
            self.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self.thread.is_alive():  # pragma: no cover — watchdog force-exits at STOP_GRACE
            log.warning("uploader thread failed to stop within %.0fs; "
                        "abandoning its queue", UPLOADER_STOP_BUDGET)

    @property
    def stopping(self):
        return self._stop.is_set() or self._ext_stop.is_set()

    # ---- uploader thread ----

    def _run(self):
        while not self.stopping:
            try:
                path = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._copy_one(path)
        self._push_list()  # final flush of capture-list.txt, if still dirty
        log.info("uploader stopped: %d uploaded, %d failed, %d dropped, "
                 "%d pending abandoned", self.counts["uploaded"],
                 self.counts["failed"], self.counts["dropped"],
                 self.queue.qsize())

    def _url(self, name):
        return f"{self.url}/{quote(name)}"

    def _request(self, method, url, **kw):
        """One HTTP call.  Redirects are refused so a 301 (say, HTTP->HTTPS)
        can't silently demote a PUT into a GET."""
        return self._session.request(
            method, url, allow_redirects=False,
            timeout=(UPLOAD_CALL_TIMEOUT, UPLOAD_CALL_TIMEOUT), **kw)

    def _seed_list(self, merge=False):
        """Make the local capture-list.txt mirror match the server's.

        The mirror is only ever pushed as a whole file, so it must first be
        synced with what the server holds — otherwise a run that starts
        while the list is unfetchable (network down, 5xx) would later
        overwrite the server's history with its own short mirror.  A 404
        means the server has no list yet, which is a fine starting point;
        any other failure leaves the mirror unsynced and pushes disabled
        until a later fetch succeeds.  With *merge*, names already appended
        locally while unsynced are kept (they are newer, and the photos
        behind them did upload).
        """
        try:
            r = self._request("GET", self._url(CAPTURE_LIST))
        except requests.RequestException as exc:
            log.warning("could not fetch %s/%s (%s); %s updates "
                        "stay off until it is fetchable",
                        self.url, CAPTURE_LIST, exc, CAPTURE_LIST)
            return
        if r.status_code == 200:
            remote = r.text.splitlines()
            if merge and self.list_cache.exists():
                known = set(remote)
                local = self.list_cache.read_text().splitlines()
                remote += [n for n in local if n and n not in known]
            self.list_cache.write_text("".join(n + "\n" for n in remote))
            self._list_synced = True
            log.info("synced %s with %s (%d names)",
                     self.list_cache, self.url, len(remote))
        elif r.status_code == 404:
            if not self.list_cache.exists():
                self.list_cache.touch()
            self._list_synced = True  # server has no list yet: start fresh
            log.info("%s has no %s yet; starting a fresh one", self.url,
                     CAPTURE_LIST)
        else:
            log.warning("HTTP %d fetching %s/%s; %s updates stay "
                        "off until it is fetchable",
                        r.status_code, self.url, CAPTURE_LIST, CAPTURE_LIST)

    def _copy_one(self, path):
        name = path.name
        if not path.is_file():
            log.warning("upload of %s skipped: file is gone", name)
            self.counts["failed"] += 1
            return
        deadline = time.monotonic() + self.timeout
        while not self.stopping:
            if self._try_upload(path, name):
                self.counts["uploaded"] += 1
                log.info("uploaded %s to %s", name, self.url)
                self._record(name)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.warning("upload of %s not done within %.0fs; abandoned",
                            name, self.timeout)
                self.counts["failed"] += 1
                return
            # Brief, stop-aware backoff before the retry.
            wake_at = time.monotonic() + min(1.0, remaining)
            while not self.stopping and time.monotonic() < wake_at:
                time.sleep(0.25)
        self.counts["failed"] += 1  # abandoned because the program is stopping

    def _try_upload(self, path, name):
        """One PUT .part + MOVE attempt; True iff the photo is now in place."""
        part = self._url(f".{name}.part")  # hidden until it is complete
        try:
            with path.open("rb") as body:
                put = self._request("PUT", part, data=body,
                                    headers={"Content-Type": "image/jpeg"})
            if put.status_code // 100 != 2:
                log.warning("PUT %s: HTTP %d", name, put.status_code)
                return False
            move = self._request("MOVE", part, headers={
                "Destination": self._url(name), "Overwrite": "T"})
            if move.status_code // 100 != 2:
                log.warning("MOVE %s: HTTP %d", name, move.status_code)
                return False
            return True
        except requests.RequestException as exc:
            log.warning("upload of %s failed (%s); retrying", name, exc)
            return False

    def _record(self, name):
        """Note the upload in the capture-list.txt mirror and push it if due."""
        with self.list_cache.open("a") as f:
            f.write(name + "\n")
        self._dirty = True
        self._pending_names += 1
        # Dual threshold: a busy stretch pushes at most one whole-file PUT
        # per LIST_PUSH_INTERVAL *and* only after LIST_PUSH_MIN_NAMES new
        # names, while a slow trickle still gets flushed within
        # LIST_PUSH_MAX_DELAY — the list never stays dirty for long.
        since_push = time.monotonic() - self._last_push
        if ((self._pending_names >= LIST_PUSH_MIN_NAMES
             and since_push >= LIST_PUSH_INTERVAL)
                or since_push >= LIST_PUSH_MAX_DELAY):
            self._push_list()

    def _push_list(self):
        """Re-upload the whole capture-list.txt; heals any torn earlier push."""
        if not self._dirty:
            return
        if not self._list_synced:
            self._seed_list(merge=True)  # maybe the server is back
            if not self._list_synced:
                log.info("not re-uploading %s: the server's copy could not "
                         "be fetched (it stays unchanged; the photos are "
                         "already up)", CAPTURE_LIST)
                return
        try:
            r = self._request("PUT", self._url(CAPTURE_LIST),
                              data=self.list_cache.read_bytes(),
                              headers={"Content-Type": "text/plain; charset=utf-8"})
            if r.status_code // 100 == 2:
                self._dirty = False
                self._last_push = time.monotonic()
                self._pending_names = 0
            else:
                log.warning("%s: HTTP %d while updating %s",
                            self.url, r.status_code, CAPTURE_LIST)
        except (requests.RequestException, OSError) as exc:
            log.warning("could not re-upload %s (%s)", CAPTURE_LIST, exc)


# Files this program names: YYYY-MM-DD_HHMMSS.jpg with an optional -N
# same-second suffix.  Retention only ever touches matches — anything
# else living in the output dir (the user's own files) is left alone.
CAPTURE_NAME_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}(-\d+)?\.jpg")


def purge_old_captures(output_dir, retain_days):
    """Delete captures older than *retain_days* days; return (count, bytes).

    Only top-level files matching CAPTURE_NAME_RE are considered and
    subdirectories are never descended into; a per-file failure logs a
    warning and moves on.
    """
    cutoff = time.time() - retain_days * 86400
    count = freed = 0
    try:
        entries = list(output_dir.iterdir())
    except OSError as exc:
        log.warning("could not list %s for retention (%s)", output_dir, exc)
        return 0, 0
    for entry in entries:
        if not entry.is_file() or not CAPTURE_NAME_RE.fullmatch(entry.name):
            continue
        try:
            stat = entry.stat()
            if stat.st_mtime >= cutoff:
                continue
            entry.unlink()
            count += 1
            freed += stat.st_size
        except OSError as exc:
            log.warning("could not delete %s (%s)", entry, exc)
    return count, freed


def unused_capture_path(output_dir, stamp):
    """An unused path for this stamp: '<stamp>.jpg', then '<stamp>-2.jpg', ...

    Seconds-precision names collide when --interval < 1 s; those frames
    get a -N suffix instead of silently overwriting each other.
    """
    candidate = output_dir / f"{stamp}.jpg"
    n = 2
    while candidate.exists():
        candidate = output_dir / f"{stamp}-{n}.jpg"
        n += 1
    return candidate


def capture_one(camera, output_dir, model, uploader=None,
                infer_size=DEFAULT_INFER_SIZE):
    """Capture one frame; return 'saved', 'discarded', or 'failed'.

    A frame saved because a person was detected is also queued for the
    V4/V5 remote upload (when *uploader* is given).  Nothing touches
    disk unless the frame will be kept.
    """
    frame = camera.grab()
    if frame is None:
        return "failed"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    final_path = unused_capture_path(output_dir, stamp)
    tmp_path = final_path.with_name(f".{final_path.stem}.partial.jpg")
    if model is not None:
        try:
            count, best = detect_persons(model, frame[:, :, ::-1],
                                         infer_size)  # BGR -> RGB
        except Exception as exc:
            log.warning("detection failed (%s); discarding frame", exc)
            return "failed"
        if count == 0:
            log.info("no person in %s; discarded", final_path.name)
            return "discarded"
    data = camera.encode_jpeg(frame)
    if data is None:
        log.warning("JPEG encode failed; skipping frame")
        return "failed"
    try:
        tmp_path.write_bytes(data)
        if tmp_path.stat().st_size == 0:
            raise OSError("empty encode")
        tmp_path.replace(final_path)
    except OSError as exc:
        log.warning("could not write %s (%s); skipping frame", tmp_path, exc)
        tmp_path.unlink(missing_ok=True)
        return "failed"
    if model is None:
        log.info("saved %s", final_path)
        return "saved"
    log.info("person detected (conf %.2f) in %s; saved", best, final_path.name)
    if uploader is not None:
        uploader.submit(final_path)
    return "saved"


def run_loop(args, stop_event):
    """Worker thread: load the detector, then capture/detect until stopped.

    Returns the process exit code.
    """
    # The max-runtime clock starts here, model load included (the spec's
    # "runs for more than N hours" self-termination), so it stays aligned with the
    # main-thread watchdog, which measures from thread start; otherwise
    # the watchdog's force-exit could fire while this thread is still
    # draining its final uploads just after the deadline.
    start = time.monotonic()
    model = None
    if not args.no_detect:
        if not args.weights.is_file():
            log.error("YOLOv5 weights not found: %s", args.weights)
            return 1
        try:
            model = load_detector(args.weights, args.conf)
        except Exception as exc:
            log.error("failed to load YOLOv5 weights (%s)", exc)
            return 1
    upload_cfg = None
    if model is not None and not args.no_upload:
        try:
            upload_cfg = load_upload_config(args)
        except ValueError as exc:
            log.error("%s", exc)
            return 1
    # Opened here, on the worker thread (the SIGINT/torch gotcha — the
    # main thread must not call into cv2), after the upload config is
    # known good, so a setup error never leaves an open camera behind.
    camera = Camera(args.device, args.resolution)
    try:
        camera.open()
    except (CameraError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    uploader = None
    if upload_cfg is not None:
        uploader = HttpsUploader(upload_cfg, args.upload_timeout, stop_event,
                                 list_dir=args.output_dir)
    log.info("capturing %s from %s every %.0fs into %s; person detection %s; "
             "uploads %s; Ctrl+C to stop, self-terminates after %.1f h or "
             "when free disk drops below %.1f GiB%s",
             args.resolution, args.device, args.interval, args.output_dir,
             "off" if model is None else f"on (infer size {args.infer_size})",
             "off" if uploader is None
             else f"to {uploader.url} (async, "
                  f"{args.upload_timeout:.0f}s timeout)",
             args.max_runtime / 3600.0, args.min_free_gib,
             "" if args.retain_days <= 0
             else f", keeps {args.retain_days:.1f} d of captures")
    if uploader is not None:
        uploader.start()
    if args.retain_days > 0:
        count, freed = purge_old_captures(args.output_dir, args.retain_days)
        log.info("retention: %d old captures deleted, %.1f MB freed "
                 "(keeping %.1f days)", count, freed / 1e6, args.retain_days)

    # Grid anchored at the first capture (post-load), so a slow load
    # doesn't make the first two frames fire back to back.
    next_tick = time.monotonic()
    counts = {"saved": 0, "discarded": 0, "failed": 0}
    try:
        while not stop_event.is_set():
            if time.monotonic() - start >= args.max_runtime:
                log.info("max runtime %.1f h reached: %d saved, %d discarded, "
                         "%d failed; exiting",
                         args.max_runtime / 3600.0, counts["saved"],
                         counts["discarded"], counts["failed"])
                return 0
            if args.min_free_gib > 0:
                free = free_gib(args.output_dir)
                if free < args.min_free_gib and args.retain_days > 0:
                    # Retention first: deleting our own old captures may
                    # lift the disk back above the floor by itself.
                    count, freed = purge_old_captures(args.output_dir,
                                                      args.retain_days)
                    if count:
                        log.info("low disk: retention freed %d captures "
                                 "(%.1f MB)", count, freed / 1e6)
                    free = free_gib(args.output_dir)
                if free < args.min_free_gib:
                    log.error("free disk on %s down to %.2f GiB, below the "
                              "%.1f GiB floor: %d saved, %d discarded, "
                              "%d failed; exiting",
                              args.output_dir, free, args.min_free_gib,
                              counts["saved"], counts["discarded"],
                              counts["failed"])
                    return EXIT_LOW_DISK
            counts[capture_one(camera, args.output_dir, model, uploader,
                                args.infer_size)] += 1
            # Fixed grid from start: no drift when captures are slow.
            next_tick += args.interval
            delay = next_tick - time.monotonic()
            if delay < 0:
                next_tick = time.monotonic()  # fell behind; resync, don't burst
                continue
            # Sleep in short slices so a stop request is noticed mid-interval.
            wake_at = time.monotonic() + delay
            while not stop_event.is_set():
                remaining = wake_at - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.5, remaining))
        log.info("stopped: %d saved, %d discarded, %d failed; exiting",
                 counts["saved"], counts["discarded"], counts["failed"])
        return 0
    finally:
        if uploader is not None:
            uploader.stop()
        camera.release()


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    device = normalize_device(args.device)
    if not Path(device).exists():
        log.error("camera device %s not found; list cameras with: v4l2-ctl --list-devices",
                  device)
        return 1
    # Created here, before the worker starts, so both the preflight check
    # below and the watchdog can stat the output filesystem right away.
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("cannot create output dir %s (%s)", args.output_dir, exc)
        return 1
    if args.min_free_gib > 0:
        free = free_gib(args.output_dir)
        if free < args.min_free_gib and args.retain_days > 0:
            # Same policy as the in-loop check: try retention before dying.
            count, freed = purge_old_captures(args.output_dir, args.retain_days)
            if count:
                log.info("low disk: retention freed %d captures (%.1f MB)",
                         count, freed / 1e6)
            free = free_gib(args.output_dir)
        if free < args.min_free_gib:
            log.error("only %.2f GiB free on %s, below the %.1f GiB floor; "
                      "exiting without capturing",
                      free, args.output_dir, args.min_free_gib)
            return EXIT_LOW_DISK

    stop_event = threading.Event()
    stop_requested_at = [0.0]

    def _stop(signum, _frame):
        if stop_event.is_set():
            log.info("second stop signal; exiting immediately")
            os._exit(130)
        stop_event.set()
        stop_requested_at[0] = time.monotonic()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    result = [1]

    def _worker():
        try:
            result[0] = run_loop(args, stop_event)
        except Exception:
            log.exception("unexpected error in capture worker")
            result[0] = 1

    thread = threading.Thread(target=_worker, name="capture", daemon=True)
    thread.start()
    started = time.monotonic()
    low_disk_seen_at = None  # when the watchdog first saw free disk below the floor
    while thread.is_alive():
        thread.join(0.5)
        now = time.monotonic()
        # Belt and braces: the 168 h limit holds even if the worker wedges,
        # and a requested stop always goes through within STOP_GRACE.
        stuck = (now - started > args.max_runtime + STOP_GRACE
                 or (stop_event.is_set()
                     and now - stop_requested_at[0] > STOP_GRACE))
        # Same for the V3 disk floor: a healthy worker notices on its own
        # within one interval and exits with EXIT_LOW_DISK, so the watchdog
        # only forces the exit when the floor has been crossed for longer
        # than STOP_GRACE without the worker stopping (wedged in native code).
        disk_low = (args.min_free_gib > 0
                    and free_gib(args.output_dir) < args.min_free_gib)
        if disk_low:
            if low_disk_seen_at is None:
                low_disk_seen_at = now
        else:
            low_disk_seen_at = None
        if stuck or (disk_low and now - low_disk_seen_at > STOP_GRACE):
            log.warning("capture thread stuck (low disk: %s); forcing exit",
                        disk_low)
            os._exit(EXIT_LOW_DISK if disk_low else 0)
    return result[0]


if __name__ == "__main__":
    sys.exit(main())
