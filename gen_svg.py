#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate a minimal architecture SVG for the PipePipe RE report."""

W, H = 1440, 940
FONT = "system-ui, -apple-system, 'PingFang SC', 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif"
MONO = "ui-monospace, 'SF Mono', Menlo, 'Noto Sans Mono CJK SC', monospace"
INK = "#111111"
MUTED = "#6b6b6b"
HAIR = "#d8d8d8"
ACCENT = "#B4530A"      # 闭源/风险
BLUE = "#1F5F8B"        # 协议数据流

out = []
def add(s): out.append(s)

def rect(x, y, w, h, rx=6, stroke=HAIR, fill="#fff", dash=None, sw=1):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="{sw}"{d}/>')

def text(x, y, s, size=14, fill=INK, weight="400", anchor="start", mono=False, opacity=1):
    f = MONO if mono else FONT
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    add(f'<text x="{x}" y="{y}" font-family="{f}" font-size="{size}" fill="{fill}" '
        f'font-weight="{weight}" text-anchor="{anchor}" opacity="{opacity}">{s}</text>')

def line(x1, y1, x2, y2, stroke=HAIR, sw=1, dash=None, marker=False):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    m = ' marker-end="url(#arrow)"' if marker else ""
    add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" stroke-width="{sw}"{d}{m}/>')

def path(d, stroke=HAIR, sw=1, dash=None, marker=False):
    ds = f' stroke-dasharray="{dash}"' if dash else ""
    m = ' marker-end="url(#arrow)"' if marker else ""
    add(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}"{ds}{m}/>')

