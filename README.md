# llm-panel — локальная веб-панель управления LLM-станцией

Реализация по ТЗ `llm-panel-tz-reviewed.md` (ред. 1.1). **Статус: Этап 1 (MVP)** —
вход и защита, статусы adopted-сервисов (`manage_only`), start/stop/restart через
ограниченный привилегированный helper, логи, сводка GPU/RAM/CPU/диск, SSE-обновления.

Реализовано этапов: **1 из 4**. Редактор конфигураций, каталог моделей, HF-загрузки,
мастер «модель → сервис», Open WebUI-интеграция — этапы **2–4**, сейчас их нет.

## 1. Стек и принципы

- Backend: Python 3.12, FastAPI, Uvicorn (один процесс), Jinja2 (без npm/сборки).
- Метрики: `nvidia-ml-py` (NVML) + `psutil`; общий фоновый сборщик раз в 3 c, SSE всем клиентам.
- Хранилище: SQLite (пользователи, сессии, попытки входа, аудит).
- Пароли: Argon2. Изменяющие запросы: POST + CSRF. GET не меняет состояние.
- **root-операции** — только через root-owned helper `/usr/local/sbin/llm-panel-systemd`
  (права `dispot` напрямую не выдаются: в sudoers ровно одна строка на helper).
- Секреты на веб-слое не выводятся; логи и скачиваемые журналы маскируют
  `hf_…`-токены/JWT/Bearer/sk-ключи (`redact_patterns` в конфигурации).

## 2. Структура

```
llm-panel/
├── llm_panel/                 код панели
│   ├── cli.py                 llm-panel init-db | set-password | lockout-reset | check-config | serve
│   ├── app.py                 FastAPI: Host/CSRF/аутентификация, SSE
│   ├── config.py   registry.py    загрузка и валидация config.yaml / services.yaml
│   ├── db.py       security.py    SQLite, сессии, блокировка входа
│   ├── systemd_units.py health.py  read-only статус systemd; health-проверки
│   ├── collector.py sse.py       общий 3-секундный сборщик, шина событий
│   ├── metrics.py                NVML + psutil («нет данных», а не выдумки)
│   ├── helper_client.py          вызов helper через sudo -n, argv без shell
│   ├── routes/{auth,dashboard,services,logs}.py
│   └── templates/  static/
├── deploy/
│   ├── helper/llm-panel-systemd   root-owned helper (ставится на станцию)
│   ├── allowlist.json.example     root-owned allowlist юнитов
│   ├── services.yaml.example      описания сервисов (заглушки имён юнитов!)
│   ├── config.yaml.example        /etc/llm-panel/config.yaml
│   ├── llm-panel.service          юнит самой панели (sandbox с осторожностью)
│   └── sudoers.llm-panel          предложение sudoers (только строка helper)
├── scripts/survey.sh          read-only обследование станции
├── tests/                     pytest: helper, статусы, конфиги, вход/CSRF/аудит
└── config.dev.yaml            конфиг для машины разработки
```

