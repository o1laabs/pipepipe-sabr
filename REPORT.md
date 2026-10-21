# PipePipe 核心功能逆向报告

> 目标：`https://github.com/InfinityLoop1308/PipePipe`
> 方法：**源码级逆向（GPLv3 全量源码）+ 运行时端点取证**。
> `rea`（mytechnotalent/rea，Rust 二进制静态分析框架）在本环境不可用（无 linux-aarch64 预编译、无 cargo、磁盘 1.8G），且对一个源码公开的项目做二进制 RE 是错配工具——它的价值在"闭源产物"，而本项目的**唯一闭源产物**恰好被我定位到了（见 §7），那部分用抓包/端点探测取证更直接。
> 取证时间：2026-10-21｜基线 commit：Client `dev`/Extractor `main`（2026-10-19）
> 配图：`architecture.svg`（同目录）

---

## 0. 一句话结论

PipePipe 不是"加了功能的 NewPipe"，而是**把 YouTube 播放链路上唯一还不由自己掌控的两段——签名解密与完整性证明（PO Token）——搬到了自己手里**，并为此新写了一个 **SABR 协议栈**（YouTube 2025 年后的服务端自适应码率协议）。它的护城河不在 UI，在 `PipePipeExtractor/services/youtube/sabr/`（5,328 行）和那个隐藏 WebView 里的 BotGuard 运行时。

三条证据决定了整个判断：

1. **上游 NewPipe 还在用 Rhino 跑本地 JS + 让 App 实现 `PoTokenProvider` 接口**；PipePipe **删掉了整个 `utils/jsextractor/` 与 `JavaScript.java`**（上游独有 46 个文件里，`utils/JavaScript.java` 与 `utils/jsextractor/*` 共 5 个正是这套引擎），改成 **WebView + yt-dlp 的 EJS solver + 远端 API 三级降级**。
2. 上游 `DeliveryMethod` 只有 5 个值；PipePipe 加了第 6 个：**`SABR`**——播放源不再是一条 URL，而是一个有状态的 POST 会话。
3. 播放器层为此写了一套"**伪造 DASH manifest + 自定义 DataSource**"，把分段流伪装成 ExoPlayer 能吃的 MPD。

---

## 1. 项目剖面（实测数字）

| 维度 | 数字 | 来源 |
|---|---|---|
| 主仓 | 7,063 ★ / 253 fork / GPL-3.0 / 25116 KB | GitHub API |
| 主仓实质 | **只有构建脚本、fastlane 元数据与 4 个 submodule**，无业务代码 | `git/trees?recursive=1`：156 个 entry，0 个 `.java/.kt` |
| 真实代码 | 两个 submodule，均托管在 **Codeberg**（`NullPointerException/PipePipeExtractor`、`PipePipeClient`） | `.gitmodules` |
| Extractor | 474 个 Java 文件 / **64,177 行** / 7 个服务（bandcamp, bilibili, media_ccc, niconico, peertube, soundcloud, youtube） | 实测 |
| Client | 428 Java + 177 Kotlin / **108,139 行**（82,191 + 25,948） | 实测 |
| 版本 | `5.5.0-beta3`，versionCode 1109 | `app/build.gradle` |
| 与上游差集 | 相对 `TeamNewPipe/NewPipeExtractor`（主源码 275 文件 / 59,903 行）：**新增 159 个文件，删除 46 个文件** | 文件集合对比 |

新增 159 个文件里，可归类为四块增量：

- **SABR 全套**（`sabr/**`，32 个文件，5,328 行）+ 客户端 `player/datasource/Sabr*`（1,550 行）
- **Bilibili 全站**（`services/bilibili/**`，约 60 个文件）——上游完全没有
- **Niconico 全站**（`services/niconico/**`，约 40 个文件）+ 弹幕协议 `.proto`
- **通用弹幕抽象**（`bulletComments/**`）+ 各站搜索过滤器体系（`search/filter/**`）

---

## 2. 分层架构

```
┌──────────────────────────── PipePipeClient (Android, 108k 行) ────────────────────────────┐
│  UI (Fragment/Compose)   Player (ExoPlayer/Media3)   DownloadManager (giga)                │
│                              │                            │                               │
│                    SabrDashMediaSource ── SabrMediaBridge ── SabrSegmentDataSource          │
│                    （伪造 MPD + 分段按需拉取）        （线程/缓存/退避）                    │
│                              │                                                            │
│       SharedWebViewRuntime ──┬── WebViewJavaScriptDecoder（EJS 解 sig / n）                 │
│       (隐藏 WebView,         └── LocalDomPoTokenProvider（BotGuard 铸 PO Token）            │
│        PipePipeWebViewBridge)                                                              │
└───────────────────────────────────────┬───────────────────────────────────────────────────┘
                                        │  Extractor API（StreamInfo / Stream / DeliveryMethod）
┌───────────────────────────────────────┴───────────────────────────────────────────────────┐
│                          PipePipeExtractor (Java, 64k 行, 纯 JVM)                          │
│  ServiceList → StreamingService → LinkHandler → Extractor                                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐                       │
│  │ Youtube      │ │ Bilibili     │ │ Niconico     │ │ SoundCloud…  │                       │
│  │ SabrSession  │ │ WBI + ticket │ │ DMC + WS     │ │              │                       │
│  │ UmpReader    │ │ DeviceForger │ │              │ │              │                       │
│  │ SabrProto    │ │              │ │              │ │              │                       │
│  └──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘                       │
│  Downloader（okhttp/cronet, 流式响应, 取消, 限速） / SponsorBlock / 弹幕抽象                │
└───────────────────────────────────────┬───────────────────────────────────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        ▼                               ▼                               ▼
  youtubei/v1/player            videoplayback?sabr=1          api.pipepipe.dev  ← 唯一闭源
  (MWEB 客户端)                 (UMP 二进制流)                 /decoder/*（sig/n 解密）
```

---

## 3. 核心链路 A：YouTube SABR 协议栈（项目最硬的部分）

### 3.1 为什么需要它

YouTube 对 MWEB/WEB 客户端已经**只下发 `serverAbrStreamingUrl`，不再给每条格式的直链**。上游 NewPipe 的做法是绕（换 android/ios 客户端拿直链）；PipePipe 的做法是**正面实现那个协议**。

入口判定（`YoutubeStreamExtractor.java:852-858`）：

```java
if ("mweb".equals(selectedClient) && streamType != LIVE && streamType != POST_LIVE
        && hasSabrStreamingUrl()) {
    buildSabrStreams(videoId);      // → DeliveryMethod.SABR
} else {
    extractDirectFormats(videoId);  // → 传统 PROGRESSIVE_HTTP / DASH / HLS
}
```

用户在设置里可选三种提取端点（`settings_keys.xml:1775`）：`VisionOS` / `MWEB (SABR)` / `TV (Samsung)`。**这是产品级设计：把"YouTube 反爬策略变更"降级成一次设置切换**，而不是一个版本更新。

### 3.2 会话建立（三条 HTTP 交互）

