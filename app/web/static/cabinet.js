/* Cabinet behaviour. The CSP forbids inline scripts, so everything hooks in via data-* attributes. */
(function () {
  "use strict";

  var body = document.body;
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function two(n) { return (n < 10 ? "0" : "") + n; }

  /* ---------- sidebar: an accordion, only one group is open at a time ----------
     The server opens the group that contains the current page; opening another one closes it. */
  var groups = qsa("details.nav-group[data-nav-key]");
  groups.forEach(function (g) {
    g.addEventListener("toggle", function () {
      if (!g.open) { return; }
      groups.forEach(function (other) { if (other !== g && other.open) { other.open = false; } });
    });
  });

  /* ---------- mobile drawer ---------- */
  var toggle = document.querySelector("[data-nav-toggle]");
  function setNav(open) {
    body.classList.toggle("nav-open", open);
    if (toggle) { toggle.setAttribute("aria-expanded", open ? "true" : "false"); }
  }
  if (toggle) {
    toggle.addEventListener("click", function () { setNav(!body.classList.contains("nav-open")); });
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-nav-close]")) { setNav(false); return; }
    if (e.target.closest && e.target.closest(".sidebar a")) { setNav(false); }
  });

  /* ---------- account menu: close on outside click and Esc ---------- */
  var account = document.querySelector("[data-account]");
  document.addEventListener("click", function (e) {
    if (account && account.open && !account.contains(e.target)) { account.open = false; }
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    if (account && account.open) { account.open = false; var s = account.querySelector("summary"); if (s) { s.focus(); } }
    if (body.classList.contains("nav-open")) { setNav(false); if (toggle) { toggle.focus(); } }
  });

  /* ---------- page tabs: each tab shows one section and hides the others ----------
     Without JavaScript every section stays visible and the tabs are plain anchor links. */
  var tabsEl = document.querySelector("[data-tabs]");
  if (tabsEl) {
    var tabLinks = qsa(".tabs-list a", tabsEl);
    var tabItems = tabLinks.map(function (a) { return { a: a, panel: document.getElementById(a.getAttribute("href").slice(1)) }; })
      .filter(function (x) { return x.panel; });
    tabItems.forEach(function (x) {
      x.a.setAttribute("role", "tab");
      x.panel.setAttribute("role", "tabpanel");
      x.panel.setAttribute("aria-labelledby", x.a.id);
    });
    var selectTab = function (id, opts) {
      var chosen = tabItems.filter(function (x) { return x.panel.id === id; })[0] || tabItems[0];
      if (!chosen) { return; }
      tabItems.forEach(function (x) {
        var on = x === chosen;
        x.panel.hidden = !on;
        x.a.setAttribute("aria-selected", on ? "true" : "false");
        x.a.tabIndex = on ? 0 : -1;
      });
      if (opts && opts.url && window.history && window.history.replaceState) {
        window.history.replaceState(null, "", "#" + chosen.panel.id);
      }
      if (opts && opts.focus) { chosen.a.focus(); }
      if (opts && opts.reveal) {
        // when the bar is stuck to the top, bring the start of the section back into view
        var delta = chosen.panel.getBoundingClientRect().top - tabsEl.getBoundingClientRect().bottom - 8;
        if (delta < 0) { window.scrollBy(0, delta); }
      }
    };
    var tabFromHash = function () {
      var raw = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : "";
      if (!raw) { return null; }
      var target = document.getElementById(raw);
      if (!target) { return null; }
      var owner = tabItems.filter(function (x) { return x.panel === target || x.panel.contains(target); })[0];
      return owner ? { id: owner.panel.id, inner: owner.panel === target ? null : target } : null;
    };
    var start = tabFromHash();
    selectTab(start ? start.id : (tabsEl.getAttribute("data-default") || ""));
    // The browser jumps to #section before the other panels are hidden, which would leave the page half
    // scrolled. Settle it: a panel link starts at the top, a link to something inside a panel scrolls to it.
    var settleScroll = function () {
      if (!start) { return; }
      if (start.inner && start.inner.scrollIntoView) { start.inner.scrollIntoView(); } else { window.scrollTo(0, 0); }
    };
    settleScroll();
    window.requestAnimationFrame(settleScroll);
    window.addEventListener("load", function () { window.setTimeout(settleScroll, 0); });
    window.addEventListener("hashchange", function () {
      var h = tabFromHash();
      if (h) { selectTab(h.id, { reveal: true }); }
    });
    tabsEl.addEventListener("click", function (e) {
      var a = e.target.closest && e.target.closest(".tabs-list a");
      if (!a) { return; }
      e.preventDefault();
      selectTab(a.getAttribute("href").slice(1), { url: true, reveal: true });
    });
    tabsEl.addEventListener("keydown", function (e) {
      var i = tabItems.map(function (x) { return x.a; }).indexOf(document.activeElement);
      if (i < 0) { return; }
      var keys = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabItems.length - 1 };
      if (!(e.key in keys)) { return; }
      e.preventDefault();
      var n = (keys[e.key] + tabItems.length) % tabItems.length;
      selectTab(tabItems[n].panel.id, { url: true, focus: true });
    });
  }

  /* ---------- filter forms that apply on change (the button stays for no-JS) ---------- */
  qsa("form[data-autosubmit]").forEach(function (f) {
    f.addEventListener("change", function () { f.requestSubmit(); });
  });

  /* ---------- generic dialog helpers ---------- */
  function closeDialogOn(dlg) {
    dlg.addEventListener("click", function (e) {
      if (e.target === dlg || (e.target.closest && e.target.closest("[data-close]"))) { dlg.close(); }
    });
  }

  /* ---------- calendar: click a free time to book, click a booking for a quick view ---------- */
  var cal = document.querySelector("[data-cal]");
  var newDlg = document.getElementById("new-booking");
  var quickDlg = document.getElementById("booking-quick");
  if (cal && newDlg && quickDlg && !newDlg.querySelector("#nb-form")) {
    // nothing to book yet (no services): the button only shows the "set up the calendar" hint
    closeDialogOn(newDlg);
    qsa("[data-new-booking]").forEach(function (b) { b.addEventListener("click", function () { newDlg.showModal(); }); });
  } else if (cal && newDlg && quickDlg) {
    var csrf = cal.getAttribute("data-csrf");
    var F = {
      name: newDlg.querySelector("[name=name]"), phone: newDlg.querySelector("[name=phone]"),
      service: newDlg.querySelector("[name=service_id]"), resource: newDlg.querySelector("[name=resource_id]"),
      day: newDlg.querySelector("[name=day]"), at: newDlg.querySelector("[name=at]"), note: newDlg.querySelector("[name=note]")
    };
    var chips = document.getElementById("nb-slots");
    var hint = document.getElementById("nb-hint");
    var free = null;   // free start times for the chosen service/day, or null while unknown
    var reqId = 0;
    closeDialogOn(newDlg);
    closeDialogOn(quickDlg);

    var renderChips = function () {
      chips.textContent = "";
      if (free === null) { return; }
      if (!free.length) {
        var none = document.createElement("span");
        none.className = "muted small-text";
        none.textContent = "На этот день свободных окон нет";
        chips.appendChild(none);
        return;
      }
      free.forEach(function (t) {
        var b = document.createElement("button");
        b.type = "button";
        b.className = "chip" + (t === F.at.value ? " is-on" : "");
        b.textContent = t;
        b.addEventListener("click", function () { F.at.value = t; renderChips(); updateHint(); placeGhost(); });
        chips.appendChild(b);
      });
    };
    var updateHint = function () {
      hint.textContent = "";
      hint.className = "small-text muted";
      var opt = F.service.options[F.service.selectedIndex];
      if (!F.at.value || !opt) { return; }
      var parts = F.at.value.split(":");
      var end = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + parseInt(opt.getAttribute("data-duration") || "0", 10);
      var text = "Запись продлится до " + two(Math.floor(end / 60) % 24) + ":" + two(end % 60) + ".";
      if (free !== null && free.indexOf(F.at.value) === -1) {
        text += " В это время нет свободного окна (занято или вне расписания): проверьте время.";
        hint.className = "small-text warn-text";
      }
      hint.textContent = text;
    };
    var refreshSlots = function () {
      if (!F.service.value || !F.day.value) { free = null; renderChips(); updateHint(); return; }
      var id = ++reqId;
      var q = new URLSearchParams({ service_id: F.service.value, day: F.day.value });
      if (F.resource.value) { q.set("resource_id", F.resource.value); }
      fetch(cal.getAttribute("data-slots-url") + "?" + q.toString(), { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : { times: null }; })
        .then(function (j) { if (id === reqId) { free = j.times; renderChips(); updateHint(); } })
        .catch(function () { if (id === reqId) { free = null; renderChips(); updateHint(); } });
    };
    var ghost = null;
    var clearGhost = function () { if (ghost && ghost.parentNode) { ghost.parentNode.removeChild(ghost); } ghost = null; };
    var geometry = function (col) {
      var grid = col.parentElement;
      var rows = parseInt(grid.getAttribute("data-rows") || "0", 10) || 1;
      return { rowPx: col.getBoundingClientRect().height / rows, step: parseInt(grid.getAttribute("data-step") || "30", 10),
               start: parseInt(grid.getAttribute("data-start-min") || "0", 10), rows: rows };
    };
    // the picked time is shown on the grid as a block as long as the service
    var placeGhost = function () {
      clearGhost();
      if (!newDlg.open || !F.at.value || !F.day.value) { return; }
      var col = cal.querySelector('.dcol[data-date="' + F.day.value + '"]');
      if (!col) { return; }
      var g = geometry(col);
      var p = F.at.value.split(":");
      var from = parseInt(p[0], 10) * 60 + parseInt(p[1], 10);
      var opt = F.service.options[F.service.selectedIndex];
      var dur = parseInt((opt && opt.getAttribute("data-duration")) || "30", 10);
      ghost = document.createElement("div");
      ghost.className = "slot-ghost";
      ghost.style.top = ((from - g.start) / g.step * g.rowPx) + "px";
      ghost.style.height = Math.max(dur / g.step * g.rowPx, g.rowPx) + "px";
      col.appendChild(ghost);
    };
    var popAt = function (x, y) {
      // a small window next to the click instead of a full-screen dialog
      newDlg.classList.add("is-pop");
      newDlg.show();
      var w = newDlg.offsetWidth, h = newDlg.offsetHeight;
      var left = x + 14, top = y - 24;
      if (left + w > window.innerWidth - 8) { left = Math.max(8, x - w - 14); }
      if (top + h > window.innerHeight - 8) { top = Math.max(8, window.innerHeight - h - 8); }
      newDlg.style.left = left + "px";
      newDlg.style.top = Math.max(8, top) + "px";
    };
    var openNew = function (pre, point) {
      pre = pre || {};
      F.day.value = pre.day || cal.getAttribute("data-date");
      F.at.value = pre.at || "";
      F.resource.value = pre.resource || cal.getAttribute("data-resource") || "";
      F.name.value = ""; F.phone.value = ""; F.note.value = "";
      free = null; chips.textContent = ""; hint.textContent = "";
      if (newDlg.open) { newDlg.close(); }
      if (point) { popAt(point.x, point.y); } else {
        newDlg.classList.remove("is-pop");
        newDlg.style.left = ""; newDlg.style.top = "";
        newDlg.showModal();
      }
      placeGhost();
      refreshSlots();
      F.service.focus();
    };
    newDlg.addEventListener("close", clearGhost);
    // the small window closes on Esc or a click anywhere else (a click on another free time moves it there)
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && newDlg.open && newDlg.classList.contains("is-pop")) { newDlg.close(); } });
    document.addEventListener("mousedown", function (e) {
      if (newDlg.open && newDlg.classList.contains("is-pop") && !newDlg.contains(e.target) && !e.target.closest(".dcol, .mcell, datalist")) { newDlg.close(); }
    });
    qsa("[data-new-booking]").forEach(function (b) { b.addEventListener("click", function () { openNew(); }); });
    ["service", "resource", "day"].forEach(function (k) { F[k].addEventListener("change", function () { refreshSlots(); placeGhost(); }); });
    F.at.addEventListener("input", function () { renderChips(); updateHint(); placeGhost(); });
    F.name.addEventListener("input", function () {
      // choosing a known client from the list fills in the phone
      var list = document.getElementById("nb-clients");
      var hit = qsa("option", list).filter(function (o) { return o.value === F.name.value; })[0];
      if (hit && !F.phone.value) { F.phone.value = hit.getAttribute("data-phone") || ""; }
    });
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if (e.key === "n" && !e.ctrlKey && !e.metaKey && !e.altKey && !/INPUT|TEXTAREA|SELECT/.test(tag) && !document.querySelector("dialog[open]")) {
        e.preventDefault();
        openNew();
      }
    });

    // clicking an empty part of a day column (week) or a day cell (month) starts a booking there
    var slotAt = function (col, clientY) {
      var g = geometry(col);
      var row = Math.max(0, Math.min(g.rows - 1, Math.floor((clientY - col.getBoundingClientRect().top) / g.rowPx)));
      return { row: row, g: g, minutes: g.start + row * g.step };
    };
    cal.addEventListener("click", function (e) {
      if (e.target.closest("a, button")) { return; }
      var col = e.target.closest(".dcol");
      if (col) {
        var sl = slotAt(col, e.clientY);
        openNew({ day: col.getAttribute("data-date"), at: two(Math.floor(sl.minutes / 60)) + ":" + two(sl.minutes % 60) }, { x: e.clientX, y: e.clientY });
        return;
      }
      var cell = e.target.closest(".mcell");
      if (cell && cell.getAttribute("data-date")) { openNew({ day: cell.getAttribute("data-date") }, { x: e.clientX, y: e.clientY }); }
    });
    // the slot under the pointer is outlined, so it is clear where a click will book
    var hov = document.createElement("div");
    hov.className = "slot-hover";
    var hideHover = function () { if (hov.parentNode) { hov.parentNode.removeChild(hov); } };
    cal.addEventListener("mousemove", function (e) {
      var col = e.target.closest && e.target.closest(".dcol");
      if (!col) { hideHover(); return; }
      var sl = slotAt(col, e.clientY);
      hov.style.top = (sl.row * sl.g.rowPx) + "px";
      hov.style.height = sl.g.rowPx + "px";
      hov.textContent = two(Math.floor(sl.minutes / 60)) + ":" + two(sl.minutes % 60);
      if (hov.parentNode !== col) { col.appendChild(hov); }
    });
    cal.addEventListener("mouseleave", hideHover);

    // clicking a booking shows a quick view with the usual actions (ctrl/middle click still opens the card)
    var quickForm = function (action, label, cls, confirmText) {
      var f = document.createElement("form");
      f.method = "post"; f.action = action;
      if (confirmText) { f.setAttribute("data-confirm", confirmText); }
      var h = document.createElement("input");
      h.type = "hidden"; h.name = "csrf"; h.value = csrf;
      var b = document.createElement("button");
      b.type = "submit"; b.className = cls; b.textContent = label;
      f.appendChild(h); f.appendChild(b);
      return f;
    };
    var STATUS = { confirmed: ["подтверждена", "ok"], pending: ["ждёт подтверждения", "warn"], cancelled: ["отменена", "err"] };
    cal.addEventListener("click", function (e) {
      var ev = e.target.closest("[data-booking]");
      if (!ev || e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) { return; }
      e.preventDefault();
      var d = ev.dataset;
      var box = document.getElementById("bq-body");
      box.textContent = "";
      var row = function (k, v) {
        if (!v) { return; }
        var dt = document.createElement("dt"); dt.textContent = k;
        var dd = document.createElement("dd"); dd.textContent = v;
        box.appendChild(dt); box.appendChild(dd);
      };
      document.getElementById("bq-title").textContent = d.service;
      var st = STATUS[d.status] || [d.status, ""];
      var pill = document.getElementById("bq-status");
      pill.textContent = st[0]; pill.className = "pill pill-" + st[1];
      row("Когда", d.when); row("Специалист", d.resource); row("Клиент", d.client); row("Телефон", d.phone);
      row("Стоимость", d.price ? d.price + " ₽" : ""); row("Заметка", d.note);
      var acts = document.getElementById("bq-actions");
      acts.textContent = "";
      var open = document.createElement("a");
      open.className = "btn btn-secondary"; open.href = d.href; open.textContent = "Открыть карточку";
      acts.appendChild(open);
      if (d.status === "pending") { acts.appendChild(quickForm(d.href + "/confirm", "Подтвердить", "btn btn-primary")); }
      if (d.status !== "cancelled") { acts.appendChild(quickForm(d.href + "/cancel", "Отменить запись", "btn btn-danger", "Отменить запись? Клиент получит сообщение.")); }
      quickDlg.showModal();
    });
  }

  /* ---------- confirmation modal for destructive forms (data-confirm) + loading state ---------- */
  var dialog = document.getElementById("confirm-dialog");
  var confirmed = new WeakSet();

  function markLoading(form, submitter) {
    var btn = submitter || form.querySelector("button[type=submit], button:not([type])");
    if (btn && !btn.classList.contains("is-loading")) { btn.classList.add("is-loading"); }
  }

  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.getAttribute) { return; }
    var msg = form.getAttribute("data-confirm");
    if (msg && !confirmed.has(form)) {
      e.preventDefault();
      var submitter = e.submitter;
      if (dialog && typeof dialog.showModal === "function") {
        document.getElementById("confirm-text").textContent = msg;
        dialog.returnValue = "";
        dialog.showModal();
        dialog.addEventListener("close", function onClose() {
          dialog.removeEventListener("close", onClose);
          if (dialog.returnValue === "ok") {
            confirmed.add(form);
            form.requestSubmit(submitter || undefined);
          }
        });
      } else if (window.confirm(msg)) {
        confirmed.add(form);
        form.requestSubmit(submitter || undefined);
      }
      return;
    }
    if (form.id !== "chat-form") { markLoading(form, e.submitter); }
  });
  window.addEventListener("pageshow", function () {
    // coming back through the browser history must not leave buttons spinning
    qsa(".is-loading").forEach(function (b) { b.classList.remove("is-loading"); });
  });

  /* ---------- bot test chat ---------- */
  var chat = document.getElementById("chat");
  if (!chat) { return; }
  var token = chat.getAttribute("data-csrf");
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var reset = document.getElementById("chat-reset");

  var LABELS = {
    answered: "ответ из базы", refused: "нет в базе", blocked: "ответ модели заблокирован — выдержка из базы",
    booking: "запись", handoff: "передано менеджеру", limit: "лимит тарифа", smalltalk: "служебный ответ"
  };

  function add(cls, text, meta) {
    var d = document.createElement("div");
    d.className = "bubble " + cls;
    d.textContent = text;
    if (meta) {
      var s = document.createElement("small");
      s.textContent = meta;
      d.appendChild(document.createElement("br"));
      d.appendChild(s);
    }
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
  }

  function call(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": token },
      body: JSON.stringify(payload || {})
    });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text) { return; }
    input.value = "";
    add("me", text);
    call("/app/test/message", { text: text }).then(function (r) {
      if (r.status === 401) { window.location.href = "/login"; return null; }
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (!res) { return; }
      if (!res.ok) { add("err", "Ошибка: " + (res.j.error || "не удалось получить ответ")); return; }
      var meta = LABELS[res.j.outcome] || res.j.outcome;
      if (res.j.sources && res.j.sources.length) {
        meta += " · источники: " + res.j.sources.map(function (s) { return "#" + s.id + " " + s.title; }).join("; ");
      }
      add("bot", res.j.reply, meta);
    }).catch(function () { add("err", "Нет связи с сервером."); });
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-ask]");
    if (!btn) { return; }
    input.value = btn.getAttribute("data-ask");
    chat.scrollIntoView({ block: "nearest" });
    form.requestSubmit();
  });

  reset.addEventListener("click", function () {
    call("/app/test/reset").then(function () { log.textContent = ""; });
  });
})();
