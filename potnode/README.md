# potnode — 本地铸造 YouTube PO Token

Node 22 + [`bgutils-js`](https://github.com/LuanRT/BgUtils) 4.0.3 + `jsdom`。
4 秒左右出一个 **124 字节、TTL 12 小时**、绑到具体 videoId 的 PO token。

```bash
cd potnode
npm install            # bgutils-js + jsdom
node mint2.mjs dQw4w9WgXcQ      # 打印 token（stdout，日志在 stderr）
```

## 为什么是这三个文件

| 文件 | 作用 |
|---|---|
| `mint2.mjs` | 唯一在用的实现：从 **youtube.com 首页**抓 `ytcfg.set({...})` 与 `window.ytAtN({...})`，用 jsdom 提供 DOM，跑 BotGuard VM，`snapshot` 出 `webPoSignalOutput`，再 `GenerateIT → WebPoMinter` |
| `mint.mjs` | 早期尝试（att/get + 手写垫片）。**保留作反例**：能拿到 3 KB `botguardResponse`，但 `webPoSignalOutput` 恒为 0，铸不出 token |

## 三个必要条件（少一个就铸不出）

1. **challenge 必须来自首页 `ytAtN`**。`youtubei/v1/att/get` 返回的 program 跑得出 botguardResponse，
   但不会产生 webpo minter —— 这是最容易卡住的一步。
2. **要有真实 DOM**：`new JSDOM(..., { url: 'https://www.youtube.com' })` 并把
   `window/document/location/origin/yt` 挂到 `globalThis`；纯 Node 会 `window is not defined`。
3. **`yt.config_` 用页面里那份 `ytcfg.set()`**，VM 需要里面的 `EVENT_ID`。

## 已知边界

- 铸出来的 token 是 **标准 base64（带 `=` padding）**，不是 base64url；传给 `?pot=` 前要转换
  （`sabr.py` 的 `decode_pot` 已兼容两种）。
- token 是 **content-bound**：一个视频一个，不能跨视频复用。
- 它解决的是 `STREAM_PROTECTION_STATUS`（58 号 part）那道墙，**不解决** `LOGIN_REQUIRED`
  （"Sign in to confirm you're not a bot"，账号/IP 级）。