```
① GET  youtubei/v1/player  (client = MWEB, clientVersion = iPad Safari UA, poToken 注入 serviceIntegrityDimensions)
     ← streamingData.serverAbrStreamingUrl / adaptiveFormats[].initRange,indexRange / videoPlaybackUstreamerConfig
② GET  <serverAbrStreamingUrl>?pot=<base64url(poToken)>   Range: bytes=initStart-initEnd   → 206
     （init 段 = ftyp+moov；indexRange 的 end 被并入同一次 Range，见 YoutubeStreamExtractor.java:1375-1382）
③ POST <serverAbrStreamingUrl>?alr=yes&cpn=<cpn>&rn=<n>
     Content-Type: application/x-protobuf
     Accept: application/vnd.yt-ump
     Accept-Encoding: identity
     User-Agent: <MWEB iPad UA>            （YoutubeSabrRequestHelper.java:317-322）
     ← Content-Type: application/vnd.yt-ump 的二进制流（UMP 封包）
```

时间线从 **`sidx` box** 解析：MP4 用 `SabrMp4SegmentIndexParser`（`SIDX_BOX = "sidx"`），WebM 用 `SabrWebmSegmentIndexParser`（Cues）。**初始化段的 Range 边界就被设计成"停在 sidx 之前"**，所以一次 GET 同时拿到"解码头 + 全片分段表"——这是整个方案的关键洞察，也是它能生成精确 DASH 时间线的前提。

### 3.3 请求体：`VideoPlaybackAbrRequest`（逆向出的 protobuf 字段表）

`YoutubeSabrRequestHelper.buildMediaRequest()`（:130-190）逐字段暴露了 schema：

| 字段 | 类型 | 语义 | 代码 |
|---|---|---|---|
| 1 | message | `clientAbrState`（见下） | :137 |
| 2 | message×N | 已选格式 `{1:itag, 2:lastModified, 3:xtags}` | :153 |
| 3 | message×N | 已缓冲区间 `{1:formatId, 2:startMs, 3:durationMs, 4:startSeq, 5:endSeq, 6:timeRange}` | :333 |
| 4 | uint64 | `playerTimeMs` | :150 |
| 5 | bytes | `videoPlaybackUstreamerConfig`（player response 原样 base64 解码，服务端下发的"ABR 策略字节码"） | :152 |
| 16 / 17 | message | 首选音频 / 视频 `formatId` | :234-241 |
| 19 | message | `streamerContext` | :247 |

`clientAbrState`（:158-190）：

| 字段 | 语义 | 观察到的写法 |
|---|---|---|
| 18 / 19 | width / height | `max(w,640)` / `max(h,360)` |
| 21 | height | `max(h,360)` |
| 23 | bandwidthEstimate | 优先用会话实测值，否则 `(音频+视频码率)×2` |
| 28 | playerTimeMs | 同上 |
| 34 | 常量 1 | 固定写 1 |
| 35 | playbackRate (float) | 倍速，缺省 1.0 |
| 40 | enabledTrackTypes | 1=只音频 / 2=只视频 / 0=两者 |
| 46 | drc bool | 音频是否为 DRC 轨 |
| 69 | audioTrackId | 多音轨选择 |

`streamerContext`（:245-275）：`{1: clientInfo{16: clientId=2(MWEB), 17: clientVersion, 21:"en-US", 22:"US"}, 2: poToken(原始字节), 3: playbackCookie, 5: sabrContext{1:type,2:value}, 6: unsentContextTypes}`。

**注意 `playbackCookie`**：服务端会在控制包里下发一个不透明的 cookie，客户端必须在后续请求回传——这是它区分"真人播放器"与"重放攻击"的会话凭据。PipePipe 把它当字节数组原样保存/回传（`YoutubeSabrSession.java:443`、`:526`）。

### 3.4 响应：UMP 封包（不是 protobuf varint，是 YouTube 自研压缩整数）

`UmpReader.readUmpInt()`（:100-122）逆向出的整数编码，**与 protobuf varint 完全不同**：

| 首字节 | 长度 | 解码 |
|---|---|---|
| `< 0x80` | 1 | `b` |
| `0x80–0xBF` | 2 | `(b&0x3f) + 64·b1` |
| `0xC0–0xDF` | 3 | `(b&0x1f) + 32·(b1 + 256·b2)` |
| `0xE0–0xEF` | 4 | `(b&0x0f) + 16·(b1 + 256·(b2 + 256·b3))` |
| `≥ 0xF0` | 5 | `b1 + 256·(b2 + 256·(b3 + 256·b4))` |

封包格式：`type(ump int) | size(ump int) | payload[size]`，循环到 EOF。`readPayloadsUntil` 用 `BoundedInputStream` 把大 payload 做成**有界流**，MEDIA 部分永不整体入堆（注释直指"4K 的 50–150MB 会被 OOM"）。

**Part type 表**（`SabrResponseDecoder.java:14-67`，全部为代码内命名，即逆向结论）：

| type | 名称 | 客户端如何用 |
|---|---|---|
| 10/11/12 | ONESIE_HEADER / DATA / ENCRYPTED_MEDIA | 遗留单段通道 |
| 20 | **MEDIA_HEADER** | protobuf `SabrMediaHeader`（见下） |
| 21 | **MEDIA** | payload[0] = headerId，其余为媒体字节 |
| 22 | MEDIA_END | 关闭该 headerId 的段 |
| 30 | CONFIG | 忽略 |
| 31/33/34 | LIVE_METADATA / PROMISE / CANCELLATION | 直播头与 promise |
| 35 | NEXT_REQUEST_POLICY | 下发 **backoff** 毫秒 |
| 36/37/38 | 格式元数据 / 选择配置 / 选中流 | 忽略 |
| 42 | FORMAT_INITIALIZATION_METADATA | 记录（generate 类的 `SabrFormatInitializationMetadata`） |
| 43 | SABR_REDIRECT | **换 host**（`YoutubeSabrSession.java:351`） |
| 44 | SABR_ERROR | 抛协议异常 |
| 45 | SABR_SEEK | seek 反馈 |
| 46 | RELOAD_PLAYER_RESPONSE | **带 token 重拉 player response**（换新 URL/config/poToken） |
| 47/48/49/50/51 | PLAYBACK_START_POLICY / ALLOWED_CACHED_FORMATS / 带宽采样提示 / PAUSE / SELECTABLE_FORMATS | 策略 |
| 57/59 | SABR_CONTEXT_UPDATE / SENDING_POLICY | 服务端下发上下文 → 回传（`ingestContextUpdate`） |
| 58 | STREAM_PROTECTION_STATUS | 0=无保护，非 0 触发认证重试 |
| 61/62 | SABR_ACK / END_OF_TRACK | 确认与片尾 |
| 63/64/65/66/67 | CACHE_LOAD / LAWNMOWER_* / PREWARM / DEBUG / SNACKBAR | 未实现或忽略 |

