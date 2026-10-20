/* Guide position only. No network, form submission or financial-state access. */
(() => {
  const rail = document.querySelector('[data-guided-rail]');
  const source = document.getElementById('fluxora-guide-data');
  if (!rail || !source) return;
  let guides;
  try { guides = JSON.parse(source.textContent); } catch (_) { return; }
  const key = 'fluxora.guide.v1';
  function valid(value) { return value && (value.mode === 'judge' || value.mode === 'owner') && Number.isInteger(value.step) && value.step >= 0 && value.step < 5; }
  function fragment() {
    const found = /^#tour-(judge|owner)-([1-5])$/.exec(window.location.hash);
    return found ? { mode: found[1], step: Number(found[2]) - 1 } : null;
  }
  function load() {
    const explicit = fragment();
    if (explicit) return explicit;
    try { const saved = JSON.parse(window.sessionStorage.getItem(key)); return valid(saved) ? saved : null; } catch (_) { return null; }
  }
  function destination(mode,step) { return guides[mode].steps[step].path + '#tour-' + mode + '-' + (step + 1); }
  function render() {
    const state = load();
    if (!valid(state)) { rail.hidden = true; document.body.classList.remove('guide-active'); return; }
    const guide = guides[state.mode];
    if (!guide || !Array.isArray(guide.steps) || guide.steps.length !== 5) return;
    const step = guide.steps[state.step];
    if (!step || typeof step.path !== 'string' || !step.path.startsWith('/') || step.path.startsWith('//')) return;
    try { window.sessionStorage.setItem(key, JSON.stringify(state)); } catch (_) { /* Explicit fragment links still work. */ }
    const fill = (selector,text) => { rail.querySelector(selector).textContent = text; };
    fill('[data-guide-name]',guide.title);
    fill('[data-guide-progress]','Guide step ' + (state.step + 1) + ' of 5 · ' + step.time);
    fill('[data-guide-title]',step.title);
    fill('[data-guide-instruction]',step.text);
    fill('[data-guide-check]','Check your understanding: ' + step.check);
    const current = rail.querySelector('[data-guide-open]');
    current.href = destination(state.mode,state.step);
    current.hidden = window.location.pathname === step.path;
    const prev = rail.querySelector('[data-guide-prev]');
    prev.hidden = state.step === 0;
    if (state.step > 0) prev.href = destination(state.mode,state.step - 1);
    const next = rail.querySelector('[data-guide-next]');
    next.href = state.step === 4 ? '/start' : destination(state.mode,state.step + 1);
    next.textContent = state.step === 4 ? 'Return to guide hub' : 'Next guide step';
    rail.hidden = false;
    document.body.classList.add('guide-active');
  }
  rail.querySelector('[data-guide-exit]').addEventListener('click', () => {
    try { window.sessionStorage.removeItem(key); } catch (_) { /* No stored preference to remove. */ }
    // Remove only our own fragment; source/evidence anchors are left intact.
    if (fragment()) window.history.replaceState(null,'',window.location.pathname + window.location.search);
    rail.hidden = true;
    document.body.classList.remove('guide-active');
    const main = document.getElementById('main'); if (main) main.focus();
  });
  window.addEventListener('hashchange',render);
  window.addEventListener('pageshow',render);
  render();
})();
