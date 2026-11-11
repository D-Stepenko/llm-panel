# llm-panel — описание проекта

Локальная веб-панель управления LLM-станцией: статусы и управление systemd-сервисами
(vLLM, llama.cpp, TTS/STT, вспомогательные), сводка GPU/RAM/CPU/дисков, журналы,
аудит действий. Универсальна: устанавливается на любой сервер с systemd и NVIDIA GPU,
конкретные юниты/карты/диски описываются конфигурацией, а не кодом.

Реализация по ТЗ `llm-panel-tz-reviewed.md` (ред. 1.1). Текущая версия: **0.1.1**,
реализован **этап 1 (MVP)** из 4.

## 1. Возможности (этап 1)

- Вход по паролю (Argon2), блокировка после 5 неудач, сессии в SQLite, CSRF на всех POST.
- Дашборд: карточки сервисов (статус systemd, health, автозапуск, uptime, GPU-привязка),
  единый блок хоста (RAM/swap/CPU/диски), карточки GPU (загрузка, память, температура,
  питание, процессы).
- Управление: start/stop/restart, toggle автозапуска (enable/disable) — через
  ограниченный root-owned helper (см. §3).
- Журналы юнитов: просмотр и скачивание с маскированием секретов.
- Ручной порядок карточек («Порядок»: перетаскивание мышью, сохранение в БД,
  переживает рестарт панели).
- SSE-обновления: один общий фоновый коллектор питает всех клиентов (интервал
  настраивается, по умолчанию 3 с).
- Панель настроек (`/config`): язык (EN/RU), единица температуры (°C/°F), интервал
  обновления, лимит записей аудита, пути дисковой сводки. Всё применяется на ходу,
  без рестарта панели.
- Полосы загрузки: ≤60 % зелёная, ≤80 % жёлтая, выше красная; имена GPU как в
  `nvidia-smi`; состояния отображаются честно («нет данных», а не выдумки).

## 2. Стек и структура

Python 3.12, FastAPI, Uvicorn (один процесс, только loopback), Jinja2 (без npm/сборки),
SQLite, NVML (`nvidia-ml-py`) + psutil. CSP без inline script/style.

```
llm-panel/
├── llm_panel/                 код панели
│   ├── cli.py                 init-db | set-password | lockout-reset | check-config | serve
│   ├── app.py                 FastAPI: Host/CSRF/аутентификация, SSE, панельные настройки
│   ├── config.py              загрузка/валидация config.yaml
│   ├── registry.py            загрузка/валидация services.yaml
│   ├── db.py       security.py   SQLite, сессии, блокировка входа, пароли
│   ├── systemd_units.py  health.py   read-only статусы systemd; health-проверки
│   ├── collector.py  sse.py    общий фоновый сборщик; шина событий
│   ├── metrics.py             NVML + psutil
│   ├── helper_client.py       вызов helper через sudo -n (argv, без shell)
│   ├── i18n.py                все строки интерфейса EN/RU
│   ├── routes/{auth,dashboard,services,logs,config}.py
│   └── templates/  static/
├── deploy/
│   ├── install.sh                  универсальный установщик (см. docs/DEPLOY_RU.md)
│   ├── llm-panel.service.template  шаблон systemd-юнита панели
│   ├── helper/llm-panel-systemd    root-owned helper (ставится на сервер)
│   ├── config.yaml.example         /etc/llm-panel/config.yaml (шаблон)
│   ├── services.yaml.example       /etc/llm-panel/services.yaml (шаблон)
│   ├── allowlist.json.example      /etc/llm-panel/allowlist.json (шаблон)
│   ├── sudoers.llm-panel           предложение sudoers (только строка helper)
│   └── examples/                   реальные примеры заполнения (станция-LLM)
├── scripts/survey.sh, survey2.sh   read-only обследование сервера
├── docs/                           PROJECT.md, DEPLOY.md, STATION-LLM.md,
│                                    gpu-detection.md (обнаружение GPU, вендоры)
├── tests/                          pytest
└── config.dev.yaml                 конфиг машины разработки (без systemd/NVML)
```

## 3. Безопасность

- **Аутентификация**: пароль Argon2, сессии с httpOnly+Secure cookie (Secure отключается
  только в dev), блокировка входа после 5 неудач (снимается `llm-panel lockout-reset`).
- **CSRF**: токен в сессии, проверяется на каждом POST; Host/Origin — только из
  `trusted_hosts`.