`SabrMediaHeader`（`sabr/generated/SabrMediaHeader.java`，手写解码，字段 1–13）：`headerId, videoId, itag, lastModified, xtags, startRange, compressionAlgorithm, initSegment(bool), sequenceNumber, bitrateBps, startMs, durationMs, contentLength, timeRange{start,duration,timescale}, sequenceLastModified`。

**这张表就是"核心功能"的骨头**：拿到它，等于拿到 YouTube 播放器与 CDN 之间那层私有协议。

### 3.5 工程细节（决定"能不能用"的地方）

- **段落地到磁盘 spool**：`SabrMediaSegmentCollector.Incremental` 把每个 headerId 的段写成临时文件，`MEDIA_END` 校验 `contentLength` 后交付。峰值内存 = 一个在飞段，不是整包。
- **host 轮转**：`buildHostCandidates()` 从 URL 生成候选 host，`SABR_REDIRECT`/失败时轮换（:184-240）。
- **退避**：`NEXT_REQUEST_POLICY` 给的 backoff 直接变成通知渠道 `sabr_backoff` 的等待（客户端 `SabrBackoffCoordinator`）。
- **认证重试**：`STREAM_PROTECTION_STATUS` → `SabrAttestationRetryHandler` → `LocalDomPoTokenProvider.invalidate()` 重新铸 token → `SabrPlayerReloader` 热换会话（新 URL + 新 ustreamerConfig + 新 poToken），**不重启播放**。
- **完整性自检**：`YoutubeSabrResponse.getIntegrityIssues()` 会报 `missing-media`，触发"清理式重拉"而非静默播坏。

---

## 4. 核心链路 B：签名与 `n` 参数（三级降级）

`n` 参数不清洗 → 限速约 **50 KB/s** 甚至 403（代码注释原话，`YoutubeJavaScriptPlayerManager.java:92-96`）。

降级顺序（`YoutubeApiDecoder.decode()`，:82-140）：

```
① 本地 WebViewJavaScriptDecoder（App 初始化时注入，App.java:137）
     └─ 失败一次 → disableLocalDecoder() 永久摘除，本进程不再尝试
② GET https://api.pipepipe.dev/decoder/decode?player=<id>&n=<v>&sig=<v>   (UA: PipePipe/4.9.0)
③ 都失败 → ParsingException
```

**① 本地实现**（`WebViewJavaScriptDecoder.java`）：

- player JS 直取 `https://www.youtube.com/s/player/<id>/player_ias.vflset/en_US/base.js`（:31），落盘缓存 `youtube-player-<id>.js`
- 用 **yt-dlp 的 EJS**（`assets/ejs/yt.solver.{polyfills,lib,core}.es5.min.js`，SPDX: **Unlicense**，内含 meriyah 6.1.4 解析器）在隐藏 WebView 里建 `jsc()` 沙箱 → 把 player JS 喂进去 → 执行 `sig`/`n` 变换
- signatureTimestamp 从 player JS 正则提取（`player\/([a-z0-9]{8})\/` + `signatureTimestamp:(\d+)`），缓存 24h

**② 远端实现**（实测，2026-10-21）：

```
GET https://api.pipepipe.dev/decoder/latest-player
→ {"player":"67288f35","variant":"main","signatureTimestamp":20744}

GET https://api.pipepipe.dev/decoder/decode?player=67288f35&n=abc
→ {"type":"result","responses":[{"type":"result","data":{"abc":"l"}}],
   "meta":{"player_id":"67288f35","source":"param"}}
```

`meta.source` 会回显 `param`，说明服务端按 query 里的 player id 加载对应 player JS 执行——**这是一个"无状态 JS 求解器"服务，不是简单查表**。

---

## 5. 核心链路 C：PO Token 本地铸造（BotGuard）

上游把 `poToken` 交给 App 实现接口（"需要良好 DOM 的 JS 引擎"）；PipePipe 直接把这台机器搬进 App：

```
① GET https://www.youtube.com   (Cookie: PREF=hl&gl=US 或用户登录 Cookie, UA = Win Chrome)
     → 解析 ytcfg 调用链（extractObjectCallArguments）拿 INNERTUBE_CONTEXT / EVENT_ID / visitorData / dataSyncId
     → 解析 initial attestation challenge：{program, globalName, interpreterJavascript | interpreterUrl}
② 隐藏 WebView 注入 assets/sabr_po_token.js
     new Function(interpreterJavascript)()      ← YouTube 的 BotGuard VM 解释器
     vm.a(program, cb, …) → asyncSnapshotFunction → snapshot()
     → botguardResponse + webPoSignalOutput[]
③ POST https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT
     body: ["O43z0dpjhgX20SCx4KAo", botguardResponse]
     headers: x-goog-api-key: AIzaSyDyT5W0Jh49F30Pqqtyfdf7pDLFKLJoAnw , x-user-agent: grpc-web-javascript/0.1
     → integrityToken + TTL
④ WebView: minter = webPoSignalOutput[0](integrityToken)   → mintCallback
⑤ poToken = mintCallback(identifierU8)
     identifier = videoId   （CONTENT 绑定）
                  | visitorData 或 dataSyncId（SESSION 绑定，登录态用后者）
⑥ base64url(poToken) 注入 → player 请求 serviceIntegrityDimensions.poToken
                            → SABR init 的 &pot=… → 请求体 streamerContext.2
```

证据：`LocalDomPoTokenProvider.kt`（:395-420 GenerateIT、:506 API key）、`sabr_po_token.js`（BotGuard 六步）、`App.java:136-139`（`LocalDomPoTokenProvider.initialize` + `NewPipe.setYoutubePoTokenResolver`）。

**这是本项目最不可替代的资产**：`poToken` 只会越来越硬，而它已经做到了"零外部依赖、端上自铸、登录态/游客态双绑定、失效自动重铸"。

---

## 6. 核心链路 D：Bilibili（第二战场，工程质量仅次于 YouTube）

`BilibiliService` 里是一整套"通过风控"的组合拳：

| 机制 | 实现 | 备注 |
|---|---|---|
| **WBI 签名** | 每日拉 `wbi_img` → `getMixinKey(img+sub)` → 参数排序 + `w_rid`/`wts`（`utils.java:296-355`） | 带日缓存 |
| **bili_ticket** | `GenWebTicket?key_id=ec02&hexsign=HMAC-SHA256("XgwSnGZ1p","ts"+ts)` | 硬编码密钥（公开已知） |
| **设备指纹** | `/x/frontend/finger/spi` 取 `buvid3/4` + 自造 `b_lsid`、`_uuid`（`infoc` 后缀算法）、随机 `buvid_fp` | `buvid_fp` **是随机 16 hex，不是真指纹** |
| **浏览器伪造** | `DeviceForger`：UA 模板 + ANGLE WebGL vendor/renderer 字符串 + viewport，随机组合 | 用于 Web 端点 |
| **播放地址** | `wbi/playurl`（DASH，需 `fnval` 位掩码）/ `pgc/player/web/v2/playurl`（番剧）/ 直播 `getRoomPlayInfo` | |
| **弹幕** | `x/v1/dm/list.so`（历史）+ `getDanmuInfo` → **WebSocket** | 16 字节包头：`packetLen(4) ver(2) op(4) seq(4)`；op=5 为消息，body brotli 压缩 |
| **直播弹幕** | `BilibiliWebSocketClient` + 心跳定时器 + 重连 | 与 Niconico 的 `NicoWebSocketClient` 共用 `bulletComments` 抽象 |

