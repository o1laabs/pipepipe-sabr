// Mint a YouTube PO token the way bgutils' official example does (jsdom + page challenge).
// usage: node mint2.mjs <videoId>
import { BotGuardClient } from 'bgutils-js/botguard';
import { WebPoMinter } from 'bgutils-js/webpo';
import { buildURL, parseLooseJSON, getHeaders, USER_AGENT } from 'bgutils-js/utils';
import { JSDOM } from 'jsdom';

const videoId = process.argv[2];
if (!videoId) { console.error('usage: node mint2.mjs <videoId>'); process.exit(2); }
const t0 = Date.now();
const log = (m) => console.error(`[${((Date.now() - t0) / 1000).toFixed(1)}s] ${m}`);

const dom = new JSDOM('<!DOCTYPE html><html lang="en"><head><title></title></head><body></body></html>', {
  url: 'https://www.youtube.com',
  referrer: 'https://www.youtube.com/',
  userAgent: USER_AGENT,
});

const cookie = process.env.POT_COOKIE || '';
const pageResponse = await fetch('https://www.youtube.com', {
  headers: Object.assign(
    { accept: '*/*', 'accept-language': 'en-US,en;q=0.7', 'user-agent': USER_AGENT },
    cookie ? { cookie } : {}),
});
const pageHtml = await pageResponse.text();
log(`home ${pageResponse.status} ${pageHtml.length}b`);

const ytConfig = pageHtml.match(/ytcfg\.set\(({.+?})\);/s)?.[1];
if (!ytConfig) throw new Error('no ytcfg');
dom.window.yt = { config_: JSON.parse(ytConfig) };

Object.assign(globalThis, {
  yt: dom.window.yt,
  window: dom.window,
  document: dom.window.document,
  location: dom.window.location,
  origin: dom.window.origin,
});
if (!('navigator' in globalThis)) {
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator });
}

const att = pageHtml.match(/window\.ytAtN\(\s*({[\s\S]*?})\s*\)/);
if (!att) throw new Error('no ytAtN challenge in page');
const attJson = parseLooseJSON(att[1]);
const challengeResponse = attJson.R;
if (!challengeResponse?.bgChallenge) throw new Error('no bgChallenge');

const bg = challengeResponse.bgChallenge;
const interpreterUrl = bg.interpreterUrl.privateDoNotAccessOrElseTrustedResourceUrlWrappedValue;
const js = await (await fetch(`https:${interpreterUrl}`)).text();
new Function(js)();
log(`vm loaded globalName=${bg.globalName} program=${bg.program.length}b`);

const client = await BotGuardClient.create({
  program: bg.program,
  globalName: bg.globalName,
  globalObject: globalThis,
});

const webPoSignalOutput = [];
const botguardResponse = await client.snapshot({ webPoSignalOutput });
log(`snapshot ${botguardResponse.length}b, signalOutput=${webPoSignalOutput.length}`);
if (!webPoSignalOutput.length) throw new Error('webPoSignalOutput is empty');

const itRes = await fetch(buildURL('GenerateIT', true), {
  method: 'POST',
  headers: getHeaders(),
  body: JSON.stringify(['O43z0dpjhgX20SCx4KAo', botguardResponse]),
});
const json = await itRes.json();
log('GenerateIT ' + itRes.status);
const [integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken] = json;

const minter = await WebPoMinter.create(
  { integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken },
  webPoSignalOutput);
const poToken = await minter.mintAsWebsafeString(videoId);
log(`poToken ${poToken.length}b ttl=${estimatedTtlSecs}`);
console.log(poToken);