- **CSP**: `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
  data:; frame-ancestors 'none'; form-action 'self'` — никаких inline script/style,
  ширины полос задаются классами `.p0…p100`.
- **root-операции**: только через helper `/usr/local/sbin/llm-panel-systemd`
  (root:root 0755). Панель вызывает `sudo -n <helper> <action> <unit>`; в sudoers ровно
  одна строка на этот путь, NOPASSWD, ничего больше. Helper сам проверяет действие,
  точное имя юнита по root-owned allowlist и тип операции; коды возврата: 0 — успех,
  2 — ошибка аргументов, 3 — отказ, 4 — сбой systemd. Юнит вне allowlist = отказ.
- **Секреты**: не выводятся на веб-слой; журналы и скачивания маскируют
  `hf_…`/JWT/Bearer/sk-ключи (паттерны настраиваются).
- **Аудит**: все действия (вход, управление сервисами, настройки) пишутся в SQLite
  с IP, пользователем, результатом и деталями; просмотр — `/audit`.

## 4. Конфигурация

### 4.1 config.yaml (`/etc/llm-panel/config.yaml`)

| Ключ | Назначение | По умолчанию |
|---|---|---|
| `panel.listen_host/port` | слушающий адрес панели (только loopback) | 127.0.0.1:8443 |
| `panel.trusted_hosts` | допустимые значения заголовка Host | — |
| `panel.cookie_secure` | Secure-cookie (true на сервере) | true |
| `panel.session_minutes`, `lockout_*` | сессии и блокировка | 720 / 5 / 15 |
| `data.data_dir`, `db_path` | каталог и БД данных панели | /var/lib/llm-panel |
| `data.services_file` | путь к services.yaml | /etc/llm-panel/services.yaml |
| `systemd.helper_path` | путь к root-owned helper | /usr/local/sbin/llm-panel-systemd |
| `systemd.log_max_lines`, `log_download_max_mb` | капы журналов | 2000 / 2 |
| `metrics.interval`, `health_interval` | интервалы сбора (стартовые) | 3.0 / 5.0 с |
| `metrics.startup_timeout_default` | тайм-аут старта по умолчанию | 600 с |
| `ports.min/max` | диапазон портов (мастер этапа 3) | 8100–8999 |
| `models.roots` | стартовые пути дисковой сводки | — |
| `redact_patterns` | маскирование секретов в журналах | hf_/JWT/Bearer/sk- |

Проверка: `.venv/bin/python -m llm_panel --config <путь> check-config`.

### 4.2 services.yaml (`/etc/llm-panel/services.yaml`)

Описания сервисов — единственное место, где перечислены юниты вашего сервера.
Поля: `id` (slug), `name`, `type` (vllm/llamacpp/script/generic), `unit` (точное имя
`.service`, регистр сохраняется), `managed` (adopted/created), `edit_mode`
(manage_only/dropin/panel), `port`, `bind_host`, `health` (http/tcp), `gpus`
(полные UUID вида `GPU-…`), `cuda_addressing` (uuid/index), `startup_timeout`,
`ram_exclusive`, `stop_signal` (SIGTERM/SIGINT; SIGKILL запрещён), `stop_timeout`,
`notes`. Шаблон — `deploy/services.yaml.example`, реальный пример —
`deploy/examples/`. Правит администратор ОС; панель читает файл, не пишет.

### 4.3 allowlist.json (`/etc/llm-panel/allowlist.json`)

Root-owned список юнитов и разрешённых действий для helper. Юнит вне списка — полный
отказ (безопасный дефолт). Проверка владельца root:root выполняется самим helper.

## 5. Панель настроек (что меняется из UI)

Раздел `/config`, права любого вошедшего пользователя, каждое сохранение — в аудит.
Хранится в БД (settings), действует на всю панель, применяется без рестарта:

| Настройка | Эффект |
|---|---|
| Language (EN/RU) | язык интерфейса |
| Temperature unit (°C/°F) | отображение температуры GPU |
| Refresh interval (2/3/5/10 с) | частота сбора метрик и SSE |
| Audit entries shown (30/50/100/200) | лимит записей на странице `/audit` |
| Disks summary paths | пути для дисковой сводки на главной (по одному абсолютному пути в строке, до 8; несуществующие показывают «нет данных») |

Что НЕ редактируется из UI (намеренно, ТЗ §5.5/§9 и модель безопасности): config.yaml,
services.yaml, allowlist.json, systemd-юниты, sudoers. Редактор конфигураций сервисов —
этап 2.

