#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B test: does a locally minted PO token lift YouTube's 60-second SABR cap?

For each video it runs the same 90-second SABR session twice — once without a
token, once with a freshly minted (content-bound) token — and prints what the
server did: stream protection status, media seconds covered, segments, backoff.

    python3 pot_ab.py 9bZkp7q19f0
    python3 pot_ab.py 9bZkp7q19f0 dQw4w9WgXcQ
"""
import json
import os
import sys

import sabr


def run_once(video_id, pot_arg, seconds=90):
    """Return a dict describing one SABR attempt."""
    out = {"video": video_id, "pot": bool(pot_arg), "status": None,
           "covered_s": 0.0, "segments": 0, "backoffs": [], "protection": [],
           "stall": False, "error": None}
    try:
        pot = sabr.resolve_pot(pot_arg, video_id) if pot_arg else None
        pr = sabr.player_response(video_id, client="mweb")
        out["status"] = (pr.get("playabilityStatus") or {}).get("status")
        if "streamingData" not in pr:
            out["error"] = "no streamingData (%s)" % out["status"]
            return out
        pr, _ = sabr.decode_mweb_urls(pr)
        fmt = sabr.pick_formats(pr, 251)
        init = None
        try:
            init = sabr.fetch_init(fmt, pot)
        except sabr.SabrError as exc:
            out["init_error"] = str(exc)
        sess = sabr.SabrSession(pr["streamingData"]["serverAbrStreamingUrl"],
                                sabr.ustreamer_config(pr), fmt, None, po_token=pot,
                                client_id=2, client_version="2.20261020.00.00",
                                verbose=False)
        path = "/tmp/pot_ab_%s_%s.webm" % (video_id, "pot" if pot_arg else "nopot")
        with open(path, "wb") as fh:
            if init:
                fh.write(init)

            def sink(seg, tmp, _fh=fh):
                if not seg["initSegment"]:
                    import shutil
                    shutil.copyfileobj(tmp, _fh, 1 << 20)

            sess.run(sink, max_seconds=seconds, wall_budget=240)
        out.update({
            "covered_s": round(sess.last_end_ms / 1000.0, 1),
            "segments": sess.segments,
            "backoffs": [b["ms"] for b in sess.stats["backoffs"]],
            "protection": [p["status"] for p in sess.stats["protection"]][:4],
            "stall": sess.stats.get("stalled", False),
            "bytes": os.path.getsize(path),
            "file": path,
        })
    except Exception as exc:                    # noqa: BLE001 - report, keep going
        out["error"] = "%s: %s" % (type(exc).__name__, exc)
    return out


def main(argv):
    videos = argv[1:] or ["dQw4w9WgXcQ"]
    report = []
    for vid in videos:
        for pot_arg in (None, "auto"):
            r = run_once(vid, pot_arg)
            report.append(r)
            tag = "pot" if pot_arg else "no-pot"
            print("%-12s %-7s status=%-14s covered=%5.1fs segs=%2d prot=%s backoff=%s%s"
                  % (vid, tag, r.get("status"), r.get("covered_s", 0), r.get("segments", 0),
                     r.get("protection"), r.get("backoffs"), " ERR:" + r["error"] if r.get("error") else ""))
    with open("pot_ab.json", "w") as fh:
        json.dump(report, fh, indent=2)
    print("\nwrote pot_ab.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
