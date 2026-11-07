(function () {
  // One floating tooltip for every [data-tip] label. It's positioned against the
  // window, so the scrolling table boxes can't cut it off.
  var tipEl = document.getElementById('tip'), current = null;
  function show(el) {
    if (current) current.removeAttribute('aria-describedby');
    current = el;
    el.setAttribute('aria-describedby', 'tip');
    tipEl.textContent = el.getAttribute('data-tip');
    tipEl.style.display = 'block';
    var r = el.getBoundingClientRect(), w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = r.bottom + 8 + h > window.innerHeight - 8 ? r.top - h - 8 : r.bottom + 8;
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  }
  function hide() {
    if (current) current.removeAttribute('aria-describedby');
    current = null;
    tipEl.style.display = 'none';
  }
  function target(e) { return e.target.closest ? e.target.closest('[data-tip]') : null; }
  document.addEventListener('mouseover', function (e) { var el = target(e); if (el) show(el); else if (current) hide(); });
  document.addEventListener('focusin', function (e) { var el = target(e); if (el) show(el); });
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  window.addEventListener('scroll', hide, true);
})();

(function () {
  var data = JSON.parse(document.getElementById('report-data').textContent);
  var uses = data.uses, PAGE = 50;
  var TYPES = { 'public': 'Public', internal: 'Internal', none: 'No comment' };
  function esc(text) {
    return String(text).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function pct(value) {
    if (value === null) return '';
    return value > 0 && value < 1 ? '&lt;1%' : Math.round(value) + '%';
  }
  function result(u) {
    var badge = '<span class="badge b-' + u.status + '" data-tip="' + esc(data.tips[u.status]) + '">' +
                esc(data.labels[u.status]) + '</span>';
    if (u.diff) return '<details><summary>' + badge + '</summary><div class="diff">' + u.diff + '</div></details>';
    return u.added ? badge + '<div class="sub">with text added</div>' : badge;
  }
  function ticket(u) {
    var label = '#' + u.ticket;
    return u.url ? '<a href="' + esc(u.url) + '" target="_blank" rel="noopener">' + label + '</a>' : label;
  }
  function row(u, showMacro) {
    var combined = u.with.length ? '<div class="sub">with ' + esc(u.with.join(', ')) + '</div>' : '';
    return '<tr><td class="nowrap">' + esc(u.when) + '</td><td>' + esc(u.agent) + '</td>' +
      (showMacro ? '<td>' + esc(u.title) + combined + '</td>' : '') +
      '<td class="nowrap">' + TYPES[u.type] + '</td><td>' + result(u) + '</td>' +
      '<td class="r">' + pct(u.pct) + '</td><td class="nowrap">' + ticket(u) + '</td></tr>';
  }
  function mostEdited(a, b) {
    var x = a.pct === null ? -1 : a.pct, y = b.pct === null ? -1 : b.pct;
    return y - x || (a.when < b.when ? 1 : a.when > b.when ? -1 : 0);
  }
  function ordered(list, order) {
    if (order === 'most') return list.slice().sort(mostEdited);
    if (order === 'old') return list.slice().reverse();
    return list;  // the data is newest first
  }
  function usesOf(shown, all) {
    return shown.toLocaleString() + ' of ' + all.toLocaleString() + (all === 1 ? ' use' : ' uses');
  }
  // Draw a list one page at a time into a table body, with a "Show more" button.
  function pager(body, more, showMacro) {
    var items = [], shown = 0;
    function grow() {
      var next = items.slice(shown, shown + PAGE);
      body.insertAdjacentHTML('beforeend', next.map(function (u) { return row(u, showMacro); }).join(''));
      shown += next.length;
      more.hidden = shown >= items.length;
      more.textContent = 'Show more (' + (items.length - shown).toLocaleString() + ' left)';
    }
    more.addEventListener('click', grow);
    return function (list) {
      items = list;
      shown = 0;
      body.innerHTML = '';
      grow();
    };
  }

  // Every macro use
  var q = document.getElementById('q'), st = document.getElementById('status'),
      ty = document.getElementById('type'), order = document.getElementById('order'),
      count = document.getElementById('count');
  var searchable = uses.map(function (u) { return (u.agent + ' ' + u.title + ' ' + u.ticket).toLowerCase(); });
  var showAll = pager(document.querySelector('#uses tbody'), document.getElementById('more'), true);
  function apply() {
    var term = q.value.trim().toLowerCase();
    var list = uses.filter(function (u, i) {
      return (!st.value || u.status === st.value) && (!ty.value || u.type === ty.value) &&
             (!term || searchable[i].indexOf(term) !== -1);
    });
    count.textContent = usesOf(list.length, uses.length);
    showAll(ordered(list, order.value));
  }
  [q, st, ty, order].forEach(function (el) { el.addEventListener('input', apply); });
  apply();

  // A macro's own uses, drawn when its row is first opened
  var macros = document.getElementById('macros'), byMacro = {};
  uses.forEach(function (u) { (byMacro[u.macro] = byMacro[u.macro] || []).push(u); });
  var COLUMNS = macros.querySelectorAll('thead th').length;
  function detailRow(tr) {
    var detail = document.createElement('tr');
    detail.className = 'detail';
    var cell = detail.insertCell();
    cell.colSpan = COLUMNS;
    cell.appendChild(document.getElementById('uses-panel').content.cloneNode(true));
    var own = byMacro[tr.dataset.macro] || [], select = detail.querySelector('select'),
        edited = detail.querySelector('input'), summary = detail.querySelector('.sub');
    var show = pager(detail.querySelector('tbody'), detail.querySelector('.more'), false);
    function refresh() {
      var list = edited.checked ? own.filter(function (u) { return u.status === 'edited'; }) : own;
      summary.textContent = usesOf(list.length, own.length);
      show(ordered(list, select.value));
    }
    select.addEventListener('input', refresh);
    edited.addEventListener('input', refresh);
    refresh();
    return detail;
  }
  function toggle(tr) {
    var button = tr.querySelector('button.open'), open = button.getAttribute('aria-expanded') === 'true';
    button.setAttribute('aria-expanded', String(!open));
    if (!tr.detail) {
      tr.detail = detailRow(tr);
      tr.parentNode.insertBefore(tr.detail, tr.nextSibling);
    }
    tr.detail.hidden = open;
  }
  macros.tBodies[0].addEventListener('click', function (e) {
    var tr = e.target.closest('tr.macro');
    if (tr) toggle(tr);
  });
  // Sorting moves each macro's row and its opened uses together.
  macros.tHead.addEventListener('click', function (e) {
    if (!e.target.closest('button.sort')) return;
    var th = e.target.closest('th'), column = th.dataset.column;
    var descending = th.getAttribute('aria-sort') !== 'descending';
    if (column === 'title' && !th.hasAttribute('aria-sort')) descending = false;
    macros.querySelectorAll('thead th').forEach(function (h) { h.removeAttribute('aria-sort'); });
    th.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
    var rows = Array.prototype.slice.call(macros.tBodies[0].querySelectorAll('tr.macro'));
    rows.sort(function (a, b) {
      var x = a.dataset[column], y = b.dataset[column];
      var diff = column === 'title' ? x.localeCompare(y) : Number(x) - Number(y);
      return descending ? -diff : diff;
    });
    rows.forEach(function (tr) {
      macros.tBodies[0].appendChild(tr);
      if (tr.detail) macros.tBodies[0].appendChild(tr.detail);
    });
  });
})();
