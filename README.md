# pipepipe-sabr

一个**从源码逆向重建**的 YouTube SABR 客户端与播放器——不是 NewPipe 的封装，而是把
YouTube 现在的私有流媒体协议（SABR / UMP）与完整性证明链路重新实现了一遍。

> 起点是对开源项目 [PipePipe](https://github.com/InfinityLoop1308/PipePipe)（GPLv3）的
> 源码级逆向分析：它的真正核心不在 UI，而在 `PipePipeExtractor/services/youtube/sabr/`
> 那 5,328 行——一套自研的 SABR 协议栈，外加客户端里 1,550 行把分段流伪装成 DASH 的
> 播放器接线。本仓库是那次分析的产物 + 一个可运行的独立实现。

分析报告：[REPORT.md](REPORT.md)（含逐字段的协议表与 `文件:行` 证据）

---

## 它做了什么

```
player response → sig/n 解码 → init/sidx → SABR 会话（UMP 分段） → 播放 / 下载
                                          ↑
                              本地 BotGuard 铸造 PO Token
```

| 能力 | 状态 |
|---|---|
| SABR 协议栈（UMP 封包、protobuf 字段、控制包） | 已实现，纯标准库 |
| 签名 / `n` 参数解码（mweb 直链 + SABR URL） | 已实现，多级降级 |
| PO Token 本地铸造（Node + jsdom + BotGuard） | 已实现，带缓存/重试/冷启动降级 |
| 断点续传、自动选质、并发双轨 | 已实现 |
| 边下边播（增长文件 + Range） | 已实现 |
| 播放器级 ABR（HLS 阶梯，播放器按带宽切档） | 已实现 |
| 移动端 Feed + 播放器 UI（真实数据，MSE 分段播放） | 已实现 |

实测（20 分 04 秒的视频，144p）：

```
音频 itag 600  121/121 段  complete   5.5 MB
视频 itag 598  225/225 段  complete   3.8 MB
合并 mkv       20:03.9                9.2 MB   ffmpeg 解码零错误
```

PO Token 的对照实验（同一视频、同一 itag）：

| 运行 | STREAM_PROTECTION_STATUS | 覆盖时长 |
|---|---|---|
| 无 token | 2 | **60.0 s**（服务器停止推进） |
| 现铸 token | 1 | **260 s**（整片） |

---

## 快速开始

只需要 Python 3.8+（纯标准库）。PO Token 铸造需要 Node 22。

```bash
# 1) 自检（离线，使用仓库内真实抓包 fixture）
python3 sabr.py selftest

# 2) 看一个视频的 SABR 会话
python3 sabr.py probe --video dQw4w9WgXcQ --client mweb --pot auto --itag 251

# 3) 下载 + 合并（本地播放）
python3 sabr.py play --video <id> --max-height 480

# 4) 边下边播（浏览器打开提示的地址）
python3 sabr.py serve --video <id> --max-height 720 --port 8765

# 5) 播放器级 ABR（HLS 阶梯）
python3 sabr.py abr --video <id> --heights 144,240,360 --port 8765

# 6) 搜索 / SponsorBlock
python3 sabr.py search "design documentary" --limit 5
python3 sabr.py sponsor --video <id>
```

PO Token 铸造：

```bash
cd potnode && npm install          # bgutils-js + jsdom
node mint2.mjs <videoId>           # 打印 token
# 或在 sabr.py 里用 --pot auto 自动铸造并缓存
```

## 移动端 Feed（`app/`）

真实数据源 + 真实分段播放：

```bash
python3 app/server.py 8800
# 浏览器打开 http://<device-ip>:8800/
```

- `/api/feed`、`/api/search`、`/api/video/<id>`、`/api/state`、`/api/download`
- `/init/<id>`、`/seg/<id>/<seq>`、`/api/index/<id>` —— MSE 分段播放
  （Chrome 的 `<video src>` 不接受总长未知的响应，所以必须走分段）
- 点赞/收藏/进度落盘到 `app/data/state.json`

---

## 仓库结构

```
sabr.py                # 核心：SABR 客户端（协议 / 解码 / 会话 / 下载）
pot_ab.py              # PO Token A/B 对照实验
ui_server.py           # 带 Range 的静态服务
app/server.py          # Feed 应用后端（真实数据 + MSE 分段）
app/web/               # Feed 前端（HTML/CSS/JS，无构建步骤）
potnode/mint2.mjs      # BotGuard 铸造器（唯一能铸出 token 的那条路）
fixtures/              # 真实抓包样本：SABR 错误包、MP4/WebM 初始化段
REPORT.md              # 逆向分析报告（协议字段表、逐行证据、实测结果）
```

## 关于合法性的说明

- 本项目用于**协议研究与互操作性**，不提供、不托管任何音视频内容；所有媒体都由 YouTube
  的 CDN 按用户请求直接提供。
- 它不解密 DRM，不绕过付费墙；受限内容（会员/年龄限制）仍需用户自己的登录态。
- 大批量抓取会触发平台限流（本仓库的实测记录里就有：同一出口 IP 约 250 次请求后被降级为
  `LOGIN_REQUIRED`）。请自行控制频率，遵守目标平台的服务条款。
- 逆向对象 PipePipe 为 GPLv3；本仓库**不包含**其源码，仅包含分析结论与独立实现。
  本仓库代码以 MIT 发布。

## 已知边界

- `poToken` 只能推开 `STREAM_PROTECTION_STATUS` 那道墙，推不开 `LOGIN_REQUIRED`（账号/IP 级）。
- 直播（`LIVE_STREAM`）走 HLS，不在 SABR 路径内。
- 播放器级 ABR 依赖播放器自行切档；本仓库不实现自适应算法。
- Feed 的视觉仍在迭代，尚未完全对齐设计参考。
