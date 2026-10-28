// Внешний скрипт панели (CSP script-src 'self': inline-скрипты запрещены).
// SSE-подписка + режим «sort» (перетаскивание карточек сервисов мышью).
(function () {
  "use strict";
  if (!document.body) return;
  var root = document.getElementById("sse-root");
  var sseWarn = document.getElementById("sse-warn");
  var orderBtn = document.getElementById("order-btn");
  var orderLabel = document.getElementById("order-btn-label");
  var csrf = document.body.dataset.csrf || "";
  var order = { active: false };   // состояние режима «sort»

  function servicesContainer() {
    return document.getElementById("services-live") || document.getElementById("services-init");
  }

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
      if (order.active) return;   // в режиме «sort» SSE не перерисовывает карточки
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

  // ------------------- sort: порядок карточек сервисов --------------------
  // Перетаскивание на mouse-событиях: надёжно для всех карточек с кнопками
  // и текстом (HTML5 drag & drop в таких картах работает нестабильно).
  if (!orderBtn) return;

  var drag = null;   // { card, x, y, moved } — текущее перетаскивание

  function cards(c) { return Array.prototype.slice.call(c.querySelectorAll(".service-card")); }

  function setLabel(saveMode) {
    if (orderLabel) orderLabel.textContent = saveMode ? (orderBtn.dataset.labelSave || "save") : (orderBtn.dataset.label || "sort");
  }

  function enterOrder() {
    var c = servicesContainer();
    if (!c) { toast(orderBtn.dataset.toastNocards || "", "err"); return; }
    order.active = true;
    setLabel(true);
    orderBtn.classList.add("order-active");
    c.classList.add("order-mode");
    if (!c.dataset.orderBound) {
      c.dataset.orderBound = "1";
      c.addEventListener("mousedown", onDown);
    }
  }

  function exitOrder(msgAndKind) {
    order.active = false;
    orderBtn.classList.remove("order-active");
    setLabel(false);
    var c = servicesContainer();
    if (c) c.classList.remove("order-mode");
    if (drag) { drag.card.classList.remove("drag-src"); drag = null; }
    document.body.classList.remove("order-dragging");
    if (msgAndKind) toast(msgAndKind[0], msgAndKind[1]);
  }

  function onDown(e) {
    if (e.button !== 0 || !order.active) return;
    var t = e.target;
    var card = null;
    while (t && t !== document.body) {
      if (t.classList && t.classList.contains("service-card")) { card = t; break; }
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
    while (under && !(under.classList && under.classList.contains("service-card"))) under = under.parentElement;
    if (under && under !== drag.card && under.parentNode) {
      // карточки — дети .service-grid внутри секции: вставляем в их реального родителя
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

  function saveOrder() {
    var c = servicesContainer();
    if (!c) { exitOrder(null); return; }
    var ids = cards(c).map(function (card) { return card.id.replace(/^svc-/, ""); });
    var fd = new FormData();
    fd.append("order", ids.join(","));
    fd.append("csrf", csrf);
    fetch("/ui/order", { method: "POST", body: fd, credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) return r.text().then(function (t) { throw new Error(t || ("HTTP " + r.status)); });
        exitOrder([orderBtn.dataset.toastOk || "", "ok"]);
      })
      .catch(function (e) {
        exitOrder([(orderBtn.dataset.toastErr || "") + (e && e.message || e), "err"]);
      });
  }

  orderBtn.addEventListener("click", function () {
    if (order.active) { saveOrder(); } else { enterOrder(); }
  });
  document.addEventListener("keydown", function (e) {
    if (order.active && e.key === "Escape") exitOrder("");
  });
})();