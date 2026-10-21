/* Geex feed — real data from /api/*, MSE playback from /init + /seg */
import { SabrMSE } from './mse.js';

const $ = (s, r = document) => r.querySelector(s);
const feedEl = $('#feed');
const stateEl = $('#state');
const player = $('#player');
const pv = $('#pv');

const CH = ['Save', 'Best Video', 'Study'];
const state = { likes: {}, saved: {}, progress: {}, items: [], meta: {} };
const cardPlayer = new SabrMSE();
const detailPlayer = new SabrMSE();
let active = null;
let current = null;

/* ---------------- utils ---------------- */
const fmt = s => {
  if (!isFinite(s) || s < 0) return '00:00';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = Math.floor(s % 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : String(m).padStart(2, '0')) + ':' + String(r).padStart(2, '0');
};
const nfmt = n => n >= 1e9 ? (n / 1e9).toFixed(1) + 'B'
  : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M'
  : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(n || 0);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 2200);
}

function showState(title, body, retry) {
  $('#stTitle').textContent = title;
  $('#stBody').textContent = body;
  stateEl.hidden = false;
  $('#stRetry').onclick = () => { stateEl.hidden = true; retry(); };
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.error + (j.message ? ': ' + j.message : ''); } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
const saveState = debounce(() => {
  api('/api/state', {
    method: 'POST',
    body: JSON.stringify({ likes: state.likes, saved: state.saved, progress: state.progress })
  }).catch(() => {});
}, 600);

/* ---------------- boot ---------------- */
async function boot() {
  feedEl.innerHTML = '';
  try {
    const [f, s] = await Promise.all([api('/api/feed'), api('/api/state')]);
    Object.assign(state, s, { items: f.items || [] });
    if (!state.items.length) return showState('没有内容', '上游返回空列表。', boot);
    render();
  } catch (err) {
    showState('加载失败', String(err.message || err), boot);
  }
}

function render() {
  feedEl.innerHTML = '';
  feedEl.setAttribute('aria-busy', 'false');
  state.items.slice(0, 18).forEach((it, i) => {
    const chap = document.createElement('section');
    chap.className = 'chapter';
    chap.innerHTML = `<div class="word">${CH[i % CH.length]}`
      + `<small>${String(i + 1).padStart(2, '0')} / ${state.items.length}</small></div>`;
    const wrap = document.createElement('div');
    wrap.className = 'cards';
    wrap.appendChild(card(it));
    chap.appendChild(wrap);
    feedEl.appendChild(chap);
  });
  observe();
}

function card(item) {
  const el = document.createElement('article');
  el.className = 'card';
  el.dataset.id = item.id;
  el.innerHTML = `
    <video muted playsinline preload="none" poster="${item.thumb}"></video>
    <div class="chip"><i>${esc((item.author || '?')[0].toUpperCase())}</i>
      <div><b>${esc(item.author || '未知作者')}</b><em>${fmt(item.duration)}</em></div></div>
    <div class="over">
      <div class="times"><span class="t-cur">00:00</span><span>${fmt(item.duration)}</span></div>
      <div class="bar"><i></i></div>
      <div class="row">
        <button class="pill"><svg viewBox="0 0 10 12"><path d="M0 0l10 6-10 6z"/></svg><span>Play</span></button>
        <span class="count">${nfmt(item.views)} views</span>
      </div>
    </div>`;
  const v = el.querySelector('video');
  el.addEventListener('click', e => { if (!e.target.closest('button')) openPlayer(item); });
  el.querySelector('.pill').addEventListener('click', e => {
    e.stopPropagation();
    if (!v.src) { activate(el).then(() => v.play().catch(() => {})); return; }
    if (v.paused) v.play().catch(() => {}); else v.pause();
    syncCardButton(el, v);
  });
  v.addEventListener('timeupdate', () => {
    const d = v.duration && isFinite(v.duration) ? v.duration : item.duration;
    el.querySelector('.bar i').style.width = (d ? v.currentTime / d * 100 : 0) + '%';
    el.querySelector('.t-cur').textContent = fmt(v.currentTime);
  });
  v.addEventListener('play', () => syncCardButton(el, v));
  v.addEventListener('pause', () => syncCardButton(el, v));
  return el;
}

function syncCardButton(el, v) {
  const btn = el.querySelector('.pill');
  btn.querySelector('span').textContent = v.paused ? 'Play' : 'Pause';
  btn.querySelector('svg').innerHTML = v.paused
    ? '<path d="M0 0l10 6-10 6z"/>'
    : '<path d="M0 0h3v12H0zM7 0h3v12H7z"/>';
}

async function activate(el) {
  const v = el.querySelector('video');
  if (v.dataset.on) return;
  const id = el.dataset.id;
  try {
    const meta = state.meta[id] || await api('/api/video/' + id);
    state.meta[id] = meta;
    const itag = pickItag(meta, 480);
    v.dataset.itag = itag;
    v.dataset.on = '1';
    await cardPlayer.attach(v, id, itag, 0);
    syncCardButton(el, v);
  } catch (err) {
    v.dataset.on = '';
    v.poster = (state.meta[id] || {}).thumb || v.poster;
  }
}

function pickItag(meta, cap) {
  const list = (meta.renditions || []).filter(r => r.height <= cap && /mp4/.test(r.mime));
  const pool = list.length ? list : (meta.renditions || []).filter(r => /mp4/.test(r.mime));
  const arr = pool.length ? pool : meta.renditions || [];
  return arr[arr.length - 1].itag;
}

