/* Local navigation guidance only; provider calls stay in the reviewed Agent. */
(() => {
  const root = document.querySelector('[data-floating-assistant]');
  if (!root) return;
  const dialog = root.querySelector('dialog');
  const launcher = root.querySelector('[data-assistant-open]');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const config = JSON.parse(document.querySelector('#fluxora-assistant-data').textContent);
  const messages = root.querySelector('[data-assistant-messages]');
  const question = root.querySelector('textarea');
  const close = () => dialog.close();
  launcher.hidden = false;
  launcher.addEventListener('click', () => {
    if (!dialog.open) dialog.showModal();
    launcher.setAttribute('aria-expanded', 'true');
    root.querySelector('[data-assistant-close]').focus();
  });
  root.querySelector('[data-assistant-close]').addEventListener('click', close);
  dialog.addEventListener('close', () => {
    launcher.setAttribute('aria-expanded', 'false');
    launcher.focus();
  });
  dialog.addEventListener('click', event => { if (event.target === dialog) {
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) close();
  }});
  const append = (text, links = [], user = false, title = '') => {
    const item = document.createElement('article');
    item.className = user ? 'assistant-user' : 'assistant-reply';
    if (title) { const heading = document.createElement('b'); heading.textContent = title; item.append(heading); }
    const copy = document.createElement('p'); copy.textContent = text; item.append(copy);
    links.forEach(([path, label]) => {
      const link = document.createElement('a'); link.setAttribute('href', path); link.textContent = label; item.append(link);
    });
    messages.append(item);
    while (messages.children.length > 12) messages.firstElementChild.remove();
    item.scrollIntoView({ block: 'nearest' });
  };
  const reply = key => { const item = config.intents[key]; append(item.text, item.links, false, item.title); };
  root.querySelectorAll('[data-assistant-intent]').forEach(button => button.addEventListener('click', () => reply(button.dataset.assistantIntent)));
  const send = () => {
    const text = question.value.trim();
    if (!text || text.length > 2000) { question.focus(); return; }
    append(text, [], true);
    const lower = text.toLowerCase();
    let intent = 'general';
    if (/approv|execute|\bpay\b|consent|receipt/.test(lower)) intent = 'approval';
    else if (/telegram|\bbot\b/.test(lower)) intent = 'telegram';
    else if (/draft|proposal/.test(lower)) intent = 'draft';
    else if (/forecast|balance|cash/.test(lower)) intent = 'forecast';
    else if (/next|priority|priorities|todo/.test(lower)) intent = 'next';
    else if (/page|explain|help/.test(lower)) intent = 'page';
    reply(intent);
  };
  root.querySelector('[data-assistant-send]').addEventListener('click', send);
  question.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); } });
  root.querySelector('form')?.addEventListener('submit', event => {
    if (!question.value.trim()) { event.preventDefault(); question.focus(); }
  });
})();
