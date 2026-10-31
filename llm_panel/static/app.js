// Внешний скрипт панели (CSP script-src 'self': inline-скрипты запрещены).
// SSE-подписка + режим «sort» (перетаскивание карточек сервисов мышью).
(function () {
  "use strict";
  if (!document.body) return;
  var root = document.getElementById("sse-root");
  var sseWarn = document.getElementById("sse-warn");
  var csrf = document.body.dataset.csrf || "";

  function toast(msg, kind) {
    var el = document.getElementById("toast");
    if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; document.body.appendChild(el); }
    el.textContent = msg;
    el.className = "toast show" + (kind ? " " + kind : "");
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { el.className = "toast"; }, 3500);
  }

  // --------------------------- SSE ---------------------------------------
  if (typeof EventSource !== "undefined" && root) {
    var es = new EventSource("/events");
    var targets = { services: null, gpus: null, host: null };

    es.addEventListener("services", function (e) { swap("services", e); });
    es.addEventListener("gpus", function (e) { swap("gpus", e); });
    es.addEventListener("host", function (e) { swap("host", e); });
    es.onerror = function () { if (sseWarn) sseWarn.hidden = false; };

    function swap(name, e) {
      if (activeSorts.some(function (s) { return s.active; })) return;   // в режиме «sort» SSE не перерисовывает карточки
      var el = targets[name];
      if (!el) {
        el = document.createElement("section");
        el.className = "sse-target";
        el.id = name + "-live";
        var ini = document.getElementById(name + "-init");
        if (ini && ini.parentNode) { ini.replaceWith(el); }
        else { root.appendChild(el); }
        targets[name] = el;
      }
      el.innerHTML = e.data;
      if (sseWarn) sseWarn.hidden = true;
    }
  } else if (sseWarn) {
    sseWarn.hidden = false;
  }

  // ------------------- sort: перетаскивание карточек --------------------
  // Один режим на сущность: сервисы (главная) и модели (/models). Включение
  // кнопкой, перетаскивание мышью (надёжнее HTML5 DnD на картах с кнопками),
  // сохранение — повторным нажатием кнопки (v0.3.9: обобщено на модели).
  var activeSorts = [];   // активные режимы (SSE не перерисовывает, пока идёт sort)

  function setupSort(cfg) {
    var btn = document.getElementById(cfg.btnId);
    if (!btn) return;
    var label = document.getElementById(cfg.labelId);
    var drag = null;   // { card, x, y, moved }
    var st = { active: false };
    activeSorts.push(st);

    function container() { return cfg.container(); }
    function cards(c) { return Array.prototype.slice.call(c.querySelectorAll(cfg.cardClass)); }
    function setLabel(saveMode) {
      if (label) label.textContent = saveMode ? (btn.dataset.labelSave || "save") : (btn.dataset.label || "sort");
    }
    function enter() {
      var c = container();
      if (!c) { toast(btn.dataset.toastNocards || "", "err"); return; }
      st.active = true;
      setLabel(true);
      btn.classList.add("order-active");
      c.classList.add("order-mode");
      if (!c.dataset.orderBound) {
        c.dataset.orderBound = "1";
        c.addEventListener("mousedown", onDown);
      }
    }
    function exit(msgAndKind) {
      st.active = false;
      btn.classList.remove("order-active");
      setLabel(false);
      var c = container();
      if (c) c.classList.remove("order-mode");
      if (drag) { drag.card.classList.remove("drag-src"); drag = null; }
      document.body.classList.remove("order-dragging");
      if (msgAndKind) toast(msgAndKind[0], msgAndKind[1]);
    }
    function onDown(e) {
      if (e.button !== 0 || !st.active) return;
      var t = e.target;
      var card = null;
      while (t && t !== document.body) {
        if (t.classList && t.classList.contains(cfg.cardClassRaw)) { card = t; break; }
        t = t.parentElement;
      }
      if (!card) return;
      drag = { card: card, x: e.clientX, y: e.clientY, moved: false };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    }
    function onMove(e) {
      if (!drag) return;
      if (!drag.moved) {
        if (Math.abs(e.clientX - drag.x) < 5 && Math.abs(e.clientY - drag.y) < 5) return;
        drag.moved = true;
        drag.card.classList.add("drag-src");
        document.body.classList.add("order-dragging");
      }
      e.preventDefault();
      var under = document.elementFromPoint(e.clientX, e.clientY);
      while (under && !(under.classList && under.classList.contains(cfg.cardClassRaw))) under = under.parentElement;
      if (under && under !== drag.card && under.parentNode) {
        var rect = under.getBoundingClientRect();
        var before = (e.clientY - rect.top) / rect.height < 0.5;
        under.parentNode.insertBefore(drag.card, before ? under : under.nextSibling);
      }
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.classList.remove("order-dragging");
      if (drag) { drag.card.classList.remove("drag-src"); drag = null; }
      // остаёмся в режиме «sort»: сохранение — повторным нажатием кнопки
    }
    function save() {
      var c = container();
      if (!c) { exit(null); return; }
      var vals = cards(c).map(cfg.cardValue).filter(function (v) { return v; });
      var fd = new FormData();
      fd.append("order", vals.join(","));
      fd.append("csrf", csrf);
      fetch(cfg.url, { method: "POST", body: fd, credentials: "same-origin" })
        .then(function (r) {
          if (!r.ok) return r.text().then(function (t) { throw new Error(t || ("HTTP " + r.status)); });
          exit([btn.dataset.toastOk || "", "ok"]);
        })
        .catch(function (e) {
          exit([(btn.dataset.toastErr || "") + (e && e.message || e), "err"]);
        });
    }
    btn.addEventListener("click", function () {
      if (st.active) { save(); } else { enter(); }
    });
    document.addEventListener("keydown", function (e) {
      if (st.active && e.key === "Escape") exit("");
    });
  }

  setupSort({
    btnId: "order-btn", labelId: "order-btn-label",
    container: function () {
      return document.getElementById("services-live") || document.getElementById("services-init");
    },
    cardClass: ".service-card", cardClassRaw: "service-card",
    cardValue: function (card) { return card.id.replace(/^svc-/, ""); },
    url: "/ui/order",
  });

  setupSort({
    btnId: "models-order-btn", labelId: "models-order-btn-label",
    container: function () { return document.getElementById("models-list"); },
    cardClass: ".model-item", cardClassRaw: "model-item",
    cardValue: function (card) { return card.dataset.model || ""; },
    url: "/ui/models-order",
  });
})();
// --- Config Panel: кнопки «Копировать» в блоке прав добавленного юнита -------
(function () {
  "use strict";
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var code = document.getElementById(btn.dataset.copy);
    if (!code || !navigator.clipboard) return;
    navigator.clipboard.writeText(code.textContent).then(function () {
      var old = btn.textContent;
      btn.textContent = btn.dataset.copyOk || "OK";
      setTimeout(function () { btn.textContent = old; }, 1200);
    }).catch(function () { /* нет доступа к буферу — текст можно выделить вручную */ });
  });
})();
