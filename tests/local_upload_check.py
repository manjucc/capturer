#!/usr/bin/env python3
"""Local end-to-end check of capture.py's HttpsUploader (V5).

Stands in for manjucc: a tiny HTTPS server that mimics the nginx pieces
the uploader talks to — basic auth, WebDAV PUT, MOVE with a Destination
header, and GET for files/directory listings — then drives the real
HttpsUploader against it over a self-signed, pinned certificate:

  1. uploads three photos: each must land under its final name with no
     .part left behind, and capture-list.txt must gain exactly those names
  2. restart with no local list mirror: it must be re-seeded from the
     server's capture-list.txt, and a fourth upload must extend it
  3. wrong password: every upload must fail within the upload timeout
     and nothing must reach the server
  4. list safety: an unfetchable server list is never clobbered
  5. a scripted PUT 500 (and a 404 empty start) heals via the retry ladder
  6. a scripted MOVE 500 heals too: the leftover .part is reused, not leaked
  7. a 301 on PUT is not followed; a wrong pinned CA fails the handshake
  8. queue accounting: a full queue drops, a stop abandons in-flight work

Run:  python3 tests/local_upload_check.py
"""

import logging
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import base64
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from capture import HttpsUploader, UPLOAD_QUEUE_MAX  # noqa: E402

PASS = 0
FAIL = 0


def check(label, ok):
    global PASS, FAIL
    print(("  ok  " if ok else "  FAIL") + f"  {label}")
    PASS, FAIL = PASS + (1 if ok else 0), FAIL + (1 if not ok else 0)


