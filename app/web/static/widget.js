/* Otvet website widget. Usage: <script src="https://HOST/widget.js" data-key="KEY" async></script> */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script) { return; }
  var key = script.getAttribute("data-key");
  if (!key || !/^[A-Za-z0-9_-]{10,40}$/.test(key)) { return; }
  var base = new URL(script.src).origin;
  var api = base + "/api/widget/" + key;

  function sessionId() {
    var k = "otvet_sid_" + key, v = null;
    try { v = window.localStorage.getItem(k); } catch (e) { v = null; }
    if (v && /^[A-Za-z0-9_-]{16,64}$/.test(v)) { return v; }
    var a = new Uint8Array(18);
    (window.crypto || window.msCrypto).getRandomValues(a);
    v = Array.prototype.map.call(a, function (b) { return ("0" + b.toString(16)).slice(-2); }).join("");
    try { window.localStorage.setItem(k, v); } catch (e) { /* private mode: in-memory only */ }
    return v;
  }
  var sid = sessionId();

  var css = [
    ":host{all:initial}",
    "*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}",
    ".fab{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:0;background:#2563eb;color:#fff;font-size:26px;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.25);z-index:2147483000}",
    ".fab:focus-visible,.send:focus-visible,input:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}",
    ".panel{position:fixed;right:20px;bottom:88px;width:360px;max-width:calc(100vw - 24px);height:480px;max-height:calc(100vh - 110px);background:#fff;color:#111827;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.28);display:none;flex-direction:column;overflow:hidden;z-index:2147483000}",
    ".panel.open{display:flex}",
    ".head{background:#2563eb;color:#fff;padding:12px 14px;font-weight:600;font-size:15px}",
    ".log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px;background:#f3f4f6}",
    ".m{max-width:85%;padding:8px 11px;border-radius:12px;font-size:14px;line-height:1.4;white-space:pre-wrap;word-wrap:break-word}",
    ".bot{background:#fff;align-self:flex-start;border:1px solid #e5e7eb}",
    ".me{background:#2563eb;color:#fff;align-self:flex-end}",
    ".err{background:#fef2f2;color:#991b1b;align-self:center;font-size:13px}",
    "form{display:flex;gap:6px;padding:10px;border-top:1px solid #e5e7eb;background:#fff}",
    "input{flex:1;border:1px solid #d1d5db;border-radius:8px;padding:9px 10px;font-size:14px;min-width:0}",
    ".send{border:0;background:#2563eb;color:#fff;border-radius:8px;padding:0 14px;font-size:14px;cursor:pointer}",
    ".send[disabled]{opacity:.5;cursor:default}"
  ].join("");

  var host = document.createElement("div");
  var root = host.attachShadow({ mode: "open" });
  try {
    var sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    root.adoptedStyleSheets = [sheet];
  } catch (e) {
    var st = document.createElement("style");
    st.textContent = css;
    root.appendChild(st);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text) { n.textContent = text; }
    return n;
  }

  var fab = el("button", "fab", "\u{1F4AC}");
  fab.type = "button";
  fab.setAttribute("aria-label", "Открыть чат");
  var panel = el("div", "panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Чат с администратором");
  var head = el("div", "head", "Чат");
  var log = el("div", "log");
  log.setAttribute("aria-live", "polite");
  var form = el("form");
  var input = el("input");
  input.type = "text";
  input.maxLength = 1000;
  input.placeholder = "Напишите вопрос…";
  input.setAttribute("aria-label", "Сообщение");
  var send = el("button", "send", "Отправить");
  send.type = "submit";
  form.appendChild(input);
  form.appendChild(send);
  panel.appendChild(head);
  panel.appendChild(log);
  panel.appendChild(form);
  root.appendChild(fab);
  root.appendChild(panel);
  document.body.appendChild(host);

  function add(cls, text) {
    var m = el("div", "m " + cls, text);
    log.appendChild(m);
    log.scrollTop = log.scrollHeight;
  }

  var greeted = false;
  function open() {
    panel.classList.add("open");
    input.focus();
    if (greeted) { return; }
    greeted = true;
    fetch(api + "/config").then(function (r) { return r.ok ? r.json() : null; }).then(function (c) {
      if (c) { head.textContent = c.name; add("bot", c.greeting); }
      else { add("err", "Чат сейчас недоступен."); }
    }).catch(function () { add("err", "Не удалось подключиться."); });
  }
  fab.addEventListener("click", function () {
    if (panel.classList.contains("open")) { panel.classList.remove("open"); } else { open(); }
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var text = input.value.trim();
    if (!text) { return; }
    input.value = "";
    add("me", text);
    send.disabled = true;
    fetch(api + "/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session: sid, text: text })
    }).then(function (r) {
      return r.json().then(function (j) { return { ok: r.ok, j: j }; });
    }).then(function (res) {
      if (res.ok) { add("bot", res.j.reply); } else { add("err", res.j.error || "Ошибка"); }
    }).catch(function () {
      add("err", "Нет связи. Попробуйте ещё раз.");
    }).then(function () { send.disabled = false; input.focus(); });
  });
})();