## 3. Локальная разработка (Windows/Linux без systemd)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt          # Linux: .venv/bin/pip
.venv/Scripts/python -m llm_panel --config config.dev.yaml init-db
.venv/Scripts/python -m llm_panel --config config.dev.yaml set-password
.venv/Scripts/python -m llm_panel --config config.dev.yaml serve
# открывать http://localhost:8000  (SSE/статусы systemd будут «нет данных»)
```

Тесты: `.venv/Scripts/python -m pytest tests -q` (57 тестов, все зелёные на этапе 1).

## 4. Установка на станцию (порядок ТЗ §12 + фактический деплой)

Порядок требований ТЗ §12: сначала read-only обследование и план, затем изменения.
Ниже — последовательность, фактически применённая на станции 2026-10-27
(LLM-станция Ubuntu 24.04.5, пользователь `dispot`):

1. **Обследование** (read-only, ничего не меняет):
   `bash scripts/survey.sh > survey.out 2>&1` и уточняющий `scripts/survey2.sh` —
   вывод использован для заполнения реальных имён юнитов, портов, `ExecStart`,
   `User`, GPU-UUID в `services.yaml`/`allowlist.json` (факты — раздел 8).
2. Код и venv в домашнем каталоге: `/home/dispot/llm-panel`, окружение `/home/dispot/llm-panel/.venv`
   (не системный Python, ТЗ §7):
   ```bash
   git clone https://github.com/D-Stepenko/llm-panel.git /home/dispot/llm-panel
   cd /home/dispot/llm-panel
   python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
   ```
3. Каталоги: `sudo mkdir -p /etc/llm-panel /var/lib/llm-panel && sudo chown dispot:dispot /var/lib/llm-panel && chmod 700 /var/lib/llm-panel`
4. `sudo install -m 644 deploy/allowlist.json.example /etc/llm-panel/allowlist.json`
   затем **отредактировать по фактическим именам юнитов**.
   `sudo chown root:root /etc/llm-panel/allowlist.json` — root-owned, проверяется helper'ом.
5. Helper: `sudo install -o root -g root -m 755 deploy/helper/llm-panel-systemd /usr/local/sbin/llm-panel-systemd`
   (проверить, что ни один каталог в пути не доступен `dispot` на запись).
6. **sudoers — отдельное подтверждение (ТЗ §12.5).** Изменение даёт `dispot` право
   управления разрешёнными юнитами через helper без пароля.
   Установка: `sudo visudo -f /etc/sudoers.d/llm-panel` → содержимое
   `deploy/sudoers.llm-panel`, затем `sudo visudo -c`. Риск: dispot получает
   управляемый доступ к allowlisted юнитам (второй рубеж проверяет helper; вне
   allowlist — отказ). Откат: `sudo rm /etc/sudoers.d/llm-panel`.
7. `sudo install -m 644 deploy/config.yaml.example /etc/llm-panel/config.yaml` —
   заполнить реальные пути/порты/roots (`db_path: /var/lib/llm-panel/panel.db`,
   порт панели `8443` на `127.0.0.1`).
8. `sudo install -m 644 deploy/services.yaml.example /etc/llm-panel/services.yaml`
   — заполнить фактическими юнитами/портами/health-URL (все значения из обследования).
9. База и пароль — **обязательно с той же конфигурацией, что у панели**
   (пароль, заданный с другим `--config`, попадает в другую SQLite-базу):
   ```bash
   .venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml init-db
   .venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml set-password
   ```
10. systemd-юнит панели (в репо: `deploy/llm-panel.service`, запуск от `dispot`,
    рабочая папка `/home/dispot/llm-panel`):
    ```bash
    sudo install -m 644 deploy/llm-panel.service /etc/systemd/system/llm-panel.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now llm-panel
    ```
11. Caddy: отдельный сайт-блок с собственным портом (под-путь `/panel/*` не
    используется — абсолютные пути панели `/login`, `/static/…` под ним не работают):
    ```
    10.10.100.44:8444 {
        tls internal
        @lan remote_ip 10.10.100.0/24
        handle @lan {
            reverse_proxy 127.0.0.1:8443
        }
    }
    ```
    После `sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy`
    панель доступна как `https://10.10.100.44:8444/` (только из LAN-подсетей).
    UFW для 8443/upstream не открывать; при необходимости разрешить 8444 лишь
    с адресов LAN.

**Обновление панели из git (новая версия кода):**
```bash
cd /home/dispot/llm-panel
git pull                      # код обновлён, конфиги в /etc и БД в /var/lib не трогаются
.venv/bin/pip install -r requirements.txt   # если requirements менялся
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml check-config
sudo systemctl restart llm-panel
```

## 5. Аварийные процедуры

- **Отключить панель, не тронув LLM-сервисы (ТЗ §7):**
  `sudo systemctl disable --now llm-panel` — панель падение/остановка сами
  останавливают-service юниты НЕ будут (панель не «владеет» ими).
- **Восстановление доступа** (блокировка входа):
  `/home/dispot/llm-panel/.venv/bin/llm-panel --config /etc/llm-panel/config.yaml lockout-reset`
- **Сброс пароля**: `llm-panel set-password` (интерактивно; все сессии инвалидируются).
- **Бэкап**: `panel.db` (WAL) + `/etc/llm-panel/config.yaml`, `services.yaml`,
  `allowlist.json`. Восстановление: остановить панель, скопировать файлы, права
  сохранять (`0600` на БД, root-owned на allowlist), `daemon-reload`, старт.
- **Откат helper/sudoers**: удалить файлы, `visudo -c`, `daemon-reload`.

## 6. Проверки на станции после установки (критерии приёмки, этап 1)

```bash
systemctl status llm-panel --no-pager           # панель стартовала от dispot
ss -ltnp | grep 8443                            # слушает ТОЛЬКО 127.0.0.1
curl -sk https://10.10.100.44:8444/ -o /dev/null -w '%{http_code}\n'   # через Caddy — 302/200
# с другой LAN-машины (не станции): прямое подключение к 127.0.0.1:8443 невозможно
curl -m 3 http://10.10.100.44:8443/             # ожидание: отказ/таймаут
# ТЗ §10 п.15: остановка панели сервисы не трогает
sudo systemctl stop llm-panel && systemctl is-active vllm.service
# ТЗ §10 п.2: старт/стоп через UI без пароля, но только для allowlisted юнитов
sudo -n /usr/local/sbin/llm-panel-systemd status vllm.service
sudo -n /usr/local/sbin/llm-panel-systemd start stranger.service   # ожидание: ОТКАЗ (rc=3)
```

## 7. Известные ограничения этапа 1

- Имена юнитов/портов/health-URL в deploy-файлах — фактические после round 1
  (см. раздел 8); три пункта остаются на `survey2.sh` (см. там).
- Редактора конфигураций нет (этап 2); нет матрицы GPU, конфликтов RAM/GPU-режима
  «предупреждение» (сейчас просто показ конфигурации), нет каталога моделей,
  HF-загрузок, мастера, Open WebUI-интеграции.
- health-проверка `readiness_timeout` не убивает процесс (по ТЗ); кнопки «Стоп» доступны вручную.
- «unhealthy»-история хранится в памяти панели; после рестарта панели сервис,
  который был активен и никогда не проходил health, без health-конфигурации покажет
  «Юнит активен; health-проверка не настроена» (ТЗ §4.2: не подменять).
- Тесты на Windows-машине проверяют логику, но НЕ systemd/NVML (недоступны) —
  обследование и приёмка только на станции.
- SSE-обновление отказоустойчиво к отключениям клиентов, но «unhealthy»-переход
  отслеживается только пока панель работает.

## 8. Факты обследования станции

### Round 2 (2026-10-27, scripts/survey2.sh) — уточнения

- llama.cpp **0.4.1-dev (build 11141)**, бинарник `/opt/llama.cpp/build/bin/llama-server`.
- `llama-qwen38_gguf.service`: `User=dispot`, модель `Qwen3.8-27B-GSQ-RCO-IQ3_S.gguf`
  (один файл), 16 ГБ карта по UUID, контекст 82144, `/health` = 200.
- `llama-qwen38_gguf.service`: почти дубль основного (другой alias/контекст),
  `Conflicts=Qwen3-TTS.service`; в Description описка («Flash»).
- `llama-qwen38-flash.service`: 2 шарда, `--lazy-mode on`, контекст 262144,
  **все три GPU по UUID**, `Conflicts=vllm.service` — совместный запуск с
  vLLM блокирует systemd; порт 8002 общий у всех llama-юнитов.
- `speaches.service`: uvicorn `127.0.0.1:8001`, `CUDA_VISIBLE_DEVICES=`` (пусто,
  CPU), `User=dispot` — добавлена в services/allowlist.
- Health-эндпоинты: vLLM `/health` 200 (`/v1/models` — 401, нужен api-key —
  секрет остаётся в файле, panel его не читает), llama `/health` 200,
  Open WebUI `/health` 200; Qwen3-TTS — сервис неактивен (проверка только при старте).
- **Maya**: v1.1.23, движок `/home/dispot/src/project-maya/maya.sh`
  (см. `maya.py`, `build/maya`, конфиг `maya-maya-m.json`), авто-разбивка по
  слоям `[0,1,2]`, первая загрузка «минуты», остановка SIGINT (тз совпадает).
  Модель: шардированный `~/models/maya/GLM-5.3-Flash-Maya-M-IQ2_S-*3`
  (`+pack/`, `vision/`, `.maya-verified.json`). Порт и флаги запуска —
  ещё не подтверждены (нужен вывод `./maya.sh --help`/`maya.py --help` перед этапом 2/3).
- `systemd-oomd` — **inactive**; `MemoryMax=infinity` в юнитах ведёт себя
  предсказуемо (cgroup limit), но защиты давлением (oomd) на станции нет.
- `sudo -l` (с паролем): `dispot` имеет `(ALL : ALL) ALL` — полноценный sudo
  уже есть с паролем. Хелпер + sudoers NOPASSWD для одного пути расширяет
  полномочия умерленно, но формально добавляет безпарольную root-операцию
  на фикс. путь — риски описаны в установке ниже.
- Порт `127.0.0.1:8888` и Node-процесс на `127.0.0.1:18789` (через Caddy
  `18769`) принадлежат dispot, назначение не идентифицировано; в панель их
  не добавляем (не systemd-юниты).

### Round 1 (2026-10-26, scripts/survey.sh)

Подтверждено: Ubuntu 24.04.5, Python 3.12.3; vLLM юнит `vllm.service`
(`User=dispot`, `ExecStart=/home/dispot/bin/start-vllm.sh`, conda env
`1cat-vllm-sm70_v1.5.0`, CUDA 12.8, порт 8000); llama.cpp юниты
`llama-qwen38_gguf` (enabled/running, 0.0.0.0:8002),
`llama-Qwen3.8_GGUF`, `llama-qwen38-flash` (disabled); `open-webui.service`
(0.0.0.0:8080); `Qwen3-TTS.service` (без `User=` → root, CVD по UUID,
16 ГБ карта); `tts-proxy.service` (dispot, 8004); `gpu-power-limit.service`
(oneshot, `nvidia-smi -pm 1 -pl 150`, без CVD); Caddy 2.6.2, `tls internal`,
upstreams 8004 (/tts/*) и 8080. GPU UUID→индексы на момент обследования:
0→GPU-9f764cdf…, 1→GPU-ca13b019…, 2→GPU-cfec683a…. Maya не запущена;
найден шардированный GGUF `~/models/maya/GLM-5.3-Flash-Maya-M-IQ2_S-*3`.

**Конвенция адресации CUDA (согласована с владельцем, сохранена как есть):**
vLLM адресует карты по индексам (`CUDA_VISIBLE_DEVICES=0,1` в start-vllm.sh),
llama.cpp/TTS — по полным UUID. В схеме панели это поле `cuda_addressing:
index|uuid` на каждом сервисе. Плановая привязка GPU в панели всегда по UUID
(ТЗ §2); при генерации CVD для `index`-сервисов индексы выводятся заново на
момент запуска из фактического маппинга NVML (с проверкой missing_gpu).

**Расхождения с ТЗ (не исправляю без вашего решения):**
1. Open WebUI и llama-server слушают `0.0.0.0` (8080, 8002) — доступны из LAN
   напрямую, минуя Caddy/TLS. Панель их не меняет (adopted, manage_only);
   рекомендация: вручную перевести на `127.0.0.1` (нужен рестарт сервисов).
2. `Qwen3-TTS.service` не определяет `User=` → работает от root; редактирование
   юнита через панель будет отклонено навсегда (ТЗ §5.5 допускает это только
   для сервисов непривилегированного пользователа).
3. Пробелы round 1 закрыты скриптом `scripts/survey2.sh` (llama-ExecStart,
   владельцы портов 8888/8001/18789, фактические health-endpoints, Maya-скрипт,
   oomd-политики) — прислать вывод следующим сообщением.
