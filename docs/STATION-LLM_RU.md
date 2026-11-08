# Станция-LLM: факты обследования и история установки

Конкретика реального сервера (dispot@llm, 10.10.100.44, Ubuntu 24.04.5), на котором
панель разрабатывалась и впервые развёрнута. Для универсальной установки — docs/DEPLOY_RU.md.

## 1. Оборудование и окружение

- Ubuntu 24.04.5, Python 3.12.3, пользователь `dispot` (sudo (ALL:ALL) ALL с паролем).
- GPU (UUID→индексы на момент обследования 2026-10-26):
  0 → GPU-9f764cdf-08bd-48a9-c219-a8660a71c1d6 (32 ГБ),
  1 → GPU-ca13b019-b898-2c2b-8c0a-2b92f64e6476 (32 ГБ),
  2 → GPU-cfec683a-1069-2f9c-0425-2e26192519e6 (16 ГБ).
- Caddy 2.6.2 (`tls internal`), upstreams: панель 127.0.0.1:8443 (порт 8444 наружу),
  tts-proxy 8004 (/tts/*), open-webui 8080.
- `systemd-oomd` — inactive; `MemoryMax=infinity` в юнитах ведёт себя предсказуемо,
  но защиты давлением (oomd) нет.
- Порты `127.0.0.1:8888` и Node `127.0.0.1:18789` (через Caddy `18769`) принадлежат
  dispot, назначение не идентифицировано; в панель не добавлены (не systemd-юниты).

## 2. Юниты (round 1 + round 2, 2026-10-26/27)

- `vllm.service`: enabled/running, `User=dispot`, `ExecStart=/home/dispot/bin/start-vllm.sh`
  (conda env 1cat-vllm-sm70_v1.5.0, CUDA 12.8, tensor-parallel-size 2, max-model-len
  262144, api-key из ~/.vllm_api_key — секрет остаётся в файле, панель его не читает),
  0.0.0.0:8000, `/health` 200 (`/v1/models` — 401), Restart=on-failure/30,
  Requires=gpu-power-limit.service. GPU 0,1 по индексам (CUDA_VISIBLE_DEVICES=0,1).
- `llama-qwen38_gguf.service`: enabled/running, `User=dispot`, llama.cpp 0.4.1-dev
  (build 11141), бинарник /opt/llama.cpp/build/bin/llama-server, модель
  Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf, 16 ГБ карта по UUID, контекст 82144, 0.0.0.0:8002,
  `/health` 200. Conflicts=Qwen3-TTS.service.
- `llama-Qwen3.8_GGUF.service`: почти дубль основного (alias qwen38-GGUF, контекст
  62144), disabled. В Description описка («Flash»). Conflicts=Qwen3-TTS.service.
- `llama-qwen38-flash.service`: disabled, 2 шарда, `--lazy-mode on`, контекст 262144,
  все три GPU по UUID, Conflicts=vllm.service (совместный запуск с vLLM блокирует
  systemd). Порт 8002 общий у всех llama-юнитов.
- `speaches.service`: enabled/running, `User=dispot`, uvicorn 127.0.0.1:8001,
  `CUDA_VISIBLE_DEVICES=` (пусто — CPU).
- `open-webui.service`: enabled/running, `User=dispot`, 0.0.0.0:8080 (РАСХОЖДЕНИЕ:
  ожидался 127.0.0.1 — прямой доступ из LAN минуя Caddy не закрыт), `/health` 200.
- `Qwen3-TTS.service`: inactive/disabled, БЕЗ `User=` → работает от root; drop-in
  через панель запрещён (ТЗ §5.5), только управление/логи. 16 ГБ карта по UUID.
- `tts-proxy.service`: inactive/disabled, `User=dispot`, 127.0.0.1:8004.
- `gpu-power-limit.service`: oneshot, `nvidia-smi -pm 1 -pl 150`, без CVD; только
  просмотр (allowlist: status+journal).
- **Maya**: v1.1.23, движок /home/dispot/src/project-maya/maya.sh (maya.py, build/maya,
  конфиг maya-maya-m.json), авто-разбивка по слоям [0,1,2], первая загрузка «минуты»,
  остановка SIGINT. Модель: шардированный ~/models/maya/GLM-5.3-Flash-Maya-M-IQ2_S-*3.
  Порт и флаги запуска НЕ подтверждены (нужен вывод `./maya.sh --help` перед этапом 2/3).

**Конвенция адресации CUDA (владелец):** vLLM — по индексам, llama.cpp/TTS — по
полным UUID. В services.yaml это поле `cuda_addressing: index|uuid`; плановая
привязка в панели всегда по UUID, индексы для index-сервисов выводятся заново из
NVML на момент запуска.

**Расхождения с ТЗ (решение владельца):**
1. Open WebUI и llama-server слушают 0.0.0.0 (8080, 8002) — владелец закрывает сам.
2. `Qwen3-TTS.service` без `User=` (root) — редактирование юнита через панель
   отклоняется навсегда.

## 3. История установки (2026-10-27) и инциденты

- Установка по ТЗ §12: обследование → код/venv в /home/dispot/llm-panel → каталоги
  /etc/llm-panel, /var/lib/llm-panel (0700) → allowlist/helper → config/services →
  init-db/set-password → юнит панели → Caddy 8444.
- **2026-10-28, инцидент «helper завершился с кодом 1»**: юнит панели на станции был
  развёрнут с sandbox-опциями (`NoNewPrivileges=yes`, `ProtectSystem=strict`,
  `ProtectHome=read-only`, `PrivateTmp=yes`). `NoNewPrivileges` блокирует sudo
  (setuid) → `sudo -n helper` падал с кодом 1 при каждом действии, при этом из
  интерактивного shell всё работало. Диагноз по auth-логу (отказов sudo там нет —
  sudo отвергается до аутентификации) и `systemctl show`. Исправление: убрать
  `NoNewPrivileges=yes`, `daemon-reload`, restart. Урок зашит в
  `deploy/llm-panel.service.template`.
- **2026-10-29, инцидент «HF-загрузка: Errno 30 EROFS»**: `/home/dispot/models`
  принадлежит dispot и доступна на запись, но процесс панели (и его дочерний
  `hf download`) живёт в песочнице юнита (`ProtectSystem=strict`,
  `ProtectHome=read-only`) → весь /home read-only вне `ReadWritePaths`.
  Исправление: добавить `ReadWritePaths=/home/dispot/models` в [Service]
  юнита llm-panel.service → `daemon-reload` → restart. Панель ловит это
  заранее пробной записью и показывает инструкцию (v0.3.3).
- sudoers `/etc/sudoers.d/llm-panel` (dispot → helper, NOPASSWD) установлен
  2026-10-28 с подтверждения владельца, `visudo -c` — OK.
- Версия 0.1.1: универсализация (install.sh, шаблоны деплоя, диски в Панели
  настроек, i18n-фиксы), docs/.

## 4. Обновление станции из репозитория

git-push на сервере недоступен без PAT — файлы копируются вручную из репозитория
(на ПК разработчика) в `/home/dispot/llm-panel` (`.venv` НЕ заменять), затем
`sudo systemctl restart llm-panel` и Ctrl+F5 в браузере. После настройки
`git pull`-обновления — см. docs/DEPLOY_RU.md §7.
