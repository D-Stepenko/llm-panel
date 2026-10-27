// Логи сервиса: фильтр + поток (CSP script-src 'self' — скрипт вынесен из шаблона).
// Параметры берутся из data-атрибутов #log (data-stream).
(function () {
  const pre = document.getElementById("log");
  if (!pre) return;
  const filter = document.getElementById("log-filter");
  const btn = document.getElementById("follow-btn");
  const streamURL = pre.dataset.stream;
  let es = null;

  function applyFilter() {
    const f = (filter.value || "").toLowerCase();
    for (const line of pre.children) {           // при follow строки — <div>
      line.hidden = f && !line.textContent.toLowerCase().includes(f);
    }
  }
  if (filter) filter.addEventListener("input", applyFilter);

  if (!btn || !streamURL) return;

  function stop() { if (es) { es.close(); es = null; } btn.textContent = "Поток"; }

  btn.addEventListener("click", () => {
    if (es) { stop(); return; }
    es = new EventSource(streamURL);
    btn.textContent = "Стоп";
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
      div.textContent = "ОШИБКА потока: " + String(e.data || "");
      pre.appendChild(div);
      stop();
    });
    es.addEventListener("end", () => stop());
  });
})();
