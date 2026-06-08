/* ──────────────────────────────────────────────────────────────────────────
   Wizard de bienvenida (onboarding) — motor vanilla, sin dependencias.
   Resalta elementos del menú y muestra explicaciones cortas paso a paso.
   Config inyectada desde base.html en window.__ONBOARDING__.
   ────────────────────────────────────────────────────────────────────────── */
(function () {
  const CFG = window.__ONBOARDING__ || {};
  const TOURS = CFG.tours || {};

  let steps = [];
  let idx = 0;
  let currentTour = null;
  let els = null;

  function csrf() {
    return document.querySelector('meta[name=csrf-token]')?.content
        || document.querySelector('input[name=csrf_token]')?.value || '';
  }

  function markDone(tour) {
    fetch('/onboarding/done', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: 'tour=' + encodeURIComponent(tour) + '&csrf_token=' + encodeURIComponent(csrf()),
    }).catch(() => {});
  }

  // ── Construcción del DOM (una sola vez) ──────────────────────────────────
  function injectStyles() {
    if (document.getElementById('ob-styles')) return;
    const s = document.createElement('style');
    s.id = 'ob-styles';
    s.textContent = `
      #ob-overlay { position:fixed; inset:0; z-index:9998; display:none; }
      #ob-spotlight {
        position:fixed; z-index:9999; border-radius:10px;
        box-shadow:0 0 0 9999px rgba(8,10,15,0.78), 0 0 0 2px rgba(59,130,246,0.7), 0 0 24px rgba(59,130,246,0.45);
        transition:all 0.28s cubic-bezier(.4,0,.2,1); pointer-events:none;
      }
      #ob-tooltip {
        position:fixed; z-index:10000; width:320px; max-width:calc(100vw - 24px);
        background:#11141b; border:1px solid rgba(255,255,255,0.1); border-radius:0.875rem;
        box-shadow:0 20px 60px rgba(0,0,0,0.6); padding:1.1rem 1.25rem;
        color:rgba(166,173,187,0.85); transition:all 0.2s ease; opacity:0;
      }
      #ob-tooltip.visible { opacity:1; }
      #ob-step-badge {
        display:inline-flex; align-items:center; gap:0.35rem; font-size:0.6rem; font-weight:700;
        letter-spacing:0.12em; text-transform:uppercase; color:#60a5fa; margin-bottom:0.5rem;
      }
      #ob-title { font-size:0.95rem; font-weight:700; color:#e5e7eb; margin-bottom:0.4rem; line-height:1.3; }
      #ob-text  { font-size:0.8rem; line-height:1.55; color:rgba(166,173,187,0.7); }
      .ob-progress { display:flex; gap:4px; margin:0.9rem 0 0.75rem; }
      .ob-dot { height:4px; flex:1; border-radius:9999px; background:rgba(255,255,255,0.1); transition:background 0.2s; }
      .ob-dot.done { background:rgba(59,130,246,0.8); }
      .ob-foot { display:flex; align-items:center; justify-content:space-between; gap:0.5rem; margin-top:0.25rem; }
      .ob-btns { display:flex; gap:0.4rem; }
      .ob-btn {
        padding:0.4rem 0.85rem; border-radius:0.5rem; font-size:0.75rem; font-weight:600;
        cursor:pointer; border:1px solid transparent; transition:all 0.15s; background:transparent;
      }
      .ob-btn-primary { background:linear-gradient(135deg,#3b82f6,#6366f1); color:white; }
      .ob-btn-primary:hover { box-shadow:0 4px 14px rgba(59,130,246,0.35); transform:translateY(-1px); }
      .ob-btn-ghost { color:rgba(166,173,187,0.6); border-color:rgba(255,255,255,0.1); }
      .ob-btn-ghost:hover { background:rgba(255,255,255,0.05); color:rgba(166,173,187,0.9); }
      .ob-skip { font-size:0.7rem; color:rgba(166,173,187,0.4); cursor:pointer; text-decoration:none; }
      .ob-skip:hover { color:rgba(166,173,187,0.75); }
      .ob-noshow { display:flex; align-items:center; gap:0.4rem; font-size:0.68rem; color:rgba(166,173,187,0.45); cursor:pointer; margin-top:0.6rem; }
      .ob-noshow input { accent-color:#3b82f6; }
    `;
    document.head.appendChild(s);
  }

  function buildDom() {
    if (els) return;
    injectStyles();
    const overlay = document.createElement('div');
    overlay.id = 'ob-overlay';

    const spot = document.createElement('div');
    spot.id = 'ob-spotlight';

    const tip = document.createElement('div');
    tip.id = 'ob-tooltip';
    tip.innerHTML = `
      <div id="ob-step-badge">
        <svg width="11" height="11" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path stroke-linecap="round" stroke-linejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z"/></svg>
        <span id="ob-step-count"></span>
      </div>
      <div id="ob-title"></div>
      <div id="ob-text"></div>
      <div class="ob-progress" id="ob-progress"></div>
      <div class="ob-foot">
        <span class="ob-skip" id="ob-skip">Saltar guía</span>
        <div class="ob-btns">
          <button class="ob-btn ob-btn-ghost" id="ob-prev">Anterior</button>
          <button class="ob-btn ob-btn-primary" id="ob-next">Siguiente</button>
        </div>
      </div>
      <label class="ob-noshow"><input type="checkbox" id="ob-noshow" checked/> No volver a mostrar esta guía</label>
    `;

    overlay.appendChild(spot);
    document.body.appendChild(overlay);
    document.body.appendChild(tip);

    els = {
      overlay, spot, tip,
      badge: tip.querySelector('#ob-step-count'),
      title: tip.querySelector('#ob-title'),
      text: tip.querySelector('#ob-text'),
      progress: tip.querySelector('#ob-progress'),
      prev: tip.querySelector('#ob-prev'),
      next: tip.querySelector('#ob-next'),
      skip: tip.querySelector('#ob-skip'),
      noshow: tip.querySelector('#ob-noshow'),
    };

    els.prev.addEventListener('click', () => go(idx - 1));
    els.next.addEventListener('click', () => { if (idx >= steps.length - 1) finish(); else go(idx + 1); });
    els.skip.addEventListener('click', () => finish(true));
    window.addEventListener('resize', () => position());
    window.addEventListener('keydown', (e) => {
      if (els.overlay.style.display !== 'block') return;
      if (e.key === 'Escape') finish(true);
      else if (e.key === 'ArrowRight') els.next.click();
      else if (e.key === 'ArrowLeft' && idx > 0) go(idx - 1);
    });
  }

  // ── Posicionamiento ──────────────────────────────────────────────────────
  function position() {
    if (!steps.length) return;
    const step = steps[idx];
    const el = document.querySelector(step.selector);
    if (!el) return;
    const r = el.getBoundingClientRect();
    const pad = 6;
    els.spot.style.top = (r.top - pad) + 'px';
    els.spot.style.left = (r.left - pad) + 'px';
    els.spot.style.width = (r.width + pad * 2) + 'px';
    els.spot.style.height = (r.height + pad * 2) + 'px';

    const tipW = els.tip.offsetWidth || 320;
    const tipH = els.tip.offsetHeight || 200;
    const gap = 14;
    const vw = window.innerWidth, vh = window.innerHeight;
    let top, left;

    if (r.right + gap + tipW < vw) {            // a la derecha
      left = r.right + gap; top = r.top;
    } else if (r.left - gap - tipW > 0) {       // a la izquierda
      left = r.left - gap - tipW; top = r.top;
    } else if (r.bottom + gap + tipH < vh) {    // debajo
      left = r.left; top = r.bottom + gap;
    } else {                                    // encima
      left = r.left; top = r.top - gap - tipH;
    }
    // clamp al viewport
    left = Math.max(12, Math.min(left, vw - tipW - 12));
    top = Math.max(12, Math.min(top, vh - tipH - 12));
    els.tip.style.left = left + 'px';
    els.tip.style.top = top + 'px';
  }

  // ── Render de un paso ──────────────────────────────────────────────────────
  function go(n) {
    idx = Math.max(0, Math.min(n, steps.length - 1));
    const step = steps[idx];
    const el = document.querySelector(step.selector);
    if (!el) { if (idx < steps.length - 1) return go(idx + 1); return finish(); }

    els.badge.textContent = `Paso ${idx + 1} de ${steps.length}`;
    els.title.textContent = step.title;
    els.text.innerHTML = step.text;
    els.prev.style.visibility = idx === 0 ? 'hidden' : 'visible';
    els.next.textContent = idx >= steps.length - 1 ? '¡Entendido!' : 'Siguiente';

    els.progress.innerHTML = steps.map((_, i) =>
      `<span class="ob-dot ${i <= idx ? 'done' : ''}"></span>`).join('');

    els.tip.classList.remove('visible');
    el.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
    setTimeout(() => { position(); els.tip.classList.add('visible'); }, 260);
  }

  function finish(skipped) {
    if (els.overlay.style.display !== 'block') return;
    els.overlay.style.display = 'none';
    els.tip.classList.remove('visible');
    els.tip.style.opacity = '';  // reset
    els.tip.style.display = 'none';
    if (els.noshow.checked && currentTour) markDone(currentTour);
    currentTour = null;
  }

  // ── API pública ──────────────────────────────────────────────────────────
  function start(tourName) {
    const def = TOURS[tourName];
    if (!def || !def.steps) return;
    buildDom();
    currentTour = tourName;
    steps = def.steps.filter(s => document.querySelector(s.selector));
    if (!steps.length) return;
    idx = 0;
    els.noshow.checked = true;
    els.overlay.style.display = 'block';
    els.tip.style.display = 'block';
    go(0);
  }

  window.startOnboarding = function (tourName) {
    start(tourName || CFG.manualTour || 'admin');
  };

  // ── Auto-arranque en el primer login ─────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    if (CFG.auto && TOURS[CFG.auto]) {
      setTimeout(() => start(CFG.auto), 600);
    }
  });
})();
