// Логи сервиса: фильтр + поток (CSP script-src 'self' — скрипт вынесен из шаблона).
// Параметры берутся из data-атрибутов #log (data-stream).
(function () {
  const pre = document.getElementById("log");
  if (!pre) return;
  const filter = document.getElementById("log-filter");
  const btn = document.getElementById("follow-btn");
  const streamURL = pre.dataset.stream;
  const followLabel = (btn && btn.dataset.follow) || "Follow";
  const stopLabel = (btn && btn.dataset.stop) || "Stop";
  const errPrefix = pre.dataset.err || "Stream error: ";
  let es = null;

  // Сервер отдаёт журнал одним текстовым узлом; фильтр и поток работают
  // построчно — нормализуем в <div> на строку один раз при загрузке.
  if (pre.textContent && pre.children.length === 0) {
    const lines = pre.textContent.split("\n");
    pre.textContent = "";
    for (const ln of lines) {
      const div = document.createElement("div");
      div.textContent = ln;
      pre.appendChild(div);
    }
  }

  function applyFilter() {
    const f = (filter.value || "").toLowerCase();
    for (const line of pre.children) {           // строки — <div>
      line.hidden = f && !line.textContent.toLowerCase().includes(f);
    }
    if (!f) pre.scrollTop = pre.scrollHeight;
  }
  if (filter) {
    filter.addEventListener("input", applyFilter);
    applyFilter();                               // значение могло прийти из GET ?filter=
  }

  if (!btn || !streamURL) return;

  function stop() { if (es) { es.close(); es = null; } btn.textContent = followLabel; }

  btn.addEventListener("click", () => {
    if (es) { stop(); return; }
    es = new EventSource(streamURL);
    btn.textContent = stopLabel;
    pre.textContent = "";
    es.addEventListener("log", e => {
      const div = document.createElement("div");
      div.textContent = String(e.data);
      pre.appendChild(div);
      while (pre.childNodes.length > 2000) pre.removeChild(pre.firstChild);
      const f = (filter.value || "").toLowerCase();
      div.hidden = f && !div.textContent.toLowerCase().includes(f);
      if (!f) pre.scrollTop = pre.scrollHeight;
    });
    es.addEventListener("error", e => {
      const div = document.createElement("div");
      div.className = "log-error";
      // native-ошибки EventSource приходят БЕЗ data — это просто обрыв соединения;
      // осмысленная причина приходит отдельным событием "error" с data от сервера
      const reason = (e.data != null && String(e.data).trim() !== "")
        ? String(e.data)
        : (pre.dataset.noconn || "connection closed");
      div.textContent = errPrefix + reason;
      pre.appendChild(div);
      stop();
    });
    es.addEventListener("end", () => stop());
  });
})();
