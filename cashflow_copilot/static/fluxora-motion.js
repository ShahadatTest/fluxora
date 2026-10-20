/* Public artwork only. No network, financial data, forms or model access. */
(() => {
  const body = document.body;
  if (!body || !body.classList.contains('site') || typeof window.matchMedia !== 'function') return;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  const buttons = Array.from(document.querySelectorAll('[data-motion-toggle]'));
  const scenes = Array.from(document.querySelectorAll('[data-depth-scene]'));
  let preference = 'on', enabled = false, observer = null;
  try { if (window.localStorage.getItem('fluxora.motion') === 'off') preference = 'off'; } catch (_) { /* Optional preference storage. */ }
  const resets = [];
  const nodes = Array.from(document.querySelectorAll('.hero-copy,.flow-scene,.value-intro,.numbered-features .site-feature,.brand-story,.agent-feature,.home-faq,.site-cta'));
  const seen = new WeakSet();
  function reveal(node) {
    if (!enabled || seen.has(node)) return;
    seen.add(node);
    node.classList.add('motion-enter');
  }
  function update() {
    enabled = !reduced.matches && preference !== 'off';
    body.classList.toggle('motion-enabled', enabled);
    body.classList.toggle('motion-off', !enabled);
    buttons.forEach(button => {
      button.hidden = false;
      button.disabled = reduced.matches;
      button.setAttribute('aria-pressed', String(enabled));
      button.textContent = enabled ? 'Motion on' : 'Motion off';
      button.title = reduced.matches ? 'Your device requests reduced motion.' : 'Turn decorative page motion on or off.';
    });
    resets.forEach(reset => reset());
    if (observer) { observer.disconnect(); observer = null; }
    if (!enabled) nodes.forEach(node => node.classList.remove('motion-enter'));
    else if (typeof window.IntersectionObserver === 'function') {
      observer = new window.IntersectionObserver(entries => entries.forEach(entry => {
        if (entry.isIntersecting) { reveal(entry.target); if (observer) observer.unobserve(entry.target); }
      }), { threshold: 0.08 });
      nodes.forEach(node => { if (!seen.has(node)) observer.observe(node); });
    }
  }
  buttons.forEach(button => button.addEventListener('click', () => {
    if (reduced.matches) return;
    preference = enabled ? 'off' : 'on';
    try { window.localStorage.setItem('fluxora.motion', preference); } catch (_) { /* Motion control still works without storage. */ }
    update();
  }));
  scenes.forEach(scene => {
    const stage = scene.querySelector('[data-depth-stage]');
    if (!stage) return;
    let frame = null, x = 7, y = -9;
    function reset() {
      if (frame !== null) window.cancelAnimationFrame(frame);
      frame = null;
      stage.style.removeProperty('--depth-x'); stage.style.removeProperty('--depth-y');
    }
    resets.push(reset);
    scene.addEventListener('pointermove', event => {
      if (!enabled || !pointer.matches || document.hidden || event.pointerType !== 'mouse' || typeof window.requestAnimationFrame !== 'function') return;
      const box = scene.getBoundingClientRect();
      if (!box.width || !box.height) return;
      x = 7 - Math.max(-0.5, Math.min(0.5, (event.clientY - box.top) / box.height - 0.5)) * 5;
      y = -9 + Math.max(-0.5, Math.min(0.5, (event.clientX - box.left) / box.width - 0.5)) * 6;
      if (frame === null) frame = window.requestAnimationFrame(() => {
        frame = null;
        if (!enabled || document.hidden || !pointer.matches) return;
        stage.style.setProperty('--depth-x', x.toFixed(2) + 'deg');
        stage.style.setProperty('--depth-y', y.toFixed(2) + 'deg');
      });
    }, { passive: true });
    scene.addEventListener('pointerleave', reset);
    scene.addEventListener('pointercancel', reset);
  });
  document.addEventListener('focusin', event => nodes.forEach(node => {
    if (node.contains(event.target)) { seen.add(node); node.classList.remove('motion-enter'); if (observer) observer.unobserve(node); }
  }));
  document.addEventListener('visibilitychange', () => { if (document.hidden) resets.forEach(reset => reset()); });
  window.addEventListener('blur', () => resets.forEach(reset => reset()));
  function listen(media, handler) {
    if (typeof media.addEventListener === 'function') media.addEventListener('change', handler);
    else if (typeof media.addListener === 'function') media.addListener(handler);
  }
  listen(reduced, update); listen(pointer, () => resets.forEach(reset => reset()));
  update();
})();
