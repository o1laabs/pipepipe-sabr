#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sabr.py — YouTube SABR (Server Adaptive BitRate) client, reimplemented from a
source-level reading of PipePipe / PipePipeExtractor (GPLv3) and verified against
the live endpoints.

What this is
------------
YouTube's MWEB client stopped handing out per-format URLs; it returns a single
`serverAbrStreamingUrl` and expects the player to drive a stateful, protobuf-over-
UMP session. This file implements that session in ~600 lines of stdlib Python:

    player response  ->  serverAbrStreamingUrl + videoPlaybackUstreamerConfig
    init range (GET) ->  ftyp+moov+sidx (MP4) / EBML header+Cues (WebM)
    POST             ->  UMP parts: MEDIA_HEADER(20) / MEDIA(21) / MEDIA_END(22)
                         plus control parts (35 backoff, 43 redirect, 46 reload,
                         44 error, 58 stream protection)

Two things are NOT solved here, by design:
  * `poToken` — YouTube requires a BotGuard proof-of-origin token bound to the
    video (or to the visitor session) for both the init range GET and the SABR
    POST. Minting it needs a JS VM running YouTube's botguard program; pass one
    in with --pot. Without it the server answers SABR_ERROR(44)
    "sabr.malformed_config" (verified live, see selftest fixture).
  * signature / `n` deobfuscation for the `mweb` client. If you pass --client
    visionos the player response already carries usable direct URLs, which is
    also what --mode direct uses.

Usage
-----
    python3 sabr.py selftest
    python3 sabr.py probe    --video dQw4w9WgXcQ [--pot ...]
    python3 sabr.py download --video dQw4w9WgXcQ --itag 137 --mode direct -o v.mp4
    python3 sabr.py download --video dQw4w9WgXcQ --itag 251 --mode sabr  --pot ... -o a.webm

Nothing here is copied from PipePipe's source; the wire format is re-derived and
documented field by field in REPORT.md §3.
"""

import argparse
import shutil
import base64
import gzip
import json
import os
import random
import re
import struct
import subprocess
import tempfile
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

MWEB_UA = ("Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1,gzip(gfe)")
VISIONOS_UA = ("com.google.visionos.youtube/1.02(RealityDevice14,1; U; CPU visionOS "
               "25_6_0 like Mac OS X; US)")
TV_UA = ("Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.0) AppleWebKit/537.36 "
         "(KHTML, like Gecko) 76.0.3809.146/6.0 TV Safari/537.36")
TV_VERSION = "7.20250923.13.00"
DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

SESSION = {"cookie": None}

PLAYER_URL = "https://www.youtube.com/youtubei/v1/player?prettyPrint=false"
HOME_URL = "https://www.youtube.com/"
DECODER_BASE = "https://api.pipepipe.dev/decoder"

# UMP part types (see REPORT.md §3.4)
PART = {
    10: "ONESIE_HEADER", 11: "ONESIE_DATA", 12: "ONESIE_ENCRYPTED_MEDIA",
    20: "MEDIA_HEADER", 21: "MEDIA", 22: "MEDIA_END",
    30: "CONFIG", 31: "LIVE_METADATA", 33: "LIVE_METADATA_PROMISE",
    34: "LIVE_METADATA_PROMISE_CANCELLATION", 35: "NEXT_REQUEST_POLICY",
    36: "USTREAMER_VIDEO_AND_FORMAT_METADATA", 37: "FORMAT_SELECTION_CONFIG",
    38: "USTREAMER_SELECTED_MEDIA_STREAM", 42: "FORMAT_INITIALIZATION_METADATA",
    43: "SABR_REDIRECT", 44: "SABR_ERROR", 45: "SABR_SEEK",
    46: "RELOAD_PLAYER_RESPONSE", 47: "PLAYBACK_START_POLICY",
    48: "ALLOWED_CACHED_FORMATS", 49: "START_BW_SAMPLING_HINT",
    50: "PAUSE_BW_SAMPLING_HINT", 51: "SELECTABLE_FORMATS",
    52: "REQUEST_IDENTIFIER", 53: "REQUEST_CANCELLATION_POLICY",
    54: "ONESIE_PREFETCH_REJECTION", 55: "TIMELINE_CONTEXT",
    56: "REQUEST_PIPELINING", 57: "SABR_CONTEXT_UPDATE",
    58: "STREAM_PROTECTION_STATUS", 59: "SABR_CONTEXT_SENDING_POLICY",
    60: "LAWNMOWER_POLICY", 61: "SABR_ACK", 62: "END_OF_TRACK",
    63: "CACHE_LOAD_POLICY", 64: "LAWNMOWER_MESSAGING_POLICY",
    65: "PREWARM_CONNECTION", 66: "PLAYBACK_DEBUG_INFO", 67: "SNACKBAR_MESSAGE",
}


class SabrError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# tiny protobuf
# ---------------------------------------------------------------------------

def _varint(value):
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class PB:
    """Minimal protobuf writer: only the wire types SABR actually uses."""

    def __init__(self):
        self.buf = bytearray()

    def tag(self, num, wire):
        self.buf += _varint((num << 3) | wire)

    def uint(self, num, value):
        self.tag(num, 0)
        self.buf += _varint(int(value))

    def float32(self, num, value):
        self.tag(num, 5)
        self.buf += struct.pack("<f", float(value))

    def bytes(self, num, data):
        self.tag(num, 2)
        self.buf += _varint(len(data))
        self.buf += data

    def string(self, num, text):
        self.bytes(num, text.encode("utf-8"))

    def message(self, num, sub):
        self.bytes(num, bytes(sub.buf) if isinstance(sub, PB) else sub)

    def out(self):
        return bytes(self.buf)


def pb_fields(data):
    """Yield (field_number, wire_type, value) — value is int, bytes, or None."""
    i = 0
    n = len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        num, wire = tag >> 3, tag & 0x07
        if wire == 0:
            val, i = _read_varint(data, i)
        elif wire == 1:
            val, i = data[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _read_varint(data, i)
            val, i = data[i:i + ln], i + ln
        elif wire == 5:
            val, i = data[i:i + 4], i + 4
        else:
            raise SabrError("unsupported wire type %d" % wire)
        yield num, wire, val


def _read_varint(data, i):
    shift = 0
    value = 0
    while True:
        if i >= len(data):
            raise SabrError("truncated varint")
        b = data[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def pb_packed(data):
    """Yield varints from a packed repeated field."""
    i = 0
    while i < len(data):
        v, i = _read_varint(data, i)
        yield v


def pb_get(data, num, wire=2):
    for n, w, v in pb_fields(data):
        if n == num and w == wire:
            return v
    return None


# ---------------------------------------------------------------------------
# UMP envelope
# ---------------------------------------------------------------------------

def ump_int(stream):
    """Read YouTube's compact integer (NOT protobuf varint). Returns None at EOF."""
    first = stream.read(1)
    if not first:
        return None
    b = first[0]
    if b < 0x80:
        return b
    if b < 0xC0:
        return (b & 0x3F) + 64 * stream.read(1)[0]
    if b < 0xE0:
        b1 = stream.read(1)[0]
        b2 = stream.read(1)[0]
        return (b & 0x1F) + 32 * (b1 + 256 * b2)
    if b < 0xF0:
        b1 = stream.read(1)[0]
        b2 = stream.read(1)[0]
        b3 = stream.read(1)[0]
        return (b & 0x0F) + 16 * (b1 + 256 * (b2 + 256 * b3))
    b1 = stream.read(1)[0]
    b2 = stream.read(1)[0]
    b3 = stream.read(1)[0]
    b4 = stream.read(1)[0]
    return b1 + 256 * (b2 + 256 * (b3 + 256 * b4))


def ump_pack(part_type, payload):
    """Inverse of ump_int, used by the selftest."""
    def enc(v):
        if v < 0x80:
            return bytes([v])
        if v < 0x4000:
            return bytes([0x80 | (v & 0x3F), v >> 6])
        if v < 0x200000:
            hi = v >> 5
            return bytes([0xC0 | (v & 0x1F), hi & 0xFF, hi >> 8])
        if v < 0x10000000:
            hi = v >> 4
            return bytes([0xE0 | (v & 0x0F), hi & 0xFF, (hi >> 8) & 0xFF, hi >> 16])
        return bytes([0xF0, v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])
    return enc(part_type) + enc(len(payload)) + payload


class CountingStream:
    """Wraps a response so we can report bytes actually pulled off the wire."""

    def __init__(self, stream):
        self.stream = stream
        self.count = 0
        self.headers = getattr(stream, "headers", {})

    def read(self, n=-1):
        data = self.stream.read(n)
        self.count += len(data)
        return data

    def close(self):
        self.stream.close()


class PartReader:
    """Iterate UMP parts from a binary stream without buffering big payloads."""

    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        while True:
            ptype = ump_int(self.stream)
            if ptype is None:
                return
            size = ump_int(self.stream)
            if size is None:
                raise SabrError("truncated UMP part header")
            yield ptype, size, _BoundedReader(self.stream, size)


class _BoundedReader:
    """A view over the next `size` bytes of a stream."""

    def __init__(self, stream, size):
        self.stream = stream
        self.left = size

    def read(self, n=-1):
        if self.left <= 0:
            return b""
        if n < 0 or n > self.left:
            n = self.left
        data = self.stream.read(n)
        self.left -= len(data)
        return data

    def readall(self):
        chunks = []
        while self.left > 0:
            chunk = self.stream.read(min(65536, self.left))
            if not chunk:
                break
            self.left -= len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)

    def drain(self):
        while self.left > 0:
            chunk = self.stream.read(min(65536, self.left))
            if not chunk:
                self.left = 0
                break
            self.left -= len(chunk)


# ---------------------------------------------------------------------------
# segment index (timeline)
# ---------------------------------------------------------------------------

def parse_sidx(data, index_start=None, index_end=None):
    """Parse an ISO-BMFF `sidx` box. Returns [(start_ms, end_ms), ...] per segment."""
    lo = 0 if index_start is None else max(0, index_start)
    hi = len(data) if index_end is None else min(len(data), index_end)
    pos = data.find(b"sidx", lo, hi)
    if pos < 0:
        return []
    start = pos - 4
    size = int.from_bytes(data[start:start + 4], "big")
    box = data[start:start + size]
    version = box[8]
    p = 12
    p += 4                                     # reference_ID
    timescale = int.from_bytes(box[p:p + 4], "big"); p += 4
    if version == 0:
        p += 4                                 # earliest_presentation_time
        p += 4                                 # first_offset
    else:
        p += 8 + 8
    p += 2                                     # reserved
    count = int.from_bytes(box[p:p + 2], "big"); p += 2
    entries = []
    t = 0
    for _ in range(count):
        if p + 12 > len(box):
            break
        ref = int.from_bytes(box[p:p + 4], "big"); p += 4
        dur = int.from_bytes(box[p:p + 4], "big"); p += 4
        p += 4                                 # SAP
        if (ref >> 31) == 0 and timescale:
            start_ms = t * 1000.0 / timescale
            t += dur
            entries.append((start_ms, t * 1000.0 / timescale))
    return entries


def _ebml_read_vint(buf, pos, keep_marker=False):
    first = buf[pos]
    length = 1
    mask = 0x80
    while length <= 8 and not first & mask:
        mask >>= 1
        length += 1
    value = first if keep_marker else first & (mask - 1)
    for i in range(1, length):
        value = (value << 8) | buf[pos + i]
    return value, pos + length