class DavHandler(BaseHTTPRequestHandler):
    """Just enough of nginx's dav + autoindex + basic auth for the test."""

    protocol_version = "HTTP/1.1"
    user, password = "u", "p"
    root = None  # set per run
    fail = {"put": 0, "move": 0}  # fault injection: scripted failures left
    redirect = False              # PUT answers 301 + Location instead

    def _authed(self):
        want = "Basic " + base64.b64encode(
            f"{self.user}:{self.password}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self._body()  # drain the body so keep-alive stays in sync
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        return True

    def _local(self, url):
        """Map a request/destination URL to a path inside root (or None)."""
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        local = (self.root / path.lstrip("/")).resolve()
        return local if self.root in local.parents or local == self.root else None

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _reply(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):
        if not self._authed():
            return
        if DavHandler.redirect:
            self._body()  # drain so keep-alive stays in sync
            self.send_response(301)
            self.send_header("Location", "https://example.invalid/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if DavHandler.fail["put"] > 0:
            DavHandler.fail["put"] -= 1
            self._body()
            return self._reply(500)
        local = self._local(self.path)
        if local is None:
            return self._reply(403)
        local.write_bytes(self._body())
        self._reply(201)

    def do_MOVE(self):
        if not self._authed():
            return
        if DavHandler.fail["move"] > 0:
            DavHandler.fail["move"] -= 1
            return self._reply(500)
        src, dst = self._local(self.path), self._local(self.headers.get("Destination", ""))
        if src is None or dst is None:
            return self._reply(403)
        if not src.is_file():
            return self._reply(404)
        src.rename(dst)
        self._reply(204)

    def do_GET(self):
        if not self._authed():
            return
        local = self._local(self.path)
        if local is None:
            return self._reply(403)
        if local.is_file():
            return self._reply(200, local.read_bytes())
        if local.is_dir():
            names = "".join(f"<li>{p.name}</li>" for p in sorted(local.iterdir()))
            return self._reply(200, f"<ul>{names}</ul>".encode(), "text/html")
        self._reply(404)

    def log_message(self, fmt, *args):  # keep the test output readable
        pass


def make_cert(dirpath):
    crt, key = dirpath / "test.crt", dirpath / "test.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-sha256", "-days", "30", "-nodes",
                    "-subj", "/CN=localhost",
                    "-keyout", str(key), "-out", str(crt),
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    return crt, key


def wait_for(cond, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.1)
    return False


def drive_uploader(url, ca, outdir, photos, timeout=10.0, user="u", password="p"):
    stop = threading.Event()
    uploader = HttpsUploader({"url": url, "user": user, "password": password,
                              "ca": str(ca)}, timeout, stop_event=stop,
                             list_dir=outdir)
    uploader.start()
    for photo in photos:
        uploader.submit(photo)
    settled = wait_for(lambda: uploader.counts["uploaded"] + uploader.counts["failed"]
                       >= len(photos))
    uploader.stop()
    return uploader, settled


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    tmp = Path(tempfile.mkdtemp(prefix="v5check-"))
    webroot, out1, out2 = tmp / "root", tmp / "out1", tmp / "out2"
    webroot.mkdir(), out1.mkdir(), out2.mkdir()
    crt, key = make_cert(tmp)

    DavHandler.root = webroot.resolve()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), DavHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{httpd.server_address[1]}"
    print(f"stub server on {url}, webroot {webroot}")

    photos = []
    stamps = ["2026-11-12_100001", "2026-11-12_100004", "2026-11-12_100007"]
    for s in stamps:
        p = out1 / f"{s}.jpg"
        p.write_bytes(b"\xff\xd8 JFIF test image " + s.encode() + b"\xff\xd9")
        photos.append(p)

    print("\n[1] three uploads + capture-list.txt")
    uploader, ok = drive_uploader(url, crt, out1, photos)
    check("all three reported uploaded", ok and uploader.counts["uploaded"] == 3)
    check("photos stored under final names",
          all((webroot / f"{s}.jpg").is_file() for s in stamps))
    check("no .part leftovers",
          not list(webroot.glob(".*.part")))
    remote_list = (webroot / "capture-list.txt").read_text().splitlines()
    check("capture-list.txt has exactly the three names, in order",
          remote_list == [f"{s}.jpg" for s in stamps])
    check("local mirror matches", (out1 / "capture-list.txt").read_text().splitlines()
          == remote_list)

    print("\n[2] restart without a local mirror (re-seed from server)")
    photo4 = out2 / "2026-11-12_100010.jpg"
    photo4.write_bytes(b"\xff\xd8 JFIF fourth \xff\xd9")
    uploader2, ok2 = drive_uploader(url, crt, out2, [photo4])
    mirror = (out2 / "capture-list.txt").read_text().splitlines()
    check("fourth photo uploaded", ok2 and uploader2.counts["uploaded"] == 1)
    check("mirror was seeded then extended",
          mirror == [f"{s}.jpg" for s in stamps] + ["2026-11-12_100010.jpg"])
    check("server list matches", (webroot / "capture-list.txt").read_text()
          .splitlines() == mirror)

    print("\n[3] wrong password: fails within the upload timeout")
    photo5 = out2 / "2026-11-12_100013.jpg"
    photo5.write_bytes(b"\xff\xd8 JFIF never arrives \xff\xd9")
    t0 = time.monotonic()
    uploader3, ok3 = drive_uploader(url, crt, out2, [photo5], timeout=3.0,
                                    password="wrong")
    took = time.monotonic() - t0
    check("abandoned as failed (not uploaded)",
          uploader3.counts["failed"] == 1
          and uploader3.counts["uploaded"] == 0)
    check(f"took ~3s (was {took:.1f}s), not the full retry ladder", 2.5 <= took < 12)
    check("nothing stored", not (webroot / "2026-11-12_100013.jpg").exists()
          and not list(webroot.glob(".*100013*")))

    print("\n[4] list safety: unfetchable server list is never clobbered")
    out3 = tmp / "out3"
    out3.mkdir()
    up4 = HttpsUploader({"url": url, "user": "u", "password": "p",
                         "ca": str(crt)}, 10.0, list_dir=out3)

    class Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    calls = {"get": 0, "put": []}
    remote_list = ["2026-11-11_201048.jpg"]  # pretend the server has history

    def fake_request(method, url, **kw):
        if method == "GET":
            calls["get"] += 1
            return Resp(500) if calls["get"] == 1 else Resp(200, "".join(
                n + "\n" for n in remote_list))
        calls["put"].append(kw.get("data", b"").decode())
        return Resp(204)

    up4._request = fake_request
    up4._seed_list()  # server list unfetchable -> must stay unsynced
    check("seed failure leaves the mirror unsynced", not up4._list_synced)
    up4._record("2026-11-12_100020.jpg")  # a photo uploads anyway
    up4._push_list()  # retry seed (now OK), merge local name in, push
    check("re-seed merged server history + new name",
          out3.joinpath("capture-list.txt").read_text().splitlines()
          == remote_list + ["2026-11-12_100020.jpg"])
    check("push happened once, with the merged content",
          calls["put"] == ["2026-11-11_201048.jpg\n2026-11-12_100020.jpg\n"]
          and up4._list_synced and not up4._dirty)

    # and the strict case: still unfetchable -> no push at all
    up5 = HttpsUploader({"url": url, "user": "u", "password": "p",
                         "ca": str(crt)}, 10.0, list_dir=out3)
    up5._request = lambda method, url, **kw: Resp(500)
    up5._seed_list()
    up5._record("2026-11-12_100023.jpg")
    before = (out3 / "capture-list.txt").read_text()
    up5._push_list()
    check("no push while unsynced (server history untouched)",
          calls["put"] == ["2026-11-11_201048.jpg\n2026-11-12_100020.jpg\n"]
          and (out3 / "capture-list.txt").read_text() == before)

    print("\n[5] scripted PUT 500 heals; 404 starts a fresh list")
    out5 = tmp / "out5"
    out5.mkdir()
    (webroot / "capture-list.txt").unlink()  # server has no list -> 404 start
    photo6 = out5 / "2026-11-12_100030.jpg"
    photo6.write_bytes(b"\xff\xd8 JFIF flaky-put \xff\xd9")
    DavHandler.fail["put"] = 1
    uploader5, ok5 = drive_uploader(url, crt, out5, [photo6], timeout=6.0)
    check("uploaded after one scripted PUT 500",
          ok5 and uploader5.counts["uploaded"] == 1)
    check("404 start: fresh mirror holds only the new photo",
          (out5 / "capture-list.txt").read_text().splitlines()
          == ["2026-11-12_100030.jpg"])
    check("no .part leftover", not list(webroot.glob(".*100030*")))

    print("\n[6] scripted MOVE 500 heals: the .part is reused, not leaked")
    out6 = tmp / "out6"
    out6.mkdir()
    photo7 = out6 / "2026-11-12_100033.jpg"
    photo7.write_bytes(b"\xff\xd8 JFIF flaky-move \xff\xd9")
    DavHandler.fail["move"] = 1
    uploader6, ok6 = drive_uploader(url, crt, out6, [photo7], timeout=6.0)
    check("uploaded after one scripted MOVE 500",
          ok6 and uploader6.counts["uploaded"] == 1)
    check("final name present, no .part leftover",
          (webroot / "2026-11-12_100033.jpg").is_file()
          and not list(webroot.glob(".*100033*")))

    print("\n[7] a 301 is not followed; a wrong pinned CA fails the handshake")
    out7 = tmp / "out7"
    out7.mkdir()
    photo8 = out7 / "2026-11-12_100036.jpg"
    photo8.write_bytes(b"\xff\xd8 JFIF redirect \xff\xd9")
    DavHandler.redirect = True
    uploader7, ok7 = drive_uploader(url, crt, out7, [photo8], timeout=2.0)
    DavHandler.redirect = False
    check("301 on PUT is not followed: upload abandoned",
          uploader7.counts["failed"] == 1 and uploader7.counts["uploaded"] == 0)
    check("nothing stored", not (webroot / "2026-11-12_100036.jpg").exists())

    tmp2 = tmp / "second-ca"
    tmp2.mkdir()
    crt2, key2 = make_cert(tmp2)
    webroot2 = tmp / "root2"
    webroot2.mkdir()

    class DavHandler2(DavHandler):
        root = webroot2.resolve()

    httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), DavHandler2)
    ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx2.load_cert_chain(crt2, key2)
    httpd2.socket = ctx2.wrap_socket(httpd2.socket, server_side=True)
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    url2 = f"https://127.0.0.1:{httpd2.server_address[1]}"
    out8 = tmp / "out8"
    out8.mkdir()
    photo9 = out8 / "2026-11-12_100039.jpg"
    photo9.write_bytes(b"\xff\xd8 JFIF wrong-ca \xff\xd9")
    uploader8, ok8 = drive_uploader(url2, crt, out8, [photo9], timeout=2.0)
    check("wrong pinned CA: handshake fails, upload abandoned",
          uploader8.counts["failed"] == 1 and uploader8.counts["uploaded"] == 0)
    check("nothing reached the second server", not any(webroot2.iterdir()))

    print("\n[8] queue accounting: full queue drops, stop abandons in-flight")
    dummy = out8 / "2026-11-12_100040.jpg"
    dummy.write_bytes(b"x" * 16)
    stop8 = threading.Event()
    up8 = HttpsUploader({"url": url, "user": "u", "password": "p",
                         "ca": str(crt)}, 10.0, stop_event=stop8, list_dir=out8)
    for _ in range(UPLOAD_QUEUE_MAX + 1):  # never started: the queue just fills
        up8.submit(dummy)
    check(f"queue full: {up8.counts['dropped']} dropped, queue capped",
          up8.counts["dropped"] == 1 and up8.queue.qsize() == UPLOAD_QUEUE_MAX)

    up9 = HttpsUploader({"url": url, "user": "u", "password": "p",
                         "ca": str(crt)}, 10.0, list_dir=out8)

    def slow_fail(_path, _name):
        time.sleep(0.5)  # still "in flight" when stop() lands
        return False

    up9._try_upload = slow_fail
    up9.start()
    up9.submit(dummy)
    time.sleep(0.15)
    up9.stop()
    check("stop mid-upload: the in-flight photo counts as failed",
          up9.counts["failed"] == 1 and up9.counts["uploaded"] == 0)

    httpd2.shutdown()
    httpd.shutdown()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