function observe() {
  const io = new IntersectionObserver(entries => {
    entries.forEach(e => {
      const el = e.target;
      if (e.intersectionRatio > 0.55) {
        if (active !== el && active) {
          const pv0 = active.querySelector('video');
          cardPlayer.detach();
          pv0.removeAttribute('src');
          active = null;
        }
        active = el;
        activate(el);
      }
    });
  }, { threshold: [0, 0.55, 1] });
  document.querySelectorAll('.card').forEach(c => io.observe(c));
}

/* ---------------- player ---------------- */
async function openPlayer(item) {
  current = item;
  player.classList.add('open');
  player.setAttribute('aria-hidden', 'false');
  $('#pTitle').textContent = item.title || '—';
  $('#pAuthor').textContent = item.author || '—';
  $('#pAv').textContent = (item.author || '?')[0].toUpperCase();
  $('#likeN').textContent = nfmt(item.views);
  $('#like').classList.toggle('on', !!state.likes[item.id]);
  $('#save').classList.toggle('on', !!state.saved[item.id]);

  let meta;
  try {
    meta = state.meta[item.id] || await api('/api/video/' + item.id);
    state.meta[item.id] = meta;
  } catch (err) {
    toast('该视频当前不可用');
    return closePlayer();
  }

  const sel = $('#quality');
  sel.innerHTML = '';
  (meta.renditions || []).filter(r => /mp4/.test(r.mime)).slice().reverse().forEach(r => {
    const o = document.createElement('option');
    o.value = r.itag;
    o.textContent = `${r.height}p · ${r.mime.split('/')[1]}`;
    sel.appendChild(o);
  });
  const itag = pickItag(meta, 480);
  sel.value = itag;
  await startAt(itag, state.progress[item.id] || 0);

  sel.onchange = () => startAt(Number(sel.value), pv.currentTime || 0);
}

async function startAt(itag, seconds) {
  try {
    await detailPlayer.attach(pv, current.id, itag, seconds);
    pv.muted = false;
    pv.volume = 1;
  } catch (err) {
    toast('无法播放：' + err.message);
  }
}

function closePlayer() {
  player.classList.remove('open');
  player.setAttribute('aria-hidden', 'true');
  if (current) state.progress[current.id] = Math.round(pv.currentTime || 0);
  saveState();
  detailPlayer.detach();
  current = null;
}

$('#close').addEventListener('click', closePlayer);

$('#toggle').addEventListener('click', () => {
  if (pv.paused) pv.play().catch(() => {}); else pv.pause();
});
pv.addEventListener('play', () => {
  $('#toggleText').textContent = 'Pause';
  $('#icoPlay').innerHTML = '<path d="M0 0h3v12H0zM7 0h3v12H7z"/>';
});
pv.addEventListener('pause', () => {
  $('#toggleText').textContent = 'Play';
  $('#icoPlay').innerHTML = '<path d="M0 0l10 6-10 6z"/>';
});

pv.addEventListener('timeupdate', () => {
  const d = pv.duration && isFinite(pv.duration) ? pv.duration : 0;
  const t = pv.currentTime || 0;
  const p = d ? t / d * 100 : 0;
  $('#fill').style.width = p + '%';
  $('#dot').style.left = p + '%';
  $('#tCur').textContent = fmt(t);
  if (d) $('#tDur').textContent = fmt(d);
  if (current && t > 0) {
    state.progress[current.id] = Math.round(t);
    if (Math.round(t) % 5 === 0) saveState();
  }
});

const scrub = $('#scrub');
let dragging = false;
const seekAt = x => {
  const r = scrub.getBoundingClientRect();
  const p = Math.min(1, Math.max(0, (x - r.left) / r.width));
  const d = pv.duration;
  if (d && isFinite(d)) pv.currentTime = p * d;
  $('#fill').style.width = p * 100 + '%';
  $('#dot').style.left = p * 100 + '%';
};
scrub.addEventListener('pointerdown', e => { dragging = true; scrub.setPointerCapture(e.pointerId); seekAt(e.clientX); });
scrub.addEventListener('pointermove', e => { if (dragging) seekAt(e.clientX); });
scrub.addEventListener('pointerup', () => { dragging = false; });

$('#like').addEventListener('click', e => {
  if (!current) return;
  const on = !state.likes[current.id];
  state.likes[current.id] = on;
  e.currentTarget.classList.toggle('on', on);
  saveState();
});

$('#save').addEventListener('click', e => {
  if (!current) return;
  const on = !state.saved[current.id];
  if (on) state.saved[current.id] = true; else delete state.saved[current.id];
  e.currentTarget.classList.toggle('on', on);
  saveState();
  toast(on ? '已保存' : '已取消保存');
  if (on) startDownload(current.id, $('#quality').value);
});

async function startDownload(id, itag) {
  try {
    const job = await api('/api/download', { method: 'POST', body: JSON.stringify({ id, itag: Number(itag) }) });
    toast('开始下载');
    const poll = async () => {
      const j = await api('/api/download/' + job.id);
      if (j.state === 'done') return toast('下载完成');
      if (j.state === 'failed') return toast('下载失败：' + (j.error || ''));
      setTimeout(poll, 1500);
    };
    setTimeout(poll, 1500);
  } catch (err) { toast('下载失败'); }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && player.classList.contains('open')) closePlayer();
  if (e.key === ' ' && player.classList.contains('open')) { e.preventDefault(); $('#toggle').click(); }
});

boot();