对比：上游 NewPipe 连 Bilibili 都不支持。**这一块是 PipePipe 在中国用户里的实际增长来源**（B 站用户无法用 NewPipe 看 B 站直播/弹幕/番剧）。

---

## 7. 唯一黑盒：`api.pipepipe.dev`

整个项目只有一处不可从源码复现：**`/decoder/*`**。它承担两件事，都是"必须随 YouTube 变更而变更"的部分：

1. `latest-player`：给出当前 player id + signatureTimestamp（**这等于告诉你"现在该用哪版 JS"**）
2. `decode`：在服务端跑同一份 player JS 解 `sig`/`n`

风险必须说清：

- **单点**：服务不可达时，若本地 EJS 也失效（被 `disableLocalDecoder` 摘除），YouTube 播放直接降级为 50 KB/s 或失败。
- **隐私**：请求带 `player`（公开）与 `n`/`sig`（每个视频的片段令牌）。拼起来是**可统计的观看指纹**——虽然不含 videoId，但 `n` 是逐视频唯一的。
- **信任**：GPLv3 客户端 + 闭源服务端，是"开源 Shell"的经典形态；客户端行为可审计，服务端不行。
- **可替代性**：本地 EJS 路径是完整可用的替代（yt-dlp 的 ejs 同样是 Unlicense），说明这个远端服务更像是"兜底 + 版本号快速下发"，而非能力必需。

---

## 8. 与上游的差集（这才是 PipePipe 本体）

| 能力 | NewPipe 上游 | PipePipe |
|---|---|---|
| YouTube 播放源 | android/ios/web 直链 | **+ SABR 会话（mweb）** |
| 签名/n 解密 | 内嵌 Rhino 跑 player JS | **WebView + EJS**，远端兜底 |
| PO Token | 接口留给 App，实际无实现 | **端上 BotGuard 自铸** |
| Bilibili | 无 | **完整（含直播/番剧/弹幕）** |
| Niconico | 无 | **完整（含 DMC 直播/弹幕 protobuf）** |
| 弹幕抽象 | 无 | **`bulletComments/**` 跨站点统一** |
| 下载 SABR | 无 | **按段下载 + 断点恢复（`SabrDownloadStreamHelper`、`MissionRecoveryInfo.isSabr`）** |
| 搜索过滤器 | 少量 | **每站一套 `Filters/FilterGroup` 体系** |
| SponsorBlock | 有 | 有（扩展到 Bilibili） |

---

## 9. 复刻路线（如果要自己做一个）

**MVP-1（可验证）**：`player` 请求 → 拿 `serverAbrStreamingUrl` + `ustreamerConfig` → 实现 UMP 读包（§3.4 的整数表 + §3.3 的请求体）→ 落段到文件 → ffmpeg 合并。
验证标准：能连续拉满 30 秒不 403，且 `n` 清洗前后吞吐差 >5×。

**MVP-2**：`sidx` 解析 → 生成标准 MPD → 交给 ExoPlayer/VLC 原生播放。此时你已经不需要 §3.6 那套伪造 manifest。

**MVP-3**：BotGuard（§5 六步）。这是唯一需要 WebView/Node 的部分；无它则只能吃"限速 + 随机 403"。

**工程上最值得抄的三件事**（与协议无关）：

1. **提取端点做成用户可切换的设置项**，把平台对抗变成配置问题，而不是发版问题。
2. **失败一次就永久摘除本地解码器**（`disableLocalDecoder`），避免每拉一个视频都卡 30 秒超时。
3. **协议层做内存上界**：`readPayloadsUntil` + 磁盘 spool + `contentLength` 校验，是"4K 不 OOM"的全部原因。

---

## 10. 判断

- **技术含量真实**：SABR 是全套自研（请求 schema、UMP 编码、part 语义、sidx 时间线、伪造 DASH、退避与换 host），不是抄 yt-dlp（yt-dlp 至今主要走直链）。
- **架构判断正确**：把"随平台变化的部分"压缩成三个可替换件（提取端点、解码器、PO Token 源），其余全是稳定的产品代码。这是它能 3 年不掉队的原因。
- **最大的债不是技术，是治理**：核心能力依赖一个个人运营的闭源 API + 个人维护的 Codeberg 仓库。7,063 星的信任度建立在"服务一直在"上。
- **对广义"逆向"的启示**：这个项目的护城河不在 UI 也不在算法，而在**"谁愿意持续跟踪一个每月变一次的私有协议"**。这是运维意志的胜利，不是聪明的胜利。

---

### 附：关键文件索引

| 主题 | 文件:行 |
|---|---|
| SABR 入口判定 | `extractor/.../extractors/YoutubeStreamExtractor.java:852` |
| SABR 请求体构造 | `.../sabr/YoutubeSabrRequestHelper.java:130` |
| UMP 整数编码 | `.../sabr/protocol/UmpReader.java:100` |
| Part type 全表 | `.../sabr/protocol/SabrResponseDecoder.java:14` |
| 媒体头字段 | `.../sabr/generated/SabrMediaHeader.java:10` |
| 会话/重载/换 host | `.../sabr/YoutubeSabrSession.java:161, 388, 439` |
| 解码降级链 | `.../YoutubeApiDecoder.java:82` |
| EJS 本地解码 | `PipePipeClient/.../WebViewJavaScriptDecoder.java:31, 203` |
| BotGuard 铸造 | `PipePipeClient/.../youtube/LocalDomPoTokenProvider.kt:395` + `assets/sabr_po_token.js` |
| 伪造 MPD | `PipePipeClient/.../player/datasource/SabrDashMediaSource.java:207` |
| 分段 DataSource | `PipePipeClient/.../player/datasource/SabrSegmentDataSource.java:47` |
| B 站 WBI / ticket / 指纹 | `.../services/bilibili/utils.java:296`、`BilibiliService.java:150-260` |
| B 站弹幕 WS | `.../services/bilibili/BilibiliWebSocketClient.java:44` |


---

## 11. 实测验证（2026-10-21，用 §10 的 `sabr.py` 跑出来的）

报告写完不代表跑得通。整套协议被重写成一份 600 行、零依赖的 Python 客户端
（`sabr.py`），并对着线上端点跑了一遍。结论与修正如下：

### 11.1 已验证成立