def _find_webm_cues(data):
    """Walk EBML master elements to locate the top-level `Cues` element."""
    CUES, SEGMENT, SEEKHEAD = 0x1C53BB6B, 0x18538067, 0x114D9B74

    def walk(start, end, depth=0):
        p = start
        while p < end and depth < 6 and p + 2 <= len(data):
            try:
                eid, p2 = _ebml_read_vint(data, p, keep_marker=True)
                size, p3 = _ebml_read_vint(data, p2)
            except IndexError:
                return None
            if size > len(data):
                size = len(data) - p3
            if eid == CUES:
                return p3, size
            if eid in (SEGMENT, SEEKHEAD) and size > 0:
                found = walk(p3, min(len(data), p3 + size), depth + 1)
                if found:
                    return found
            p = p3 + size
        return None

    return walk(0, len(data))


def _webm_timestamp_scale(data, default=1000000):
    """Read Info/TimestampScale (0x2AD7B1); Matroska default is 1 ms per tick."""
    i = data.find(b"\x15\x49\xa9\x66")           # Info
    if i < 0:
        return default
    try:
        size, p = _ebml_read_vint(data, i + 4)
    except IndexError:
        return default
    end = min(len(data), p + size)
    while p < end:
        try:
            eid, p = _ebml_read_vint(data, p, keep_marker=True)
            esize, p = _ebml_read_vint(data, p)
        except IndexError:
            return default
        if eid == 0x2AD7B1:
            return int.from_bytes(data[p:p + esize], "big") or default
        p += esize
    return default


def parse_webm_cues(data):
    """Parse Matroska `Cues` -> [(start_ms, end_ms), ...] (approximate boundaries)."""
    found = _find_webm_cues(data)
    if not found:
        return []
    p, cues_size = found
    end = min(len(data), p + cues_size)
    scale = _webm_timestamp_scale(data)
    times = []
    while p < end:
        try:
            eid, p = _ebml_read_vint(data, p, keep_marker=True)
            esize, p = _ebml_read_vint(data, p)
        except IndexError:
            break
        if eid == 0xBB:                          # CuePoint
            sub_end = min(end, p + esize)
            while p < sub_end:
                try:
                    sid, p = _ebml_read_vint(data, p, keep_marker=True)
                    ssize, p = _ebml_read_vint(data, p)
                except IndexError:
                    break
                if sid == 0xB3:                  # CueTime, in TimestampScale ticks
                    ticks = int.from_bytes(data[p:p + ssize], "big")
                    times.append(ticks * scale / 1e6)
                p += ssize
        else:
            p += esize
    out = []
    for k, t in enumerate(times):
        out.append((t, times[k + 1] if k + 1 < len(times) else t))
    return out


def init_segment_len(fmt):
    """initRange end is inclusive; the index range (sidx) is fetched in the same GET."""
    start = int(fmt["initRange"]["start"])
    end = int(fmt["initRange"]["end"])
    if fmt.get("indexRange"):
        end = max(end, int(fmt["indexRange"]["end"]))
    return start, end


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http(url, data=None, headers=None, timeout=40, retries=3):
    hdr = {"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"}
    hdr.update(headers or {})
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 404):
                break
            time.sleep(0.8 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            time.sleep(0.8 * (attempt + 1))
    if isinstance(last, urllib.error.HTTPError):
        raise SabrError("HTTP %d for %s" % (last.code, url.split("?")[0]))
    raise SabrError("network error for %s: %s" % (url.split("?")[0], last))


def http_stream(url, data=None, headers=None, timeout=60):
    hdr = {"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"}
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr)
    resp = urllib.request.urlopen(req, timeout=timeout)
    if resp.headers.get("Content-Encoding") == "gzip":
        raise SabrError("unexpected gzip on a binary stream")
    return resp


# ---------------------------------------------------------------------------
# player response
# ---------------------------------------------------------------------------

def load_cookie_header(path):
    """Read a Netscape cookies.txt (or a raw 'a=b; c=d' string) into a Cookie header."""
    if not path:
        return None
    if "=" in path and "\t" not in path and os.sep not in path and not os.path.exists(path):
        return path.strip()          # already a header
    try:
        parts = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                f = line.rstrip("\n").split("\t")
                if len(f) >= 7:
                    parts.append("%s=%s" % (f[5], f[6]))
        return "; ".join(parts) or None
    except Exception:
        return None


