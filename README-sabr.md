# sabr.py — 从 PipePipe 逆向出来的 YouTube SABR 客户端

单文件、零第三方依赖（Python 3.8+ stdlib）。实现了 YouTube **SABR（Server Adaptive
BitRate）** 会话：`player response → init range → UMP 分段流 → 可播放文件`。

协议细节来路全部写在 [`REPORT.md`](REPORT.md) §3：字段号、UMP 压缩整数、part type
语义，逐条对应 `PipePipeExtractor/services/youtube/sabr/**`。

## 实测状态（2026-10-21，本机）

| 项 | 结果 |
|---|---|
| `selftest` | 16 项全过（UMP 五档整数、真实错误包、真实 sidx、真实 WebM Cues、请求字段） |
| `probe --client visionos --itag 251` | 27 个格式；init 630 B；Cues 22 段 / 210.0 s；SABR 首次响应 14 个 part（含 1 个 MEDIA 段 164,496 B） |
| `download --mode sabr`（mweb） | 8 段 / 2.5 MiB / 实际 90.0 s，`ffmpeg` 解码无错（opus 48 kHz） |
| `download --mode direct`（137，mp4） | sidx 38 段 / 213.04 s，8 秒拉 3.0 MiB |
| 连播 10.5 分钟 | 63 请求 / 63 段 / 0 退避 / 0 重定向 / 0 错误，3 个文件 ffmpeg 全部解码通过（`stability.png`） |
| poToken | 本地铸造 + 磁盘缓存 + 3 次重试 + 冷启动降级；**已验证：无 token 60 秒停推（prot=2），带 token 跑满 260 秒（prot=1）** |
| 整片（20 分钟） | 144p 视频 225/225 段 + 音频 121/121 段，合并 20:03 / 9.2 MB，vp9+opus 解码无错 |

## 用法

```bash
# 1) 离线自检（不需要网络，跑仓库自带 fixtures）
python3 sabr.py selftest

# 2) 看一眼某个视频的 SABR 会话（会真的发一次 POST）
python3 sabr.py probe --video dQw4w9WgXcQ --client visionos --itag 251

# 3) 下载单条自适应流
python3 sabr.py download --video dQw4w9WgXcQ --itag 251 --mode sabr   --seconds 30 -o a.webm
python3 sabr.py download --video dQw4w9WgXcQ --itag 137 --mode direct --max-segments 40 -o v.mp4

# 4) 稳定性长跑（跨视频 / 连播 10 分钟）
python3 sabr.py stress --videos dQw4w9WgXcQ,dQw4w9WgXcQ,dQw4w9WgXcQ --itag 251 --wall-budget 900 --outdir stress
python3 sabr.py stress --videos <id1>,<id2>,<id3> --itag 251 --seconds 300 --outdir stress

# 5) 整片下载并合并 / 边下边播
python3 sabr.py play  --video <id> --video-itag 598 --audio-itag 600     # → downloads/<id>.mkv
python3 sabr.py serve --video <id> --video-itag 598 --audio-itag 600 --port 8765

# 6) 自动选质 / 续传 / 登录态
python3 sabr.py play --video <id> --max-height 144          # 144p 最小体积
python3 sabr.py download --video <id> --itag 598 --resume -o a.webm
python3 sabr.py play --video <id> --cookies cookies.txt     # 会员/受限内容

# 7) 播放器级 ABR（HLS 阶梯）/ 搜索 / SponsorBlock
python3 sabr.py abr --video <id> --heights 144,240,360 --port 8765   # 播放器开 master.m3u8
python3 sabr.py search "关键词" --limit 5
python3 sabr.py sponsor --video <id>

# 8) 现铸 poToken（Node 22 + bgutils-js + jsdom，需先 cd potnode && npm i）
python3 sabr.py mint --video <id>                 # 打印 token
python3 sabr.py download --video <id> --itag 251 --mode sabr --pot auto -o a.webm

# 6) A/B：同一个视频，无 token vs 现铸 token，各 90 秒
python3 pot_ab.py 9bZkp7q19f0

# 7) 需要时手工喂 poToken（base64 或 base64url 都行）
python3 sabr.py download --video ... --itag 251 --mode sabr --pot "$POT" -o a.webm
```

音视频合并照旧交给 ffmpeg：

```bash
ffmpeg -i a.webm -i v.mp4 -c copy out.mkv
```

## 它怎么工作

```
player_response()            MWEB / VISIONOS 客户端，拿 serverAbrStreamingUrl
   │                         + playerConfig.mediaCommonConfig.ustreamerConfig
   ├─ decode_mweb_urls()     mweb 的 signatureCipher → decoder API 解 sig / n
   ▼
fetch_init()                 GET  format.url   Range: bytes=<init_start>-<index_end>
   │                         一次拿「解码头 + 分段索引」
   ├─ parse_sidx()           MP4：sidx box → 每段时长 → 精确时间线
   └─ parse_webm_cues()      WebM：EBML 遍历到 Cues，按 TimestampScale 换算
   ▼
SabrSession.request()        POST <serverAbrStreamingUrl>?alr=yes&cpn&rn
   │                         body = VideoPlaybackAbrRequest（字段 1/2/3/4/5/16/17/19）
   ▼
PartReader                   UMP: type|size|payload（自研压缩整数，不是 varint）
   ├─ 20 MEDIA_HEADER        protobuf：1 headerId 3 itag 9 seq 11 startMs 12 durMs 14 len
   ├─ 21 MEDIA               payload[0]=headerId，其余直接落盘
   ├─ 22 MEDIA_END           校验 contentLength
   ├─ 35 NEXT_REQUEST_POLICY 字段 4 = backoff ms，字段 7 = playbackCookie
   ├─ 43 SABR_REDIRECT       字段 1 = 新 URL（换 host）
   ├─ 44 SABR_ERROR          字段 1 = type，字段 2 = code
   ├─ 46 RELOAD_PLAYER_RESPONSE  字段 1.1 = token → 需重取 player response
   └─ 58 STREAM_PROTECTION_STATUS 字段 1 = 状态（1 = 无需 attestation）
```

## 刻意没做的

- **BotGuard / poToken 铸造**：需要跑 YouTube 的 VM 解释器 + `GenerateIT`，属于
  JS 运行时的事（PipePipe 用隐藏 WebView 做）。本工具只接受外部传入的 token。
- **自适应切码率**：只按你指定的 itag 顺序拉，不跟随 `SELECTABLE_FORMATS`。
- **seek / 直播**：`playerTimeMs` 已接到请求里，但没有实现精确 seek 与
  `LIVE_METADATA` 的头部跟随。
- **重载会话**：收到 46 号 part 时直接报错退出（会打印 token），由调用方换新
  player response 重启——PipePipe 也是这么做的，只是它有热替换。
