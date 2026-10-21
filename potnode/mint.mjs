// Mint a YouTube PO token with BotGuard (bgutils-js, Node 22).
// usage: node mint.mjs <videoId> [--visitor <visitorData>]
import vm from 'node:vm';
import { BotGuardClient, getChallenge } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';

const REQUEST_KEY = 'O43z0dpjhgX20SCx4KAo';
const API_KEY = 'AIzaSyDyT5W0Jh49F30Pqqtyfdf7pDLFKLJoAnw';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
         + '(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36';

const videoId = process.argv[2];
if (!videoId) { console.error('usage: node mint.mjs <videoId>'); process.exit(2); }

const t0 = Date.now();
const log = (m) => console.error(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${m}`);

// 1. challenge via att/get
const attRes = await fetch('https://www.youtube.com/youtubei/v1/att/get?prettyPrint=false', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'User-Agent': UA,
    'Origin': 'https://www.youtube.com',
    'Referer': 'https://www.youtube.com/',
    'X-YouTube-Client-Name': '1',
    'X-YouTube-Client-Version': '2.20261020.00.00',
  },
  body: JSON.stringify({
    context: { client: { clientName: 'WEB', clientVersion: '2.20261020.00.00', hl: 'en', gl: 'US' } },
    engagementType: 'ENGAGEMENT_TYPE_UNBOUND',
  }),
});
if (!attRes.ok) throw new Error('att/get HTTP ' + attRes.status);
const att = await attRes.json();
let bg = att.bgChallenge;
if (typeof bg === 'string') bg = JSON.parse(bg);
if (!bg || !bg.program) throw new Error('no bgChallenge in att/get response');
if (process.env.WAA) {
  const ch = await getChallenge({ requestKey: REQUEST_KEY, fetchFunction: fetch, useYouTubeAPI: true });
  if (ch && ch.program) { bg = { ...bg, ...ch }; log('using WAA/Create challenge'); }
}
log(`challenge globalName=${bg.globalName} program=${bg.program.length}b`);

// 2. interpreter (inline, else fetch interpreterUrl)
let interpreter = bg.interpreterJavascript?.privateDoNotAccessOrElseSafeScriptWrappedValue;
if (!interpreter) {
  const u = bg.interpreterUrl?.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
  if (!u) throw new Error('no interpreter');
  const url = u.startsWith('//') ? 'https:' + u : u;
  interpreter = await (await fetch(url, { headers: { 'User-Agent': UA } })).text();
}
// minimal browser shim: the interpreter expects window/document/navigator
globalThis.window = globalThis;
globalThis.self = globalThis;
try {
  Object.defineProperty(globalThis, 'navigator', {
    value: { userAgent: UA, language: 'en-US', languages: ['en-US', 'en'],
             platform: 'Win32', vendor: 'Google Inc.', webdriver: false },
    configurable: true, writable: true,
  });
} catch (e) { /* leave the built-in navigator */ }
globalThis.location = globalThis.location || {
  href: 'https://www.youtube.com/', origin: 'https://www.youtube.com',
  protocol: 'https:', host: 'www.youtube.com', hostname: 'www.youtube.com',
  pathname: '/', search: '', hash: '',
};
if (!globalThis.document) {
  const el = () => ({
    style: {}, children: [], appendChild(c) { this.children.push(c); return c; },
    setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    addEventListener() {}, removeEventListener() {}, insertBefore() {},
    getElementsByTagName() { return []; }, getContext() { return null; },
  });
  globalThis.document = {
    documentElement: el(), body: el(), head: el(), createElement: el,
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, addEventListener() {},
    createTextNode(t) { return { textContent: t }; }, cookie: '',
  };
}

// run in the real global scope so the VM registers `globalThis[globalName]`
vm.runInThisContext(interpreter, { filename: 'botguard-interpreter.js' });
if (!globalThis[bg.globalName]) throw new Error('VM not registered');
log('interpreter loaded');

// 3. BotGuard client + snapshot
const client = await BotGuardClient.create({
  program: bg.program,
  globalName: bg.globalName,
  globalObject: globalThis,
});
const webPoSignalOutput = [];
const contentBinding = {
  c: att.challenge,
  e: 'ENGAGEMENT_TYPE_UNBOUND',
};
const botguardResponse = await client.snapshot({ contentBinding, webPoSignalOutput });
log(`botguardResponse ${botguardResponse.length}b, signalOutput=${webPoSignalOutput.length}`);
if (!webPoSignalOutput.length) throw new Error('webPoSignalOutput is empty');

// 4. integrity token
const itRes = await fetch(
  'https://jnn-pa.googleapis.com/$rpc/google.internal.waa.v1.Waa/GenerateIT',
  {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json+protobuf',
      'x-goog-api-key': API_KEY,
      'x-user-agent': 'grpc-web-javascript/0.1',
      'User-Agent': UA,
    },
    body: JSON.stringify([REQUEST_KEY, botguardResponse]),
  });
const raw = await itRes.json();
log('GenerateIT ' + itRes.status + ' ' + JSON.stringify(raw).slice(0, 80));
const integrityToken = raw[3] || raw[0];
const ttl = raw[1] || 0;
if (!integrityToken) throw new Error('no integrity token');

// 5. mint
const minter = await WebPoMinter.create({ integrityToken, estimatedTtlSecs: ttl }, webPoSignalOutput);
const poToken = await minter.mintAsWebsafeString(videoId);
log(`poToken ${poToken.length}b ttl=${ttl}s`);
console.log(poToken);
