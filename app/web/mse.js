/* SabrMSE — plays a YouTube video out of per-segment HTTP endpoints.
   Chrome refuses unknown-length media responses, so the feed (which only ever grows)
   is fetched segment by segment and appended to a MediaSource instead. */
export class SabrMSE {
  constructor() {
    this.video = null; this.ms = null; this.sb = null;
    this.segs = []; this.i = 0; this.busy = false;
    this.id = null; this.itag = null; this.ended = false; this.alive = false;
  }

  async attach(videoEl, videoId, itag, startSec = 0) {
    this.detach();
    this.alive = true;
    this.video = videoEl;
    this.id = videoId; this.itag = itag; this.ended = false;

    const [meta, idx] = await Promise.all([
      fetch('/api/video/' + videoId).then(r => r.json()),
      fetch(`/api/index/${videoId}?itag=${itag}`).then(r => r.json()),
    ]);
    const rend = (meta.renditions || []).find(r => r.itag === Number(itag));
    if (!rend) throw new Error('rendition missing');
    const mime = `video/mp4; codecs="${rend.codecs}"`;
    if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) {
      throw new Error('codec unsupported: ' + mime);
    }

    let init = null;
    for (let attempt = 0; attempt < 40 && !init; attempt++) {
      const res = await fetch(`/init/${videoId}?itag=${itag}`);
      if (res.ok) init = await res.arrayBuffer();
      else await new Promise(r => setTimeout(r, 700));
    }
    if (!init) throw new Error('init unavailable');

    this.ms = new MediaSource();
    videoEl.src = URL.createObjectURL(this.ms);
    await new Promise((res, rej) => {
      this.ms.addEventListener('sourceopen', res, { once: true });
      setTimeout(() => rej(new Error('sourceopen timeout')), 8000);
    });
    if (!this.alive) return;
    this.sb = this.ms.addSourceBuffer(mime);
    this.segs = idx.segments || [];
    this.i = this._indexFor(startSec);

    await this._append(init);
    this.video.addEventListener('seeking', () => this._onSeek());
    this._feed();
    this.video.play().catch(() => {});
  }

  detach() {
    this.alive = false;
    if (this.video) {
      try { this.video.pause(); } catch (_) {}
      this.video.removeAttribute('src');
      try { this.video.load(); } catch (_) {}
    }
    if (this.ms && this.ms.readyState === 'open') {
      try { this.ms.endOfStream(); } catch (_) {}
    }
    this.video = null; this.ms = null; this.sb = null; this.segs = [];
  }

  _indexFor(sec) {
    if (!this.segs.length) return 0;
    for (let k = 0; k < this.segs.length; k++) {
      if (this.segs[k].start + this.segs[k].dur > sec * 1000) return k;
    }
    return this.segs.length - 1;
  }

  _wait() {
    return new Promise(res => {
      const sb = this.sb;
      const done = () => { sb.removeEventListener('updateend', done); res(); };
      sb.addEventListener('updateend', done);
    });
  }

  async _append(buf) {
    if (!this.sb || !this.alive) return;
    while (this.sb.updating) await this._wait();
    try { this.sb.appendBuffer(buf); } catch (e) { if (e.name === 'QuotaExceededError') { this._trim(); } }
    await this._wait();
  }

  _trim() {
    const t = this.video.currentTime;
    if (this.sb && this.sb.buffered.length && t > 30) {
      try { this.sb.remove(0, t - 20); } catch (_) {}
    }
  }

  async _feed() {
    if (this.busy) return;
    this.busy = true;
    try {
      while (this.alive) {
        const bufEnd = this._bufferedEnd();
        if (bufEnd > this.video.currentTime + 20) break;
        if (this.i >= this.segs.length) {
          if (!this.ended) {
            const idx = await fetch(`/api/index/${this.id}?itag=${this.itag}`).then(r => r.json())
              .catch(() => null);
            if (idx) { this.segs = idx.segments || this.segs; this.ended = !!idx.done; }
            if (this.i >= this.segs.length) await new Promise(r => setTimeout(r, 800));
            continue;
          }
          if (this.ms && this.ms.readyState === 'open') this.ms.endOfStream();
          break;
        }
        const seg = this.segs[this.i];
        let res = await fetch(`/seg/${this.id}/${seg.seq}?itag=${this.itag}`);
        if (res.status === 404) {
          // segment not downloaded yet: wait and retry
          await new Promise(r => setTimeout(r, 800));
          const idx = await fetch(`/api/index/${this.id}?itag=${this.itag}`).then(r => r.json());
          this.segs = idx.segments || this.segs;
          this.ended = !!idx.done;
          continue;
        }
        if (!res.ok) break;
        const buf = await res.arrayBuffer();
        await this._append(buf);
        this.i++;
        this._trim();
      }
    } finally {
      this.busy = false;
    }
  }

  _bufferedEnd() {
    const b = this.video && this.video.buffered;
    if (!b || !b.length) return 0;
    for (let k = 0; k < b.length; k++) {
      if (b.start(k) <= this.video.currentTime + 1 && b.end(k) >= this.video.currentTime) return b.end(k);
    }
    return b.end(b.length - 1);
  }

  _onSeek() {
    const t = this.video.currentTime;
    const b = this.video.buffered;
    let covered = false;
    for (let k = 0; k < b.length; k++) if (t >= b.start(k) && t <= b.end(k)) covered = true;
    if (covered) return;
    this.i = this._indexFor(t);
    this._feed();
  }
}