add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
add(f'''<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="{BLUE}"/>
  </marker>
  <marker id="arroww" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
    <path d="M0,0 L10,5 L0,10 z" fill="{HAIR}"/>
  </marker>
</defs>''')
add(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')

# ---- header
text(60, 64, "PipePipe · 核心功能逆向", 26, INK, "600")
text(60, 92, "GPLv3 源码基线 2026-10-19 ｜ Client 108,139 行 + Extractor 64,177 行 ｜ 唯一闭源件：api.pipepipe.dev", 13, MUTED)
line(60, 112, 1380, 112)

# ---- column labels
text(60, 150, "客户端 · Android", 13, MUTED, "500")
text(560, 150, "提取层 · 纯 JVM", 13, MUTED, "500")
text(1090, 150, "外部端点", 13, MUTED, "500")

# ---- client column
rect(60, 172, 420, 96)
text(84, 200, "ExoPlayer / Media3", 15, INK, "600")
text(84, 224, "SabrDashMediaSource — 伪造 MPD", 12.5, MUTED, mono=False)
text(84, 244, "SabrMediaBridge / SabrSegmentDataSource", 12.5, MUTED)

rect(60, 288, 420, 84)
text(84, 316, "SharedWebViewRuntime", 15, INK, "600")
text(84, 340, "隐藏 WebView · PipePipeWebViewBridge", 12.5, MUTED)
text(84, 358, "EJS 解码器  ｜  BotGuard 铸造器", 12.5, MUTED)

rect(60, 392, 420, 76)
text(84, 420, "DownloadManager (giga)", 15, INK, "600")
text(84, 444, "按段下载 · 断点恢复 · SABR 会话", 12.5, MUTED)

rect(60, 488, 420, 88, fill="#FBFBFB")
text(84, 516, "Settings", 15, INK, "600")
text(84, 540, "提取端点：VisionOS / MWEB(SABR) / TV", 12.5, MUTED)
text(84, 560, "把平台对抗降级成配置项", 12.5, ACCENT)

# ---- extractor column
rect(560, 172, 400, 132)
text(584, 200, "StreamingService → LinkHandler → Extractor", 15, INK, "600")
for i, s in enumerate(["YouTube (SABR)", "Bilibili (WBI + ticket)", "Niconico / SoundCloud / PeerTube…"]):
    text(584, 226 + i * 22, "· " + s, 12.5, MUTED)

rect(560, 324, 400, 176, fill="#FCFCFC")
text(584, 352, "YouTube SABR 会话", 15, INK, "600")
rows = [("sabr/YoutubeSabrSession", "761 行 · 换 host / 退避"),
        ("sabr/YoutubeSabrRequestHelper", "请求体 protobuf 1–19"),
        ("sabr/protocol/UmpReader", "UMP 自研压缩整数"),
        ("sabr/protocol/SabrResponseDecoder", "part type 10–67"),
        ("sabr/media/Sabr*IndexParser", "sidx / Cues → 分段表"),
        ("sabr/YoutubeSabrInfo", "formats / initRange")]
for i, (a, b) in enumerate(rows):
    y = 378 + i * 20
    text(584, y, a, 10.5, INK, mono=True)
    text(800, y, b, 11, MUTED)

rect(560, 520, 400, 116)
text(584, 548, "解码与完整性", 15, INK, "600")
text(584, 572, "YoutubeApiDecoder —— 三级降级", 12.5, MUTED)
text(584, 594, "YoutubeJavaScriptPlayerManager", 12.5, MUTED, mono=True)
text(584, 616, "serviceIntegrityDimensions.poToken", 12.5, MUTED, mono=True)

# ---- endpoints column
rect(1090, 172, 290, 88)
text(1114, 200, "youtubei/v1/player", 13.5, INK, "600", mono=True)
text(1114, 224, "MWEB 客户端 · poToken 注入", 12, MUTED)
text(1114, 244, "→ serverAbrStreamingUrl", 12, BLUE, mono=True)

rect(1090, 280, 290, 80)
text(1114, 308, "videoplayback?sabr=1", 13.5, INK, "600", mono=True)
text(1114, 332, "application/vnd.yt-ump", 12, MUTED, mono=True)
text(1114, 350, "UMP part 流（MEDIA/CONTROL）", 12, MUTED)

rect(1090, 380, 290, 80)
text(1114, 408, "jnn-pa.googleapis.com", 13.5, INK, "600", mono=True)
text(1114, 432, "Waa/GenerateIT", 12, MUTED, mono=True)
text(1114, 450, "BotGuard → integrityToken", 12, MUTED)

rect(1090, 480, 290, 76)
text(1114, 508, "youtube.com", 13.5, INK, "600", mono=True)
text(1114, 532, "首页 ytcfg + attestation challenge", 12, MUTED)
text(1114, 550, "player_ias…/base.js", 12, MUTED, mono=True)

# ---- closed source box
rect(1090, 596, 290, 156, rx=6, stroke=ACCENT, dash="5 4", fill="#FFFBF6")
text(1114, 626, "api.pipepipe.dev", 15, ACCENT, "600", mono=True)
text(1114, 650, "唯一闭源件（个人运营）", 12, ACCENT)
text(1114, 676, "GET /decoder/latest-player", 11.5, INK, mono=True)
text(1114, 696, "GET /decoder/decode?player&sig&n", 11.5, INK, mono=True)
text(1114, 722, "实测返回 {\"player\":\"67288f35\",", 11.5, MUTED, mono=True)
text(1114, 740, "         \"signatureTimestamp\":20744}", 11.5, MUTED, mono=True)

# ---- flow arrows: client <-> extractor
line(480, 212, 560, 212, BLUE, 1.4, marker=True)
line(560, 252, 480, 252, BLUE, 1.4, marker=True)
text(520, 200, "Stream / SABR", 10.5, MUTED, anchor="middle")
text(520, 272, "segment bytes", 10.5, MUTED, anchor="middle")

line(480, 330, 560, 330, HAIR, 1.2, marker=True)
text(520, 320, "minter / solver", 10.5, MUTED, anchor="middle")

# extractor -> endpoints
line(960, 200, 1090, 200, BLUE, 1.4, marker=True)
line(1090, 244, 960, 244, BLUE, 1.4, marker=True)
line(960, 360, 1090, 320, BLUE, 1.4, marker=True)
line(1090, 350, 960, 390, BLUE, 1.4, marker=True)
line(960, 420, 1090, 420, HAIR, 1.2, marker=True)
line(960, 470, 1090, 508, HAIR, 1.2, marker=True)
line(960, 590, 1090, 640, ACCENT, 1.4, marker=True, )
line(1090, 700, 960, 600, ACCENT, 1.0, dash="4 3", marker=False)

# step numbers
steps = [(1020, 190, "①"), (1020, 300, "②"), (1020, 405, "③"), (1020, 495, "④")]
for x, y, s in steps:
    text(x, y, s, 13, BLUE, "600", anchor="middle")

# ---- bottom: protocol summary strip
line(60, 792, 1380, 792)
text(60, 822, "SABR 握手（逆向结论）", 14, INK, "600")
flow = [("① player", "serverAbrStreamingUrl + ustreamerConfig + initRange/indexRange"),
        ("② init GET", "Range: bytes=init…indexEnd  → 206（ftyp+moov+sidx）"),
        ("③ POST UMP", "?alr=yes&cpn&rn · x-protobuf / vnd.yt-ump · 字段 1–19"),
        ("④ poToken", "youtube.com → BotGuard VM → GenerateIT → mint(videoId|visitorData)"),
        ("⑤ 消费", "MEDIA_HEADER(20)/MEDIA(21)/MEDIA_END(22) → spool → 段索引 → 伪造 MPD")]
coords = [(60, 852), (560, 852), (1010, 852), (60, 890), (560, 890)]
for (a, b), (x, y) in zip(flow, coords):
    text(x, y, a, 12, INK, "600", mono=True)
    text(x + 88, y, b, 11.5, MUTED)

add('</svg>')

with open('/var/minis/workspace/pipepipe-re/architecture.svg', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print('ok', len(out))
