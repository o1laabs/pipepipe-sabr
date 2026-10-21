#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PipePipe-style feed app: production server.

Serves a mobile feed UI backed by YouTube data pulled through the SABR client in
../sabr.py. Everything the UI shows comes from this API -- no hardcoded rows.

  GET  /api/feed                 curated feed (cached)
  GET  /api/search?q=            search results
  GET  /api/video/<id>           metadata + available renditions
  GET  /api/related/<id>         related videos (from search)
  GET  /stream/<id>?itag=NNN     growing MP4; blocks on reads until bytes arrive
  GET  /api/state                likes / bookmarks / progress
  POST /api/state                merge state
  POST /api/download             full download job
  GET  /api/download/<job>       job progress
  GET  /                         the app (static, from ./web)
"""
import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sabr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
CACHE = os.path.join(HERE, "cache", "default")
DATA = os.path.join(HERE, "data")
STATE_FILE = os.path.join(DATA, "state.json")
CACHE_LIMIT = 1_600 * 1024 * 1024        # keep the device from filling up
FEED_QUERIES = ["design documentary", "how things are made", "motion design reel"]
FEED_TTL = 900
FEED_FILE = os.path.join(DATA, "feed.json")

os.makedirs(CACHE, exist_ok=True)
os.makedirs(DATA, exist_ok=True)

_lock = threading.Lock()
_meta_cache = {}
_feed_cache = {"at": 0, "items": []}


# --------------------------------------------------------------------- state
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except Exception:
            return {"likes": {}, "saved": {}, "progress": {}}

    def get(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def merge(self, patch):
        with self.lock:
            for key in ("likes", "saved", "progress"):
                if isinstance(patch.get(key), dict):
                    self.data.setdefault(key, {}).update(patch[key])
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.data, fh)
            os.replace(tmp, self.path)
            return json.loads(json.dumps(self.data))


STORE = Store(STATE_FILE)


# ------------------------------------------------------------------ metadata
def video_meta(video_id, pot=None):
    """Metadata + renditions, cached. Raises SabrError with a machine code."""
    now = time.time()
    hit = _meta_cache.get(video_id)
    if hit and now - hit[0] < 1800:
        return hit[1]
    pr, client, cid, cver = sabr.player_response_matrix(video_id)
    status = (pr.get("playabilityStatus") or {}).get("status")
    sd = pr.get("streamingData") or {}
    if not sd:
        raise sabr.SabrError("UNAVAILABLE:%s" % (status or "unknown"))
    if client == "mweb":
        pr, _ = sabr.decode_mweb_urls(pr)
        sd = pr.get("streamingData") or {}
    vd = pr.get("videoDetails") or {}
    fmts = sd.get("adaptiveFormats") or []
    rend = []
    for f in fmts:
        mime = f.get("mimeType") or ""
        h = int(f.get("height") or 0)
        if h and mime.startswith("video/"):
            mm = re.search(r'codecs="([^"]+)"', mime)
            rend.append({"itag": int(f.get("itag")), "height": h,
                         "bitrate": int(f.get("bitrate") or 0),
                         "mime": mime.split(";")[0],
                         "codecs": mm.group(1) if mm else ""})
    seen, uniq = set(), []
    for r in sorted(rend, key=lambda r: (r["height"], r["bitrate"])):
        key = (r["height"], "mp4" in r["mime"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    audio = sabr.pick_audio(pr, mp4_only=True)
    meta = {
        "id": video_id,
        "title": vd.get("title") or "",
        "author": vd.get("author") or "",
        "channelId": vd.get("channelId") or "",
        "duration": int(vd.get("lengthSeconds") or 0),
        "views": int(vd.get("viewCount") or 0),
        "thumb": "https://i.ytimg.com/vi/%s/maxresdefault.jpg" % video_id,
        "renditions": uniq,
        "audioItag": audio,
        "client": {"name": client, "id": cid, "version": cver},
    }
    _meta_cache[video_id] = (now, meta)
    return meta


def search_items(query, limit=12):
    rows = sabr.run_search(query, limit=limit) if False else _search(query, limit)
    return rows


def _search(query, limit):
    body = {"context": {"client": {"clientName": "WEB",
                                   "clientVersion": "2.20261020.00.00",
                                   "hl": "en", "gl": "US"}},
            "query": query}
    st, hd, raw = sabr.http("https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                            data=json.dumps(body).encode(),
                            headers={"Content-Type": "application/json",
                                     "User-Agent": sabr.DESKTOP_UA,
                                     "X-YouTube-Client-Name": "1",
                                     "X-YouTube-Client-Version": "2.20261020.00.00"})
    data = json.loads(raw)
    out = []

    def text_of(node):
        if not isinstance(node, dict):
            return ""
        if "simpleText" in node:
            return node["simpleText"]
        return "".join(r.get("text", "") for r in node.get("runs", []) or [])

    def walk(node):
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            if "videoRenderer" in node:
                v = node["videoRenderer"]
                vid = v.get("videoId")
                if vid:
                    dur = text_of(v.get("lengthText"))
                    secs = 0
                    if dur.count(":") == 1:
                        m, s = dur.split(":")
                        secs = int(m) * 60 + int(s)
                    elif dur.count(":") == 2:
                        h, m, s = dur.split(":")
                        secs = int(h) * 3600 + int(m) * 60 + int(s)
                    out.append({
                        "id": vid,
                        "title": text_of(v.get("title")),
                        "author": text_of(v.get("ownerText")),
                        "duration": secs,
                        "views": _views(text_of(v.get("viewCountText"))),
                        "thumb": "https://i.ytimg.com/vi/%s/maxresdefault.jpg" % vid,
                    })
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out[:limit]


def _views(text):
    m = re.search(r"([\d.,]+)\s*([KMB])?", text or "")
    if not m:
        return 0
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((m.group(2) or "").upper(), 1)
    return int(n * mult)


# -------------------------------------------------------------------- streams
class Stream:
    """One growing MP4 for (video_id, itag); readers block until bytes exist."""

    def __init__(self, video_id, itag, path, total):
        self.video_id = video_id
        self.itag = itag
        self.path = path
        self.total = total
        self.written = 0
        self.done = False
        self.error = None
        self.cond = threading.Condition()
        self.fh = None
        self.segments = []      # [{seq, start, dur, offset, len}]
        self.init_len = 0

    def append(self, data):
        with self.cond:
            self.fh.write(data)
            self.fh.flush()
            self.written += len(data)
            self.cond.notify_all()

    def finish(self, error=None):
        with self.cond:
            self.done = True
            self.error = error
            self.cond.notify_all()
            try:
                self.fh.close()
            except Exception:
                pass


STREAMS = {}
STREAMS_LOCK = threading.Lock()


def cache_size():
    total = 0
    for name in os.listdir(CACHE):
        try:
            total += os.path.getsize(os.path.join(CACHE, name))
        except OSError:
            pass
    return total


def evict_if_needed(keep=None):
    if cache_size() <= CACHE_LIMIT:
        return
    files = []
    for name in os.listdir(CACHE):
        p = os.path.join(CACHE, name)
        if p == keep or not os.path.isfile(p):
            continue
        files.append((os.path.getmtime(p), p))
    for _, p in sorted(files):
        try:
            os.remove(p)
        except OSError:
            pass
        if cache_size() <= CACHE_LIMIT:
            break


def start_stream(video_id, itag):
    key = (video_id, int(itag))
    with STREAMS_LOCK:
        st = STREAMS.get(key)
        if st:
            return st
        meta = video_meta(video_id)
        path = os.path.join(CACHE, "%s-%s.mp4" % (video_id, itag))
        total = 0
        for r in meta["renditions"]:
            if r["itag"] == int(itag):
                total = r["bitrate"] * max(1, meta["duration"]) // 8
        st = Stream(video_id, itag, path, total)
        STREAMS[key] = st

    def worker():
        try:
            st.fh = open(st.path, "ab")
            if st.fh.tell() > 0:
                st.fh.seek(0)
                st.fh.truncate(0)
            pr = sabr.player_response(video_id, client=meta["client"]["name"])
            if meta["client"]["name"] == "mweb":
                pr, _ = sabr.decode_mweb_urls(pr)
            fmt = sabr.pick_formats(pr, itag)
            init, _ = sabr.fetch_init_with_fallback(video_id, fmt, None)
            st.append(init)
            st.init_len = len(init)
            try:
                st.total = int(fmt.get("contentLength") or 0) or st.total
            except Exception:
                pass
            is_audio = "audio/" in (fmt.get("mimeType") or "")
            sess = sabr.SabrSession(pr["streamingData"]["serverAbrStreamingUrl"],
                                    sabr.ustreamer_config(pr),
                                    fmt if is_audio else None,
                                    None if is_audio else fmt,
                                    po_token=sabr.resolve_pot("auto", video_id),
                                    client_id=meta["client"]["id"],
                                    client_version=meta["client"]["version"],
                                    verbose=False)
            sess.want_itag = int(itag)
            sess.pot_refresher = lambda force=False: sabr.pot_provider().get(video_id, force=force)
            def sink(seg, tmp):
                data = tmp.read()
                with st.cond:
                    st.segments.append({"seq": seg["sequenceNumber"],
                                        "start": seg["startMs"], "dur": seg["durationMs"],
                                        "offset": st.written, "len": len(data)})
                    st.fh.write(data)
                    st.fh.flush()
                    st.written += len(data)
                    st.cond.notify_all()

            sess.run(sink, wall_budget=3600)
            st.finish()
            evict_if_needed(keep=st.path)
        except Exception as exc:                      # noqa: BLE001
            st.finish(error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return st


# --------------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # --- helpers -----------------------------------------------------------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_error_json(self, status, code, message=""):
        self.send_json({"error": code, "message": message}, status)

    def read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    # --- routing -----------------------------------------------------------
    def do_HEAD(self):
        self.route(head=True)

    def do_GET(self):
        self.route(head=False)

    def do_POST(self):
        self.route(head=False)

    def route(self, head):
        try:
            path, _, qs = self.path.partition("?")
            query = {}
            for k, v in re.findall(r"([^&=]+)=([^&]*)", qs):
                query[unquote(k)] = unquote(v)

            if path.startswith("/api/"):
                return self.api(path, query)
            if path.startswith("/stream/"):
                return self.stream(path.split("/")[2], query, head)
            if path.startswith("/init/"):
                return self.segment_bytes(path.split("/")[2], query, init_only=True)
            if path.startswith("/seg/"):
                parts = path.split("/")
                return self.segment_bytes(parts[2], query, seq=int(parts[3]))
            return self.static(path, head)
        except sabr.SabrError as exc:
            msg = str(exc)
            code = "UNAVAILABLE" if "UNAVAILABLE" in msg else "UPSTREAM"
            self.send_error_json(502, code, msg[:300])
        except Exception as exc:                      # noqa: BLE001
            traceback.print_exc()
            self.send_error_json(500, "INTERNAL", str(exc)[:300])

    def api(self, path, query):
        if path == "/api/feed":
            now = time.time()
            if not _feed_cache["items"]:
                try:
                    with open(FEED_FILE) as fh:
                        disk = json.load(fh)
                    _feed_cache.update({"at": disk.get("at", 0), "items": disk.get("items", [])})
                except Exception:
                    pass
            if now - _feed_cache["at"] > FEED_TTL or not _feed_cache["items"]:
                items, seen = [], set()
                for q in FEED_QUERIES:
                    try:
                        for item in _search(q, 8):
                            if item["id"] not in seen:
                                seen.add(item["id"])
                                items.append(item)
                    except sabr.SabrError:
                        continue
                if items:
                    _feed_cache.update({"at": now, "items": items})
                    try:
                        with open(FEED_FILE, "w") as fh:
                            json.dump(_feed_cache, fh)
                    except Exception:
                        pass
            return self.send_json({"items": _feed_cache["items"][:24]})

        if path == "/api/search":
            q = query.get("q", "").strip()
            if not q:
                return self.send_error_json(400, "BAD_REQUEST", "q is required")
            return self.send_json({"items": _search(q, 16)})

        if path.startswith("/api/index/"):
            return self.index(path.split("/")[3], query)

        if path.startswith("/api/video/"):
            vid = path.split("/")[3]
            return self.send_json(video_meta(vid))

        if path.startswith("/api/related/"):
            vid = path.split("/")[3]
            meta = _meta_cache.get(vid)
            title = meta[1]["title"] if meta else ""
            items = _search(title[:60] or "trending", 6) if title else []
            return self.send_json({"items": [i for i in items if i["id"] != vid][:4]})

        if path == "/api/state":
            if self.command == "POST":
                return self.send_json(STORE.merge(self.read_json()))
            return self.send_json(STORE.get())

        if path == "/api/download" and self.command == "POST":
            body = self.read_json()
            vid, itag = body.get("id"), int(body.get("itag") or 0)
            if not vid or not itag:
                return self.send_error_json(400, "BAD_REQUEST", "id and itag are required")
            job = start_download(vid, itag)
            return self.send_json(job)

        if path.startswith("/api/download/"):
            job = JOBS.get(path.split("/")[3])
            if not job:
                return self.send_error_json(404, "NOT_FOUND")
            return self.send_json(job)

        return self.send_error_json(404, "NOT_FOUND")

    def index(self, video_id, query):
        itag = int(query.get("itag") or 0)
        st = start_stream(video_id, itag)
        with st.cond:
            return self.send_json({
                "itag": itag,
                "init": st.init_len,
                "total": st.total,
                "done": st.done,
                "error": st.error,
                "segments": list(st.segments),
            })

    def segment_bytes(self, video_id, query, seq=0, init_only=False):
        itag = int(query.get("itag") or 0)
        st = start_stream(video_id, itag)

        if init_only:
            def ready():
                return st.init_len > 0 or st.done
            def get():
                # MSE rejects an init segment that carries sidx/moof, so cut at the
                # first top-level box past ftyp/moov.
                with open(st.path, "rb") as fh:
                    data = fh.read(st.init_len)
                cut = len(data)
                for tag in (b"sidx", b"moof", b"mdat"):
                    i = data.find(tag)
                    if i >= 4:
                        cut = min(cut, i - 4)
                return data[:cut]
        else:
            def ready():
                return any(x["seq"] == seq for x in st.segments) or st.done
            def get():
                for x in st.segments:
                    if x["seq"] == seq:
                        with open(st.path, "rb") as fh:
                            fh.seek(x["offset"])
                            return fh.read(x["len"])
                return b""

        # short wait so a client that is one segment ahead doesn't have to retry
        with st.cond:
            deadline = time.time() + 25
            while not ready() and time.time() < deadline:
                st.cond.wait(0.25)
        body = get()
        if not body:
            return self.send_error_json(
                404, "NOT_READY",
                "init_len=%d done=%s segs=%d err=%s"
                % (st.init_len, st.done, len(st.segments), (st.error or "")[:80]))
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # --- media -------------------------------------------------------------
    def stream(self, video_id, query, head):
        itag = int(query.get("itag") or 0)
        if not itag:
            return self.send_error_json(400, "BAD_REQUEST", "itag required")
        st = start_stream(video_id, itag)

        # Respond immediately: a media element that waits for its first byte for tens
        # of seconds tends to give up and retry the whole request.
        if st.done and st.written == 0:
            return self.send_error_json(502, "STREAM_FAILED", st.error or "no data")

        rng = self.headers.get("Range")
        start, end = 0, None
        status = 200
        if rng and rng.startswith("bytes="):
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1) or 0)
                end = int(m.group(2)) if m.group(2) else None
                status = 206

        if st.total and end is None:
            end = st.total - 1
        if end is not None:
            end = min(end, (st.total - 1) if st.total else end)

        # While the file is still growing we must not promise a Content-Length:
        # a response that never ends makes Chrome's demuxer wait forever.
        growing = not st.done
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if growing:
            # A 206 without a known total is rejected by Chrome's media stack, so a
            # still-growing resource is served as a plain chunked 200 instead.
            status = 200
            self.send_header("Transfer-Encoding", "chunked")
        else:
            real_end = min(end if end is not None else st.written - 1, st.written - 1)
            self.send_header("Content-Length", str(max(0, real_end - start + 1)))
            if status == 206:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start, real_end, st.written))
        self.end_headers()
        if head:
            return
        chunked = growing

        pos = start
        try:
            while True:
                with st.cond:
                    deadline = time.time() + 60
                    while st.written <= pos and not st.done and time.time() < deadline:
                        st.cond.wait(0.5)
                    available = st.written
                    finished, err = st.done, st.error
                if pos >= available:
                    if finished or err:
                        break
                    continue
                with open(st.path, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read(min(1 << 20, available - pos))
                if not chunk:
                    if finished:
                        break
                    continue
                if end is not None and not chunked:
                    chunk = chunk[:end - pos + 1]
                if chunked:
                    self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                pos += len(chunk)
                if end is not None and pos > end:
                    break
                if finished and pos >= available:
                    break
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:                             # noqa: BLE001
            return

    # --- static ------------------------------------------------------------
    def static(self, path, head):
        if path == "/":
            path = "/index.html"
        full = os.path.abspath(os.path.join(WEB, path.lstrip("/")))
        if not full.startswith(WEB) or not os.path.isfile(full):
            return self.send_error_json(404, "NOT_FOUND")
        size = os.path.getsize(full)
        etag = 'W/"%d-%d"' % (size, int(os.path.getmtime(full)))
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".woff2": "font/woff2",
        }.get(os.path.splitext(full)[1], "application/octet-stream")
        rng = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1) or 0)
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        with open(full, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


JOBS = {}


def start_download(video_id, itag):
    job_id = "%s-%s-%d" % (video_id, itag, int(time.time()))
    job = {"id": job_id, "video": video_id, "itag": itag, "state": "running",
           "bytes": 0, "path": None, "error": None}
    JOBS[job_id] = job

    def worker():
        try:
            st = start_stream(video_id, itag)
            with st.cond:
                while not st.done:
                    st.cond.wait(0.5)
                    job["bytes"] = st.written
            if st.error:
                job.update(state="failed", error=st.error)
            else:
                out_dir = os.path.join(DATA, "downloads")
                os.makedirs(out_dir, exist_ok=True)
                out = os.path.join(out_dir, "%s-%s.mp4" % (video_id, itag))
                os.replace(st.path, out)
                job.update(state="done", path=out, bytes=os.path.getsize(out))
        except Exception as exc:                      # noqa: BLE001
            job.update(state="failed", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return job


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def unquote(s):
    from urllib.parse import unquote as _u
    return _u(s)


def main(argv):
    global CACHE
    port = int(argv[1]) if len(argv) > 1 else 8800
    CACHE = os.path.join(HERE, "cache", str(port))
    os.makedirs(CACHE, exist_ok=True)
    server = ThreadedServer(("0.0.0.0", port), Handler)
    print("feed app on http://0.0.0.0:%d  (web=%s cache=%s)" % (port, WEB, CACHE),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main(sys.argv)
