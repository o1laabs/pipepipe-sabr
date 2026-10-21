#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render the stability chart from stress10/report.json (minimal, editorial)."""
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

INK = "#111111"
MUTED = "#6b6b6b"
HAIR = "#dddddd"
ACCENT = "#1F5F8B"
WARN = "#B4530A"

d = json.load(open("/var/minis/workspace/pipepipe-re/stress10/report.json"))
vids = [v for v in d["videos"] if "error" not in v]

lat, cum_media, cum_wall = [], [], []
wall = 0.0
media = 0.0
for v in vids:
    for ms in v["req_ms"]:
        lat.append(ms)
        wall += ms / 1000.0
        media += 10.0
        cum_media.append(media)
        cum_wall.append(wall)

from matplotlib import font_manager
CJK = "/usr/share/fonts/noto/NotoSansCJK-Regular.ttc"
try:
    font_manager.fontManager.addfont(CJK)
    CJK_NAME = font_manager.FontProperties(fname=CJK).get_name()
except Exception:
    CJK_NAME = "Noto Sans CJK SC"

plt.rcParams.update({
    "font.family": [CJK_NAME, "DejaVu Sans"],
    "axes.edgecolor": HAIR,
    "axes.labelcolor": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "figure.facecolor": "white",
})

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.0), dpi=200,
                               gridspec_kw={"width_ratios": [1.35, 1]})

# left: per-request latency
ax1.plot(range(1, len(lat) + 1), lat, color=ACCENT, linewidth=1.4)
ax1.fill_between(range(1, len(lat) + 1), lat, color=ACCENT, alpha=0.07)
ax1.axhline(sum(lat) / len(lat), color=HAIR, linewidth=1, linestyle="--")
for x in (21.5, 42.5):
    ax1.axvline(x, color=HAIR, linewidth=1)
ax1.text(11, max(lat) * 1.10, "第 1 段", ha="center", color=MUTED, fontsize=8)
ax1.text(32, max(lat) * 1.10, "第 2 段", ha="center", color=MUTED, fontsize=8)
ax1.text(53, max(lat) * 1.10, "第 3 段", ha="center", color=MUTED, fontsize=8)
ax1.set_title("每请求耗时（63 次，3 段连续会话）", loc="left", fontsize=10.5, color=INK)
ax1.set_xlabel("SABR request #", fontsize=9)
ax1.set_ylabel("ms", fontsize=9)
ax1.set_ylim(0, max(lat) * 1.18)
ax1.xaxis.set_major_locator(MaxNLocator(6))
for s in ("top", "right"):
    ax1.spines[s].set_visible(False)

# right: cumulative media vs wall clock
ax2.plot(cum_wall, cum_media, color=INK, linewidth=1.6)
ax2.plot([0, max(cum_wall)], [0, max(cum_wall)], color=HAIR, linewidth=1, linestyle="--")
ax2.text(max(cum_wall) * 0.52, max(cum_wall) * 0.44, "real time (1×)",
         color=MUTED, fontsize=8, rotation=0)
ax2.set_title("媒体时长 vs 实际耗时", loc="left", fontsize=10.5, color=INK)
ax2.set_xlabel("wall clock (s)", fontsize=9)
ax2.set_ylabel("media covered (s)", fontsize=9)
for s in ("top", "right"):
    ax2.spines[s].set_visible(False)

fig.text(0.012, 0.015,
         "10.5 min 连续播放 · 63 请求 / 63 段 · 0 退避 / 0 重定向 / 0 错误 · 10.2 MiB · 全部可在 ffmpeg 解码",
         fontsize=8.2, color=MUTED)

fig.tight_layout(rect=(0, 0.045, 1, 1))
fig.savefig("/var/minis/workspace/pipepipe-re/stability.png", facecolor="white")
print("saved stability.png", len(lat), "requests,", media, "s media")
