#!/usr/bin/env bash
# read-only обследование LLM-станции (этап 1, ТЗ §12.1–12.2).
# НИЧЕГО НЕ МЕНЯЕТ: только чтение. Не собирает секреты (токены/пароли не запрашиваются).
# Вывод по секциям небольшой (grep/head/wc). Запуск: bash scripts/survey.sh > survey.out 2>&1
set -u

hr() { echo; echo "=== $* ==="; }

hr "0. Базовое"
uname -a
lsb_release -a 2>/dev/null | head -4
id
python3 --version
which python3; which pip3

hr "1. GPU: постоянные UUID + индексы (индексы меняются, UUID — нет; ТЗ §2)"
nvidia-smi --query-gpu=index,uuid,name,memory.total --format=csv

hr "2. Юниты: полный список сервисов (выборка по ключевым словам)"
systemctl list-unit-files --type=service --no-pager | grep -Ei 'vllm|llama|maya|tts|webui|web-ui|gpu|power|caddy|speach|searx|panel' || true

hr "3. Свойства известных юнитов (проверить реальные имена, User, ExecStart)"
for u in vllm.service llama-cpp.service llama_cpp.service llama.service open-webui.service Qwen3-TTS.service tts-proxy.service gpu-power-limit.service llmp-*.service; do
  if systemctl list-unit-files "$u" 2>/dev/null | grep -q "$u"; then
    echo "--- $u"
    systemctl show "$u" --no-pager \
      --property=Id,User,Group,WorkingDirectory,Environment,ExecStart,ExecCondition,StopSignal,TimeoutStopUSec,MemoryMax,Restart,UnitFileState,ActiveState,SubState,Result 2>/dev/null
  else
    echo "--- $u : ЮНИТ НЕ НАЙДЕН (имя проверить фактическим списком секции 2)"
  fi
done

hr "4. drop-in'ы существующих юнитов (если есть)"
for d in /etc/systemd/system/*.service.d; do
  [ -d "$d" ] && find "$d" -type f -maxdepth 1 -print -exec head -40 {} \;
done 2>/dev/null || true

hr "5. Порты прослушивания (адрес:порт; имена процессов вне своего UID скрыты ядром)"
ss -ltnp 2>/dev/null | head -40 || ss -ltn | head -40

hr "6. Maya: найти процесс и скрипт запуска (путь читаем, содержимое НЕ выводим целиком)"
ps aux | grep -iE 'maya|mayaproj' | grep -v grep | head -5 || true

hr "7. llama.cpp: версия и флаги установленного бинарника (ТЗ §5.6: подтверждать флагами --help)"
ls -la /opt/llama.cpp/build/bin/ 2>/dev/null | head -10
/opt/llama.cpp/build/bin/llama-server --version 2>/dev/null || true
/opt/llama.cpp/build/bin/llama-server --help 2>&1 | head -25 || true

hr "8. vLLM: пакет и версия (в том venv, где реально стоит)"
for p in /opt/vllm* /home/dispot/vllm* /home/dispot/venvs/* /home/dispot/.venv*; do
  [ -d "$p" ] && echo "каталог: $p"
done 2>/dev/null | head -10
systemctl cat vllm.service 2>/dev/null | head -30 || echo "vllm.service не найден"

hr "9. systemd-oomd: настройки (MemoryMax/MemoryPressureWatching)"
systemctl status systemd-oomd --no-pager 2>/dev/null | head -8
grep -rEi 'MemoryMax|ManagedOOM' /etc/systemd/system/ /etc/systemd/system.control/ 2>/dev/null | head -10
cat /usr/lib/systemd/systemd-oomd-defaults 2>/dev/null | head -5 || ls -la /usr/lib/systemd/systemd-oomd-defaults 2>/dev/null || true

hr "10. Caddy: где конфиг, какие hosts/upstreams (без доменных сертификатов)"
which caddy && caddy version 2>/dev/null
systemctl cat caddy --no-pager 2>/dev/null | grep -E 'ExecStart|Environment' | head -5
for c in /etc/caddy/Caddyfile /etc/caddy/*.json; do
  [ -e "$c" ] && echo "--- $c" && grep -vE '^\s*$' "$c" | head -40
done

hr "11. Модели (только имена/размеры, содержимое не выводим)"
ls -la ~/models 2>/dev/null | head -20
find ~/models -maxdepth 2 -name config.json 2>/dev/null | head -10
find ~/models -maxdepth 2 -name '*.gguf' 2>/dev/null | head -10
df -h ~/models 2>/dev/null

hr "12. Права dispot (sudo -l; только список разрешений)"
sudo -n -l 2>/dev/null | head -20 || echo "(sudo -n недоступен без пароля — нормально для read-only)"

hr "13. UFW/сеть (sudo может спросить пароль — секцию прервать Ctrl-C безопасно)"
sudo -n ufw status verbose 2>/dev/null | head -25 || true

hr "Конец. Приложите вывод целиком к отчёту для реализации этапа 1."
