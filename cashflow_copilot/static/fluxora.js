/* Progressive enhancement only. Financial state and approvals stay on server. */
(() => {
  // Keep newly opened settings visible inside the navigation's own scrollport.
  // Native details still works without script; no page/form state is changed.
  document.querySelectorAll('.nav-settings').forEach(settings => {
    settings.addEventListener('toggle', () => {
      if (!settings.open) return;
      window.requestAnimationFrame(() => {
        if (!settings.open) return;
        const scroller = settings.closest('.sidebar > nav, .mobile-menu > nav');
        if (!scroller || !scroller.clientHeight) return;
        const box = settings.getBoundingClientRect();
        const view = scroller.getBoundingClientRect();
        if (box.height > scroller.clientHeight - 16 || box.top < view.top + 8) {
          scroller.scrollTop += box.top - view.top - 8;
        } else if (box.bottom > view.bottom - 8) {
          scroller.scrollTop += box.bottom - view.bottom + 8;
        }
      });
    });
  });
  const telegramDemoLog = document.querySelector('[data-telegram-demo-log]');
  if (telegramDemoLog) telegramDemoLog.scrollTop = telegramDemoLog.scrollHeight;
  const composer = document.querySelector('#task-question');
  document.querySelectorAll('[data-task-example]').forEach(button => {
    button.addEventListener('click', () => {
      if (composer) { composer.value = button.dataset.taskExample; composer.focus(); }
    });
  });
  document.querySelectorAll('main table').forEach(table => {
    if (table.closest('.table-region,.weekly-cash-scroll,.table-scroll')) return;
    const region = document.createElement('div');
    region.className = 'table-region';
    region.tabIndex = 0;
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', table.getAttribute('aria-label') || 'Record table; scroll horizontally if needed');
    table.before(region);
    region.append(table);
  });
  // Optional local filtering. No requests, financial calculations or reordering.
  // Never filter an editable table; all its authority controls remain visible.
  document.querySelectorAll('[data-table-filter]').forEach(input => {
    const host = document.getElementById(input.dataset.tableFilter);
    if (!host) return;
    const table = host.querySelector('table');
    if (!table || table.querySelector('input,select,textarea,button,form') || table.closest('form')) return;
    const rows = Array.from(table.querySelectorAll('tr')).filter(row => row.querySelector('td'));
    const status = input.closest('.data-table').querySelector('[data-table-status]');
    input.closest('label').hidden = false;
    input.addEventListener('input', () => {
      const query = input.value.trim().toLocaleLowerCase();
      let count = 0;
      rows.forEach(row => {
        row.hidden = !!query && !row.textContent.toLocaleLowerCase().includes(query);
        if (!row.hidden) count += 1;
      });
      if (status) {
        status.hidden = !query;
        status.textContent = count+' of '+rows.length+' rows match. Filtering does not change totals or saved records. Clear the search to see every row.';
      }
    });
  });
  const featureFilter = document.querySelector('[data-feature-filter]');
  if (featureFilter) {
    featureFilter.closest('label').hidden = false;
    const groups = Array.from(document.querySelectorAll('.directory-group'));
    const empty = document.querySelector('[data-directory-empty]');
    const status = document.querySelector('[data-directory-status]');
    featureFilter.addEventListener('input', () => {
      const query = featureFilter.value.trim().toLocaleLowerCase();
      let matches = 0;
      groups.forEach(group => {
        let visible = 0;
        group.querySelectorAll('.feature-tile').forEach(tile => {
          tile.hidden = !!query && !tile.textContent.toLocaleLowerCase().includes(query);
          if (!tile.hidden) visible += 1;
        });
        group.hidden = visible === 0;
        matches += visible;
      });
      if (empty) empty.hidden = matches !== 0;
      if (status) {
        status.hidden = !query;
        status.textContent = matches+' workspace pages match. Clear the search to browse all pages.';
      }
    });
  }
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', event => {
      if (event.defaultPrevented || !form.checkValidity()) return;
      if (form.getAttribute('aria-busy') === 'true') { event.preventDefault(); return; }
      form.setAttribute('aria-busy', 'true');
      const button = form.querySelector('button[type=submit],button:not([type])');
      if (button) button.setAttribute('aria-disabled', 'true');
      if (button && !form.querySelector('.loading-note')) {
        const note = document.createElement('p');
        note.className = 'loading-note';
        note.setAttribute('role', 'status');
        note.textContent = 'Request submitted. Waiting for the server response; completion is not confirmed yet. Avoid submitting again. If the response is lost, check saved records and action history before retrying. No background retry is scheduled.';
        form.append(note);
      }
      // Do not disable controls: submitted button values can be meaningful.
      // Server-side exact identities and transactions enforce idempotency.
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('.mobile-menu[open],.site-mobile-nav[open],.page-outline[open],.connection-status[open]').forEach(menu => menu.removeAttribute('open'));
  });
  // Back/forward navigation must not retain a client-only subset of the rows.
  window.addEventListener('pageshow', () => {
    document.querySelectorAll('[data-table-filter],[data-feature-filter]').forEach(input => {
      input.value = '';
      input.dispatchEvent(new Event('input'));
    });
    document.querySelectorAll('form[aria-busy]').forEach(form => {
      form.removeAttribute('aria-busy');
      const button = form.querySelector('button[type=submit],button:not([type])');
      if (button) button.removeAttribute('aria-disabled');
    });
    document.querySelectorAll('.loading-note').forEach(note => note.remove());
  });
})();