## 6. Универсальность: что определяется автоматически

- **GPU**: количество, имена, UUID, память, температура, питание, процессы — NVML,
  автоматически. Никаких зашитых карт.
- **CPU/RAM/swap**: psutil + `/proc/cpuinfo`, автоматически.
- **Диски**: список путей задаётся в Панели настроек (стартово — `models.roots`).
- **Юниты сервисов**: `services.yaml` + `allowlist.json` под конкретный сервер;
  обследование — `scripts/survey.sh` (read-only).
- **Пользователь/пути установки**: параметры `deploy/install.sh` (`--user`, `--dir`,
  `--config-dir`, `--data-dir`); systemd-юнит генерируется из шаблона.

## 7. Этапы ТЗ

- **Этап 1 (MVP) — реализован** (версии 0.1.0–0.1.1): вход/защита, статусы, управление
  через helper, логи, сводка GPU/RAM/CPU/диск, SSE, порядок карточек, i18n EN/RU,
  Панель настроек, универсальная установка.
- **Этап 2 — реализован частично** (версии 0.2.0, доработки 0.2.1): матрица GPU
  (`/matrix`: план services.yaml vs факт NVML, разрешение индексов, сниппеты
  привязки CVD/`gpu[]`, конфликты GPU/RAM/портов; в 0.2.1 — основная таблица
  только для сервисов с привязкой карт, остальные в приглушённой секции
  «Без привязки GPU»); редактор конфигураций сервисов (`/config/files`:
  регистрация dispot-owned файлов, JSON-валидация, версии/diff/rollback в SQLite,
  аудит). В 0.2.1: починен SSE-поток логов («Ошибка потока:» — корутина без
  await в событиях; теперь осмысленная причина или «соединение закрыто»),
  порт юнита в карточке — ссылка в новой вкладке (host панели : порт; для
  loopback-only bind_host ссылка не даётся). В 0.2.2: атрибуция «факта» GPU
  по ПОТОМКАМ MainPID — Maya (обёртка maya.sh, движок build/maya — ребёнок)
  теперь видна в матрице как занявшая карты («план+факт»), а не только план.
  Maya-специфика закрыта (2026-10-29): сервис работает, старт/стоп — вручную
  и через панель.
