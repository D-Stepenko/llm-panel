#!/usr/bin/env bash
# Уточняющее read-only обследование (round 2) — закрыть пробелы round 1.
# Ничего не меняет, секреты не запрашивает. Вывод небольшими секциями.
set -u

hr() { echo; echo "=== $* ==="; }

hr "A. Свойства трёх llama-юнитов + speaches + gpu-manager"
for u in llama-qwen38_gguf.service llama-Qwen3.8_GGUF.service llama-qwen38-flash.service speaches.service gpu-manager.service; do
  echo "--- $u"
  systemctl show "$u" --no-pager \
    --property=User,Group,WorkingDirectory,Environment,ExecStart,ExecStartPre,ExecStartPost,StopSignal,Restart,UnitFileState,ActiveState,SubState 2>/dev/null
done

hr "B. Полные unit-файлы llama-юнитов (systemctl cat, read-only)"
for u in llama-qwen38_gguf.service llama-Qwen3.8_GGUF.service llama-qwen38-flash.service; do
  echo "--- $u"; systemctl cat "$u" --no-pager 2>/dev/null | head -40
done

hr "C. Переменные окружения running-процессов llama-server (CVD; без секретов)"
for pid in $(pgrep -f llama-server); do
  echo "--- llama-server pid=$pid"
  tr '\0' '\n' < /proc/$pid/environ 2>/dev/null | grep -Ei 'CUDA_VISIBLE|CUDA_DEVICE' || echo "(нет CVD в окружении)"
  tr '\0' '\n' < /proc/$pid/cmdline 2>/dev/null | head -20
done

hr "D. Владельцы неизвестных портов (8888, 8001, 18789, 8010)"
sudo -n ss -ltnp 2>/dev/null | grep -E ':(8888|8001|18789|8010|18769)\b' || ss -ltnp | grep -E ':(8888|8001|18789|8010|18769)\b'
for p in $(ss -ltnp 2>/dev/null | grep -E ':(8888|8001|18789|8010)\b' | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u); do
  echo "--- pid $p"; ps -o pid,user,cmd -p $p --no-headers 2>/dev/null
done

hr "E. Health-эндпоинты фактические (read-only curl, только коды)"
for x in "vLLM 8000 /health" "vLLM 8000 /v1/models" "llama 8002 /health" "webui 8080 /health" "tts 8003 /health"; do
  set -- $x
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$2$3" 2>/dev/null || echo ERR)
  echo "$1 ($2$3): $code"
done

hr "F. Maya: движок/скрипт запуска (найти, содержимое НЕ выводим целиком)"
ps aux | grep -vi grep | grep -iE 'maya|glm' | head -5 || echo "(процесс Maya не запущен)"
find /home/dispot/bin /home/dispot/src /opt /home/dispot -maxdepth 3 -iname '*maya*' -type f 2>/dev/null | head -10
ls -la /home/dispot/models/maya 2>/dev/null | head -15

hr "G. llama.cpp: полная версия (head --version)"
/opt/llama.cpp/build/bin/llama-server --version 2>&1 | head -6

hr "H. systemd-oomd: фактические политики"
systemctl is-active systemd-oomd 2>/dev/null
ls /usr/lib/systemd/systemd-oomd-defaults/ 2>/dev/null
grep -rEi 'ManagedOOM|MemoryMax' /etc/systemd/system/ 2>/dev/null | head -8 || true

hr "I. sudo -l (интерактивно; нужно для оценки шага sudoers; можно прервать Ctrl-C)"
sudo -l 2>/dev/null | head -20 || echo "(без пароля не выполнено — пропущено)"

hr "Конец round 2. Вывод приложить к отчёту."
