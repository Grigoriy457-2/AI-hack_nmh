/* NEWSFLOW AI — клиентская часть. Без сборки и зависимостей.
   Контракт API: docs/CONTRACTS_v2.md. */
(function () {
  'use strict';

  // ================================================================ утилиты
  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function store(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      if (val === null) localStorage.removeItem(key); else localStorage.setItem(key, val);
    } catch (e) { return null; }
    return null;
  }
  function parseISO(iso) {
    if (!iso) return null;
    var s = String(iso);
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';   // сервер отдаёт naive UTC
    var d = new Date(s);
    return isNaN(d) ? null : d;
  }
  var timeFmt;
  try { timeFmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' }); }
  catch (e) { timeFmt = new Intl.DateTimeFormat('ru-RU', { hour: '2-digit', minute: '2-digit' }); }
  function hhmm(iso) { var d = parseISO(iso); return d ? timeFmt.format(d) : '—'; }
  function plural(n, one, few, many) {
    n = Math.abs(n) % 100; var n1 = n % 10;
    if (n > 10 && n < 20) return many;
    if (n1 > 1 && n1 < 5) return few;
    if (n1 === 1) return one;
    return many;
  }

  function api(method, url, body) {
    var opt = { method: method, headers: { 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(url, opt).then(function (r) {
      if (r.status === 401) { location.href = '/login'; throw new Error('Нужно войти заново'); }
      if (r.status === 204) return null;
      return r.text().then(function (t) {
        var data = null;
        try { data = t ? JSON.parse(t) : null; } catch (e) { data = null; }
        if (!r.ok) {
          var d = data && data.detail;
          if (Array.isArray(d)) d = d.map(function (x) { return x.msg; }).join('; ');
          throw new Error(d || ({ 404: 'Не найдено (404)', 409: 'Сейчас это действие недоступно (409)', 422: 'Сервер не принял данные (422) — проверьте поля' }[r.status] || 'Ошибка сервера: ' + r.status));
        }
        return data;
      });
    });
  }

  var toastTimer;
  function toast(msg, isErr) {
    var t = $('#toast');
    if (!t) return;
    t.textContent = msg;
    t.classList.toggle('is-err', !!isErr);
    t.classList.add('is-on');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove('is-on'); }, isErr ? 6000 : 3200);
  }
  function reloadWith(msg) {
    try { sessionStorage.setItem('nf-toast', msg); } catch (e) { /* без сообщения */ }
    location.reload();
  }
  function goWith(url, msg) {
    try { if (msg) sessionStorage.setItem('nf-toast', msg); } catch (e) { /* ок */ }
    location.href = url;
  }
  function busy(btn, on) {
    if (!btn) return;
    btn.disabled = !!on;
    btn.classList.toggle('is-busy', !!on);
  }
  function readJSON(id) {
    var el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  var ICON = {
    phone: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2"/></svg>',
    image: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 16l-5-5-8 8"/></svg>',
    search: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>',
    grow: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 17l6-6 4 4 6-7M15 8h5v5"/></svg>',
    fwd: '<svg class="i" viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5l6 6-6 6M20 11H9a5 5 0 0 0-5 5v3"/></svg>'
  };
  var PF = {
    max: { label: 'MAX', short: 'MAX', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 6.5L8 2.5l5 4V12H7l-3 2v-2H3z"/></svg>' },
    telegram: { label: 'Telegram', short: 'TG', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="7.5" r="5.5"/><path d="M4.2 11.6L3 14l3-1.2"/></svg>' },
    rss: { label: 'СМИ (RSS)', short: 'СМИ', svg: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="3" width="11" height="10" rx="1"/><path d="M5 6h6M5 8.5h6M5 11h3.5"/></svg>' }
  };
  var KIND_LABELS = { channel: 'канал', group: 'группа', chat: 'чат', user: 'пользователь', feed: 'лента новостей' };
  function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } }
  function httpUrl(u) { return u && /^https?:\/\//i.test(u) ? u : null; }
  function extLink(u, text, cls) {
    var h = httpUrl(u);
    if (!h) return '';
    return '<a class="' + (cls || 'ext') + '" href="' + esc(h) + '" target="_blank" rel="noopener noreferrer">' + esc(text || hostOf(h)) + '</a>';
  }
  function pfHTML(p, short) {
    var x = PF[p] || PF.max;
    return '<span class="pf pf--' + esc(p || 'max') + '" title="' + x.label + '">' + x.svg + (short ? x.short : x.label) + '</span>';
  }
  var STATUS_LABELS = { 'new': 'Новый', researching: 'AI ищет', review: 'На проверке', handed_off: 'Передано дальше', rejected: 'Не подтвердилось' };
  function statusHTML(s, label) {
    return '<span class="status status--' + esc(s) + '">' + esc(label || STATUS_LABELS[s] || s) + '</span>';
  }

  // ================================================================ общее: тема, подсказки, колонки
  function initTheme() {
    var btn = $('#theme-toggle');
    function current() { return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }
    function paint() {
      var t = current();
      $$('[data-when]', btn).forEach(function (el) { el.hidden = el.getAttribute('data-when') !== t; });
    }
    if (!btn) return;
    paint();
    btn.addEventListener('click', function () {
      var next = current() === 'light' ? 'dark' : 'light';
      document.documentElement.setAttribute('data-theme', next);
      store('nf-theme', next);
      paint();
    });
  }

  function initModePop() {
    var box = $('[data-mode-pop]');
    if (!box) return;
    var btn = $('.mode__btn', box);
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !box.classList.contains('is-open');
      box.classList.toggle('is-open', open);
      btn.setAttribute('aria-expanded', String(open));
    });
    document.addEventListener('click', function (e) {
      if (!box.contains(e.target)) { box.classList.remove('is-open'); btn.setAttribute('aria-expanded', 'false'); }
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { box.classList.remove('is-open'); btn.blur(); } });
  }

  function initHints() {
    $$('details[data-hint]').forEach(function (d) {
      var key = 'nf-hint-' + d.getAttribute('data-hint');
      var saved = store(key);
      // по умолчанию подсказка открыта на десктопе и свёрнута на телефоне
      d.open = saved ? saved === 'open' : !window.matchMedia('(max-width: 860px)').matches;
      d.addEventListener('toggle', function () { store(key, d.open ? 'open' : 'closed'); });
    });
  }

  function showColumn(id) {
    var desk = $('[data-desk]');
    if (!desk) return;
    $$(':scope > *', desk).forEach(function (c) { c.classList.toggle('is-shown', c.id === id); });
    $$('.switcher [data-show]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-show') === id)); });
  }
  function initSwitcher() {
    $$('.switcher [data-show]').forEach(function (b) {
      b.addEventListener('click', function () { showColumn(b.getAttribute('data-show')); window.scrollTo(0, 0); });
    });
  }

  function initDialogs() {
    $$('[data-open]').forEach(function (b) {
      b.addEventListener('click', function () {
        var d = document.getElementById(b.getAttribute('data-open'));
        if (!d) return;
        if (typeof d.showModal === 'function') d.showModal(); else d.setAttribute('open', '');
        d.dispatchEvent(new CustomEvent('nf-open'));
        var f = $('textarea, input:not([type=radio])', d);
        if (f) f.focus();
      });
    });
    $$('dialog [data-close]').forEach(function (b) {
      b.addEventListener('click', function () { var d = b.closest('dialog'); if (d.close) d.close(); else d.removeAttribute('open'); });
    });
  }

  // кнопки вида data-api="POST /api/..." (Источники)
  function initApiButtons() {
    $$('[data-api]').forEach(function (b) {
      b.addEventListener('click', function () {
        var conf = b.getAttribute('data-confirm');
        if (conf && !window.confirm(conf)) return;
        var parts = b.getAttribute('data-api').split(' ');
        busy(b, true);
        api(parts[0], parts[1], parts[0] === 'POST' ? {} : undefined)
          .then(function (res) {
            var msg = b.getAttribute('data-done') || 'Готово';
            if (res && typeof res.new_messages === 'number') msg += ': новых сообщений ' + res.new_messages;
            reloadWith(msg);
          })
          .catch(function (e) { busy(b, false); toast(e.message, true); });
      });
    });
  }

  // ================================================================ РАДАР
  function initRadar() {
    var data = readJSON('radar-data');
    var list = $('[data-feed]');
    if (!data || !list) return;

    var scroller = $('[data-feed-scroll]');
    var newBtn = $('[data-feed-new]');
    var onair = $('#onair');
    var seen = {};
    var byKeyTimes = {};       // emerging_key -> [Date] для «пульса» сюжета
    var cursor = (data.feed && data.feed.cursor) || 0;
    var filter = 'all';
    var unseenAbove = 0;
    var MAX_ITEMS = 250;
    var NATIVE_ANCHOR = !!(window.CSS && CSS.supports && CSS.supports('overflow-anchor', 'auto'));
    var emerging = data.emerging || [];

    function msgMatches(li) {
      switch (filter) {
        case 'unique': return li.dataset.unique === '1';
        case 'important': return li.dataset.important === '1';
        case 'phone': return li.dataset.phone === '1';
        case 'max': case 'telegram': case 'rss': return li.dataset.platform === filter;
        default: return true;
      }
    }
    function updateCount() {
      var all = $$('.msg', list);
      var shown = all.filter(function (li) { return !li.hidden; }).length;
      var c = $('[data-feed-count]');
      if (c) c.textContent = (filter === 'all' ? all.length : shown + ' из ' + all.length);
      $('[data-feed-empty]').hidden = all.length > 0;
    }

    function renderMsg(m, arriving) {
      var b = m.badges || {};
      var li = document.createElement('li');
      // маркер (жёлтая полоса и заливка) — только для уникального И важного; просто уникальное — тихий бейдж
      li.className = 'msg' + (b.unique && b.important ? ' is-unique' : '') + (b.is_duplicate ? ' is-dup' : '') + (arriving ? ' is-arriving' : '');
      li.dataset.id = m.id;
      li.dataset.platform = m.platform || 'max';
      li.dataset.unique = b.unique ? '1' : '0';
      li.dataset.important = b.important ? '1' : '0';
      li.dataset.phone = b.has_phone ? '1' : '0';
      var badges = '';
      if (b.unique) badges += '<span class="badge ' + (b.important ? 'badge--unique' : 'badge--soft') + '" title="Первое сообщение, не перепечатка">уникальное</span>';
      if (b.important) badges += '<span class="badge badge--important">важное</span>';
      if (b.duplicates) badges += '<span class="badge badge--dup" title="Столько раз сообщение перепечатали в других чатах">дубль ×' + b.duplicates + '</span>';
      if (b.is_duplicate) badges += '<span class="badge badge--dup" title="Перепечатка более раннего сообщения">копия</span>';
      if (b.has_phone) badges += '<span class="badge badge--phone">' + ICON.phone + 'есть телефон</span>';
      if (m.has_media) badges += '<span class="msg__media">' + ICON.image + 'фото</span>';
      if (m.is_forwarded) badges += '<span class="msg__media">' + ICON.fwd + 'переслано</span>';
      var link = extLink(m.message_url, hostOf(m.message_url) === 't.me' ? 'открыть в Telegram' : hostOf(m.message_url));
      if (link) badges += link;
      var showAuthor = m.author_name && m.author_name !== m.chat_title;
      li.innerHTML =
        '<time class="msg__time" datetime="' + esc(m.sent_at) + '">' + hhmm(m.sent_at) + '</time>' +
        '<div>' +
          '<div class="msg__head">' + pfHTML(m.platform, true) +
            '<span class="plate" title="' + esc(m.chat_title) + (m.district ? ', ' + esc(m.district) : '') + '">' + esc(m.chat_title || 'Чат') + '</span>' +
            (showAuthor ? '<span class="msg__author">' + esc(m.author_name) + '</span>' : '') +
          '</div>' +
          '<p class="msg__text">' + esc(m.text) + '</p>' +
          (badges ? '<div class="msg__badges">' + badges + '</div>' : '') +
        '</div>';
      li.hidden = !msgMatches(li);
      if (arriving) setTimeout(function () { li.classList.remove('is-arriving'); }, 3000);
      return li;
    }

    function remember(m) {
      if (m.emerging_key) {
        var d = parseISO(m.sent_at);
        if (d) (byKeyTimes[m.emerging_key] = byKeyTimes[m.emerging_key] || []).push(d);
      }
    }

    function addMessages(items, live) {
      if (!items || !items.length) return;
      var fresh = items.filter(function (m) { return m && !seen[m.id]; });
      if (!fresh.length) return;
      var ts = function (m) { var d = parseISO(m.sent_at); return d ? d.getTime() : 0; };
      fresh.sort(function (a, b) { return ts(a) - ts(b) || a.id - b.id; });
      var atTop = !scroller || scroller.scrollTop < 40;
      // Удерживаем то, что редактор читает. В Chrome/Edge/Firefox это делает сам браузер
      // (overflow-anchor), не трогая scrollTop — иначе обрывается инерция тачпада.
      // Ручная подстройка — только там, где scroll anchoring не поддерживается (Safari).
      var anchor = null, anchorTop = 0;
      if (live && scroller && !atTop && !NATIVE_ANCHOR) {
        var top = scroller.getBoundingClientRect().top;
        for (var a = list.firstChild; a; a = a.nextSibling) {
          if (a.nodeType === 1 && !a.hidden && a.getBoundingClientRect().bottom > top) { anchor = a; break; }
        }
        if (anchor) anchorTop = anchor.getBoundingClientRect().top;
      }
      fresh.forEach(function (m) {
        seen[m.id] = true;
        if (m.id > cursor) cursor = m.id;
        remember(m);
        var li = renderMsg(m, live);
        li.dataset.ts = ts(m);
        // лента по времени отправки: новое — сверху
        var after = null;
        for (var k = list.firstChild; k; k = k.nextSibling) {
          if (+k.dataset.ts <= +li.dataset.ts) { after = k; break; }
        }
        list.insertBefore(li, after);
        if (live && !li.hidden && !atTop) unseenAbove++;
      });
      var all = $$('.msg', list);
      for (var i = MAX_ITEMS; i < all.length; i++) { all[i].remove(); }
      if (anchor && anchor.isConnected) {
        var shift = anchor.getBoundingClientRect().top - anchorTop;
        if (Math.abs(shift) >= 1) scroller.scrollTop += shift;
      }
      if (newBtn) {
        newBtn.textContent = unseenAbove + ' ' + plural(unseenAbove, 'новое сообщение', 'новых сообщения', 'новых сообщений') + ', показать';
        newBtn.classList.toggle('is-on', unseenAbove > 0);
      }
      updateCount();
    }
    if (newBtn) newBtn.addEventListener('click', function () {
      unseenAbove = 0; newBtn.classList.remove('is-on');
      scroller.scrollTo({ top: 0, behavior: 'smooth' });
    });
    if (scroller) scroller.addEventListener('scroll', function () {
      if (scroller.scrollTop < 40 && unseenAbove) { unseenAbove = 0; newBtn.classList.remove('is-on'); }
    }, { passive: true });

    list.addEventListener('click', function (e) {
      var t = e.target.closest('.msg__text');
      if (t && !window.getSelection().toString()) t.classList.toggle('is-open');
    });
    $$('[data-filter]').forEach(function (b) {
      b.addEventListener('click', function () {
        filter = b.getAttribute('data-filter');
        $$('[data-filter]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
        $$('.msg', list).forEach(function (li) { li.hidden = !msgMatches(li); });
        updateCount();
      });
    });

    // ---------------------------------------------------- возникающие сюжеты
    var sigList = $('[data-signals]');
    function pulseHTML(e) {
      var times = (byKeyTimes[e.key] || []).slice();
      if (times.length < 2 && e.sample) e.sample.forEach(function (m) { var d = parseISO(m.sent_at); if (d) times.push(d); });
      var first = parseISO(e.first_at), last = parseISO(e.last_at);
      if (!first || !last) return '';
      var bins = 16, span = Math.max(last - first, 60000), counts = [];
      for (var i = 0; i < bins; i++) counts.push(0);
      times.forEach(function (t) {
        var k = Math.min(bins - 1, Math.max(0, Math.floor((t - first) / span * bins)));
        counts[k]++;
      });
      var max = Math.max.apply(null, counts.concat([1]));
      var recentFrom = Math.floor(bins * 0.75);
      var bars = counts.map(function (c, i) {
        var h = c ? Math.max(18, Math.round(c / max * 100)) : 6;
        return '<i style="height:' + h + '%"' + (c && i >= recentFrom && e.trend === 'rising' ? ' class="is-recent"' : '') + '></i>';
      }).join('');
      return '<div class="pulse" aria-hidden="true">' + bars + '</div>' +
        '<div class="pulse__axis"><span>' + hhmm(e.first_at) + '</span><span>' + hhmm(e.last_at) + '</span></div>';
    }
    var lastSig = null;
    function renderSignals(items) {
      var sig = JSON.stringify(items || []);
      if (sig === lastSig) return;          // не перерисовываем то же самое: кнопки не «уплывают» из-под курсора
      lastSig = sig;
      emerging = items || [];
      $$('[data-signal-count]').forEach(function (c) { c.textContent = emerging.length; });
      $('[data-signals-empty]').hidden = emerging.length > 0;
      sigList.innerHTML = emerging.map(function (e) {
        var chats = (e.chats || []).slice(0, 4).map(function (c) { return '<span class="plate">' + esc(c) + '</span>'; }).join('');
        if ((e.chats || []).length > 4) chats += '<span class="muted" style="font-size:13px">и ещё ' + (e.chats.length - 4) + '</span>';
        var pfs = (e.platforms || []).map(function (p) { return pfHTML(p, false); }).join('');
        var grow = e.trend === 'rising'
          ? '<span class="signal__grow">' + ICON.grow + ' ' + esc(e.trend_label || 'растёт') + (e.recent_count ? ', +' + e.recent_count + ' за последние минуты' : '') + '</span>'
          : '<span class="muted">' + esc(e.trend_label || '') + '</span>';
        var actions = e.investigation_id
          ? '<a class="btn btn--sm" href="/stories/' + e.investigation_id + '">Открыть сюжет</a> ' + (e.investigation_status ? statusHTML(e.investigation_status) : '')
          : '<button class="btn btn--primary btn--sm" type="button" data-investigate="' + esc(e.key) + '">' + ICON.search + 'Исследовать</button>' +
            '<button class="btn btn--quiet btn--sm" type="button" data-dismiss="' + esc(e.key) + '">Скрыть</button>';
        return '<li class="signal' + (e.trend === 'rising' ? ' is-hot' : '') + '" data-key="' + esc(e.key) + '">' +
          '<div class="signal__title">' + esc(e.title) + '</div>' +
          '<div class="signal__facts">' +
            '<span><b>' + e.messages_count + '</b> ' + plural(e.messages_count, 'сообщение', 'сообщения', 'сообщений') + '</span>' +
            '<span>из <b>' + e.chats_count + '</b> ' + plural(e.chats_count, 'чата', 'чатов', 'чатов') + '</span>' +
            '<span>первое в <b>' + hhmm(e.first_at) + '</b></span>' + grow +
          '</div>' +
          pulseHTML(e) +
          '<div class="signal__facts" style="margin-top:6px">' +
            (e.unique_count ? '<span><b>' + e.unique_count + '</b> уникальных</span>' : '') +
            (e.important_count ? '<span><b>' + e.important_count + '</b> важных</span>' : '') +
            (e.has_phone_count ? '<span><b>' + e.has_phone_count + '</b> с телефоном</span>' : '') +
          '</div>' +
          '<div class="signal__plates">' + pfs + chats + '</div>' +
          (e.summary ? '<p class="signal__quote">' + esc(e.summary) + '</p>' : '') +
          '<div class="btn-row">' + actions + '</div>' +
        '</li>';
      }).join('');
    }
    sigList.addEventListener('click', function (ev) {
      var inv = ev.target.closest('[data-investigate]');
      var dis = ev.target.closest('[data-dismiss]');
      if (inv) {
        busy(inv, true);
        inv.lastChild.textContent = 'Запускаем поиск…';
        api('POST', '/api/emerging/' + encodeURIComponent(inv.getAttribute('data-investigate')) + '/investigate', {})
          .then(function (s) { goWith('/stories/' + s.id, 'AI начал поиск по сюжету'); })
          .catch(function (e) { busy(inv, false); inv.lastChild.textContent = 'Исследовать'; toast(e.message, true); });
      } else if (dis) {
        var key = dis.getAttribute('data-dismiss');
        api('POST', '/api/emerging/' + encodeURIComponent(key) + '/dismiss', {})
          .then(function () { renderSignals(emerging.filter(function (e) { return e.key !== key; })); toast('Карточка скрыта'); })
          .catch(function (e) { toast(e.message, true); });
      }
    });

    // ---------------------------------------------------- сюжеты в работе
    function updateStory(s) {
      var li = $('[data-inwork] [data-story="' + s.id + '"]');
      if (!li) return;
      var st = $('.status', li);
      if (st) st.outerHTML = statusHTML(s.status, s.status_label);
      var bar = $('.bar i', li);
      var pct = s.progress && s.progress.percent;
      if (s.status === 'researching') {
        if (!bar) { $('a', li).insertAdjacentHTML('beforeend', '<div class="bar"><i style="width:3%"></i></div>'); bar = $('.bar i', li); }
        if (pct != null) bar.style.width = Math.max(3, pct) + '%';
      } else if (bar) { bar.parentNode.remove(); }
    }

    // ---------------------------------------------------- живой поток: SSE, запасной вариант — опрос
    function setLive(on) { if (onair) onair.classList.toggle('is-off', !on); }
    var pollTimer = null;
    function poll() {
      Promise.all([
        api('GET', '/api/feed?since=' + cursor + '&limit=100'),
        api('GET', '/api/emerging')
      ]).then(function (r) {
        setLive(true);
        addMessages(r[0] && r[0].items, true);
        if (r[0] && r[0].cursor > cursor) cursor = r[0].cursor;
        renderSignals(r[1]);
      }).catch(function () { setLive(false); });
    }
    function startPolling() {
      if (pollTimer) return;
      poll();
      pollTimer = setInterval(poll, 4000);
    }
    function startStream() {
      if (!window.EventSource) { startPolling(); return; }
      var errors = 0;
      var es = new EventSource('/api/stream?since=' + cursor);
      es.addEventListener('hello', function () { errors = 0; setLive(true); });
      es.addEventListener('new_message', function (e) {
        errors = 0;
        try { addMessages([JSON.parse(e.data)], true); } catch (x) { /* битое событие пропускаем */ }
      });
      es.addEventListener('emerging_update', function (e) {
        try { renderSignals(JSON.parse(e.data).items); } catch (x) { /* пропуск */ }
      });
      es.addEventListener('story_update', function (e) {
        try { updateStory(JSON.parse(e.data)); } catch (x) { /* пропуск */ }
      });
      es.onerror = function () {
        errors++;
        setLive(false);
        if (es.readyState === 2 || errors >= 3) { es.close(); startPolling(); }
      };
    }

    // начальная отрисовка: старые сообщения без анимации
    addMessages((data.feed && data.feed.items) || [], false);
    renderSignals(emerging);
    startStream();

    // «Своя зацепка»
    var form = $('[data-lead-form]');
    var ex = $('[data-fill-example]');
    if (ex) ex.addEventListener('click', function () { var t = $('#lead'); t.value = data.demo_lead || ''; t.focus(); });
    if (form) form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var lead = form.lead_text.value.trim();
      if (lead.length < 3) { toast('Опишите зацепку хотя бы парой слов', true); return; }
      var btn = $('[type=submit]', form);
      busy(btn, true);
      var body = { lead_text: lead, autorun: true };
      if (form.channel_id && form.channel_id.value) body.channel_id = parseInt(form.channel_id.value, 10);
      api('POST', '/api/stories', body)
        .then(function (s) { goWith('/stories/' + s.id, 'AI начал поиск по зацепке'); })
        .catch(function (e) { busy(btn, false); toast(e.message, true); });
    });
  }

  // ================================================================ СЮЖЕТ
  var TOKEN_RE = /\s+|[^\s]+/g;
  function wordDiff(a, b) {
    var ta = (a || '').match(TOKEN_RE) || [], tb = (b || '').match(TOKEN_RE) || [];
    var n = ta.length, m = tb.length;
    if (n * m > 3e6) return { ops: [['del', a], ['ins', b]], ratio: 0, changed: 1 };
    var dp = new Array(n + 1);
    for (var i = 0; i <= n; i++) { dp[i] = new Uint16Array(m + 1); }
    for (i = n - 1; i >= 0; i--) {
      for (var j = m - 1; j >= 0; j--) {
        dp[i][j] = ta[i] === tb[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    var ops = [], ii = 0, jj = 0, same = 0, changedWords = 0;
    function push(op, s) {
      if (ops.length && ops[ops.length - 1][0] === op) ops[ops.length - 1][1] += s; else ops.push([op, s]);
      if (op !== 'eq' && /\S/.test(s)) changedWords++;
    }
    while (ii < n && jj < m) {
      if (ta[ii] === tb[jj]) { push('eq', ta[ii]); same++; ii++; jj++; }
      else if (dp[ii + 1][jj] >= dp[ii][jj + 1]) { push('del', ta[ii++]); }
      else { push('ins', tb[jj++]); }
    }
    while (ii < n) push('del', ta[ii++]);
    while (jj < m) push('ins', tb[jj++]);
    return { ops: ops, ratio: (n + m) ? (2 * same) / (n + m) : 1, changed: changedWords };
  }

  function initStory() {
    var data = readJSON('story-data');
    if (!data) return;
    var base = '/api/stories/' + data.id;

    // ---------------------------------------------------- вкладки
    function openTab(name) {
      $$('[data-tab]').forEach(function (t) {
        var on = t.getAttribute('data-tab') === name;
        t.setAttribute('aria-selected', String(on));
        var p = document.getElementById('panel-' + t.getAttribute('data-tab'));
        if (p) p.hidden = !on;
      });
    }
    $$('[data-tab]').forEach(function (t) {
      t.addEventListener('click', function () {
        var name = t.getAttribute('data-tab');
        openTab(name);
        try { history.replaceState(null, '', name === 'report' ? location.pathname : '#' + name); } catch (e) { /* ок */ }
      });
    });
    var h = location.hash.replace('#', '');
    if (h === 'draft' || h === 'history') openTab(h);

    // ---------------------------------------------------- действия по статусу
    $$('[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-act');
        busy(b, true);
        api('POST', base + '/' + act, {})
          .then(function () { reloadWith({ run: 'AI начал поиск', stop: 'Поиск остановлен', reopen: 'Сюжет снова в работе' }[act] || 'Готово'); })
          .catch(function (e) { busy(b, false); toast(e.message, true); });
      });
    });
    var deeper = $('[data-deeper-form]');
    if (deeper) deeper.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var btn = $('[type=submit]', deeper); busy(btn, true);
      api('POST', base + '/deeper', { question: deeper.question.value.trim() })
        .then(function () { reloadWith('AI копает глубже: ещё один круг поиска'); })
        .catch(function (e) { busy(btn, false); toast(e.message, true); });
    });
    var reject = $('[data-reject-form]');
    if (reject) reject.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var btn = $('[type=submit]', reject); busy(btn, true);
      api('POST', base + '/reject', { comment: reject.comment.value.trim() || null })
        .then(function () { reloadWith('Сюжет отмечен: не подтвердилось'); })
        .catch(function (e) { busy(btn, false); toast(e.message, true); });
    });

    // ---------------------------------------------------- передать дальше
    var hdlg = document.getElementById('dlg-handoff');
    var hform = $('[data-handoff-form]');
    if (hdlg && hform) {
      hdlg.addEventListener('nf-open', function () {
        var box = $('[data-handoff-check]', hdlg);
        box.className = 'warnbox'; box.textContent = 'Проверяем факты…';
        api('GET', base + '/handoff/check').then(function (c) {
          var un = (c && c.unconfirmed) || [];
          if (!un.length) {
            box.className = 'warnbox is-clean';
            box.textContent = 'Все утверждения справки и черновика подтверждены.';
            return;
          }
          var lbl = { unconfirmed: 'не подтверждено', 'false': 'ложь' };
          box.innerHTML = '<b>' + un.length + ' ' + plural(un.length, 'утверждение не подтверждено', 'утверждения не подтверждены', 'утверждений не подтверждены') +
            '.</b> Передать можно, но получатель должен об этом знать — напишите в комментарии.' +
            '<ul>' + un.slice(0, 6).map(function (u) { return '<li>' + esc(u.text) + ' <span class="muted">(' + (lbl[u.state] || 'не проверено') + ')</span></li>'; }).join('') +
            (un.length > 6 ? '<li class="muted">и ещё ' + (un.length - 6) + '</li>' : '') + '</ul>' +
            ((c.warnings || []).filter(function (w) { return !/не подтвержд/.test(w); }).map(function (w) { return '<div>' + esc(w) + '</div>'; }).join(''));
        }).catch(function (e) { box.textContent = 'Не удалось проверить факты: ' + e.message; });
      });
      hform.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var to = hform.to.value.trim();
        if (!to) { toast('Укажите, кому передаёте', true); hform.to.focus(); return; }
        var chEl = $('input[name=channel]:checked', hform);
        var btn = $('[data-handoff-submit]', hform); busy(btn, true);
        api('POST', base + '/handoff', { to: to, channel: chEl ? chEl.value : 'other', comment: hform.comment.value.trim() || null })
          .then(function () { reloadWith('Передано дальше: ' + to); })
          .catch(function (e) { busy(btn, false); toast(e.message, true); });
      });
    }

    // ---------------------------------------------------- переход к источнику
    function revealSource(id) {
      var el = document.getElementById('src-' + id);
      if (!el) { toast('Источник не найден на странице', true); return; }
      if (window.matchMedia('(max-width: 860px)').matches) showColumn('col-side');
      var det = el.closest('details'); if (det) det.open = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.remove('is-flash'); void el.offsetWidth; el.classList.add('is-flash');
    }
    document.addEventListener('click', function (ev) {
      var r = ev.target.closest('[data-src]');
      if (r) { ev.preventDefault(); revealSource(r.getAttribute('data-src')); }
    });

    // ---------------------------------------------------- отметки фактов
    function recount() {
      var t = { confirmed: 0, open: 0, 'false': 0 };
      $$('[data-fact]').forEach(function (li) {
        var s = li.dataset.state;
        t[s === 'confirmed' ? 'confirmed' : s === 'false' ? 'false' : 'open']++;
      });
      Object.keys(t).forEach(function (k) { var el = $('[data-tally="' + k + '"]'); if (el) el.textContent = t[k]; });
    }
    $$('[data-fact]').forEach(function (li) {
      $$('.mark button', li).forEach(function (b) {
        b.addEventListener('click', function () {
          var state = b.dataset.state, prev = li.dataset.state;
          li.dataset.state = state;
          $$('.mark button', li).forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
          recount();
          var text = li.dataset.text || $('.fact__text', li).textContent.replace(/\s+/g, ' ').trim();
          api('POST', base + '/facts', { fact_text: text, state: state, claim_id: li.dataset.fact })
            .then(function () {
              data.claims.forEach(function (c) { if (String(c.id) === li.dataset.fact) c.state = state; });
              renderDraft();
            })
            .catch(function (e) {
              li.dataset.state = prev;
              $$('.mark button', li).forEach(function (x) { x.setAttribute('aria-pressed', String(x.dataset.state === prev)); });
              recount(); toast(e.message, true);
            });
        });
      });
    });

    // ---------------------------------------------------- черновик
    var readEl = $('[data-draft-read]');
    var diffEl = $('[data-diff]');
    var statEl = $('[data-diff-stat]');
    var claimsEl = $('[data-claims]');
    var dform = $('[data-draft-form]');
    var cur = data.current;
    var mode = 'read';

    function srcButtons(ids) {
      return (ids || []).map(function (sid) {
        var s = data.sources[sid] || data.sources[String(sid)];
        return s ? '<button type="button" class="src-ref" data-src="' + sid + '" title="' + esc(s.title) + '">' + s.n + '</button>' : '';
      }).join('');
    }
    function liveBody() { return dform && mode === 'edit' ? dform.body.value : (cur ? cur.body : ''); }
    function liveTitle() { return dform && mode === 'edit' ? dform.title.value : (cur ? cur.title : ''); }

    function highlighted(body) {
      var spans = [];
      (data.claims || []).forEach(function (c, idx) {
        if (!c.text) return;
        var at = body.indexOf(c.text);
        if (at >= 0) spans.push({ s: at, e: at + c.text.length, c: c, idx: idx });
      });
      spans.sort(function (a, b) { return a.s - b.s; });
      var out = '', pos = 0;
      spans.forEach(function (sp) {
        if (sp.s < pos) return;
        var cls = sp.c.state === 'confirmed' ? 'hl hl--confirmed' : sp.c.state === 'false' ? 'hl hl--false' : 'hl';
        var first = (sp.c.source_ids || [])[0];
        out += esc(body.slice(pos, sp.s)) + '<span class="' + cls + '"' + (first != null ? ' data-src="' + first + '"' : '') +
          ' title="' + (sp.c.state === 'confirmed' ? 'подтверждено' : sp.c.state === 'false' ? 'ложь' : 'не подтверждено') + ', открыть источник">' +
          esc(body.slice(sp.s, sp.e)) + '</span>';
        pos = sp.e;
      });
      return out + esc(body.slice(pos));
    }
    function renderClaims() {
      if (!claimsEl) return;
      var body = liveBody();
      var lbl = { confirmed: 'подтверждено', unconfirmed: 'не подтверждено', 'false': 'ложь' };
      claimsEl.innerHTML = (data.claims || []).map(function (c) {
        var missing = c.text && body.indexOf(c.text) < 0;
        return '<li class="claim' + (missing ? ' is-missing' : '') + '" data-state="' + esc(c.state || '') + '">' +
          '<span class="claim__dot"></span>' +
          '<span class="claim__text">' + (missing ? 'изменено вами: ' : '') + esc(c.text) + ' <span class="fact__src">' + srcButtons(c.source_ids) + '</span></span>' +
          '<span class="muted" style="font-size:13px;white-space:nowrap">' + (lbl[c.state] || 'не проверено') + '</span></li>';
      }).join('');
    }
    function renderDiffStat() {
      if (!statEl || !data.ai) return;
      var d = wordDiff(data.ai.body, liveBody());
      statEl.textContent = d.changed
        ? 'изменено ' + d.changed + ' ' + plural(d.changed, 'слово', 'слова', 'слов') + ', совпадение с AI ' + Math.round(d.ratio * 100) + '%'
        : 'совпадает с текстом AI';
      return d;
    }
    function renderDraft() {
      if (!cur) return;
      if (readEl) readEl.innerHTML = '<h4>' + esc(liveTitle()) + '</h4>' + highlighted(liveBody());
      var d = renderDiffStat();
      if (diffEl && d) {
        diffEl.innerHTML = d.ops.map(function (o) {
          return o[0] === 'eq' ? esc(o[1]) : o[0] === 'ins' ? '<ins>' + esc(o[1]) + '</ins>' : '<del>' + esc(o[1]) + '</del>';
        }).join('');
      }
      renderClaims();
    }
    function setMode(m) {
      mode = m;
      $$('[data-mode]').forEach(function (b) { b.setAttribute('aria-pressed', String(b.getAttribute('data-mode') === m)); });
      $$('[data-view]').forEach(function (v) { v.hidden = v.getAttribute('data-view') !== m; });
      renderDraft();
      if (m === 'edit' && dform) dform.body.focus();
    }
    $$('[data-mode]').forEach(function (b) { b.addEventListener('click', function () { setMode(b.getAttribute('data-mode')); }); });
    if (dform) {
      var tmr;
      dform.addEventListener('input', function () { clearTimeout(tmr); tmr = setTimeout(function () { renderDiffStat(); renderClaims(); }, 250); });
      $('[data-draft-reset]', dform).addEventListener('click', function () {
        dform.title.value = cur.title; dform.body.value = cur.body; renderDraft();
      });
      dform.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var body = dform.body.value.replace(/\r\n/g, '\n');
        if (!body.trim()) { toast('Текст не может быть пустым', true); return; }
        if (body === cur.body && dform.title.value === cur.title) { toast('Изменений нет'); return; }
        var btn = $('[type=submit]', dform); busy(btn, true);
        api('POST', base + '/drafts', { title: dform.title.value.trim() || cur.title, body: body })
          .then(function (d) {
            busy(btn, false);
            cur = { version: d.version, title: d.title, body: d.body, author_kind: 'editor' };
            var v = $('[data-version]');
            if (v) v.innerHTML = 'Ваша правка v' + d.version + ', <span class="t">' + hhmm(d.created_at) + '</span>';
            var tabCount = $('#tab-draft .count'); if (tabCount) tabCount.textContent = 'v' + d.version;
            toast('Правка сохранена как версия v' + d.version);
            setMode('diff');
          })
          .catch(function (e) { busy(btn, false); toast(e.message, true); });
      });
    }
    renderDraft();

    // ---------------------------------------------------- живой прогресс поиска
    if (data.status === 'researching') {
      var stagesEl = $('[data-stages]'), pctEl = $('[data-percent]'), barEl = $('[data-bar]');
      var tick = function () {
        api('GET', base + '/progress').then(function (p) {
          var st = p.status && (p.status.value || p.status);
          if (st && st !== 'researching') { reloadWith(st === 'review' ? 'Справка готова — проверьте факты' : 'Статус сюжета изменился'); return; }
          var pr = p.progress || {};
          if (pctEl) pctEl.textContent = (pr.percent || 0) + '%';
          if (barEl) barEl.style.width = Math.max(3, pr.percent || 0) + '%';
          if (stagesEl && pr.stages) {
            stagesEl.innerHTML = pr.stages.map(function (s) {
              return '<li class="stage is-' + esc(s.status) + '"><span class="stage__dot"></span><span>' + esc(s.title) + '</span><span class="stage__note">' + esc(s.note || '') + '</span></li>';
            }).join('');
          }
          var its = p.iterations || [];
          var chain = $('[data-chain]');
          if (its.length && (!chain || chain.children.length !== its.length)) {
            var html = '<ol class="chain" data-chain>' + its.map(function (it) {
              return '<li><div class="chain__why">' + esc(it.rationale || 'Первые запросы по зацепке') + '</div><ul class="chain__q">' +
                (it.queries_json || []).map(function (q) {
                  var AG = { web: 'веб', max_chats: 'чаты', max_chat: 'чаты', official: 'официальные', media: 'медиа', telegram: 'Telegram' };
                  return '<li><span><span class="agent">' + esc(AG[q.agent] || q.agent) + '</span> ' + esc(q.query) + '</span><span class="n">' + (q.results || 0) + (q['new'] != null ? ' / ' + q['new'] : '') + '</span></li>';
                }).join('') + '</ul></li>';
            }).join('') + '</ol>';
            if (chain) chain.outerHTML = html;
            else stagesEl.insertAdjacentHTML('afterend', '<h3 style="font-size:15px;margin:6px 0 10px">Цепочка гипотез</h3>' + html);
          }
          var log = $('[data-log]');
          if (log && pr.log) {
            log.innerHTML = pr.log.slice(-40).map(function (l) {
              return '<li class="is-' + esc(l.level) + '"><span class="t">' + hhmm(l.ts) + '</span><span>' + esc(l.message) + '</span></li>';
            }).join('');
            log.scrollTop = log.scrollHeight;
          }
          if (pr.state === 'error') { toast(pr.error || 'Поиск остановился с ошибкой', true); return; }
          setTimeout(tick, 1500);
        }).catch(function () { setTimeout(tick, 4000); });
      };
      setTimeout(tick, 800);
    }
  }

  // ================================================================ ИСТОЧНИКИ
  function initSources() {
    var form = $('[data-add-source]');
    if (form) {
      var input = form.ref, box = $('[data-preview]', form), addBtn = $('[data-add-btn]', form);
      var preview = null, seq = 0, timer = null, lastRef = '';
      var fmtNum = function (n) {
        if (n >= 1e6) return (n / 1e6).toFixed(1).replace('.', ',').replace(',0', '') + ' млн';
        if (n >= 1e4) return Math.round(n / 1e3) + ' тыс.';
        return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
      };
      var setState = function (cls, html) {
        box.hidden = !cls;
        box.className = 'preview' + (cls && cls !== 'ok' ? ' is-' + cls : '');
        box.innerHTML = html || '';
      };
      var renderPreview = function (p) {
        var pf = PF[p.platform] ? p.platform : 'max';
        var ava = httpUrl(p.avatar_url)
          ? '<img class="ava" src="' + esc(p.avatar_url) + '" alt="" referrerpolicy="no-referrer">'
          : '<span class="ava ava--ph">' + esc((p.title || '?').trim().slice(0, 1).toUpperCase()) + '</span>';
        var link = p.url || p.site_url;
        var meta = pfHTML(pf, false) +
          (p.kind ? '<span>' + esc(KIND_LABELS[p.kind] || p.kind) + '</span>' : '') +
          (p.subscribers ? '<span class="t">' + fmtNum(p.subscribers) + ' ' + (p.subscribers >= 1e4 ? 'подписчиков' : plural(p.subscribers, 'подписчик', 'подписчика', 'подписчиков')) + '</span>' : '') +
          (p.username ? '<span>@' + esc(p.username) + '</span>' : '') +
          extLink(link, hostOf(link));
        setState('ok', ava + '<div><div class="preview__title">' + esc(p.title) + '</div><div class="preview__meta">' + meta + '</div>' +
          (p.description ? '<p class="preview__desc">' + esc(p.description) + '</p>' : '') +
          (p.already_added ? '<div class="preview__note">Этот источник уже подключён — он есть в списке ниже.</div>' : '') + '</div>');
      };
      var resolve = function (force) {
        var ref = input.value.trim();
        if (!ref) { preview = null; addBtn.disabled = true; setState(null); lastRef = ''; return; }
        if (ref === lastRef && !force) return;
        lastRef = ref;
        var my = ++seq;
        preview = null; addBtn.disabled = true;
        setState('loading', 'Ищем источник…');
        api('POST', '/api/sources/resolve', { ref: ref }).then(function (p) {
          if (my !== seq) return;
          if (!p || p.error) {
            var why = String((p && p.error) || 'Источник не найден').replace(/\b[A-Z]\w*(Error|Exception):\s*/g, '');
            setState('error', '<b>Не получилось подключить.</b> ' + esc(why) +
              '<div class="muted" style="margin-top:4px">Проверьте ссылку: нужен публичный канал, группа или сайт с RSS-лентой.</div>');
            return;
          }
          preview = p;
          renderPreview(p);
          addBtn.disabled = !!p.already_added;
          form.platform.value = p.platform || 'auto';
        }).catch(function (e) {
          if (my !== seq) return;
          lastRef = '';
          setState('error', '<b>Не получилось проверить ссылку.</b> ' + esc(e.message));
        });
      };
      input.addEventListener('paste', function () { setTimeout(function () { resolve(true); }, 0); });
      input.addEventListener('input', function () { clearTimeout(timer); addBtn.disabled = true; timer = setTimeout(resolve, 700); });
      input.addEventListener('blur', function () { clearTimeout(timer); resolve(); });
      form.addEventListener('submit', function (ev) {
        ev.preventDefault();
        if (!preview || preview.error || input.value.trim() !== lastRef) { clearTimeout(timer); resolve(true); return; }
        if (preview.already_added) { toast('Этот источник уже подключён'); return; }
        busy(addBtn, true);
        var body = { platform: preview.platform || 'auto', ref: input.value.trim() };
        if (form.district.value.trim()) body.district = form.district.value.trim();
        api('POST', '/api/sources', body)
          .then(function (c) { reloadWith('Источник «' + ((c && c.title) || preview.title) + '» подключён'); })
          .catch(function (e) { busy(addBtn, false); addBtn.disabled = false; toast(e.message, true); });
      });
    }

    // удаление источника
    var ddlg = document.getElementById('dlg-del-src'), dform = $('[data-del-form]'), delId = null;
    $$('[data-del-src]').forEach(function (b) {
      b.addEventListener('click', function () {
        delId = b.getAttribute('data-del-src');
        $('[data-del-title]', ddlg).textContent = b.getAttribute('data-title');
        if (ddlg.showModal) ddlg.showModal(); else ddlg.setAttribute('open', '');
      });
    });
    if (dform) dform.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var btn = $('[type=submit]', dform); busy(btn, true);
      api('DELETE', '/api/sources/chats/' + delId).then(function (r) {
        busy(btn, false); ddlg.close();
        var row = $('[data-src-row="' + delId + '"]');
        if (row) { row.classList.add('is-removing'); setTimeout(function () { row.remove(); var left = $$('[data-src-row]').length; var c = $('[data-src-count]'); if (c) c.textContent = left; if (!left) { var t = $('[data-src-table]'); if (t) t.remove(); $('[data-src-empty]').hidden = false; } }, 300); }
        var n = (r && r.archived_messages) || 0, m = (r && r.story_references) || 0;
        toast('Источник «' + ((r && r.title) || '') + '» удалён: ' + n + ' ' + plural(n, 'сообщение ушло', 'сообщения ушли', 'сообщений ушли') + ' из ленты' +
          (m ? ', ссылок в сюжетах сохранено: ' + m : ''));
      }).catch(function (e) { busy(btn, false); toast(e.message, true); });
    });
    $$('[data-toggle-chat]').forEach(function (cb) {
      cb.addEventListener('change', function () {
        var on = cb.checked;
        api('PATCH', '/api/sources/chats/' + cb.getAttribute('data-toggle-chat'), { enabled: on })
          .then(function (c) { toast((c && c.title ? '«' + c.title + '» ' : '') + (on ? 'слушаем' : 'больше не слушаем')); })
          .catch(function (e) { cb.checked = !on; toast(e.message, true); });
      });
    });
  }

  // ================================================================ старт
  document.addEventListener('DOMContentLoaded', function () {
    initTheme();
    initModePop();
    initHints();
    initSwitcher();
    initDialogs();
    initApiButtons();
    initRadar();
    initStory();
    initSources();
    var pending = null;
    try { pending = sessionStorage.getItem('nf-toast'); sessionStorage.removeItem('nf-toast'); } catch (e) { pending = null; }
    if (pending) toast(pending);
  });
})();
