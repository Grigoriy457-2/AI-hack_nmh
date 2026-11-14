/* NEWSFLOW AI — клиентская логика (vanilla JS, без сборки). Зона DEV-FRONTEND.
 * Вкладки, живой прогресс пайплайна, панель источников, подсветка утверждений,
 * отметки фактов без перезагрузки, word-level diff в редакторе. */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function readJSON(id, fallback) {
    var el = document.getElementById(id);
    if (!el) return fallback;
    try { return JSON.parse(el.textContent || "null") || fallback; } catch (e) { return fallback; }
  }
  function store(key, val) {
    try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch (e) { return null; }
    return null;
  }

  var MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
  /* naive UTC ISO -> МСК */
  function fmtTime(iso, withSec) {
    if (!iso) return "";
    var s = String(iso);
    if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += "Z";
    var d = new Date(s);
    if (isNaN(d)) return String(iso);
    var m = new Date(d.getTime() + 3 * 3600 * 1000);
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return p(m.getUTCHours()) + ":" + p(m.getUTCMinutes()) + (withSec ? ":" + p(m.getUTCSeconds()) : "");
  }

  /* ------------------------------------------------------------ тост */
  var toastTimer;
  function toast(msg, isErr) {
    var t = $("#toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast is-on" + (isErr ? " toast--err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.className = "toast"; }, 2800);
  }

  /* ------------------------------------------------------------ меню пользователя */
  $$("[data-dropdown]").forEach(function (dd) {
    var btn = $("button", dd);
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = dd.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  });
  document.addEventListener("click", function (e) {
    $$("[data-dropdown].is-open").forEach(function (dd) { if (!dd.contains(e.target)) dd.classList.remove("is-open"); });
  });

  /* ------------------------------------------------------------ подсказки для жюри */
  $$("[data-hint]").forEach(function (h) {
    var key = "nf-hint-" + h.getAttribute("data-hint");
    if (store(key) === "hidden") h.classList.add("hidden");
    var close = $(".hint__close", h);
    if (close) close.addEventListener("click", function () { h.classList.add("hidden"); store(key, "hidden"); });
  });

  /* ------------------------------------------------------------ вкладки */
  var tabsNav = $("[data-tabs]");
  function activateTab(name, push) {
    if (!tabsNav) return false;
    var btn = $('[data-tab="' + name + '"]', tabsNav);
    if (!btn) return false;
    $$("[data-tab]", tabsNav).forEach(function (b) {
      var on = b === btn;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    $$("[data-panel]").forEach(function (p) { p.classList.toggle("is-active", p.getAttribute("data-panel") === name); });
    if (push !== false) {
      try {
        var u = new URL(location.href);
        u.searchParams.set("tab", name);
        u.searchParams.delete("msg"); u.searchParams.delete("level");
        u.hash = "";
        history.replaceState(null, "", u.toString());
      } catch (e) { /* ignore */ }
    }
    return true;
  }
  if (tabsNav) {
    tabsNav.addEventListener("click", function (e) {
      var b = e.target.closest("[data-tab]");
      if (b) activateTab(b.getAttribute("data-tab"));
    });
  }
  document.addEventListener("click", function (e) {
    var g = e.target.closest("[data-goto-tab]");
    if (g) { activateTab(g.getAttribute("data-goto-tab")); window.scrollTo({ top: 0, behavior: "smooth" }); }
  });

  function flash(el) {
    if (!el) return;
    el.classList.add("is-flash");
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(function () { el.classList.remove("is-flash"); }, 2200);
  }
  // якорь на пункт справки после отметки факта / перехода
  if (location.hash && location.hash.length > 1) {
    var target = document.getElementById(location.hash.slice(1));
    if (target) {
      var panel = target.closest("[data-panel]");
      if (panel) activateTab(panel.getAttribute("data-panel"), false);
      setTimeout(function () { flash(target); }, 150);
    }
  }

  /* ------------------------------------------------------------ панель источника */
  var SOURCES = readJSON("nf-sources", {});
  var CLAIMS = readJSON("nf-claims", []);
  var STATES = readJSON("nf-states", {});
  var STATE_LABEL = { confirmed: "подтверждён", unconfirmed: "не подтверждён", "false": "ложь" };

  function claimState(c) {
    if (c.state) return c.state;
    var s = STATES[String(c.id)];
    if (s && s.state) return s.state;
    return c.status || null;
  }
  function uniqHTML(u) {
    if (u == null) return "";
    var p = Math.round(u * 100);
    return '<span class="uniq"><span class="uniq__bar"><i class="' + (u < 0.5 ? "low" : "") + '" style="width:' + p + '%"></i></span>уникальность <b>' + p + "%</b></span>";
  }
  function sourceHTML(s) {
    if (!s) return '<div class="src-detail muted">Источник не найден</div>';
    var b = '<span class="badge badge--kind-' + esc(s.kind) + '">' + esc(s.kind_label) + "</span>";
    if (s.is_primary) b += ' <span class="badge badge--primary-src">первоисточник</span>';
    if (s.duplicates > 0) b += ' <span class="badge badge--dup">дубль ×' + s.duplicates + "</span>";
    if (s.is_important) b += ' <span class="badge badge--important">важное</span>';
    var meta = [];
    if (s.published && s.published !== "—") meta.push(esc(s.published));
    if (s.chat) meta.push(esc(s.chat) + (s.author ? " · " + esc(s.author) : ""));
    if (s.iteration) meta.push("итерация " + s.iteration + (s.query ? " · «" + esc(s.query) + "»" : ""));
    var link = s.url ? '<a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">Открыть источник ↗</a>'
      : (s.raw_url ? '<span class="mono small muted">' + esc(s.raw_url) + "</span>" : "");
    return '<div class="src-detail">' +
      '<div class="src__badges"><b>[' + s.n + "]</b> " + b + " " + uniqHTML(s.uniqueness) + "</div>" +
      '<div class="src__title">' + esc(s.title) + "</div>" +
      (s.snippet ? '<div class="src__snippet">' + esc(s.snippet) + "</div>" : "") +
      '<div class="src__meta">' + meta.join(" · ") + "</div>" +
      '<div class="row" style="margin-top:8px">' + link +
      (document.getElementById("src-" + s.id) ? ' <button type="button" class="btn btn--xs" data-show-in-list="' + s.id + '">Показать в списке источников</button>' : "") +
      "</div></div>";
  }
  function openDrawer(title, html) {
    $("#drawer-title").textContent = title;
    $("#drawer-body").innerHTML = html;
    document.body.classList.add("drawer-open");
    $("#drawer").setAttribute("aria-hidden", "false");
  }
  function closeDrawer() {
    document.body.classList.remove("drawer-open");
    var d = $("#drawer"); if (d) d.setAttribute("aria-hidden", "true");
  }
  function openClaim(claim) {
    var st = claimState(claim);
    var ids = claim.source_ids || [];
    var html = '<div class="drawer__claim"><div class="small muted" style="margin-bottom:4px">Утверждение</div>' + esc(claim.text) +
      '<div style="margin-top:8px">' + (st && STATE_LABEL[st] ? '<span class="fact fact--' + st + '">' + STATE_LABEL[st] + "</span>" : '<span class="fact fact--none">не проверен</span>') + "</div></div>";
    if (!ids.length) html += '<div class="flash flash--err">У этого утверждения нет ссылки на источник — проверьте его вручную или уберите из текста.</div>';
    html += '<div class="section-title">Источники (' + ids.length + ")</div>";
    ids.forEach(function (id) { html += sourceHTML(SOURCES[String(id)]); });
    openDrawer("Откуда это известно", html);
    $$("[data-claim-card]").forEach(function (c) { c.classList.toggle("is-active", c.getAttribute("data-claim-card") === String(claim.id)); });
  }
  document.addEventListener("click", function (e) {
    var fn = e.target.closest("[data-src]");
    if (fn) {
      e.preventDefault();
      var s = SOURCES[String(fn.getAttribute("data-src"))];
      openDrawer(s ? "Источник [" + s.n + "]" : "Источник", sourceHTML(s));
      return;
    }
    var show = e.target.closest("[data-show-in-list]");
    if (show) {
      closeDrawer();
      var el = document.getElementById("src-" + show.getAttribute("data-show-in-list"));
      if (el) {
        activateTab("sources");
        var det = el.closest("details"); if (det) det.open = true;
        setTimeout(function () { flash(el); }, 80);
      }
      return;
    }
    if (e.target.closest("[data-drawer-close]")) { closeDrawer(); return; }
    var cl = e.target.closest("[data-claim]");
    if (cl) { var c = findClaim(cl.getAttribute("data-claim")); if (c) openClaim(c); return; }
    var cc = e.target.closest("[data-claim-card]");
    if (cc && !e.target.closest("form") && !e.target.closest("button")) {
      var c2 = findClaim(cc.getAttribute("data-claim-card"));
      if (c2) {
        openClaim(c2);
        var mark = $('[data-claim="' + c2.id + '"]');
        if (mark) mark.scrollIntoView({ behavior: "smooth", block: "center" });
      }
    }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeDrawer();
    if (e.key === "Enter" && e.target.matches && e.target.matches("[data-claim-card]")) e.target.click();
  });
  function findClaim(id) {
    for (var i = 0; i < CLAIMS.length; i++) if (String(CLAIMS[i].id) === String(id)) return CLAIMS[i];
    return null;
  }

  /* ------------------------------------------------------------ подсветка утверждений в тексте */
  function highlightClaims(el, text) {
    if (text == null) text = el.textContent;
    var ranges = [];
    CLAIMS.forEach(function (c) {
      if (!c.text) return;
      var i = text.indexOf(c.text);
      if (i >= 0) ranges.push({ start: i, end: i + c.text.length, claim: c });
    });
    ranges.sort(function (a, b) { return a.start - b.start || b.end - a.end; });
    var frag = document.createDocumentFragment(), pos = 0, found = {};
    ranges.forEach(function (r) {
      if (r.start < pos) return; // перекрытие — пропускаем
      frag.appendChild(document.createTextNode(text.slice(pos, r.start)));
      var span = document.createElement("span");
      var st = claimState(r.claim);
      span.className = "claim claim--" + (STATE_LABEL[st] ? st : "none");
      span.setAttribute("data-claim", r.claim.id);
      span.setAttribute("title", (STATE_LABEL[st] || "не проверено") + " — нажмите, чтобы увидеть источник");
      span.textContent = text.slice(r.start, r.end);
      frag.appendChild(span);
      found[String(r.claim.id)] = true;
      pos = r.end;
    });
    frag.appendChild(document.createTextNode(text.slice(pos)));
    el.innerHTML = "";
    el.appendChild(frag);
    return found;
  }
  $$("[data-highlight-claims]").forEach(function (el) { if (el.id !== "check-view") highlightClaims(el); });

  /* ------------------------------------------------------------ отметки фактов без перезагрузки */
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.matches("[data-ajax-fact]")) {
      e.preventDefault();
      var fd = new FormData(f);
      fetch(f.action, { method: "POST", body: fd, headers: { Accept: "application/json" }, credentials: "same-origin" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) {
          var holder = f.closest(".ritem, .claim-card");
          if (holder) {
            var badge = $("[data-fact-badge]", holder);
            if (badge) {
              badge.className = "fact fact--" + data.state;
              badge.innerHTML = esc(data.label) + ' <span class="by">· человек</span>';
            }
            $$(".mark-btn", holder).forEach(function (b) {
              var st = b.closest("form").querySelector('[name="state"]').value;
              b.classList.toggle("is-on", st === data.state);
            });
            if (holder.classList.contains("claim-card")) {
              holder.className = holder.className.replace(/claim-card--\w+/, "claim-card--" + data.state);
            }
          }
          var c = findClaim(data.claim_id);
          if (c) {
            c.state = data.state;
            $$('[data-claim="' + data.claim_id + '"]').forEach(function (s) { s.className = "claim claim--" + data.state; });
          }
          toast("Отмечено: " + data.label);
        })
        .catch(function () { f.submit(); });
      return;
    }
    // комментарий к переходу
    if (f.matches("[data-ask-comment]") && !f.dataset.asked) {
      var answer = window.prompt(f.getAttribute("data-ask-comment"), "");
      if (answer === null) { e.preventDefault(); return; }
      var inp = f.querySelector('[name="comment"]'); if (inp) inp.value = answer;
    }
    if (f.matches("[data-guard-dirty]") && window.NF_DIRTY && window.NF_DIRTY()) {
      if (!window.confirm("Есть несохранённые правки. Перейти без сохранения? (Отмена — вернуться и нажать «Сохранить правку»)")) {
        e.preventDefault(); return;
      }
      window.NF_SKIP_UNLOAD = true;
    }
    if (f.matches("[data-busy]")) {
      var btn = f.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.textContent = f.getAttribute("data-busy"); }
    }
  });

  /* ------------------------------------------------------------ фильтр источников */
  var filt = $("[data-src-filter]");
  if (filt) {
    filt.addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      $$(".chip", filt).forEach(function (c) { c.classList.toggle("is-active", c === chip); });
      var k = chip.getAttribute("data-kind");
      $$(".src-group").forEach(function (g) {
        var kinds = (g.getAttribute("data-kinds") || "").split(" ");
        g.classList.toggle("hidden", !!k && kinds.indexOf(k) < 0);
      });
    });
  }

  /* ------------------------------------------------------------ копирование, картинки */
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-copy]");
    if (!b) return;
    var src = $(b.getAttribute("data-copy"));
    if (!src) return;
    var title = src.parentNode.querySelector("h2");
    var text = (title ? title.textContent + "\n\n" : "") + src.textContent;
    var done = function () { toast("Текст скопирован"); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { toast("Не удалось скопировать", true); });
    else toast("Буфер обмена недоступен", true);
  });
  $$("img[data-hide-on-error]").forEach(function (img) {
    var hide = function () { img.style.display = "none"; };
    img.addEventListener("error", hide);
    if (img.complete && img.naturalWidth === 0) hide();
  });

  /* ------------------------------------------------------------ Channel DNA */
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-rebuild-dna]");
    if (!b) return;
    b.disabled = true; b.textContent = "Считаем…";
    fetch("/api/channels/" + b.getAttribute("data-rebuild-dna") + "/dna", { method: "POST", credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 501) throw new Error("Пересчёт DNA ещё не реализован бэкендом");
        if (!r.ok) throw new Error("Ошибка " + r.status);
        toast("DNA пересчитан"); setTimeout(function () { location.reload(); }, 600);
      })
      .catch(function (err) { toast(err.message, true); b.disabled = false; b.textContent = "Пересчитать DNA"; });
  });

  /* ------------------------------------------------------------ новая зацепка */
  var newForm = $("#new-form");
  if (newForm) {
    var demoBtn = $("#fill-demo");
    if (demoBtn) demoBtn.addEventListener("click", function () {
      var ta = $("#lead_text"), title = $("#title");
      ta.value = "";
      var text = newForm.getAttribute("data-demo-lead"), i = 0;
      ta.focus();
      var timer = setInterval(function () {
        i += 6; ta.value = text.slice(0, i);
        if (i >= text.length) { clearInterval(timer); ta.value = text; }
      }, 12);
      if (title && !title.value) title.value = newForm.getAttribute("data-demo-title");
      var first = $('input[name="channel_id"]', newForm);
      if (first && !$('input[name="channel_id"]:checked', newForm)) first.checked = true;
    });
    newForm.addEventListener("submit", function () {
      var ov = $("#launch-overlay"); if (ov) ov.classList.add("is-on");
    });
    window.addEventListener("pageshow", function () { var ov = $("#launch-overlay"); if (ov) ov.classList.remove("is-on"); });
  }

  /* ------------------------------------------------------------ живой прогресс */
  var root = $("#progress-root");
  if (root && root.getAttribute("data-poll") === "1") {
    var invId = root.getAttribute("data-inv");
    var apiUrl = "/api/investigations/" + invId + "/progress";
    var fallbackUrl = "/investigations/" + invId + "/progress.json";
    var useFallback = false, failures = 0, finished = false, lastLogLen = -1, lastIterKey = "";
    var AGENT = { web: "Web-поиск", max_chats: "Чаты MAX", max_chat: "Чаты MAX", official: "Официальные", media: "Медиа" };

    var renderStages = function (stages) {
      if (!stages || !stages.length) return;
      $("#pg-stages").innerHTML = stages.map(function (s, i) {
        return '<div class="stage is-' + esc(s.status || "pending") + '" data-stage="' + esc(s.key) + '"><span class="stage__icon"></span>' +
          '<div class="stage__n">ЭТАП ' + (i + 1) + '</div><div class="stage__title">' + esc(s.title || s.key) + "</div>" +
          '<div class="stage__note">' + esc(s.note || "") + "</div></div>";
      }).join("");
    };
    var renderLog = function (log) {
      log = log || [];
      if (log.length === lastLogLen) return;
      lastLogLen = log.length;
      var box = $("#pg-log");
      var atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = log.length ? log.map(function (r) {
        return '<div class="log__row ' + esc(r.level || "info") + '"><span class="log__ts">' + esc(fmtTime(r.ts, true)) + '</span><span class="log__stage">' +
          esc(r.stage || "") + '</span><span class="log__msg">' + esc(r.message) + "</span></div>";
      }).join("") + (finished ? "" : '<div class="log__row"><span class="log__cursor"></span></div>')
        : '<div class="log__empty">Ждём первых событий от пайплайна… <span class="log__cursor"></span></div>';
      if (atBottom || log.length < 12) box.scrollTop = box.scrollHeight;
    };
    var renderIterations = function (its) {
      its = its || [];
      var key = its.map(function (i) { return i.id + ":" + (i.queries_json || []).length + ":" + i.results_count; }).join("|");
      if (key === lastIterKey) return;
      lastIterKey = key;
      if (!its.length) return;
      $("#pg-chain").innerHTML = its.map(function (it) {
        var qs = (it.queries_json || []).map(function (q) {
          return '<div class="query" title="' + esc(q.query) + '"><span class="query__agent query__agent--' + esc(q.agent) + '">' + esc(AGENT[q.agent] || q.agent) +
            '</span><span class="query__q">' + esc(q.query) + '</span><span class="query__n">' + esc(q.results == null ? "" : q.results) + "</span></div>";
        }).join("");
        return '<div class="iter"><div class="iter__n">' + it.n + '</div><div class="iter__card"><div class="iter__head"><b>Итерация ' + it.n + "</b>" +
          '<span class="muted small">' + (it.queries_json || []).length + " запросов · " + (it.results_count || 0) + " результатов · " + esc(fmtTime(it.created_at, true)) + "</span></div>" +
          (it.rationale ? '<div class="rationale">' + esc(it.rationale) + "</div>" : "") + '<div class="queries">' + qs + "</div></div></div>";
      }).join("");
    };
    var apply = function (data) {
      var p = data.progress || {};
      var pct = Math.max(0, Math.min(100, p.percent || 0));
      $("#pg-bar").style.width = pct + "%";
      $("#pg-percent").textContent = pct + "%";
      var stateText = { running: "NEWSFLOW работает…", done: "Исследование завершено", error: "Ошибка: " + (p.error || "см. лог"), idle: "Ожидание запуска" };
      var cur = (p.stages || []).filter(function (s) { return s.status === "running"; })[0];
      $("#pg-state").textContent = (p.state === "running" && cur ? "Сейчас: " + (cur.title || cur.key) : stateText[p.state] || "Ожидание запуска");
      renderStages(p.stages);
      renderIterations(data.iterations);
      var done = p.state === "done" || (data.status && data.status !== "researching");
      if (done) finished = true;
      renderLog(p.log);
      if (done) {
        $(".progress-big__bar").classList.add("is-done");
        $("#pg-bar").style.width = "100%"; $("#pg-percent").textContent = "100%";
        toast("Исследование завершено — открываем справку");
        setTimeout(function () {
          var u = new URL(location.href); u.searchParams.set("tab", "report"); u.searchParams.delete("msg"); u.searchParams.delete("level");
          location.href = u.toString();
        }, 1400);
        return false;
      }
      if (p.state === "error") { finished = true; renderLog(p.log); return false; }
      return true;
    };
    var poll = function () {
      fetch(useFallback ? fallbackUrl : apiUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) {
          if (!r.ok) {
            if (!useFallback && r.status !== 401) {
              useFallback = true;
              var note = $("#pg-api-note");
              if (note) { note.textContent = "API прогресса ещё не готово (HTTP " + r.status + ") — читаем прогресс напрямую из базы."; note.classList.remove("hidden"); }
              return null;
            }
            throw new Error(r.status);
          }
          return r.json();
        })
        .then(function (data) {
          failures = 0;
          if (data === null) { setTimeout(poll, 200); return; }
          if (apply(data) !== false) setTimeout(poll, 1500);
        })
        .catch(function () { failures++; if (failures < 20) setTimeout(poll, 3000); });
    };
    poll();
  }

  /* ------------------------------------------------------------ редактор: word-level diff */
  function tokenize(s) { return s.match(/\s+|[^\s]+/g) || []; }
  function isWord(t) { return /\S/.test(t); }
  /* LCS по токенам → массив операций [{op:'equal'|'delete'|'insert', t}] */
  function diffTokens(a, b) {
    var start = 0, endA = a.length, endB = b.length;
    while (start < endA && start < endB && a[start] === b[start]) start++;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    var ops = [], i, j;
    for (i = 0; i < start; i++) ops.push({ op: "equal", t: a[i] });
    var A = a.slice(start, endA), B = b.slice(start, endB), n = A.length, m = B.length;
    if (n * m > 4000000) {
      A.forEach(function (t) { ops.push({ op: "delete", t: t }); });
      B.forEach(function (t) { ops.push({ op: "insert", t: t }); });
    } else if (n || m) {
      var W = m + 1, L = new Uint32Array((n + 1) * W);
      for (i = n - 1; i >= 0; i--) for (j = m - 1; j >= 0; j--)
        L[i * W + j] = A[i] === B[j] ? L[(i + 1) * W + j + 1] + 1 : Math.max(L[(i + 1) * W + j], L[i * W + j + 1]);
      i = 0; j = 0;
      while (i < n && j < m) {
        if (A[i] === B[j]) { ops.push({ op: "equal", t: A[i] }); i++; j++; }
        else if (L[(i + 1) * W + j] >= L[i * W + j + 1]) { ops.push({ op: "delete", t: A[i] }); i++; }
        else { ops.push({ op: "insert", t: B[j] }); j++; }
      }
      while (i < n) ops.push({ op: "delete", t: A[i++] });
      while (j < m) ops.push({ op: "insert", t: B[j++] });
    }
    for (i = endA; i < a.length; i++) ops.push({ op: "equal", t: a[i] });
    return ops;
  }
  function renderDiff(ops) {
    var html = "", cur = null, buf = "";
    var flush = function () {
      if (!buf) return;
      html += cur === "insert" ? "<ins>" + esc(buf) + "</ins>" : cur === "delete" ? "<del>" + esc(buf) + "</del>" : esc(buf);
      buf = "";
    };
    ops.forEach(function (o) {
      // пробелы между двумя изменёнными словами держим внутри изменения, чтобы не дробить подсветку
      if (o.op !== cur) { flush(); cur = o.op; }
      buf += o.t;
    });
    flush();
    return html;
  }

  var editor = $("#editor");
  if (editor) {
    var aiBody = readJSON("nf-ai-body", "") || "";
    var aiTitle = readJSON("nf-ai-title", "") || "";
    var body = $("#edit-body"), titleIn = $("#edit-title");
    var initialBody = body.value, initialTitle = titleIn.value;
    var diffView = $("#diff-view"), checkView = $("#check-view"), stats = $("#diff-stats");
    var pane = $("#edit-pane");
    var aiTokens = tokenize(aiBody);

    window.NF_DIRTY = function () { return body.value !== initialBody || titleIn.value !== initialTitle; };

    var update = function () {
      var text = body.value.replace(/\r\n/g, "\n");
      var ops = diffTokens(aiTokens, tokenize(text));
      diffView.innerHTML = renderDiff(ops);
      var eq = 0, del = 0, ins = 0, wa = 0, wb = 0;
      ops.forEach(function (o) {
        if (!isWord(o.t)) return;
        if (o.op === "equal") { eq++; wa++; wb++; } else if (o.op === "delete") { del++; wa++; } else { ins++; wb++; }
      });
      var ratio = (wa + wb) ? (2 * eq) / (wa + wb) : 1;
      var pct = Math.round((1 - ratio) * 100);
      $("#change-pct").textContent = pct + "%";
      $("#change-bar").style.width = Math.min(100, pct) + "%";
      stats.innerHTML = '<span class="plus">+' + ins + "</span> <span class=\"minus\">−" + del + "</span> " +
        '<span class="muted">слов · ' + (pct < 15 ? "малая правка — AI-черновик принят" : pct < 40 ? "заметная правка" : "существенная переработка") + "</span>";
      var found = highlightClaims(checkView, text);
      $$("[data-claim-card]").forEach(function (card) {
        var flag = $("[data-claim-changed]", card);
        if (flag) flag.classList.toggle("hidden", !!found[card.getAttribute("data-claim-card")]);
      });
      pane.classList.toggle("is-dirty", window.NF_DIRTY());
    };
    var t;
    body.addEventListener("input", function () { clearTimeout(t); t = setTimeout(update, 120); });
    titleIn.addEventListener("input", function () { pane.classList.toggle("is-dirty", window.NF_DIRTY()); });
    update();

    var reset = $("#reset-ai");
    if (reset) reset.addEventListener("click", function () {
      if (!window.confirm("Заменить текст в поле правки на исходную AI-версию?")) return;
      body.value = aiBody; titleIn.value = aiTitle; update();
    });
    $("#edit-form").addEventListener("submit", function () { window.NF_SKIP_UNLOAD = true; });
    window.addEventListener("beforeunload", function (e) {
      if (!window.NF_SKIP_UNLOAD && window.NF_DIRTY()) { e.preventDefault(); e.returnValue = ""; }
    });
    // вкладки нижней панели
    var subtabs = $("[data-subtabs]");
    if (subtabs) subtabs.addEventListener("click", function (e) {
      var b = e.target.closest("[data-subtab]");
      if (!b) return;
      var name = b.getAttribute("data-subtab");
      $$("[data-subtab]").forEach(function (x) { x.classList.toggle("is-active", x === b); });
      $$("[data-subpanel]").forEach(function (p) { p.classList.toggle("hidden", p.getAttribute("data-subpanel") !== name); });
      $("#diff-hint").textContent = name === "diff" ? "обновляется при вводе" : "утверждения AI, найденные в вашей правке";
    });
  }
})();