| 环节 | 证据 |
|---|---|
| player response 字段路径 | `playerConfig.mediaCommonConfig.mediaUstreamerRequestConfig.videoPlaybackUstreamerConfig`（1,356 B），`streamingData.serverAbrStreamingUrl` 存在 |
| init Range 合并取索引 | `Range: bytes=0-629` → 206 / 630 B，EBML 头 + Tracks + **Cues(22 个 CuePoint，0→210.0 s)** |
| MP4 `sidx` | itag 137 的 init（1,230 B = ftyp 28 + moov 714 + **sidx 488**）解出 **38 段 / 213.04 s**，时间戳严格单调 |
| UMP 读包 | 一次真实响应解析出 14 个 part：`SELECTABLE_FORMATS / STREAM_PROTECTION_STATUS / REQUEST_IDENTIFIER / NEXT_REQUEST_POLICY / START_BW_SAMPLING_HINT / MEDIA_HEADER / MEDIA×6 / PLAYBACK_START_POLICY / MEDIA_END` |
| 分段落盘 | `MEDIA_HEADER` 报 `seq=1 startMs=0 durMs=10001 len=164496`，`MEDIA_END` 实收 164,496 B，逐字节吻合 |
| 连续会话 | 按 `playerTimeMs` 递进 + 序号去重，8 段 / 2.5 MiB / 实际 **90.0 s**，`ffmpeg` 解码 0 错（opus 48 kHz） |
| 直链模式 | VisionOS 直链 8 秒拉 3.0 MiB，`sidx` 38 段与 SABR 侧完全一致 |

### 11.2 必须修正的两个判断

**① 「SABR 必须要 PO Token」——今天不成立。**
用 `VISIONOS`(clientId 101) 与 `MWEB`(2) 两条会话，**不带 `pot`** 都拿到了
`STREAM_PROTECTION_STATUS=1` 和正常 MEDIA。1 = 无需 attestation；PipePipe 之所以
把铸造器做进 App，是为 58 号 part 翻到 2/3 的那天做准备，不是当下必需。
`pot` 只在**直链 init 段**上被验证为必需（无 pot 时 403），而 SABR POST 本身没有强制。

**② 失败是有形状的。**
请求字段一旦不对，服务端返回的不是 403，而是一个 31 字节的 UMP 包：

```
2c 1d | 0a 15 "sabr.malformed_config" | 10 02 | 1a 02 20 06
 type=44                              code=2
```

这条已被做成 `fixtures/sabr_error.bin`，进 `selftest`。也就是说：**协议错误可以被
离线断言**，不必每次打网络去赌。

### 11.3 仍未验证的部分

- `RELOAD_PLAYER_RESPONSE`（46）后的热切换：客户端目前直接报错退出，打印 token。
- `SABR_CONTEXT_UPDATE`（57）双向上下文的实际作用（代码里只做透传）。
- 直播：`LIVE_METADATA`(31) 的头部跟随与 `isPostLiveDvr`。
- 长会话下的 host 轮转与 35 号退避的真实触发频率。
- **端上 poToken 铸造**：本次在沙箱浏览器里跑通了 BotGuard 的 `vm.a(program,…)` 与
  snapshot（拿到 3,255 字符的 `botguardResponse`、`GenerateIT` 也返回了 integrity token），
  但 `webPoSignalOutput[0]` 始终未填充 → minter 建不起来。说明 PipePipe 那条路依赖的
  WebView 环境细节（UA / ytcfg / 页面生命周期）比代码本身更关键，这一点没有复现。

### 11.4 产物

| 文件 | 说明 |
|---|---|
| `sabr.py` | 客户端本体：player 请求、sig/n 解码、init/sidx/Cues、UMP、会话、下载 |
| `fixtures/sabr_error.bin` | 线上真实 `SABR_ERROR` 包（31 B） |
| `fixtures/init_137.mp4` | 真实 MP4 初始化段（1,230 B，含 sidx 488 B） |
| `fixtures/init_251.webm` | 真实 WebM 初始化段（630 B，含 Cues 22 点） |
| `README-sabr.md` | 用法与边界 |


---

## 12. 稳定性实测：连播 10 分钟，看它什么时候崩

静态协议对了不等于能用。用 `sabr.py stress` 跑了两轮：一轮跨 3 个视频，一轮同一视频连播 **10.5 分钟**（3 段连续会话）。
原始数据 `stress10/report.json`，图 `stability.png`。

### 12.1 结果

| 指标 | 数值 |
|---|---|
| 连续播放 | **630 s（10.5 min）**，63 次请求 / 63 段 / 10.2 MiB |
| 每请求耗时 | 2.4–2.9 s（中位 2.7 s），**1 次 4.2 s 尖峰**，无超时 |
| 播放倍率 | 约 **2.9× 实时**（214 s 墙钟拉完 630 s 媒体） |
| 35 号退避 | **0 次**（但每次响应都下发 35 号 part，只是 backoff 字段恒为 0） |
| 43 号重定向 | **0 次**（102 次请求里总共只见过 1 次，见下） |
| 44 号错误 / 46 号重载 | 0 / 0 |
| 落盘校验 | 3 个文件全部 `ffmpeg` 解码通过，时间戳严格单调 |

结论：**在"未被门控"的视频上，SABR 链路是稳的**，没有退避、没有换 host、没有重载。

### 12.2 真正的天花板：60 秒

跨视频那轮暴露了关键事实。12 个热门视频样本里，**11 个 `STREAM_PROTECTION_STATUS = 2`**，只有 1 个（`dQw4w9WgXcQ`）是 1。

| 视频 | 门控 | 结果 |
|---|---|---|
| `dQw4w9WgXcQ` | 1 | 完整 213 s，21 请求，零异常 |
| `9bZkp7q19f0` | 2 | **正好 60 s 后服务器停止推进**（6 个 10 s 段），客户端连续 3 次无进展后停下 |
| `aqz-KE-bpKQ` | 2 | init Range **403**，SABR 仍能吐 60 s，但没有 init 段 → 容器不完整、ffmpeg 拒绝解析 |

三点必须记住：

1. **门控是视频属性，不是会话随机。** 同一视频连开 4 次会话，状态稳定复现（2,2,2,2 vs 1,1,1,1）；换 `VISIONOS` 客户端对门控视频直接 `LOGIN_REQUIRED`。
2. **代价是"60 秒"而不是"403"。** 前 6 个段正常给，然后服务器反复回放同一窗口。这解释了为什么 PipePipe 要常驻一个 WebView 铸 poToken——没有它，YouTube 只让你试听一分钟。
3. **init Range 与 SABR 是两套门。** 同一个视频可以 `init 403` 但 SABR 仍吐数据；也可以反过来。客户端必须能把两者解耦（本工具已改成 init 失败也继续跑，只是没有时间线）。

### 12.3 顺手修掉的一个真 bug：分段被截断 → 容器损坏

第一版把 `MEDIA(21)` 的字节**直接追加**到输出文件。结果：文件能播，但每 10 秒的 cluster 出现两次，`ffmpeg` 报
`non monotonically increasing dts`，213 s 的视频解出 3:33。

原因在协议层：YouTube 会在一次响应里**只发一半**的下一段（`MEDIA_HEADER` 有、`MEDIA_END` 没有），下一次请求再完整重发这一段。
直接落盘 → 半个 cluster + 一个完整 cluster 拼在一起，帧号回退。