- **Этап 3**: каталог моделей, HF-загрузки, мастер «модель → сервис».
  Реализовано частично (0.3.0): каталог моделей `/models` — read-only скан
  model_roots (Config Panel → «Пути дисковой сводки»): GGUF (шарды группируются),
  HF-каталоги safetensors (model_type/torch_dtype/quant из config.json),
  квантование и семейство из имени, размер/файлы, свободное место корней,
  лимиты глубины/записей, кэш скана с TTL и кнопка «Пересканировать».
  HF-загрузки (0.3.1): проверка наличия hf/huggingface-cli (нет — уведомление
  с копируемой командой установки); запуск `hf download <repo> --local-dir`
  в выбранный корень (подкаталог опционально) как фоновой задачи: статус,
  хвост вывода (HTMX-поллинг 2 с), стоп (SIGTERM, без SIGKILL — ТЗ §4.2),
  очистка; лимит 3 одновременных; dest только внутри model_roots; аудит.
  Учёт conda-окружения (0.3.2): hf вне PATH юнита панели (conda env) — в
  Config Panel появилось поле «Путь к hf» (settings.hf_tool_path, полный путь
  до бинаря env, например …/miniconda3/envs/<env>/bin/hf — запускается без
  активации; валидация isabs+isfile+X_OK, иначе редирект err=hfpath). Баннер
  без инструмента: три копируемых варианта установки (pipx — рекомендован,
  pip внутри conda env, pip --user --break-system-packages) + подсказка
  «conda activate <env> && which hf» и указание прописать путь в Config Panel.
  Исправления (0.3.3): (1) недоступный для записи каталог назначения
  (Errno 30) ловится заранее пробной записью (check_writable) → внятное
  уведомление с инструкцией вместо падения загрузчика/500 при Subdirectory.
  Причина на станции — НЕ read-only ФС, а песочница юнита панели
  (ProtectSystem=strict + ProtectHome=read-only): процесс панели (и его hf)
  может писать только в ReadWritePaths; исправление на станции — добавить
  ReadWritePaths=/home/dispot/models в llm-panel.service;
  (2) кнопки Стоп/Убрать
  задач — hx-post (HTMX), без ухода на голый фрагмент; (3) каталог моделей:
  дедуп каталогов по realpath (симлинк maya → Maya-M и вложенные корни больше
  не дублируются) и одна запись на каталог с несколькими разными GGUF
  (talker/tokenizer/vocab/mmproj больше не отдельные «модели»).
  Продолжение (0.3.4): после открытия /home/dispot/models загрузка больших
  файлов всё равно ломалась — hf/xet пишут служебные данные в
  ~/.cache/huggingface (refs/main, логи/чанки xet), закрытый
  ProtectHome=read-only. Решение в коде: hf_env(state) направляет HF_HOME
  процесса hf в <data_dir>/hf-home (каталог данных панели уже в
  ReadWritePaths, создаются/проверяются пробной записью; при недоступности —
  тихий откат к исходному окружению). Расширять песочницу на ~/.cache
  больше не нужно.
  Мастер «модель → сервис» (0.3.5): страница /wizard в три шага — модель из
  каталога → движок (llamacpp для GGUF / vllm для safetensors, предвыбор по
  kind) + карты (полные UUID из NVML с метками «план/занята» из матрицы
  конфликтов) + свободный порт (занятые из реестра + bind-проба, диапазон
  ports.min/max) → валидный YAML-блок services.yaml (проверяется реестром),
  шаблон systemd-юнита под движок и шаги установки. Панель НИЧЕГО не пишет:
  root-owned services.yaml и юниты правит владелец (ТЗ §5.5). Генерация — в
  аудит. Этап 3 ЗАКРЫТ полностью (каталог, HF-загрузки, мастер).
  Температура/мощность CPU и шкалы «Clean line» (0.3.6): в строку CPU
  добавлены температура («Package id 0» из coretemp/k10temp/cpu_thermal,
  иначе максимум по сенсорам) и потребление в Вт из RAPL
  (intel-rapl energy_uj, дельта за интервал; нечитаемо/переполнение/
  нереалистично > 10 кВт → поле просто не показывается). Процент загрузки
  из текста CPU убран. ВСЕ шкалы (RAM/swap/CPU/диск + GPU память/мощность)
  переделаны по образцу пользователя: высота 10 px, рамка 1 px тон-цветом,
  заливка с отступом 2 px (inset), градиент, процент по центру (.bar-pct),
  пороги единые: зелёный ≤50 / жёлтый ≤51–75 / красный ≥76.
  Доведение шкал (0.3.7, по скриншотам владельца): все шкалы блока
  выровнены в колонку (host-card/gpu-body — grid, .host-row — contents:
  метки/значения/шкалы по колонкам, общий левый край после самой длинной
  строки и общий правый); рамка шкал — нейтральная var(--line-soft) в
  обеих темах (раньше была rgba белой — в светлой теме невидима), тон
  красит ТОЛЬКО заливку; внутренняя тень трека убрана — фон трека просто
  var(--track-bg) (полтона от фона блока); цвет процентов = цвету текста
  блока (var(--val-fg)) в каждой теме.
  Доведение по скриншотам владельца (0.3.8): шкалы выровнены ПО САМОЙ
  ДЛИННОЙ СТРОКЕ (колонка значений max-content, шкалы одной ширины
  minmax(240px,320px) у host-блока; в GPU-картах шкала тянется к правому
  краю); GPU-метки сокращены (en: VRAM/Temp), значения памяти/мощности
  обёрнуты в .nb (white-space: nowrap) — при сжатии перенос только по
  « / », минимум в строке «15.8 GB» вместо побуквенного столбика;
  из GPU matrix убраны предупреждения «запланирован для: …» (дубль
  таблицы, несколько плановых привязок на карту — штатная схема);
  /models: нумерация строк и кнопка-переключатель сортировки
  (по имени ↔ по размеру) в начале списка.
  Доведение по замечаниям владельца (0.3.9): «Models: sort» — ручной
  порядок перетаскиванием, как у сервисов (app.js: обобщённый setupSort
  для .service-card/.model-row; порядок моделей хранится в settings.
  models_order по полным путям, новые модели — в конец; POST
  /ui/models-order, аудит); «Services: sort» перенесён ПОД карточки
  сервисов (отступ 5 px); процентам шкал возвращено лёгкое затенение
  для читаемости на фоне заливки; GPU-pill (98%) — нейтральный: рамка
  как у карточки (--gpu-border), текст как в блоке (--val-fg),
  цветовая индикация нагрузки отключена (загруженные карты — норма).
  Документировано включение мощности CPU: docs/DEPLOY_RU.md §6.1 — drop-in
  AmbientCapabilities=CAP_DAC_READ_SEARCH (только чтение RAPL).
  Исправления по замечаниям владельца (0.3.10): sort моделей сломан —
  контейнер .models-list не имел id="models-list", JS получал null и
  показывал «Service cards not found» (id добавлен, тост пустого списка —
  свой: order_no_models «Строки моделей не найдены»); на /matrix блок
  «Конфликтов GPU/RAM/портов не обнаружено» (и конфликты) перенесён ПОД
  таблицу матрицы (отступ сверху 14 px), тесты: позиция уведомления и
  наличие контейнера drag&drop — 210/210.
  Правки по замечаниям владельца (0.3.11): «Services: sort» возвращён
  ПЕРЕД карточками сервисов (отступ сверху 26 px возвращён, у Models
  свой компактный .models-head 5 px); нумерация моделей вынесена ЗА
  границу карточек (обёртка .model-item, номер снаружи, data-model и
  перетаскивание — на обёртке); на /matrix блок конфликтов/«нет
  конфликтов» — под ПЕРВОЙ таблицей, над «Without GPU binding»;
  тени процентов шкал по темам (--bar-shadow: тёмные в тёмной, светлые
  в светлой — тёмный текст не тонет); задокументирована причина ошибки
  «sudo: unable to change to root gid» после включения RAPL: строка
  CapabilityBoundingSet в drop-in заменяла весь набор capabilities
  (sudo теряет CAP_SETGID) — из инструкции DEPLOY.md §6.1 убрана,
  предупреждение добавлено также в deploy/llm-panel.service.template.
  Новое (0.3.12): выключение/перезагрузка СЕРВЕРА с панели — Config
  Panel, низ страницы: кнопки «Перезагрузить сервер» / «Выключить
  сервер» с подтверждением и отсчётом 5 секунд с отменой (кнопка
  «Отмена», повторный клик, Esc; app.js без inline-скриптов по CSP).
  POST /ui/power (CSRF, аудит power:poweroff/power:reboot) → root-helper
  `power <poweroff|reboot>` → systemctl от root; разрешение —
  опциональная секция "power" allowlist.json (false/отсутствие — отказ,
  безопасный дефолт), sudoers-правил НЕ нужно (helper-путь уже разрешён).
  Инструкция включения — docs/DEPLOY_RU.md §6.2; пример секции —
  deploy/allowlist.json.example. Тесты: helper (план/отказ/usage) и роут
  (кнопки, CSRF/400, аудит, ошибка helper) — 214/214.
  Правки (0.3.13): power-кнопки приведены к общему стилю проекта —
  «Перезагрузить сервер» янтарная (--pill-warn-*), «Выключить сервер»
  красная (--pill-err-*, иконка --red); зафиксировано тестом.
  Правки (0.3.14): по замечаниям владельца со скриншотов станции —
  GPU-карточки: строки привязок (Сервисы: / «плановая привязка»)
  вынесены из сетки .gpu-body в отдельный блок .gpu-binds вне сетки
  (раньше разрывали колонки меток/шкал: «плановая привязка» уезжала в
  колонку шкалы температуры, карточку ломало); отступы уплотнены
  (head 11/16/9, body 11/16/10); убран span-2 последней карты — все
  карты ВСЕГДА равной ширины (3 → 2 → 1 колонки); при сжатии колонка
  шкалы сжимается до 64px (трек не вылезает за рамку карточки).
  Сервисы: 3 карточки в ряд на ширине >1200px (2 при ≤1200px, 1 при
  ≤820px), компактные отступы .service-main/.service-fields/
  .service-bottom. Вендорные рамки GPU (v0.3.14): gpu_vendor() в
  metrics.py (radeon/amd→amd, intel/arc→intel, иначе nvidia) →
  gpu-vendor-* на .gpu-card: NVIDIA зелёная (базовая), AMD красная,
  Intel синяя (переменные --gpu-border-amd/-intel в обеих темах);
  документация обнаружения GPU — docs/gpu-detection_RU.md. DEPLOY_RU.md §6.2
  переписан под ручное копирование ВСЕЙ директории проекта (станция НЕ
  обновляется git): проверка grep POWER_ACTIONS перед sudo install,
  причина «ОШИБКА: неизвестное действие» — старый helper от v0.3.11.
  Тесты: 217/217.
  Правки (0.3.15, по скриншотам владельца): поля сервис-карт —
  «метка + значение» в одной строке (10px между), доп-надписи у порта
  и systemd убраны (важное в подсказках); GPU секция карточки —
  отдельной полосой на всю ширину (10px от рамки), чипы карт целые
  (white-space: nowrap), 5px между чипами.
  Правки (0.3.16): поля — сетка на 2 max-content колонки (таблица без
  границ: Порт/Перезапусков в одной вертикали, Время работы/Systemd
  во второй), GPU-полоса выровнена по колонке 1 (16px). Добавлен
  llm_panel/users_panel_config_.sh — интерактивное меню управления
  учётными записями панели (создать / удалить / смена пароля / список;
  Argon2 через llm_panel.security.set_password, getpass, сессии
  сбрасываются, удаление admin запрещено) + тесты исполнения
  встроенного python-блока. Причина долгих «старых» копий на станции —
  вложенная папка llm-panel/llm-panel при копировании (WinSCP drag&drop),
  подтверждена владельцем. Тесты: 221/221.
