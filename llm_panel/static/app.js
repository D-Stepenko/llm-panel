// Внешний скрипт панели (CSP script-src 'self': inline-скрипты запрещены).
// SSE-подписка + режим «Порядок» карточек сервисов.
(function () {
  "use strict";
  if (!document.body) return;
  var root = document.getElementById("sse-root");
  var sseWarn = document.getElementById("sse-warn");
  var orderBtn = document.getElementById("order-btn");
  var csrf = document.body.dataset.csrf || "";
  var order = { active: false, dragEl: null };   // состояние режима «Порядок»

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
      if (order.active) return;   // в режиме «Порядок» SSE не перерисовывает карточки
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

  // ------------------- Порядок карточек сервисов --------------------------
  if (!orderBtn) return;

  function cards(c) { return Array.prototype.slice.call(c.querySelectorAll(".service-card")); }

  function enterOrder() {
    var c = servicesContainer();
    if (!c) { toast("Карточки сервисов не найдены", "err"); return; }
    order.active = true;
    orderBtn.textContent = "Запомнить порядок";
    orderBtn.classList.add("order-active");
    c.classList.add("order-mode");
    if (!c.dataset.orderBound) {
      c.dataset.orderBound = "1";
      c.addEventListener("dragover", function (e) { e.preventDefault(); });
      c.addEventListener("drop", onDrop);
    }
    cards(c).forEach(function (card) {
      card.draggable = true;
      card.addEventListener("dragstart", onDragStart);
      card.addEventListener("dragend", onDragEnd);
    });
  }

  function exitOrder(msgAndKind) {
    order.active = false;
    order.dragEl = null;
    orderBtn.textContent = "Порядок";
    orderBtn.classList.remove("order-active");
    var c = servicesContainer();
    if (c) {
      c.classList.remove("order-mode");
      cards(c).forEach(function (card) {
        card.draggable = false;
        card.classList.remove("drag-src");
      });
    }
    if (msgAndKind) toast(msgAndKind[0], msgAndKind[1]);
  }

  function onDragStart(e) {
    order.dragEl = e.currentTarget;
    e.currentTarget.classList.add("drag-src");
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", ""); } catch (err) {}
    }
  }

  function onDragEnd() {
    if (order.dragEl) order.dragEl.classList.remove("drag-src");
    order.dragEl = null;
  }

  function onDrop(e) {
    e.preventDefault();
    if (!order.active || !order.dragEl) return;
    var card = e.target;
    while (card && !card.classList.contains("service-card")) card = card.parentElement;
    if (!card || card === order.dragEl) return;
    var rect = card.getBoundingClientRect();
    var before = (e.clientY - rect.top) / rect.height < 0.5;
    var c = servicesContainer();
    if (c) c.insertBefore(order.dragEl, before ? card : card.nextSibling);
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
        exitOrder(["Порядок сохранён", "ok"]);
      })
      .catch(function (e) {
        exitOrder(["Не удалось сохранить порядок: " + (e && e.message || e), "err"]);
      });
  }

  orderBtn.addEventListener("click", function () {
    if (order.active) { saveOrder(); } else { enterOrder(); }
  });
  document.addEventListener("keydown", function (e) {
    if (order.active && e.key === "Escape") exitOrder("");
  });
})();