修法（PipePipe 用 `SabrMediaSegmentCollector` 做的同一件事）：**每段先在内存/临时文件里缓冲，`MEDIA_END` 到了、长度与
`contentLength` 吻合才提交**；响应结束时仍未闭合的段直接丢弃。修完 10.5 分钟的文件时间戳严格单调、零解码错误。

### 12.4 判断

- 协议稳定性：**够当库用**。63 次请求的耗时才 2.4–4.2 s，方差主要来自 CDN 而不是协议。
- 可用性瓶颈不在协议，在**门控**：没有 poToken，绝大多数视频只有 60 秒。之前报告里"pot 只在 init 段必需"的说法要再收窄一格：**pot 决定的是 60 秒之后能不能继续**。
- 35/43 这两个"看起来很重要"的控制包，在正常路径上几乎不出现。把它们当成异常通道，而不是心跳。


---

## 13. BotGuard 补上了：本地铸造 PO Token（2026-10-21）

结论先行：**铸造这一环打通了**（Node 22 + `bgutils-js` 4.0.3 + `jsdom`，4 秒出一个 124 字节、
TTL 12 小时的 content-bound token）。但**"60 秒变整片"的对照实验没能跑成**——原因不在我们这边，
是 YouTube 对这个出口 IP 已经升级了处置等级，见 §13.3。

### 13.1 为什么之前拿不到 webPoSignalOutput

同一段 VM 代码，三种 challenge 来源，结果完全不同：

| challenge 来源 | 环境 | `webPoSignalOutput.length` |
|---|---|---|
| `youtubei/v1/att/get`（`ENGAGEMENT_TYPE_UNBOUND`） | Node + 手写垫片 | **0** |
| 播放页内联 `bgChallenge` | 沙箱浏览器（真 DOM） | **0** |
| **首页 `window.ytAtN({...})` 内联 challenge** | **Node + jsdom** | **1** ← 成功 |

三个必要条件，缺一不可：

1. **首页 challenge，不是 att/get 的。** 首页那份 `ytAtN` 里带的 `messageId` / 实验位才让 program
   进入"webpo 可用"分支；att/get 返回的 program 跑得出 `botguardResponse`（2.9 KB），但不出 minter。
2. **真实 DOM（jsdom 即可）。** VM 会探 `document`/`navigator`/`location`/`origin`，纯 Node 会直接
   `ReferenceError: window is not defined`；手写垫片能跑通 snapshot 但不产生 minter。
3. **`yt.config_` 必须是 page 里那份 `ytcfg.set({...})`**，VM 从中取 `EVENT_ID` 等运行时参数。

### 13.2 可复现的最小实现

`potnode/mint2.mjs`（约 90 行，改自 bgutils 官方示例）：

```
home 200                          → 抓 ytcfg.set({...}) 与 window.ytAtN({...})
vm loaded globalName=trayride     → new Function(interpreter)()
snapshot 4277b, signalOutput=1    → client.snapshot({ webPoSignalOutput: [] })
GenerateIT 200                    → ["O43z0dpjhgX20SCx4KAo", botguardResponse]
poToken 124b ttl=43200            → minter.mintAsWebsafeString(videoId)
```

已接进客户端：`python3 sabr.py mint --video <id>`，或 `--pot auto` 在下载时按视频现铸
（每视频一个 token，content-bound）。

### 13.3 对照实验：60 秒上限被 poToken 推开了

换了出口 IP 之后（原 IP 被打到 `LOGIN_REQUIRED`），同一个视频 `9bZkp7q19f0`、
同一个 itag 251、同一套代码，只差一个 token：

| 运行 | `STREAM_PROTECTION_STATUS` | 覆盖时长 | 段数 | 退避 | 结果 |
|---|---|---|---|---|---|
| 无 token | **2** | **60.0 s** | 6 | 无 | 正好 60 秒后服务器停止推进 |
| 现铸 token（`--pot auto`） | **1** | **90.0 s**（撞到我设的 90 s 上限） | 9 | 无 | 没有停，还在继续 |
| 现铸 token，整片 | 1 | **260 s / 26 段 / 7.5 MiB** | 26 | 无 | `ffmpeg` 解出 4:12，零解码错误 |

**这就是判据**：token 让 `STREAM_PROTECTION_STATUS` 从 2 掉到 1，60 秒的闸门消失。
一次 60 秒的会话 vs 一次 260 秒的会话，差别不在网络、不在客户端，只在 `streamerContext.2`
里那 124 个字节。

注意几个细节：

- **token 必须 content-bound 到同一个视频**，且 **不能塞进 player 请求**。塞进 player 请求时
  （`serviceIntegrityDimensions.poToken`）YouTube 反而回 `LOGIN_REQUIRED`；正确位置是
  SABR 请求体的 `streamerContext`（字段 2）与 init GET 的 `&pot=`。
- 整片跑到 260 s 时出现了 "no progress after 3 requests" 自我停止，视频全长 281.5 s，
  尾段约 20 s 没拿到。是尾段调度问题还是服务器的收尾策略，未进一步追（不影响结论）。
- 全程零退避、零重定向、零错误，说明 token 有效时服务端是"正常放行"，不是"勉强放过"。

### 13.4 顺带确认的两件事

- **`att/get` 那条路是可行的抓取方式，但不是可行的铸造方式**——很多实现（包括我上一版）
  卡在 `webPoSignalOutput` 为空，根因就是 challenge 来源，而不是 VM 调用姿势。
- **token 是 124 字节、base64（带 padding），不是 base64url。** 传给 `pot` 查询参数前要转换，
  否则会被当成无效 token（我们的 `decode_pot` 已用 `altchars=b"-_"` 兼容两种）。


---

## 14. 还缺什么：PipePipe 已实现、我的 `sabr.py` 没做的

判据不是"功能够不够多"，而是**缺了会发生什么**。按这个标准分三档。

### 14.1 T0：「不算能用」的那几条

| # | PipePipe 的做法 | 证据 | 我的现状 | 后果 |
|---|---|---|---|---|
| 1 | **重试编排** `SabrRequestCoordinator`：backoff 期间持续请求直到出现进展；`MAX_BACKOFF_MS=30s`、`MAX_REDIRECTS_PER_SESSION=3`、`MAX_RELOADS_PER_SESSION=2`、`MAX_INCOMPLETE_MEDIA_RESPONSES=3` | `YoutubeSabrSession.java:38-42`、`SabrRequestCoordinator.java:37-60` | 连续 3 次无进展就退出 | 网络抖一下、服务器回一个 4.8 s 退避，整次下载就报废 |
| 2 | **认证失效恢复链**：`STREAM_PROTECTION_STATUS` 变化 → `LocalDomPoTokenProvider.invalidate()` → 重铸 → `SabrPlayerReloader` 带 reload token 重取 player → 续传 | `SabrAttestationRetryHandler.java:22-47`、`SabrPlayerReloader.kt:8-18` | 只处理 46 号 reload；58 号状态变化不触发重铸 | 会话跑到 token 过期（12 h）或被降级时直接失败，而不是自愈 |
| 3 | **token 生命周期**：TTL + 60 s 余量（`EXPIRY_MARGIN_MS`）、失效即换、按内容缓存 | `LocalDomPoTokenProvider.kt:203,265,414,467` | 每次运行铸一次，无缓存、无续期、**铸造失败无重试** | 今天拉 144p 就死在这：Node 铸造器偶发崩溃，Python 侧直接 `minting failed` 退出 |
| 4 | **seek**：`playerTimeMs` 前跳 + `bufferedRanges` + 45 号 `SABR_SEEK` 反馈 | `YoutubeSabrRequestHelper.java:130-190`、`YoutubeSabrSession.java:451-474` | 只能从 0 顺序前进 | 不能拖进度、不能断点续传、缺段无法回补 |
| 5 | **完整性对账**：`getIntegrityIssues()` 报 `missing-media` / `missing-media-end`，然后"干净批次重拉" | `YoutubeSabrResponse.java:214-246` | 未闭合段直接丢弃，不记账 | 20 分钟的视频可能少几段而**不自知**（播放器表现为卡顿，下载器表现为静默残缺） |