- **Этап 4**: Open WebUI-интеграция — ПРОПУЩЕН решением владельца
  (2026-11-01): Open WebUI виден в панели как обычный сервис
  (старт/стоп/логи, health по /health) — достаточно; сквозная
  интеграция моделей/настроек не требуется. Фактически этапы ТЗ
  ЗАВЕРШЕНЫ (1, 2, 3 реализованы; 4 — владельцем снят с плана).
- **0.3.17 — security-audit (внешний аудит 2026-11-08, 12 находок)**.
  🔴1 Зависимости: fastapi 0.115.12→0.162.2, starlette pin `>=1.3.1,<2`
  (все advisories 2026 — CVE-2025-54121/GHSA-2c2j-9gv5-cj73 и
  сопутствующие — закрыты только в ветке 1.x; TemplateResponse переведён
  на сигнатуру `(request, name, ctx)`), python-multipart→0.0.31,
  pytest 9.0.3/pytest-asyncio 1.4.1; `pip_audit -r requirements.txt` —
  0 уязвимостей; в CI добавлены `.github/workflows/security-audit.yml`
  (pip-audit --strict, еженедельно) и `.github/dependabot.yml`.
  🔴2 `_write_atomic` (config_files) сохраняет st_mode исходника
  (chmod tmp до os.replace), повторная `_validate_new_path` на каждом
  save/rollback; отказ при смене владельца (0600 больше не «деградирует»
  в 0644 — режимные тесты skipif win32).
  🟠3 Lockout: блокировка по паре (username, ip) + глобальный порог по
  аккаунту (failures×3 за окно minutes×3); успех чистит неудачи только
  своего IP (per-IP isolation + global threshold тесты).
  🟠4 db.cleanup() вызывается при старте и каждые 6 ч (фоновая задача
  cleanup_task, гасится в shutdown); retention 90 дней для
  login_attempts и audit; on_event мигрирован на lifespan.
  🟠5 Тайминг: при несуществующем пользователе выполняется фиктивная
  verify_password на лениво сгенерированном настоящем argon2id-хеше
  (одинаковое время ответа).
  🟠6 Пустой Host-заголовок → 400 (в dev_mode — пропуск для отладки).
  🟠7 Open redirect: next отклоняет `//`, `/\` (scheme-relative) и
  `\\` внутри — фолбэк на `/`.
  🟡8 helper: allowlist проверяется на group/other-write биты (0644/0640
  root), каталог /etc/llm-panel — root-owned без записи group/other.
  🟡9 deploy/llm-panel.service.template: добавлен комментарий-блок с
  sandbox-опциями, совместимыми с sudo (ProtectSystem=strict +
  ReadWritePaths, PrivateTmp, ProtectKernel*, RestrictNamespaces,
  LockPersonality) — включать по одной с проверкой.
  🟡10 CSRF: `csrf_matches()` через secrets.compare_digest — единый
  хелпер вместо 17 копий `form.get("csrf") != request.state.csrf`.
  🟡11 Политика паролей (cli set-password и users_panel_config_.sh):
  ≥10 символов И ≥3 из 4 классов (строчные/ПРОПИСНЫЕ/цифры/прочие).
  🟡12 Pickle-RCE: предупреждение в UI (Models → «Загрузка с Hugging
  Face», notice.warn hf_pickle_warning) + зафиксированный риск в §8
  «Известные ограничения».
  UI-фиксы этой же сборки: убрана строка-подсказка на /login
  (login_note), CSP-фикс htmx-индикаторов в static/app.js
  (includeIndicatorStyles=false). docs/ACCEPTANCE_RU.md — план приёмки.
  Тесты: 233/233 + 2 skip (win32).
- **0.3.18 — новые действия сервиса: reset-failed и daemon-reload** (10.11.2026).
  По запросу владельца — две кнопки на странице настроек сервиса
  (`/services/<id>/settings`, POST + CSRF, как у карточек):
  «Сбросить статус failed» (`systemctl reset-failed <unit>` — пер-unit,
  когда причина сбоя разобрана) и «Daemon reload» (`systemctl daemon-reload`
  — операция над МЕНЕДЖЕРОМ целиком, юнит не участвует: перечитывает ВСЕ
  файлы юнитов; включать после ручной правки юнит-файлов вне панели).
  Scope-обработка: system-юниты — через helper (reset-failed — обычное
  per-unit allowlist-действие; daemon-reload — НОВЫЙ уровень разрешения:
  опциональный ключ allowlist `"daemon_reload": true`, отсутствие/false =
  отказ, безопасный дефолт по образцу секции "power"); user-юниты —
  напрямую systemctl --user (helper не нужен, как остальные user-действия).
  Helper: "reset-failed" добавлен в ACTIONS (extra не принимает),
  ветка daemon-reload без аргумента-юнита (любой аргумент = код 2), dry-run
  план `["systemctl","daemon-reload","--no-ask-password"]`. Аудит:
  `reset-failed[:user]` / `daemon-reload[:user]` с фактическим состоянием
  юнита в результате. i18n: кнопки/тайтлы/сообщения EN+RU.
  Шаблоны: кнопки в service_settings.html; примеры allowlist (универсальный
  и станции) — reset-failed в списке действий + секция daemon_reload;
  llm_panel/helper_client.py — call_daemon_reload; systemd_units.py —
  daemon_reload_user, reset-failed в run_user_action.
  Тесты: 249/249 + 2 skip (win32) — +12 (helper: план/отказ/лишние
  аргументы; роуты: system через моки call_helper/call_daemon_reload,
  user через моки run_user_action/daemon_reload_user, CSRF 403, рендер
  кнопок, аудит-записи). Живая проверка на станции — после копии.
- **0.3.19 — «Конфиг юнита»: просмотр/правка юнит-файла + правки UI**
  (11.11.2026). По запросу владельца:
  (1) кнопки страницы настроек в ОДНУ линию с зазорами 20px. Причина
  переносов: `<form>` внутри `<p class="settings-links">` запрещён
  HTML-парсером — `<p>` закрывается до формы, кнопки выпадают из
  flex-контейнера и становятся блоками. Контейнер теперь `<div>`, CSS:
  `.settings-links` gap 20px + `.settings-links form { display: contents }`.
  (2) «Daemon reload» больше НЕ бледный: убран класс btn-muted
  (opacity .5 — ставился по ошибке в 0.3.18).
  (3) Новая кнопка «Конфиг юнита» → страница /services/<id>/unit-config:
  полный юнит-файл в textarea (как nano /etc/systemd/system/<unit>.service),
  кнопки «Сохранить» (POST unit-config/save) и «Daemon reload» на той же
  странице. system-юниты — через helper: НОВЫЕ allowlist-действия
  unit-cat (чтение) и unit-write (правка: содержимое в STDIN, только
  СУЩЕСТВУЮЩИЕ файлы — создание юнитов через панель запрещено; валидация
  UTF-8/NUL/секций [Unit]/[Service]/[Install]; резервная копия в
  .panel-backups перед каждой записью; атомарная запись tmp+os.replace,
  root:root 0644; отказ при ошибке возвращает правки в форму);
  user-юниты — напрямую ~/.config/systemd/user (unit_cat_user/
  unit_write_user, backup .panel-backups). Аудит: unit-write[:user].
  (4) Глобальные ключи allowlist для «действий по умолчанию» (решение
  владельца — действия разрешены всем текущим и БУДУЩИМ юнитам):
  "reset_failed": true — reset-failed любому allowlist-юниту без
  per-unit вхождения; "daemon_reload": true — manager-wide (уже покрывал
  все юниты). unit-cat/unit-write остаются строго per-unit (правка
  root-файла — опаснее).
  Тесты: 272/272 + 2 skip (win32) — +23 (helper: глобальный ключ, планы
  unit-cat/unit-write, реальный roundtrip с backup/отказами; роуты:
  рендер, ошибки загрузки, save ok/отказ с сохранением правок, CSRF,
  пустой ввод, кнопки/раскладка; user-юниты: чтение/запись, файловый
  roundtrip). Живая проверка на станции — после копии.
  Дополнение (11.11.2026, после первого деплоя на станцию): (а)
  __version__ в llm_panel/__init__.py остался «0.3.17» — шапка панели
  показывала старую версию; поднят до 0.3.19 + тест синхронизации с
  pyproject.toml; (б) при коде 2 helper панель теперь показывает и
  stderr helper («ошибка аргументов helper: неизвестное действие») —
  «неизвестное действие» это признак НЕ переустановленного helper
  (старый v0.3.18 не знает unit-cat/unit-write; установка нового:
  sudo install -o root -g root -m 0755 deploy/helper/llm-panel-systemd
  /usr/local/sbin/llm-panel-systemd). Тесты: 274/274 + 2 skip.
- **0.3.20 — allowlist_tool: автоматизация правки allowlist.json**
  (11.11.2026). По запросу владельца («крайне неудобно править по каждому
  юниту вручную»): новый инструмент deploy/allowlist_tool.py — безопасный
  редактор /etc/llm-panel/allowlist.json вместо ручного JSON. Команды:
  list/check, add-unit/remove-unit (--actions, --note), grant/revoke
  (добавить/убрать действия юниту), set-global (reset_failed |
  daemon_reload), sync-services — добавляет юниты из
  /etc/llm-panel/services.yaml, которых нет в allowlist (существующие НЕ
  трогает; user-юниты пропускает — им allowlist не нужен; дефолт для
  новых: управление БЕЗ unit-cat/unit-write — они остаются строго
  per-unit, --actions переопределяет). Безопасность: валидация имён
  юнитов и действий ДО записи, backup allowlist.json.bak-<ts> перед
  каждой записью (последние 5), атомарная запись tmp+os.replace,
  root:root 0644, ключи-комментарии и порядок ключей сохраняются,
  --dry-run у каждой мутации, правка системного файла только через sudo.
  Правка allowlist из UI панели СОЗНАТЕЛЬНО не делается (панель, умеющая
  менять собственные права — путь к эскалации). Тесты: 294/294 + 2 skip
  — +20 (валидация, backup/prune, команды, sync-services, idempotency).

## 8. Известные ограничения

- Редактор /config/files правит только dispot-owned файлы; /etc/llm-panel и
  systemd-юниты из UI не редактируются (ТЗ §5.5). Матрица конфликтов — только
  advisory (не блокирует действия).
- Maya: закрыто (2026-10-29). Юнит maya-maya-m.service работает, запуск/остановка
  — вручную и через панель; «факт» GPU виден в матрице (v0.2.2, атрибуция по
  потомкам MainPID). Политику конфликтов/idle_unload_s отложили за ненадобностью.
- health-проверка `readiness_timeout` не убивает процесс (по ТЗ).
- «unhealthy»-история хранится в памяти панели и теряется при рестарте.
- Тесты на машине разработки проверяют логику, но не systemd/NVML (их там нет) —
  приёмка только на целевом сервере.
- Sandbox-опции systemd юнита панели: `NoNewPrivileges` запрещён (ломает sudo до
  helper), остальные — включать по одной с проверкой (урок станции 2026-10-28,
  см. `deploy/llm-panel.service.template` и docs/STATION-LLM_RU.md).
- Pickle-RCE через HF-загрузки (аудит 2026-11-08, №12): панель скачивает модели
  из ЛЮБЫХ указанных репозиториев Hugging Face. Некоторые репозитории содержат
  pickle-файлы (.bin / .pt из transformers), код которых выполняется при загрузке
  модели обслуживающим сервисом (vLLM и т. п.) — сами файлы панели безопасны,
  но потребитель может исполнить чужой код. Смягчение: предупреждение в UI
  (Models → «Загрузка с Hugging Face»); скачивать только доверенные источники,
  предпочитать safetensors, недоверенные модели запускать под выделенным
  непривилегированным пользователем (не dispot/root). Автотест safetensors-
  инспекции загрузок — в TODO этапа 3+.