def fetch_visitor_data():
    st, hd, body = http(HOME_URL, headers={"User-Agent": DESKTOP_UA,
                                           "Cookie": "PREF=hl=en&gl=US"})
    html = body.decode("utf-8", "replace")
    vis = re.search(r'"VISITOR_DATA":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    return (vis.group(1) if vis else None), (ver.group(1) if ver else None)


def player_response(video_id, client="visionos", pot=None, visitor_data=None,
                    reload_token=None, cookie=None):
    if client == "mweb":
        if not visitor_data:
            visitor_data, _ = fetch_visitor_data()
        ctx_client = {"clientName": "MWEB", "clientVersion": "2.20261020.00.00",
                      "hl": "en", "gl": "US", "userAgent": MWEB_UA,
                      "visitorData": visitor_data}
        ua, cid, cver = MWEB_UA, "2", "2.20261020.00.00"
    elif client == "tv":
        ctx_client = {"clientName": "TVHTML5", "clientVersion": TV_VERSION,
                      "hl": "en", "gl": "US", "userAgent": TV_UA}
        ua, cid, cver = TV_UA, "7", TV_VERSION
    else:
        ctx_client = {"clientName": "VISIONOS", "clientVersion": "1.02",
                      "hl": "en", "gl": "US", "userAgent": VISIONOS_UA}
        ua, cid, cver = VISIONOS_UA, "101", "1.02"

    body = {
        "context": {"client": ctx_client},
        "videoId": video_id,
        "contentCheckOk": True, "racyCheckOk": True,
        "cpn": random_cpn(),
        "playbackContext": {"contentPlaybackContext": {
            "html5Preference": "HTML5_PREF_WANTS", "signatureTimestamp": 20744}},
    }
    if reload_token:
        body["playbackContext"]["reloadPlaybackContext"] = {
            "reloadPlaybackParams": {"token": reload_token}}
    if pot:
        token_str = (pot if isinstance(pot, str)
                     else base64.urlsafe_b64encode(pot).decode().rstrip("="))
        body["serviceIntegrityDimensions"] = {"poToken": token_str}
    extra = {"Content-Type": "application/json", "User-Agent": ua,
           "X-YouTube-Client-Name": cid,
           "X-YouTube-Client-Version": cver}
    cookie = cookie or SESSION.get("cookie")
    if cookie:
        extra["Cookie"] = cookie
    st, hd, raw = http(PLAYER_URL, data=json.dumps(body).encode(), headers=extra)
    return json.loads(raw)


def random_cpn():
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(random.choice(alphabet) for _ in range(16))


def ustreamer_config(pr):
    cfg = (((pr.get("playerConfig") or {}).get("mediaCommonConfig") or {})
           .get("mediaUstreamerRequestConfig") or {}).get("videoPlaybackUstreamerConfig")
    if not cfg:
        raise SabrError("player response has no videoPlaybackUstreamerConfig")
    cfg = cfg.strip().replace("\n", "")
    return base64.b64decode(cfg + "=" * (-len(cfg) % 4), altchars=b"-_", validate=False)


def decoder_stage(path, params):
    """Call the PipePipe decoder API (the same fallback the app ships)."""
    qs = urllib.parse.urlencode(params)
    st, hd, raw = http("%s/%s?%s" % (DECODER_BASE, path, qs),
                       headers={"User-Agent": "PipePipe/4.9.0"})
    return json.loads(raw)


def decode_mweb_urls(pr):
    """For the mweb client: deobfuscate `sig` and `n` so the init range GET works.

    Two response shapes exist: `signatureCipher` (needs sig + n) and a plain `url`
    (needs n only). Both carry the throttling parameter; an undecoded `n` makes the
    CDN answer 403, which used to look like a poToken problem.
    """
    player = decoder_stage("latest-player", {})["player"]
    fmts = (pr.get("streamingData") or {}).get("adaptiveFormats") or []
    sigs, ns = set(), set()

    def url_of(f):
        if f.get("signatureCipher"):
            q = dict(urllib.parse.parse_qsl(f["signatureCipher"]))
            if "s" in q:
                sigs.add(q["s"])
            return q.get("url", "")
        return f.get("url") or ""

    for f in fmts:
        m = re.search(r"[?&]n=([^&]+)", url_of(f))
        if m:
            ns.add(m.group(1))
    sabr_url = pr["streamingData"].get("serverAbrStreamingUrl") or ""
    m = re.search(r"[?&]n=([^&]+)", sabr_url)
    if m:
        ns.add(m.group(1))

    data_n, data_sig = {}, {}
    if ns or sigs:
        params = {"player": player}
        if ns:
            params["n"] = ",".join(ns)
        if sigs:
            params["sig"] = ",".join(sigs)
        resp = decoder_stage("decode", params)
        idx = 0
        if ns:
            data_n = resp["responses"][idx]["data"]; idx += 1
        if sigs:
            data_sig = resp["responses"][idx]["data"]

    def resolve(kind, value):
        """The batch call can silently drop values; fall back to one-by-one."""
        store = data_n if kind == "n" else data_sig
        if value in store:
            return store[value]
        try:
            r = decoder_stage("decode", {"player": player, kind: value})
            got = r["responses"][0]["data"].get(value)
            if got:
                store[value] = got
                return got
        except Exception:
            pass
        return None

    for f in fmts:
        if f.get("signatureCipher"):
            q = dict(urllib.parse.parse_qsl(f["signatureCipher"]))
            url = q.get("url", "")
            if q.get("s"):
                sig = resolve("sig", q["s"])
                if sig:
                    url += "&" + q.get("sp", "sig") + "=" + urllib.parse.quote(sig, safe="")
            mu = re.search(r"[?&]n=([^&]+)", url)
            if mu:
                nn = resolve("n", mu.group(1))
                if nn:
                    url = url.replace("n=" + mu.group(1), "n=" + nn)
            f["url"] = url
        elif f.get("url"):
            mu = re.search(r"[?&]n=([^&]+)", f["url"])
            if mu:
                nn = resolve("n", mu.group(1))
                if nn:
                    f["url"] = f["url"].replace("n=" + mu.group(1), "n=" + nn)

    if m:
        nn = resolve("n", m.group(1))
        if nn:
            pr["streamingData"]["serverAbrStreamingUrl"] = sabr_url.replace(
                "n=" + m.group(1), "n=" + nn)
    return pr, player


def format_id(fmt):
    pb = PB()
    pb.uint(1, fmt["itag"])
    if fmt.get("lastModified"):
        pb.uint(2, int(fmt["lastModified"]))
    if fmt.get("xtags"):
        pb.string(3, fmt["xtags"])
    return pb.out()


def build_request(cfg, audio=None, video=None, player_time_ms=0,
                  playback_rate=1.0, bandwidth_estimate=0,
                  buffered_through=None, client_id=2, client_version="2.20261020.00.00",
                  po_token=None, playback_cookie=None, sabr_contexts=None):
    state = PB()
    if video:
        state.uint(18, max(int(video.get("width") or 0), 640))
        state.uint(19, max(int(video.get("height") or 0), 360))
        state.uint(21, max(int(video.get("height") or 0), 360))
        est = bandwidth_estimate or (int(video.get("bitrate") or 0) * 2)
        if est:
            state.uint(23, int(est))
    state.uint(34, 1)
    state.float32(35, playback_rate or 1.0)
    track_types = 1 if (audio and not video) else 2 if (video and not audio) else 0
    if track_types:
        state.uint(40, track_types)
    if audio and audio.get("isDrc"):
        state.uint(46, True)
    state.uint(28, int(player_time_ms))
    if audio and audio.get("audioTrackId"):
        state.string(69, audio["audioTrackId"])

    req = PB()
    req.message(1, state)
    for fmt in (audio, video):
        if fmt:
            req.message(2, format_id(fmt))
    if buffered_through and audio:
        # buffered range: {1:formatId, 2:startMs, 3:durationMs, 4:startSeq, 5:endSeq, 6:timeRange}
        rng = PB()
        rng.message(1, format_id(audio))
        rng.uint(2, 0)
        rng.uint(3, int(buffered_through.get("end_ms", 0)))
        rng.uint(4, 1)
        rng.uint(5, int(buffered_through.get("end_seq", 0)))
        tr = PB()
        tr.uint(1, 0)
        tr.uint(2, int(buffered_through.get("end_ms", 0)))
        tr.uint(3, 1000)
        rng.message(6, tr)
        req.message(3, rng)
    req.uint(4, int(player_time_ms))
    req.bytes(5, cfg)
    if audio:
        req.message(16, format_id(audio))
    if video:
        req.message(17, format_id(video))

    ci = PB()
    ci.uint(16, client_id)
    ci.string(17, client_version)
    ci.string(21, "en-US")
    ci.string(22, "US")
    ctx = PB()
    ctx.message(1, ci)
    if po_token:
        ctx.bytes(2, po_token)
    if playback_cookie:
        ctx.bytes(3, playback_cookie)
    for ctx_type, value in (sabr_contexts or {}).items():
        sc = PB()
        sc.uint(1, ctx_type)
        sc.bytes(2, value)
        ctx.message(5, sc)
    req.message(19, ctx)
    return req.out()


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

class SabrSession:
    def __init__(self, url, cfg, audio_fmt, video_fmt, po_token=None,
                 client_id=2, client_version="2.20261020.00.00", verbose=True):
        self.url = url
        self.cfg = cfg
        self.audio = audio_fmt
        self.video = video_fmt
        self.po = po_token
        self.client_id = client_id
        self.client_version = client_version
        self.cpn = random_cpn()
        self.rn = 0
        self.cookie = None
        self.backoff_s = 0.0
        self.verbose = verbose
        self.segments = 0
        self.bytes_media = 0
        self.last_seq = 0
        self.last_end_ms = 0
        self.stats = {
            "requests": [],          # {rn, ms, bytes, segments, media_bytes, parts}
            "parts": {},
            "backoffs": [],          # {rn, ms}
            "redirects": [],         # {rn, to}
            "protection": [],        # {rn, status, max_retries}
            "reloads": [],           # {rn, token}
            "errors": [],            # {rn, type, code}
            "first_segment_ms": None,
            "segment_meta": [],      # {seq, bytes, first_bytes}
            "transport_errors": [],
            "contexts": [],
        }
        self.ended = False
        self.reload_token = None
        self.reloader = None
        self.reloads_done = 0
        self.want_itag = None
        self.pot_refresher = None      # callable(force=False) -> bytes
        self.sabr_contexts = {}        # type -> value
        self.active_contexts = set()
        self.written_seqs = set()
        self.host_candidates = self.build_host_candidates(url)
        self.host_index = 0
        self.redirects = 0
        self.expected_segments = None
        self.repair_attempts = 0
        self.on_segment = None

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    def _url(self):
        url = self.url
        if "&alr=" not in url:
            url += ("&" if "?" in url else "?") + "alr=yes"
        url = re.sub(r"[?&]cpn=[^&]*", "", url) + "&cpn=" + self.cpn
        url = re.sub(r"[?&]rn=[^&]*", "", url) + "&rn=" + str(self.rn)
        return url

    @staticmethod
    def build_host_candidates(url):
        """Mirror PipePipe: derive alternate CDN nodes from the `mn` / `mvi` params."""
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname or ""
            q = dict(urllib.parse.parse_qsl(parts.query))
            mn = q.get("mn") or ""
            mvi = q.get("mvi")
            m = re.match(r"rr(\d+)---(.+)\.googlevideo\.com$", host)
            if not m or not mn:
                return [url]
            rr = mvi if (mvi and mvi.isdigit()) else m.group(1)
            out = [url]
            for node in mn.split(","):
                node = node.strip()
                if not node:
                    continue
                cand_host = "rr%s---%s.googlevideo.com" % (rr, node)
                if cand_host == host:
                    continue
                out.append(urllib.parse.urlunsplit(
                    (parts.scheme, cand_host, parts.path, parts.query, parts.fragment)))
            return out
        except Exception:
            return [url]

    def rotate_host(self):
        if len(self.host_candidates) < 2:
            return False
        self.host_index = (self.host_index + 1) % len(self.host_candidates)
        self.url = self.host_candidates[self.host_index]
        self.log("  host -> %s" % (urllib.parse.urlsplit(self.url).hostname or "?"))
        return True

    def request(self, player_time_ms=0, sink=None):
        buffered = None
        if self.last_end_ms > 0 and self.last_seq > 0:
            buffered = {"end_ms": self.last_end_ms, "end_seq": self.last_seq}
        body = build_request(self.cfg, self.audio, self.video,
                             player_time_ms=player_time_ms,
                             buffered_through=buffered,
                             bandwidth_estimate=0,
                             client_id=self.client_id,
                             client_version=self.client_version,
                             po_token=self.po, playback_cookie=self.cookie,
                             sabr_contexts={t: self.sabr_contexts[t]
                                            for t in self.active_contexts
                                            if t in self.sabr_contexts})
        headers = {"Content-Type": "application/x-protobuf",
                   "Accept": "application/vnd.yt-ump",
                   "Accept-Encoding": "identity",
                   "User-Agent": MWEB_UA if self.client_id == 2 else VISIONOS_UA}
        if SESSION.get("cookie"):
            headers["Cookie"] = SESSION["cookie"]
        t0 = time.time()
        attempts = 0
        while True:
            try:
                resp = http_stream(self._url(), data=body, headers=headers)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                # only real transport failures justify switching CDN nodes
                attempts += 1
                self.stats["transport_errors"].append(str(exc)[:120])
                if attempts <= 2 and self.rotate_host():
                    continue
                raise SabrError("transport failed after %d attempts: %s"
                                % (attempts, str(exc)[:120]))
        counted = CountingStream(resp)
        ctype = resp.headers.get("Content-Type") or ""
        if "application/vnd.yt-ump" not in ctype.lower():
            raise SabrError("expected UMP, got %s (HTTP %d)" % (ctype, resp.status))
        self.rn += 1
        before_segments, before_media = self.segments, self.bytes_media
        counts = self._consume(counted, sink)
        self.stats["requests"].append({
            "rn": self.rn,
            "ms": int((time.time() - t0) * 1000),
            "bytes": counted.count,
            "segments": self.segments - before_segments,
            "media_bytes": self.bytes_media - before_media,
            "parts": len(counts) and sum(counts.values()),
        })
        return counts

    def _consume(self, resp, sink):
        """Read one UMP response.

        Media is buffered per segment (spooled to disk past 8 MiB) and only handed to
        `sink(seg, fileobj)` when MEDIA_END arrives with a matching length. YouTube
        routinely truncates a response in the middle of a segment and re-sends that
        segment in full on the next request, so writing MEDIA parts straight into the
        output file duplicates them and corrupts the container.
        """
        open_seg = {}          # headerId -> segment dict
        part_counts = {}
        for ptype, size, payload in PartReader(resp):
            name = PART.get(ptype, str(ptype))
            part_counts[name] = part_counts.get(name, 0) + 1
            self.stats["parts"][name] = self.stats["parts"].get(name, 0) + 1

            if ptype == 20:                                   # MEDIA_HEADER
                data = payload.readall()
                meta = {}
                for num, wire, val in pb_fields(data):
                    meta[num] = val
                hid = meta.get(1, 0)
                start_ms = meta.get(11)
                dur_ms = meta.get(12)
                if (start_ms is None or dur_ms is None) and isinstance(meta.get(15), bytes):
                    tr = {}
                    for n2, w2, v2 in pb_fields(meta[15]):
                        tr[n2] = v2
                    ts = tr.get(3) or 0
                    if ts:
                        start_ms = start_ms if start_ms is not None else (tr.get(1, 0) * 1000 // ts)
                        dur_ms = dur_ms if dur_ms is not None else (tr.get(2, 0) * 1000 // ts)
                seg = {"headerId": hid,
                       "itag": meta.get(3, 0),
                       "sequenceNumber": meta.get(9, 0),
                       "startMs": start_ms if start_ms is not None else 0,
                       "durationMs": dur_ms if dur_ms is not None else 0,
                       "contentLength": meta.get(14) if isinstance(meta.get(14), int) else -1,
                       "initSegment": bool(meta.get(8, 0)),
                       "tmp": None, "written": 0}
                if self.want_itag is not None and seg["itag"] != self.want_itag:
                    seg["other"] = True
                seg["skip"] = (seg.get("other", False)
                               or (not seg["initSegment"]
                                   and (seg["startMs"] <= self.last_start_ms()
                                        or seg["sequenceNumber"] in self.written_seqs)))
                if not seg["skip"]:
                    seg["tmp"] = tempfile.SpooledTemporaryFile(max_size=8 << 20)
                open_seg[hid] = seg
                self._last_open_hid = hid
                self.log("  MEDIA_HEADER rn=%d hid=%s seq=%s itag=%s startMs=%s durMs=%s len=%s%s"
                         % (self.rn, hid, seg["sequenceNumber"], seg["itag"], seg["startMs"],
                            seg["durationMs"], seg["contentLength"],
                            "  (dup, skipped)" if seg["skip"] else ""))

            elif ptype == 21:                                 # MEDIA
                if size <= 0:
                    continue
                hid = payload.read(1)[0]
                seg = open_seg.get(hid)
                while True:
                    chunk = payload.read(65536)
                    if not chunk:
                        break
                    self.bytes_media += len(chunk)
                    if seg is not None and seg["tmp"] is not None:
                        if "first" not in seg:
                            seg["first"] = chunk[:4]
                        seg["tmp"].write(chunk)
                        seg["written"] += len(chunk)

            elif ptype == 22:                                 # MEDIA_END
                data = payload.readall()
                hid = 0
                try:
                    hid = pb_get(data, 1, 0) or 0
                except SabrError:
                    hid = 0
                if hid not in open_seg and getattr(self, "_last_open_hid", None) in open_seg:
                    # some responses send an empty MEDIA_END: it closes the segment
                    # that is currently open, in stream order
                    hid = self._last_open_hid
                seg = open_seg.pop(hid, None)
                if seg is not None:
                    remaining = [h for h in open_seg]
                    self._last_open_hid = remaining[-1] if remaining else None
                if not seg or seg["skip"]:
                    if seg and seg["tmp"]:
                        seg["tmp"].close()
                    continue
                marker = seg["startMs"]
                bad = (seg["contentLength"] > 0 and seg["written"] != seg["contentLength"])
                if not bad and seg["written"] > 0 and sink is not None:
                    seg["tmp"].seek(0)
                    sink(seg, seg["tmp"])
                if not bad:
                    self.segments += 1
                    if self.stats["first_segment_ms"] is None:
                        self.stats["first_segment_ms"] = int(
                            (time.time() - getattr(self, "_t_start", time.time())) * 1000)
                    self.written_seqs.add(seg["sequenceNumber"])
                    self.last_seq = max(self.last_seq, seg["sequenceNumber"])
                    self.last_end_ms = max(self.last_end_ms,
                                           seg["startMs"] + seg["durationMs"])
                    self._last_start_ms = max(getattr(self, "_last_start_ms", -1), marker)
                    if self.on_segment is not None:
                        self.on_segment(self)
                    self.stats["segment_meta"].append({
                        "seq": seg["sequenceNumber"],
                        "bytes": seg["written"],
                        "start": (seg.get("first") or b"").hex(),
                    })
                self.log("  MEDIA_END   rn=%d hid=%s seq=%s %d bytes%s" %
                         (self.rn, hid, seg["sequenceNumber"], seg["written"],
                          "  !! length mismatch (expected %d, dropped)" % seg["contentLength"]
                          if bad else ""))
                if seg["tmp"]:
                    seg["tmp"].close()

            elif ptype == 35:                                 # NEXT_REQUEST_POLICY
                data = payload.readall()
                for num, wire, val in pb_fields(data):
                    if num == 4:
                        self.backoff_s = val / 1000.0
                    elif num == 7:
                        self.cookie = val
                if self.backoff_s:
                    self.stats["backoffs"].append({"rn": self.rn, "ms": int(self.backoff_s * 1000)})
                    self.log("  backoff %.2fs" % self.backoff_s)

            elif ptype == 57:                                 # SABR_CONTEXT_UPDATE
                data = payload.readall()
                ctype, value, send_by_default, write_policy = None, None, False, -1
                for num, wire, val in pb_fields(data):
                    if num == 1:
                        ctype = val
                    elif num == 3:
                        value = val
                    elif num == 4:
                        send_by_default = bool(val)
                    elif num == 5:
                        write_policy = val
                if ctype is not None and value:
                    if not (write_policy == 2 and ctype in self.sabr_contexts):
                        self.sabr_contexts[ctype] = value
                        if send_by_default:
                            self.active_contexts.add(ctype)
                        self.stats["contexts"].append({"type": ctype, "len": len(value)})

            elif ptype == 59:                                 # SABR_CONTEXT_SENDING_POLICY
                data = payload.readall()
                for num, wire, val in pb_fields(data):
                    values = [val] if wire == 0 else (list(pb_packed(val)) if isinstance(val, bytes) else [])
                    for v in values:
                        if num == 1:
                            self.active_contexts.add(v)
                        elif num == 2:
                            self.active_contexts.discard(v)
                        elif num == 3:
                            self.active_contexts.discard(v)
                            self.sabr_contexts.pop(v, None)

            elif ptype == 43:                                 # SABR_REDIRECT
                data = payload.readall()
                new_url = pb_get(data, 1, 2)
                if new_url:
                    self.redirects += 1
                    if self.redirects > 3:
                        raise SabrError("SABR redirect limit exceeded")
                    self.url = new_url.decode("utf-8", "replace")
                    self.stats["redirects"].append({"rn": self.rn, "to": self.url.split("?")[0]})
                    self.log("  redirect -> %s" % self.url.split("?")[0])

            elif ptype == 44:                                 # SABR_ERROR
                data = payload.readall()
                etype = (pb_get(data, 1, 2) or b"").decode("utf-8", "replace")
                code = pb_get(data, 2, 0) or 0
                self.stats["errors"].append({"rn": self.rn, "type": etype, "code": code})
                raise SabrError("SABR error: type=%s code=%s" % (etype, code))

            elif ptype == 46:                                 # RELOAD_PLAYER_RESPONSE
                data = payload.readall()
                inner = pb_get(data, 1, 2)
                token = pb_get(inner, 1, 2) if inner else None
                self.reload_token = token.decode("utf-8", "replace") if token else None
                self.stats["reloads"].append({"rn": self.rn,
                                              "token": (self.reload_token or "")[:16]})
                self.log("  reload player response requested (token %s)"
                         % ("present" if token else "absent"))

            elif ptype == 58:                                 # STREAM_PROTECTION_STATUS
                data = payload.readall()
                st = pb_get(data, 1, 0)
                mr = pb_get(data, 2, 0)
                self.stats["protection"].append({"rn": self.rn, "status": st, "max_retries": mr})
                self.log("  stream protection status=%s maxRetries=%s" % (st, mr))

            elif ptype == 62:                                 # END_OF_TRACK
                self.ended = True
                self.log("  END_OF_TRACK")

            else:
                payload.drain()

        for seg in open_seg.values():          # truncated at response end: drop
            if seg["tmp"]:
                seg["tmp"].close()
        resp.close()
        return part_counts

    def last_start_ms(self):
        return getattr(self, "_last_start_ms", -1)

    def run(self, sink, max_seconds=None, start_ms=0, wall_budget=None,
            expected_segments=None):
        """Drive the session until `max_seconds` of media, with recovery budgets.

        Mirrors PipePipe's SabrRequestCoordinator: server-instructed backoff is honoured
        and retried instead of being treated as failure; an attestation downgrade
        refreshes the token; reloads/redirects are capped; missing segments are
        accounted for at the end.
        """
        started = time.time()
        self._t_start = started
        target_ms = int(max_seconds * 1000) if max_seconds else None
        self.expected_segments = expected_segments
        pos = start_ms
        idle = 0
        prev_end = -1
        refreshes = 0
        backoff_streak = 0

        while True:
            if wall_budget and time.time() - started >= wall_budget:
                self.stats["wall_budget_hit"] = True
                break

            if self.reload_token:
                token = self.reload_token
                self.reload_token = None
                if self.reloader is not None and self.reloads_done < 2:
                    self.reloads_done += 1
                    self.log("  reloading player response (#%d)" % self.reloads_done)
                    self.url, self.cfg = self.reloader(token)
                    self.host_candidates = self.build_host_candidates(self.url)
                    self.host_index = 0
                    self.cpn = random_cpn()
                    self.rn = 0
                    continue
                raise SabrError("player reload requested but no reloader is installed")

            try:
                self.request(self.last_end_ms or pos, sink)
            except SabrError as exc:
                msg = str(exc).lower()
                if ("attestation" in msg or "stream protection" in msg) \
                        and self.pot_refresher and refreshes < 3:
                    refreshes += 1
                    self.log("  attestation required -> refreshing token (#%d)" % refreshes)
                    self.po = self.pot_refresher(force=True)
                    continue
                raise

            progressed = self.last_end_ms > 0 and self.last_end_ms != prev_end
            if progressed:
                idle, prev_end, backoff_streak = 0, self.last_end_ms, 0
            else:
                idle += 1

            sps = self.stats["protection"][-1]["status"] if self.stats["protection"] else None
            if not progressed and sps not in (None, 0, 1) and self.pot_refresher \
                    and refreshes < 3:
                refreshes += 1
                self.log("  stream protection=%s -> refreshing token (#%d)" % (sps, refreshes))
                self.po = self.pot_refresher(force=True)
                continue

            if self.backoff_s:
                wait = min(self.backoff_s, 30.0)
                self.backoff_s = 0.0
                backoff_streak += 1
                if backoff_streak > 6:
                    raise SabrError("server kept asking for backoff (%d in a row)"
                                    % backoff_streak)
                if wait > 0:
                    self.log("  waiting %.1fs as instructed" % wait)
                    time.sleep(wait)
                if idle >= 3:
                    self.stats["stalled"] = True
                    self.log("  no progress after %d requests" % idle)
                    break
                continue

            if idle >= 3:
                self.stats["stalled"] = True
                self.log("  no progress after %d requests" % idle)
                break
            if self.ended:
                break
            if target_ms and self.last_end_ms >= target_ms:
                break

        if self.expected_segments:
            missing = [n for n in range(1, self.expected_segments + 1)
                       if n not in self.written_seqs]
            self.stats["missing_segments"] = missing[:100]
            if missing:
                self.log("  integrity: %d/%d segments, %d missing"
                         % (len(self.written_seqs), self.expected_segments, len(missing)))
            else:
                self.log("  integrity: %d/%d segments, complete"
                         % (len(self.written_seqs), self.expected_segments))


# ---------------------------------------------------------------------------
# stress / stability run
# ---------------------------------------------------------------------------

def pick_by_height(pr, max_height, min_height=0, mp4_only=False, prefer="best"):
    """Pick a video track under the height ceiling (best by default)."""
    best = None
    for f in (pr.get("streamingData") or {}).get("adaptiveFormats") or []:
        mime = f.get("mimeType") or ""
        if not mime.startswith("video/"):
            continue
        if mp4_only and "mp4" not in mime:
            continue
        h = int(f.get("height") or 0)
        if h <= 0 or h > max_height or h < min_height:
            continue
        key = (h, int(f.get("bitrate") or 0))
        if best is None:
            best = (key, int(f.get("itag")))
        else:
            better = key > best[0] if prefer == "best" else key < best[0]
            if better:
                best = (key, int(f.get("itag")))
    if best is None:
        raise SabrError("no video track at or below %dp" % max_height)
    return best[1]


def pick_exact_height(pr, height, mp4_only=True):
    """One rendition per ladder rung: exact height first, else the closest below."""
    cands = []
    for f in (pr.get("streamingData") or {}).get("adaptiveFormats") or []:
        mime = f.get("mimeType") or ""
        if not mime.startswith("video/") or (mp4_only and "mp4" not in mime):
            continue
        h = int(f.get("height") or 0)
        if h > 0:
            cands.append((h, int(f.get("bitrate") or 0), int(f.get("itag"))))
    exact = [c for c in cands if c[0] == height]
    if exact:
        return min(exact, key=lambda c: c[1])[2]
    below = [c for c in cands if c[0] < height]
    if below:
        best_h = max(c[0] for c in below)
        return min([c for c in below if c[0] == best_h], key=lambda c: c[1])[2]
    raise SabrError("no rendition at or below %dp" % height)


def pick_audio(pr, prefer_lowest=True, mp4_only=False):
    best = None
    for f in (pr.get("streamingData") or {}).get("adaptiveFormats") or []:
        mime = f.get("mimeType") or ""
        if not mime.startswith("audio/"):
            continue
        if mp4_only and "mp4" not in mime:
            continue
        br = int(f.get("bitrate") or 0)
        if best is None or (br < best[0] if prefer_lowest else br > best[0]):
            best = (br, int(f.get("itag")))
    if best is None:
        raise SabrError("no audio track")
    return best[1]


def pick_formats(pr, itag=None):
    fmts = pr["streamingData"].get("adaptiveFormats") or []
    if itag:
        chosen = [f for f in fmts if int(f.get("itag", 0)) == int(itag)]
        if not chosen:
            raise SabrError("itag %s not offered for this video" % itag)
        fmt = chosen[0]
    else:
        fmt = fmts[0]
    fmt = dict(fmt)
    fmt["bitrate"] = fmt.get("bitrate") or fmt.get("averageBitrate")
    at = fmt.get("audioTrack") or {}
    fmt["audioTrackId"] = at.get("id")
    fmt["width"] = fmt.get("width")
    fmt["height"] = fmt.get("height")
    fmt["isDrc"] = fmt.get("isDrc", False)
    return fmt


def fetch_init(fmt, pot=None):
    start, end = init_segment_len(fmt)
    url = fmt.get("url")
    if not url:
        raise SabrError("format has no direct url (mweb client needs sig/n decoding)")
    if pot:
        url += ("&" if "?" in url else "?") + "pot=" + \
            base64.urlsafe_b64encode(pot).decode().rstrip("=")
    st, hd, body = http(url, headers={"Range": "bytes=%d-%d" % (start, end),
                                      "User-Agent": MWEB_UA})
    if st != 206 and not (st == 200 and start == 0):
        raise SabrError("init range failed: HTTP %d" % st)
    return body


def fetch_init_with_fallback(video_id, fmt, pot=None):
    """Init range from the mweb URL; retry a fresh player response, then VisionOS."""
    last = None
    for _ in range(3):
        try:
            return fetch_init(fmt, pot), "mweb"
        except SabrError as exc:
            last = exc
            if "no direct url" not in str(exc):
                break
            pr_retry = player_response(video_id, client="mweb")
            if "streamingData" not in pr_retry:
                break
            pr_retry, _ = decode_mweb_urls(pr_retry)
            for f in pr_retry["streamingData"].get("adaptiveFormats") or []:
                if int(f.get("itag", 0)) == int(fmt["itag"]) and f.get("url"):
                    fmt.clear()
                    fmt.update(pick_formats(pr_retry, f["itag"]))
                    break
    for client in ("visionos", "tv"):
        try:
            pr_alt = player_response(video_id, client=client)
            if "streamingData" not in pr_alt:
                continue
            for f in pr_alt["streamingData"].get("adaptiveFormats") or []:
                if int(f.get("itag", 0)) == int(fmt["itag"]):
                    return fetch_init(pick_formats(pr_alt, f["itag"]), None), client
        except SabrError:
            continue
    raise last or SabrError("could not fetch the initialization range")


def download_direct(fmt, fh, pot, seconds, out):
    url = fmt["url"]
    if pot:
        url += ("&" if "?" in url else "?") + "pot=" + \
            base64.urlsafe_b64encode(pot).decode().rstrip("=")
    total = int(fmt.get("contentLength") or 0)
    pos = 0
    chunk = 1 << 20
    started = time.time()
    while True:
        if total and pos >= total:
            break
        if seconds and time.time() - started > seconds:
            break
        end = pos + chunk - 1
        if total:
            end = min(end, total - 1)
        st, hd, body = http(url, headers={"Range": "bytes=%d-%d" % (pos, end),
                                          "User-Agent": VISIONOS_UA}, retries=2)
        if not body:
            break
        fh.write(body)
        pos += len(body)
        if total:
            sys.stdout.write("\r  direct  %5.1f%%  %.1f MiB"
                             % (100.0 * pos / total, pos / 1048576.0))
            sys.stdout.flush()
    sys.stdout.write("\n")


CLIENT_MATRIX = [("mweb", 2, "2.20261020.00.00"),
                 ("visionos", 101, "1.02"),
                 ("tv", 7, TV_VERSION)]


def player_response_matrix(video_id, pot=None):
    """Try clients in order until one returns usable streaming data."""
    errors = []
    for name, cid, cver in CLIENT_MATRIX:
        try:
            pr = player_response(video_id, client=name)
        except Exception as exc:                       # noqa: BLE001
            errors.append("%s: %s" % (name, str(exc)[:60]))
            continue
        status = (pr.get("playabilityStatus") or {}).get("status")
        sd = pr.get("streamingData") or {}
        if sd.get("serverAbrStreamingUrl") or sd.get("adaptiveFormats"):
            if name != CLIENT_MATRIX[0][0]:
                print("  client fallback -> %s (%s)" % (name, status))
            return pr, name, cid, cver
        errors.append("%s: %s" % (name, status))
    raise SabrError("no client returned streaming data (%s)" % "; ".join(errors))


def run_probe(video_id, client, pot_arg, itag):
    pot = resolve_pot(pot_arg, video_id)
    pr = player_response(video_id, client=client,
                         pot=pot if os.environ.get("SABR_POT_IN_PLAYER") else None)
    status = (pr.get("playabilityStatus") or {}).get("status")
    sd = pr.get("streamingData") or {}
    print("playability   :", status, (pr.get("playabilityStatus") or {}).get("reason") or "")
    print("sabr url      :", "yes" if sd.get("serverAbrStreamingUrl") else "no")
    if "streamingData" not in pr:
        return
    print("ustreamer cfg : %d bytes" % len(ustreamer_config(pr)))
    fmts = sd.get("adaptiveFormats") or []
    print("formats       : %d" % len(fmts))
    fmt = pick_formats(pr, itag)
    print("chosen itag   : %s %s init=%s index=%s"
          % (fmt.get("itag"), fmt.get("mimeType"), fmt.get("initRange"), fmt.get("indexRange")))
    try:
        init, src = fetch_init_with_fallback(video_id, fmt, pot)
        print("init bytes    : %d (via %s)" % (len(init), src))
        entries = (parse_sidx(init) if "mp4" in (fmt.get("mimeType") or "")
                   else parse_webm_cues(init))
        print("segment index : %d entries" % len(entries))
        if entries:
            print("timeline      : %.3fs .. %.3fs"
                  % (entries[0][0] / 1000.0, entries[-1][1] / 1000.0))
    except SabrError as e:
        print("init range    : FAILED -", e)
    if sd.get("serverAbrStreamingUrl"):
        is_audio = "audio/" in (fmt.get("mimeType") or "")
        sess = SabrSession(sd["serverAbrStreamingUrl"], ustreamer_config(pr),
                           fmt if is_audio else None, None if is_audio else fmt,
                           po_token=pot, client_id=2,
                           client_version="2.20261020.00.00")
        try:
            counts = sess.request()
            print("sabr response : %d parts %s" % (sum(counts.values()), counts))
            print("protection    : %s"
                  % [p["status"] for p in sess.stats["protection"]][:3])
        except SabrError as e:
            print("sabr response :", e)


def _state_path(out):
    return out + ".sabrstate.json"


def _load_state(out, video_id, itag):
    try:
        with open(_state_path(out)) as fh:
            st = json.load(fh)
        if st.get("video") == video_id and int(st.get("itag", -1)) == int(itag):
            return st
    except Exception:
        pass
    return None


def _save_state(out, video_id, itag, sess, size):
    try:
        tmp = _state_path(out) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"video": video_id, "itag": int(itag),
                       "last_end_ms": sess.last_end_ms, "last_seq": sess.last_seq,
                       "segments": len(sess.written_seqs), "size": size,
                       "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh)
        os.replace(tmp, _state_path(out))
    except Exception:
        pass


def run_download(video_id, itag, mode, pot_arg, out, seconds, max_segments,
                 start_ms=0, resume=False):
    if str(itag) == "auto":
        pr_probe, _, _, _ = player_response_matrix(video_id)
        itag = pick_by_height(pr_probe, SESSION.get("max_height") or 720)
        print("auto itag     : %s (<= %sp)" % (itag, SESSION.get("max_height") or 720))
    pot = resolve_pot(pot_arg, video_id)
    pr, client_name, client_id, client_version = player_response_matrix(video_id)
    if mode == "sabr" and client_name == "mweb":
        pr, player_id = decode_mweb_urls(pr)
        print("player js     :", player_id)
    fmt = pick_formats(pr, itag)
    init, init_src = fetch_init_with_fallback(video_id, fmt, pot)
    print("init          : %d bytes (via %s)" % (len(init), init_src))
    entries = (parse_sidx(init) if "mp4" in (fmt.get("mimeType") or "")
               else parse_webm_cues(init))
    print("index         : %d segments, %.3fs" %
          (len(entries), (entries[-1][1] / 1000.0) if entries else 0))

    state = _load_state(out, video_id, itag) if resume else None
    mode_open = "ab" if state and os.path.exists(out) else "wb"
    if state:
        start_ms = int(state.get("last_end_ms") or start_ms)
        print("resume        : from %.1fs (%d segments already on disk)"
              % (start_ms / 1000.0, state.get("segments", 0)))

    with open(out, mode_open) as fh:
        if mode_open == "wb":
            fh.write(init)
        if mode == "direct":
            download_direct(fmt, fh, pot, seconds, out)
        else:
            url = pr["streamingData"]["serverAbrStreamingUrl"]
            is_audio = "audio/" in (fmt.get("mimeType") or "")

            def sink(seg, tmp, _fh=fh):
                if seg["initSegment"]:
                    return
                shutil.copyfileobj(tmp, _fh, 1 << 20)

            sess = SabrSession(url, ustreamer_config(pr),
                               fmt if is_audio else None,
                               None if is_audio else fmt,
                               po_token=pot, client_id=client_id,
                               client_version=client_version)
            sess.want_itag = int(itag)
            if state:
                sess.last_end_ms = int(state.get("last_end_ms") or 0)
                sess.last_seq = int(state.get("last_seq") or 0)
                sess._last_start_ms = sess.last_end_ms
                sess.written_seqs = set(range(1, sess.last_seq + 1))
            sess.on_segment = lambda s, _o=out: _save_state(_o, video_id, itag, s,
                                                            os.path.getsize(_o))

            def reloader(token, _vid=video_id):
                fresh = player_response(_vid, client="mweb", reload_token=token)
                if "streamingData" not in fresh:
                    raise SabrError("reload failed: %s"
                                    % (fresh.get("playabilityStatus") or {}).get("status"))
                fresh, _ = decode_mweb_urls(fresh)
                return (fresh["streamingData"]["serverAbrStreamingUrl"],
                        ustreamer_config(fresh))

            sess.reloader = reloader
            sess.pot_refresher = lambda force=False: pot_provider().get(video_id, force=force)
            if pot is None:
                sess.pot_refresher = None
            sess.run(sink, max_seconds=seconds or None, start_ms=start_ms,
                     wall_budget=1800,
                     expected_segments=(len(entries) or None) if not seconds else None)
            print("segments      : %d (%.1f MiB media)"
                  % (sess.segments, sess.bytes_media / 1048576.0))
            if sess.stats.get("missing_segments"):
                print("missing       : %s%s"
                      % (sess.stats["missing_segments"][:10],
                         " ..." if len(sess.stats["missing_segments"]) > 10 else ""))
    print("written       :", out, os.path.getsize(out), "bytes")


def run_stress(videos, itag, seconds, outdir, wall_budget=1200.0):
    os.makedirs(outdir, exist_ok=True)
    report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "itag": itag, "wall_budget_s": wall_budget, "videos": []}
    t_all = time.time()

    for vi, vid in enumerate(videos, 1):
        if time.time() - t_all > wall_budget:
            print("wall budget exhausted, stopping before", vid)
            break
        print("\n=== [%d/%d] %s" % (vi, len(videos), vid), flush=True)
        entry = {"video": vid}
        try:
            pr = player_response(vid, client="mweb")
            status = (pr.get("playabilityStatus") or {}).get("status")
            if status != "OK":
                entry["error"] = "playability %s" % status
                report["videos"].append(entry)
                continue
            pr, player_id = decode_mweb_urls(pr)
            fmt = pick_formats(pr, itag)
            init = None
            entries = []
            init_error = None
            try:
                init = fetch_init(fmt)
                entries = (parse_sidx(init) if "mp4" in (fmt.get("mimeType") or "")
                           else parse_webm_cues(init))
            except SabrError as exc:
                # init range can be gated (403) while the SABR session itself still
                # works; run without a timeline instead of failing the video
                init_error = str(exc)
            duration_s = entries[-1][1] / 1000.0 if entries else 0
            fallback = seconds or 300.0
            target = min(seconds, duration_s) if (seconds and duration_s) else (duration_s or fallback)
            out = os.path.join(outdir, "%02d_%s_%s%s" % (
                vi, vid, itag, ".mp4" if "mp4" in (fmt.get("mimeType") or "") else ".webm"))

            sess = SabrSession(pr["streamingData"]["serverAbrStreamingUrl"],
                               ustreamer_config(pr), fmt, None, po_token=None,
                               client_id=2, client_version="2.20261020.00.00",
                               verbose=False)
            t0 = time.time()
            with open(out, "wb") as fh:
                if init:
                    fh.write(init)

                def sink(seg, tmp, _fh=fh):
                    if seg["initSegment"]:
                        return
                    shutil.copyfileobj(tmp, _fh, 1 << 20)

                sess.run(sink, max_seconds=target,
                         wall_budget=max(60.0, wall_budget - (time.time() - t_all)))
            elapsed = time.time() - t0
            size = os.path.getsize(out)
            decode_errors = ffmpeg_check(out)

            entry.update({
                "player_js": player_id,
                "init_ok": init is not None,
                "init_error": init_error,
                "duration_s": round(duration_s, 2),
                "target_s": round(target, 2),
                "covered_s": round(sess.last_end_ms / 1000.0, 2),
                "segments": sess.segments,
                "bytes": size,
                "elapsed_s": round(elapsed, 1),
                "throughput_kib_s": round(size / 1024.0 / max(elapsed, 0.1), 1),
                "first_segment_ms": sess.stats["first_segment_ms"],
                "requests": len(sess.stats["requests"]),
                "req_ms": [r["ms"] for r in sess.stats["requests"]],
                "backoffs": sess.stats["backoffs"],
                "redirects": sess.stats["redirects"],
                "protection": sess.stats["protection"],
                "reloads": sess.stats["reloads"],
                "errors": sess.stats["errors"],
                "parts": sess.stats["parts"],
                "stalled": sess.stats.get("stalled", False),
                "decode_errors": decode_errors,
                "file": out,
            })
            print("  %s  %.1fs media / %.1fs wall  %.2f MiB  segs=%d reqs=%d  "
                  "backoff=%d redirect=%d errors=%d decode=%s"
                  % (vid, entry["covered_s"], elapsed, size / 1048576.0, sess.segments,
                     entry["requests"], len(entry["backoffs"]), len(entry["redirects"]),
                     len(entry["errors"]), "ok" if not decode_errors else decode_errors),
                  flush=True)
        except Exception as exc:                      # noqa: BLE001 - report, do not abort
            entry["error"] = "%s: %s" % (type(exc).__name__, exc)
            print("  FAILED:", entry["error"], flush=True)
        report["videos"].append(entry)

    ok = [v for v in report["videos"] if "error" not in v]
    report["totals"] = {
        "videos_ok": len(ok),
        "videos_failed": len(report["videos"]) - len(ok),
        "media_s": round(sum(v.get("covered_s", 0) for v in ok), 1),
        "wall_s": round(time.time() - t_all, 1),
        "bytes": sum(v.get("bytes", 0) for v in ok),
        "segments": sum(v.get("segments", 0) for v in ok),
        "requests": sum(v.get("requests", 0) for v in ok),
        "backoffs": sum(len(v.get("backoffs", [])) for v in ok),
        "redirects": sum(len(v.get("redirects", [])) for v in ok),
        "errors": sum(len(v.get("errors", [])) for v in ok),
        "protection_status_values": sorted({p["status"] for v in ok
                                            for p in v.get("protection", [])}),
    }
    path = os.path.join(outdir, "report.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2)
    print("\n=== totals:", json.dumps(report["totals"]))
    print("report:", path)
    return report


def ffmpeg_check(path):
    """Return None when ffmpeg decodes the file, else a short error string."""
    import subprocess
    if not shutil.which("ffmpeg"):
        return None
    proc = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                          capture_output=True, text=True)
    err = (proc.stderr or "").strip()
    if not err:
        return None
    # dts warnings on truncated streams are expected; only report hard errors
    hard = [ln for ln in err.splitlines()
            if "non monotonically" not in ln.lower()
            and "ended prematurely" not in ln.lower()]
    return " | ".join(hard[:2])[:160] if hard else None


# ---------------------------------------------------------------------------
# playback: local file + progressive HTTP
# ---------------------------------------------------------------------------

def _ffmpeg_mux(video_path, audio_path, out_path):
    import shutil
    if not shutil.which("ffmpeg"):
        return None
    args = ["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts",
            "-i", video_path, "-i", audio_path,
            "-c", "copy", "-shortest", "-avoid_negative_ts", "make_zero"]
    if out_path.endswith(".mp4"):
        args += ["-movflags", "+faststart"]
    proc = subprocess.run(args + [out_path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SabrError("mux failed: %s" % (proc.stderr or "").strip()[:200])
    return out_path


def _mime_of(pr, itag):
    for f in (pr.get("streamingData") or {}).get("adaptiveFormats") or []:
        if int(f.get("itag", 0)) == int(itag):
            return f.get("mimeType") or ""
    return ""


def _pick_container(pr, video_itag, audio_itag):
    want = SESSION.get("container") or "auto"
    if want in ("mp4", "mkv"):
        return "." + want
    v_mime = _mime_of(pr, video_itag)
    a_mime = _mime_of(pr, audio_itag)
    return ".mp4" if ("mp4" in v_mime and "mp4" in a_mime) else ".mkv"


def fetch_tracks(video_id, video_itag, audio_itag, base, pot_arg, seconds=None,
                 log=print, parallel=True):
    """Download one video + one audio track (in parallel) and mux them."""
    import threading
    pr_o, _, _, _ = player_response_matrix(video_id)
    if str(video_itag) == "auto":
        prefer_mp4 = (SESSION.get("container") or "auto") in ("auto", "mp4")
        video_itag = pick_by_height(pr_o, SESSION.get("max_height") or 144,
                                    mp4_only=prefer_mp4)
        log("  auto video itag: %s (%dp, %s)"
            % (video_itag, SESSION.get("max_height") or 144,
               "mp4" if prefer_mp4 else "any"))
    if str(audio_itag) == "auto":
        audio_itag = pick_audio(pr_o)
        log("  auto audio itag: %s" % audio_itag)
    ext = _pick_container(pr_o, video_itag, audio_itag)
    a_ext = ".m4a" if "mp4" in _mime_of(pr_o, audio_itag) else ".webm"
    audio = base + ".audio" + a_ext
    video = base + ".video" + (".mp4" if "mp4" in _mime_of(pr_o, video_itag) else ".webm")
    jobs = [("audio", audio_itag, audio), ("video", video_itag, video)]
    errors = []

    def work(kind, itag, path):
        try:
            log("  %s: itag %s -> %s" % (kind, itag, path))
            run_download(video_id, itag, "sabr", pot_arg, path, seconds or 0, 0)
        except Exception as exc:                        # noqa: BLE001
            errors.append("%s: %s" % (kind, exc))

    if parallel:
        threads = [threading.Thread(target=work, args=j) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for j in jobs:
            work(*j)
    if errors:
        raise SabrError("; ".join(errors))
    final = base + ext
    _ffmpeg_mux(video, audio, final)
    log("  muxed: %s" % final)
    return final


def _growing_handler(state):
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self._serve(True)

        def do_GET(self):
            self._serve(False)

        def _serve(self, head_only):
            path = state.get("file")
            if not path or not os.path.exists(path):
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = os.path.getsize(path)
            if size <= 0:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            ctype = ("video/x-matroska" if path.endswith(".mkv")
                     else "video/webm" if path.endswith(".webm")
                     else "video/mp4" if path.endswith(".mp4")
                     else "audio/mp4" if path.endswith(".m4a")
                     else "audio/webm" if path.endswith(".webm")
                     else "application/octet-stream")
            rng = self.headers.get("Range")
            start, end, status = 0, max(0, size - 1), 200
            if rng and rng.startswith("bytes="):
                try:
                    a, b = rng[6:].split("-", 1)
                    start = int(a) if a else 0
                    end = int(b) if b else size - 1
                    end = min(end, max(0, size - 1))
                    status = 206
                except Exception:
                    start, end, status = 0, max(0, size - 1), 200
            if start > end:
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if status == 206:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            if head_only:
                return
            with open(path, "rb") as fh:
                fh.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    remaining -= len(chunk)

    return Handler


def run_serve(video_id, video_itag, audio_itag, port, pot_arg, seconds, outdir):
    """Download in the background while serving the growing file over HTTP."""
    import threading
    import http.server

    os.makedirs(outdir, exist_ok=True)
    base = os.path.join(outdir, video_id)
    state = {"file": None, "ready": False, "error": None}

    pr_o, _, _, _ = player_response_matrix(video_id)
    if str(video_itag) == "auto" or str(audio_itag) == "auto":
        if str(video_itag) == "auto":
            prefer_mp4 = (SESSION.get("container") or "auto") in ("auto", "mp4")
            video_itag = pick_by_height(pr_o, SESSION.get("max_height") or 720,
                                        mp4_only=prefer_mp4)
        if str(audio_itag) == "auto":
            audio_itag = pick_audio(pr_o)
        print("  auto itags   : video=%s audio=%s" % (video_itag, audio_itag), flush=True)
    ext = _pick_container(pr_o, video_itag, audio_itag)

    def worker():
        try:
            a_ext = ".m4a" if "mp4" in _mime_of(pr_o, audio_itag) else ".webm"
            audio = base + ".audio" + a_ext
            video = base + ".video" + (".mp4" if "mp4" in _mime_of(pr_o, video_itag) else ".webm")
            # picture first: the video track is served as it is written, audio is pulled
            # in parallel and merged at the end (the browser reloads for sound)
            open(video, "ab").close()
            state["file"] = video
            import threading as _t
            ath = _t.Thread(target=lambda: (print("  [bg] audio %s -> %s"
                                                 % (audio_itag, audio), flush=True),
                                            run_download(video_id, audio_itag, "sabr",
                                                         pot_arg, audio, seconds or 0, 0)),
                            daemon=True)
            ath.start()
            print("  [bg] video %s -> %s" % (video_itag, video), flush=True)
            run_download(video_id, video_itag, "sabr", pot_arg, video, seconds or 0, 0)
            ath.join()
            final = base + ext
            _ffmpeg_mux(video, audio, final)
            state["file"] = final
            state["ready"] = True
            print("  [bg] muxed: %s" % final, flush=True)
        except Exception as exc:                       # noqa: BLE001
            state["error"] = str(exc)
            print("  [bg] failed: %s" % exc, flush=True)

    threading.Thread(target=worker, daemon=True).start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), _growing_handler(state))
    print("serving on http://<device-ip>:%d/  (file grows while it downloads)" % port,
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# search / SponsorBlock / ABR ladder
# ---------------------------------------------------------------------------

def run_search(query, limit=10):
    """InnerTube search -> (videoId, title, channel, duration)."""
    body = {"context": {"client": {"clientName": "WEB",
                                   "clientVersion": "2.20261020.00.00",
                                   "hl": "en", "gl": "US"}},
            "query": query}
    st, hd, raw = http("https://www.youtube.com/youtubei/v1/search?prettyPrint=false",
                       data=json.dumps(body).encode(),
                       headers={"Content-Type": "application/json",
                                "User-Agent": DESKTOP_UA,
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
        if isinstance(node, dict):
            if "videoRenderer" in node:
                v = node["videoRenderer"]
                out.append((v.get("videoId"), text_of(v.get("title")),
                            text_of(v.get("ownerText")),
                            text_of(v.get("lengthText"))))
                return
            for v in node.values():
                walk(v)
                if len(out) >= limit:
                    return
        elif isinstance(node, list):
            for v in node:
                walk(v)
                if len(out) >= limit:
                    return

    walk(data)
    for i, (vid, title, owner, dur) in enumerate(out[:limit], 1):
        print("%2d. %-11s  %-6s  %s  [%s]" % (i, vid, dur, title[:56], owner[:24]))
    return out


def sponsor_segments(video_id, categories=("sponsor", "selfpromo", "interaction",
                                           "intro", "outro")):
    """SponsorBlock API v2 -> [{start, end, category}].

    The `?videoID=` form was retired; the public API now only answers
    `/api/skipSegments/<sha256(videoId)[:4]>`, so we fetch the bucket and filter.
    """
    import hashlib
    prefix = hashlib.sha256(video_id.encode()).hexdigest()[:4]
    qs = urllib.parse.urlencode({"categories": json.dumps(list(categories))})
    try:
        st, hd, raw = http("https://sponsor.ajay.app/api/skipSegments/%s?%s"
                           % (prefix, qs),
                           headers={"User-Agent": "PipePipe-re/1.0",
                                    "Accept": "application/json"}, retries=2)
    except SabrError:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    for entry in data if isinstance(data, list) else []:
        if entry.get("videoID") != video_id:
            continue
        for item in entry.get("segments") or []:
            seg = item.get("segment") or []
            if len(seg) == 2:
                out.append({"start": float(seg[0]), "end": float(seg[1]),
                            "category": item.get("category", "?")})
    return sorted(out, key=lambda s: s["start"])


def cut_sponsor(src_path, out_path, segments):
    """Re-encode around the sponsored ranges (concat of the kept pieces)."""
    import shutil
    if not shutil.which("ffmpeg"):
        raise SabrError("ffmpeg not available")
    keep, cursor = [], 0.0
    for seg in segments:
        if seg["start"] > cursor:
            keep.append((cursor, seg["start"]))
        cursor = max(cursor, seg["end"])
    keep.append((cursor, None))
    parts, filters = [], []
    for i, (a, b) in enumerate(keep):
        parts.append("[0:v]trim=%s%s,setpts=PTS-STARTPTS[v%d]"
                     % (a, (":%f" % b) if b else "", i))
        parts.append("[0:a]atrim=%s%s,asetpts=PTS-STARTPTS[a%d]"
                     % (a, (":%f" % b) if b else "", i))
        filters.append("[v%d][a%d]" % (i, i))
    graph = ";".join(parts) + ";" + "".join(filters) + \
        "concat=n=%d:v=1:a=1[v][a]" % len(keep)
    proc = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src_path,
                           "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                           out_path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SabrError("cut failed: %s" % (proc.stderr or "").strip()[:200])
    return out_path


def run_ui(video_id, max_height, port, pot_arg, seconds):
    """Download one video, cut three portrait clips, and serve the feed UI."""
    import shutil
    here = os.path.dirname(os.path.abspath(__file__))
    ui_dir = os.path.join(here, "ui")
    clips = os.path.join(ui_dir, "clips")
    os.makedirs(clips, exist_ok=True)

    SESSION["max_height"] = max_height
    SESSION["container"] = "mp4"
    src = os.path.join(ui_dir, "_src.mp4")
    print("downloading %s (<=%dp)..." % (video_id, max_height))
    run_download(video_id, "auto", "sabr", pot_arg, src, seconds or 0, 0)

    total = 0.0
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", src], capture_output=True, text=True)
        total = float((out.stdout or "0").strip() or 0)
    except Exception:
        pass
    if seconds:
        # the container still advertises the full length; only trust what we pulled
        total = min(total, float(seconds))
    for i, frac in enumerate((0.05, 0.40, 0.72), start=1):
        ss = min(max(0.0, total - 15.0), max(0.0, total * frac))
        dst = os.path.join(clips, "%d.mp4" % i)
        for attempt in range(3):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "%.2f" % ss, "-i", src,
                            "-t", "14", "-vf", "crop=ih*9/16:ih,scale=720:-2",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", dst],
                           capture_output=True)
            if os.path.exists(dst) and os.path.getsize(dst) > 20000:
                break
            ss *= 0.5                      # the pulled file may end sooner than its metadata
        print("  clip %d @ %.0fs -> %s" % (i, ss, dst))
    try:
        os.remove(src)
    except OSError:
        pass
    print("serving UI on http://<device-ip>:%d/" % port)
    subprocess.run([sys.executable, os.path.join(here, "ui_server.py"), ui_dir, str(port)])


def run_serve_abr(video_id, heights, port, pot_arg, seconds, outdir):
    """Build a live HLS ladder: each rendition grows while the player picks one."""
    import threading
    import http.server
    import shutil

    os.makedirs(outdir, exist_ok=True)
    pr, client_name, cid, cver = player_response_matrix(video_id)
    if client_name == "mweb":
        pr, _ = decode_mweb_urls(pr)
    audio_itag = pick_audio(pr, mp4_only=True)
    variants = []
    for h in sorted(set(heights)):
        try:
            itag = pick_exact_height(pr, h, mp4_only=True)
        except SabrError:
            continue
        fmt = pick_formats(pr, itag)
        variants.append((h, itag, int(fmt.get("bitrate") or 1)))
    if not variants:
        raise SabrError("no video renditions available")

    print("abr ladder    : audio=%s video=%s"
          % (audio_itag, ", ".join("%dp/itag%s" % (h, i) for h, i, _ in variants)),
          flush=True)

    playlists = {}

    def download_rendition(itag, name, audio=False):
        fmt = pick_formats(pr, itag)
        init_path = os.path.join(outdir, "init_%s.mp4" % name)
        init, _ = fetch_init_with_fallback(video_id, fmt, pot_arg if pot_arg != "auto" else None)
        with open(init_path, "wb") as fh:
            fh.write(init)
        pl_path = os.path.join(outdir, "%s.m3u8" % name)
        entries = []
        with open(pl_path, "w") as pl:
            pl.write("#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-PLAYLIST-TYPE:EVENT\n")
            pl.write("#EXT-X-TARGETDURATION:11\n#EXT-X-MEDIA-SEQUENCE:0\n")
            pl.write('#EXT-X-MAP:URI="init_%s.mp4"\n' % name)
            pl.flush()

            def sink(seg, tmp):
                if seg["initSegment"]:
                    return
                path = os.path.join(outdir, "%s_%05d.m4s" % (name, seg["sequenceNumber"]))
                with open(path, "wb") as out:
                    shutil.copyfileobj(tmp, out, 1 << 20)
                dur = max(0.001, (seg["durationMs"] or 10000) / 1000.0)
                pl.write("#EXTINF:%.3f,\n%s\n"
                         % (dur, os.path.basename(path)))
                pl.flush()

            sess = SabrSession(pr["streamingData"]["serverAbrStreamingUrl"],
                               ustreamer_config(pr), fmt if audio else None,
                               None if audio else fmt, po_token=resolve_pot("auto", video_id),
                               client_id=cid, client_version=cver, verbose=False)
            sess.want_itag = int(itag)
            sess.pot_refresher = lambda force=False: pot_provider().get(video_id, force=force)
            try:
                sess.run(sink, max_seconds=seconds or None, wall_budget=3600)
            except Exception as exc:                    # noqa: BLE001
                print("  [abr] %s failed: %s" % (name, exc), flush=True)
            with open(pl_path, "a") as pl:
                pl.write("#EXT-X-ENDLIST\n")
        print("  [abr] %s done: %d segments" % (name, sess.segments), flush=True)

    master = os.path.join(outdir, "master.m3u8")
    with open(master, "w") as fh:
        fh.write("#EXTM3U\n#EXT-X-VERSION:7\n")
        fh.write('#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="audio",'
                 'DEFAULT=YES,AUTOSELECT=YES,URI="a_%s.m3u8"\n' % audio_itag)
        for h, itag, br in variants:
            fh.write('#EXT-X-STREAM-INF:BANDWIDTH=%d,RESOLUTION=%dx%d,AUDIO="aud"\n'
                     % (br, int(h * 16 / 9), h))
            fh.write("v_%s.m3u8\n" % itag)
    playlists["master"] = master

    threads = [threading.Thread(target=download_rendition, args=(audio_itag, "a_%s" % audio_itag, True))]
    for h, itag, _ in variants:
        threads.append(threading.Thread(target=download_rendition, args=(itag, "v_%s" % itag, False)))
    for t in threads:
        t.start()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=outdir, **kw)

        def log_message(self, *args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("serving HLS on http://<device-ip>:%d/master.m3u8" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

def selftest():
    here = os.path.dirname(os.path.abspath(__file__))
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("%s %s %s" % ("PASS" if cond else "FAIL", name, extra))

    # 1. UMP integer round-trip across all five widths
    for v in (0x10, 0x7F, 0x80, 0x1234, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000, 0x0FFFFFFF, 0x10000000):
        frame = ump_pack(21, b"\x00" * v if v < 70000 else b"")
        ptype = ump_int(_Mem(frame))
        check("ump int %#x" % v, ptype == 21)

    # 2. real SABR error response captured from YouTube (fixture)
    fixture = os.path.join(here, "fixtures", "sabr_error.bin")
    if os.path.exists(fixture):
        data = open(fixture, "rb").read()
        parts = [(t, s, p.readall()) for t, s, p in PartReader(_Mem(data))]
        check("fixture parses as UMP", len(parts) == 1 and parts[0][0] == 44,
              "%r" % [(PART.get(t), s) for t, s, _ in parts])
        if parts:
            etype = (pb_get(parts[0][2], 1, 2) or b"").decode()
            code = pb_get(parts[0][2], 2, 0)
            check("fixture error decoded", etype == "sabr.malformed_config",
                  "type=%s code=%s" % (etype, code))

    # 3. sidx from a real itag 137 init segment
    mp4 = os.path.join(here, "fixtures", "init_137.mp4")
    if os.path.exists(mp4):
        data = open(mp4, "rb").read()
        entries = parse_sidx(data)
        check("sidx parsed", len(entries) > 10, "%d entries, %.2fs" %
              (len(entries), entries[-1][1] / 1000.0 if entries else 0))
        if entries:
            check("sidx monotonic", all(b[0] == a[1] for a, b in zip(entries, entries[1:])))

    # 4. WebM Cues from a real itag 251 init segment
    webm = os.path.join(here, "fixtures", "init_251.webm")
    if os.path.exists(webm):
        data = open(webm, "rb").read()
        check("webm ebml magic", data[:4] == b"\x1aE\xdf\xa3")
        cues = parse_webm_cues(data)
        check("cues parsed", len(cues) > 0, "%d cue points" % len(cues))

    # 5. request builder shape
    cfg = b"\xc0\x81\x02"
    req = build_request(cfg, audio={"itag": 251, "bitrate": 128000}, video={"itag": 137,
                        "width": 1920, "height": 1080, "bitrate": 4000000},
                        player_time_ms=5000)
    fields = {n: w for n, w, _ in pb_fields(req)}
    check("request fields", fields.get(1) == 2 and fields.get(5) == 2 and
          fields.get(19) == 2 and fields.get(16) == 2 and fields.get(17) == 2,
          str(sorted(fields)))
    print("\n%s" % ("ALL PASS" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


class _Mem:
    def __init__(self, data):
        self.data = data
        self.i = 0

    def read(self, n=-1):
        if n < 0:
            n = len(self.data) - self.i
        chunk = self.data[self.i:self.i + n]
        self.i += len(chunk)
        return chunk


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="YouTube SABR client (reverse-engineered)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="run offline checks against shipped fixtures")

    p = sub.add_parser("probe", help="inspect a player response and try one SABR request")
    p.add_argument("--video", required=True)
    p.add_argument("--client", default="visionos", choices=["visionos", "mweb"])
    p.add_argument("--pot", help="base64url poToken")
    p.add_argument("--itag", type=int)

    m = sub.add_parser("mint", help="mint a PO token (delegates to potnode/mint2.mjs)")
    m.add_argument("--video", required=True)

    sv = sub.add_parser("serve", help="stream a video while downloading it")
    sv.add_argument("--video", required=True)
    sv.add_argument("--video-itag", default="auto")
    sv.add_argument("--audio-itag", default="auto")
    sv.add_argument("--cookies", help="Netscape cookies.txt or a raw Cookie header")
    sv.add_argument("--max-height", type=int, default=0)
    sv.add_argument("--container", choices=["auto", "mp4", "mkv"], default="auto")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--pot", default="auto")
    sv.add_argument("--seconds", type=float, default=0)
    sv.add_argument("--outdir", default="stream")

    pl = sub.add_parser("play", help="download a video+audio pair, mux it, open it")
    pl.add_argument("--video", required=True)
    pl.add_argument("--video-itag", default="auto")
    pl.add_argument("--audio-itag", default="auto")
    pl.add_argument("--cookies", help="Netscape cookies.txt or a raw Cookie header")
    pl.add_argument("--max-height", type=int, default=0)
    pl.add_argument("--container", choices=["auto", "mp4", "mkv"], default="auto")
    pl.add_argument("--pot", default="auto")
    pl.add_argument("--seconds", type=float, default=0)
    pl.add_argument("--outdir", default="downloads")

    se = sub.add_parser("search", help="search YouTube and print video ids")
    se.add_argument("query")
    se.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("sponsor", help="list SponsorBlock segments for a video")
    sp.add_argument("--video", required=True)

    ab = sub.add_parser("abr", help="serve a live HLS ladder (player-side ABR)")
    ab.add_argument("--video", required=True)
    ab.add_argument("--heights", default="144,240,360")
    ab.add_argument("--port", type=int, default=8765)
    ab.add_argument("--pot", default="auto")
    ab.add_argument("--seconds", type=float, default=0)
    ab.add_argument("--outdir", default="abr")

    ui = sub.add_parser("ui", help="build the feed UI from a video and serve it")
    ui.add_argument("--video", required=True)
    ui.add_argument("--max-height", type=int, default=480)
    ui.add_argument("--port", type=int, default=8791)
    ui.add_argument("--pot", default="auto")
    ui.add_argument("--seconds", type=float, default=0)

    st = sub.add_parser("stress", help="long-run stability test across videos")
    st.add_argument("--videos", required=True, help="comma separated video ids")
    st.add_argument("--itag", type=int, default=251)
    st.add_argument("--seconds", type=float, default=0, help="cap per video (0 = full)")
    st.add_argument("--wall-budget", type=float, default=1200)
    st.add_argument("--outdir", default="stress")

    d = sub.add_parser("download", help="download one adaptive format")
    d.add_argument("--video", required=True)
    d.add_argument("--itag", type=int, required=True)
    d.add_argument("--mode", default="direct", choices=["direct", "sabr"])
    d.add_argument("--pot", help="base64url poToken (required by mweb/sabr)")
    d.add_argument("--seconds", type=float, default=0)
    d.add_argument("--start", type=float, default=0, help="start offset in seconds")
    d.add_argument("--resume", action="store_true", help="continue a previous run")
    d.add_argument("--cookies", help="Netscape cookies.txt or a raw Cookie header")
    d.add_argument("--max-height", type=int, default=0,
                   help="with --itag auto: pick the cheapest track at or below this height")
    d.add_argument("--max-segments", type=int, default=0)
    d.add_argument("-o", "--out", required=True)

    args = ap.parse_args(argv)
    cookie = load_cookie_header(getattr(args, "cookies", None))
    if cookie:
        SESSION["cookie"] = cookie
    if getattr(args, "max_height", 0):
        SESSION["max_height"] = args.max_height
    if getattr(args, "container", None):
        SESSION["container"] = args.container
    try:
        if args.cmd == "selftest":
            return selftest()
        if args.cmd == "mint":
            pot = resolve_pot("auto", args.video)
            print(base64.urlsafe_b64encode(pot).decode().rstrip("="))
            return 0
        if args.cmd == "ui":
            run_ui(args.video, args.max_height, args.port, args.pot, args.seconds)
            return 0
        if args.cmd == "search":
            run_search(args.query, args.limit)
            return 0
        if args.cmd == "sponsor":
            segs = sponsor_segments(args.video)
            if not segs:
                print("no SponsorBlock segments")
            for s in segs:
                print("%7.1f - %7.1f  %s" % (s["start"], s["end"], s["category"]))
            return 0
        if args.cmd == "abr":
            run_serve_abr(args.video, [int(x) for x in args.heights.split(",")],
                          args.port, args.pot, args.seconds, args.outdir)
            return 0
        if args.cmd == "serve":
            run_serve(args.video, args.video_itag, args.audio_itag, args.port,
                      args.pot, args.seconds, args.outdir)
            return 0
        if args.cmd == "play":
            os.makedirs(args.outdir, exist_ok=True)
            mkv = fetch_tracks(args.video, args.video_itag, args.audio_itag,
                               os.path.join(args.outdir, args.video), args.pot,
                               args.seconds or None)
            print("ready:", mkv)
            return 0
        if args.cmd == "stress":
            run_stress([v.strip() for v in args.videos.split(",") if v.strip()],
                       args.itag, args.seconds, args.outdir, args.wall_budget)
            return 0
        if args.cmd == "probe":
            run_probe(args.video, args.client, args.pot, args.itag)
        else:
            run_download(args.video, args.itag, args.mode, args.pot, args.out,
                         args.seconds, args.max_segments,
                         start_ms=int(args.start * 1000), resume=args.resume)
    except SabrError as e:
        print("error:", e, file=sys.stderr)
        return 2
    return 0


class PotProvider:
    """PO token lifecycle: cache -> mint (with retries) -> cold-start fallback.

    A token is content-bound and lives ~12 h. Minting is a Node subprocess (jsdom +
    BotGuard) which is heavy and occasionally dies, so every failure path has to
    degrade instead of aborting the download.
    """

    CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pot_cache.json")
    MINT_ATTEMPTS = 3
    EXPIRY_MARGIN_S = 60

    def __init__(self, verbose=True):
        self.verbose = verbose
        self.cache = {}
        self._load()

    def log(self, msg):
        if self.verbose:
            print("  pot: " + msg, flush=True)

    def _load(self):
        try:
            with open(self.CACHE) as fh:
                self.cache = json.load(fh)
        except Exception:
            self.cache = {}

    def _save(self):
        try:
            tmp = self.CACHE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.cache, fh)
            os.replace(tmp, self.CACHE)
        except Exception:
            pass

    def get(self, video_id, force=False):
        if not force:
            entry = self.cache.get(video_id)
            if entry and time.time() - entry.get("ts", 0) < entry.get("ttl", 0) - self.EXPIRY_MARGIN_S:
                try:
                    return decode_pot(entry["token"])
                except Exception:
                    pass
        token, ttl = self._mint(video_id)
        self.cache[video_id] = {"token": token, "ts": time.time(), "ttl": ttl}
        self._save()
        return decode_pot(token)

    def _mint(self, video_id):
        here = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(here, "potnode", "mint2.mjs")
        if os.path.exists(script):
            for attempt in range(1, self.MINT_ATTEMPTS + 1):
                try:
                    env = dict(os.environ)
                    if SESSION.get("cookie"):
                        env["POT_COOKIE"] = SESSION["cookie"]
                    proc = subprocess.run(["node", script, video_id],
                                          capture_output=True, text=True, timeout=180,
                                          env=env)
                    token = (proc.stdout or "").strip().splitlines()
                    if proc.returncode == 0 and token:
                        self.log("minted %s (%d bytes)" % (video_id, len(token[-1])))
                        return token[-1], 43200
                    tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
                    self.log("mint attempt %d/%d failed: %s"
                             % (attempt, self.MINT_ATTEMPTS, tail[0][:120]))
                except Exception as exc:
                    self.log("mint attempt %d/%d error: %s" % (attempt, self.MINT_ATTEMPTS, exc))
                time.sleep(1.5 * attempt)
        # last resort: a locally constructed cold-start token (valid while sps == 2)
        self.log("falling back to a cold-start token")
        return cold_start_token(video_id), 6 * 3600

    def invalidate(self, video_id):
        self.cache.pop(video_id, None)
        self._save()


def cold_start_token(content_binding, client_state=1):
    """Port of bgutils' createColdStartToken: works while sps == 2, no VM needed."""
    import struct as _struct
    ts = int(time.time())
    keys = [random.randrange(256), random.randrange(256)]
    header = bytes(keys + [0, client_state & 0xFF] + list(_struct.pack(">I", ts)))
    payload = bytearray(header + content_binding.encode("utf-8"))
    for i in range(2, len(payload)):
        payload[i] ^= payload[i % 2]
    packet = bytes([34, len(payload)]) + bytes(payload)
    return base64.urlsafe_b64encode(packet).decode().rstrip("=")


_POT_PROVIDER = None


def pot_provider():
    global _POT_PROVIDER
    if _POT_PROVIDER is None:
        _POT_PROVIDER = PotProvider()
    return _POT_PROVIDER


def resolve_pot(pot_arg, video_id, force=False):
    """`--pot auto` uses the cached minting pipeline; a literal token is used as-is."""
    if not pot_arg:
        return None
    if pot_arg != "auto":
        return decode_pot(pot_arg)
    return pot_provider().get(video_id, force=force)


def decode_pot(text):
    if not text:
        return None
    text = text.strip().replace("\n", "")
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=False)


if __name__ == "__main__":
    sys.exit(main())