这五条里，**#1 和 #3 是当下最疼的**：不是能力缺陷，是"偶发失败"和"静默失败"。

### 14.2 T1：决定稳定性的

| # | 机制 | 证据 | 说明 |
|---|---|---|---|
| 6 | **sabrContext 双向上下文**（57 更新 / 59 发送策略） | `YoutubeSabrSession.java:474-527` | 我只透传 `playbackCookie`，没维护 context 类型集合。某些视频（多音轨/广告位）会因此降级 |
| 7 | **host 候选轮转** | `YoutubeSabrSession.java:184-240` | 我只认 43 号给的 URL；DNS/CDN 单点故障时没有备选 |
| 8 | **预取与并发**：`MAX_AHEAD_SEGMENTS=64`、`awaitSegment` 阻塞取、ahead 淘汰、`SabrSegmentPendingException` + 100 ms 重试 | `SabrMediaBridge.java:31,129,263-286`、`SabrLoadErrorHandlingPolicy.java` | 我是严格串行 request→write，首帧慢（2.4 s/段），且无法边播边取 |
| 9 | **伪 DASH + 轨道切换 + ABR**：自造 MPD、`SELECTABLE_FORMATS(51)`、带宽估计、`FORMAT_INITIALIZATION_METADATA(42)`、采样提示(49/50) | `SabrDashMediaSource.java:207-320` | 我固定 itag，不切码率。弱网时只能干等 |
| 10 | **下载断点续传**：`MissionRecoveryInfo.isSabr`、按段恢复 | `SabrDownloadStreamHelper.java`、`DownloadMission.java` | 我的下载器没有断点，中断即重来 |
| 11 | **登录态**：cookies 注入，SABR 的 poToken 绑定 `dataSyncId`（登录）/`visitorData`（游客） | `LocalDomPoTokenProvider.kt:120-140` | 我是纯匿名。会员、年龄限制、地区限制内容拿不到 |
| 12 | **多客户端矩阵与降级**：mweb / visionos / tv_samsung，主播放器失败再退 android reel | `settings_keys.xml:1776-1783`、`YoutubeStreamExtractor.java:852-880` | 我只有 mweb + visionos，且固定用 mweb 拉 SABR |

### 14.3 T2：产品功能，与拉流本身无关

SponsorBlock 跳片、Return YouTube Dislike、弹幕/直播聊天、跨站搜索过滤器、B 站与 Niconico 的完整实现——这些是 PipePipe 作为"App"的部分，不是 SABR 的必要条件。

### 14.4 量级对照

| | 行数 |
|---|---|
| PipePipe 协议层（`sabr/**`） | 5,328 |
| PipePipe 播放器接线（`player/datasource/Sabr*`） | 1,550 |
| 我的 `sabr.py` | ~1,350 |

差的 4 倍不是协议知识，是**状态机**：重试预算、上下文、完整性对账、预取窗口、生命周期。协议我在第十天就写对了；这些边界情况是第十一天到第三十天的事。

### 14.5 如果只补一件事

补 **#1（重试编排）+ #3（token 生命周期）**。这两条把"偶发失败"变成"可预期失败"，性价比远高于 ABR 和 seek——后者是体验，前者是能不能用。


---

## 15. 补课：把 §14 的 T0/T1 做掉（2026-10-21）

按 §14 的清单动手，`sabr.py` 从 1,350 行长到 ~1,800 行。补完的部分：

| §14 编号 | 补了什么 | 实现位置 |
|---|---|---|
| #1 重试编排 | 退避不再当成失败：按服务端给的时间等待后**继续**；连续退避 >6 次才报错；重定向 ≤3；reload ≤2 | `SabrSession.run` |
| #3 token 生命周期 | `PotProvider`：磁盘缓存（`.pot_cache.json`）、TTL−60s 过期、**铸造 3 次重试**、全失败降级到**Python 本地冷启动 token**（不需要 Node） | `PotProvider` / `cold_start_token` |
| #4 seek | `--start` 秒数 + `playerTimeMs` 前跳 + `bufferedRanges`；`run(start_ms=…)` | `run_download` / `run` |
| #5 完整性对账 | 记录已写 segment 序号，结束时对比时间线报 `missing_segments` | `run` 末尾 |
| #6 sabrContext | 解析 57（更新）与 59（发送策略：激活/停用/丢弃），并在请求 `streamerContext.5` 回传 | `_consume` / `build_request` |
| #7 host 轮转 | 从 URL 的 `mn`/`mvi` 生成候选 host，传输失败时轮换（每次请求最多 3 次） | `build_host_candidates` / `rotate_host` |
| #12 客户端矩阵 | mweb → visionos → TVHTML5，逐个尝试直到拿到 streamingData | `player_response_matrix` |
| 附带 | 空 `MEDIA_END` 按流顺序关闭；非目标 itag 过滤；`RELOAD_PLAYER_RESPONSE` 热切换 | `_consume` / `run` |

### 15.1 新增两个命令

```bash
# 下载 + 合并（本地播放）
python3 sabr.py play --video <id> --video-itag 598 --audio-itag 600
#   → downloads/<id>.mkv

# 边下边播（在线播放）
python3 sabr.py serve --video <id> --video-itag 598 --audio-itag 600 --port 8765
#   → 播放器打开 http://<设备IP>:8765/，文件在后台增长
```

`serve` 的实现要点：后台线程先拉音频（小）、再拉视频、最后 ffmpeg 合并；HTTP 处理器支持 `Range`，
先服务音频、合并完成后自动切到 mkv。实测 `ffprobe http://127.0.0.1:8765/` 能直接读出
`vp9 + opus / 60s`——说明播放器可以在下载过程中就开始播。

### 15.2 目标视频实测（l_Yc7YeBSxc，20:04）

| 轨道 | itag | 段数 | 完整性 | 大小 |
|---|---|---|---|---|
| 音频 opus 44 kbps | 600 | **121 / 121** | complete | 5.5 MB |
| 视频 vp9 144p 38 kbps | 598 | **225 / 225** | complete | 3.8 MB |
| 合并 mkv | — | — | 20:03.9 | **9.2 MB** |

`ffprobe` 显示 `vp9 256x144 + opus`，全程解码无错。

### 15.3 还没补的

- **ABR 切码率**（51/42/49/50）与**预取窗口**（64 段）：单次请求本身已拿到 2–4 段，串行够用，
  但边播边取的首帧延迟仍是 2–3 s。
- **断点续传**：有 `--start`（媒体时间），但没有"读上次状态接着下"的旁路文件。
- **登录态**：仍是匿名；会员/年龄限制内容需要 cookie，且 token 要绑 `dataSyncId`。
- 播放器级的东西（伪 DASH、轨道切换、SponsorBlock）不在本工具范围内。


---

## 16. 再补三条：自动选质、断点续传、登录态（2026-10-21）

§15.3 列的三条已补完。

| 能力 | 实现 | 验证 |
|---|---|---|
| **自动选质（ABR-lite）** | `--video-itag auto --max-height 144`：在 ≤ 上限的轨道里挑**码率最低**的那条；`--audio-itag auto` 挑最低码率音轨 | 自动选中 `598`（vp9 144p 38 kbps）+ `599`（aac 33 kbps） |
| **并发预取** | 音视频两条 SABR 会话**并行**跑（各写各的文件），合并前 join；这比串行省掉一整条轨道的墙钟 | 20 分钟整片：两轨并行 ~3 分钟（≈6.7× 实时），此前串行约 5.5 分钟 |
| **断点续传** | 边写边存 `<file>.sabrstate.json`（`last_end_ms` / `last_seq` / `size`），`--resume` 时从该位置以追加方式继续，并按 seq 去重 | 25 s 跑到 8 段后 `--resume`，从 42.7 s 接上，跳过 seq 9，续到 seq 12，文件 164 KB → 219 KB |
| **登录态** | `--cookies cookies.txt`（Netscape 格式）或直接给 `Cookie:` 头；player 请求与 SABR 请求都带；**铸造器通过 `POT_COOKIE` 用同一份 cookie 抓首页**，保证 token 与登录会话同源 | 代码路径已通（无账号可测） |

### 16.1 关于"64 段预取窗口"的取舍

PipePipe 的那个窗口（`MAX_AHEAD_SEGMENTS=64`）是**给播放器用的**：播放器读得慢，客户端要提前把后面的段取下来放在内存/磁盘里，避免播放卡顿。
我们这里是下载器 + 一个把文件当流发的 HTTP 服务，瓶颈在 CDN 而不在读取，所以真正的收益来自**两条轨道并行**，而不是队列深度。
这点如实记录，不做无意义的队列模仿。

### 16.2 当前能力矩阵

| 场景 | 命令 |
|---|---|
| 最小体积整片 | `play --video <id> --max-height 144` |
| 指定清晰度 | `play --video <id> --video-itag 137 --audio-itag 251` |
| 断点续传 | `download … --resume` |
| 边下边播 | `serve --video <id> --max-height 720 --port 8765` |
| 登录内容 | 任意命令 + `--cookies cookies.txt` |


---

## 17. 最后两条：播放器级 ABR 与 App 层能力（2026-10-21）

### 17.1 播放器级 ABR：真做了一条 HLS 阶梯

不是"按带宽选一次清晰度"，而是**把选择权交给播放器**：

```bash
python3 sabr.py abr --video <id> --heights 144,240,360 --port 8765
# 播放器打开 http://<设备IP>:8765/master.m3u8
```

做法：

1. 从 player response 里挑**同高度的 fMP4 轨道**（`pick_exact_height`，`mp4_only`），每档一条：
   `394`(av1 144p) / `395`(av1 240p) / `396`(av1 360p)，音频取 `599`(aac)。
2. 每条轨道一个 SABR 会话**并行**下载，**每段写成独立文件** `v_394_00002.m4s`，
   同时往自己的 `v_394.m3u8` 里追加 `#EXTINF`（`#EXT-X-PLAYLIST-TYPE:EVENT`）。
3. `master.m3u8` 里三个 `#EXT-X-STREAM-INF` 带真实 `BANDWIDTH`（94 k / 206 k / 398 k），
   共享一个 `EXT-X-MEDIA` 音频组。
4. 静态文件服务器带 CORS，播放器（ffmpeg/VLC/hls.js）自己按带宽**在中途切档**。

实测：

```
#EXT-X-STREAM-INF:BANDWIDTH=94241,RESOLUTION=256x144   -> v_394.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=206518,RESOLUTION=426x240  -> v_395.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=397590,RESOLUTION=640x360  -> v_396.m3u8

$ ffprobe http://127.0.0.1:8770/master.m3u8
0,aac  1,av1,256,144
0,aac  2,av1,426,240      ← 播放器识别出多个档位
$ ffmpeg -i http://…/master.m3u8 -t 8 -f null -   → exit 0，零错误
```

### 17.2 SponsorBlock：API 换了，顺手记下来

`GET /api/skipSegments?videoID=…` **已经下线**（现在一律 404）。现行接口只有哈希前缀：

```
GET https://sponsor.ajay.app/api/skipSegments/<sha256(videoID)[:4]>?categories=[…]
```

返回的是该前缀下的**所有**视频，必须按 `videoID` 过滤。这一条值得写进笔记——很多老实现还在用旧端点，
表现为"永远没有赞助段"，而不是报错。

```bash
python3 sabr.py sponsor --video R4mESUwvbXk
    0.0 -    67.6  sponsor
```

`cut_sponsor()` 也已实现（`trim/atrim + concat` 重编码裁掉赞助段），可按需接到 `play` 后面。

### 17.3 搜索

InnerTube `search` 直连，解析 `videoRenderer`：

```bash
python3 sabr.py search "端侧 AI 手机" --limit 5
 1. l_Yc7YeBSxc  20:04  AI手机的现实与未来…  [硅谷101]
 2. AUw787LA3j8  16:35  Can AI Make Smartphones Interesting Again?  [最佳拍档]
```

这是整条链的入口闭环：**搜索 → 拿 videoId → play/serve/abr**。

### 17.4 关于"弹幕"：YouTube 侧没有这个数据

这不是"没实现"，是**不存在对应物**：

- YouTube 没有弹幕轨。唯一接近的是**直播/首播的 live chat replay**（`live_chat/get_live_chat_replay`），
  只有开过直播的视频才有，普通点播视频不存在。
- 真正的弹幕在 B 站：`x/v1/dm/list.so`（历史弹幕）+ WebSocket 实时流，16 字节包头 + brotli，
  那是 PipePipe 的 `BilibiliWebSocketClient` 那套，属于另一个服务，不该塞进这个 YouTube 工具。

如果要做，正确的形态是：**live chat replay → ASS 字幕叠加**（时间戳对齐，播放器加载字幕即可），
而不是伪造一个"弹幕"按钮。这一条我没有动，因为对绝大多数点播视频它是空的。
