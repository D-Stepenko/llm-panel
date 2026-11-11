# Добавление сервисов: systemd-юниты и панель

Как оформить новый LLM-сервис как systemd-юнит и показать его на панели — для
system-юнитов и user-юнитов. На примере станции-LLM (`docs/STATION-LLM_RU.md`);
имена пользователя (`dispot`), пути и юниты замените на свои.

Три способа показать сервис на панели (по возрастанию данных):

| Способ | Что даёт | Когда использовать |
|---|---|---|
| «Поиск сервисов» в Config Panel | карточка с состоянием и действиями | юнит уже загружен хотя бы раз |
| «Добавить юнит по имени» (там же) | то же, в т.ч. для ни разу не стартовавших юнитов | быстрое добавление без правки файлов |
| Запись в `services.yaml` | карточка + имя, порт, health, GPU, notes | основной сервис с известной конфигурацией |

Все три способа дают карточку на странице Статус с действиями
(start/stop/restart/enable/disable): system-юниты — через root-owned helper
(см. §3), user-юниты — напрямую `systemctl --user`. Никакой из них не требует
рестарта панели (кроме правки `services.yaml` — он перечитывается при рестарте
панели или drop-in'ом `SIGHUP`, см. ниже).

---

## 1. Как оформить юнит: system или user

**System-юнит** (`/etc/systemd/system/*.service`) — по умолчанию для LLM-сервисов:

- переживает выход пользователя из системы, стартует при загрузке;
- ограничения ресурсов (`OOMScoreAdjust`, `ManagedOOM*`) задаются в самом юните;
- управление из панели — через helper + allowlist (root, см. §3);
- редактирование юнита — только `sudo` (панель юниты не редактирует, ТЗ §5.5).

**User-юнит** (`~/.config/systemd/user/*.service`) — для личных сервисов
пользователя панели (чаты-шлюзы, прокси и т.п.):

- управляется `systemctl --user` от имени того же пользователя, что и панель;
- helper и allowlist НЕ нужны (панель выполняет systemctl напрямую);
- для старта при загрузке нужен linger: `loginctl enable-linger dispot`;
- не виден системному менеджеру: в `services.yaml` — `scope: user`, а в системных
  `systemctl ... show` такого юнита нет.

> Замечание systemd-oomd: user-менеджер (`user@.service`) часто имеет
> `ManagedOOMMemoryPressure=kill` — сервис, пиннующий почти всю RAM (например,
> Maya), в user-юните рискует быть убит oomd. У system-юнита такие свойства по
> умолчанию выключены. Ещё одна причина предпочесть system-юнит.

### 1.1 System-юнит — шаблон

```bash
sudo tee /etc/systemd/system/myservice.service >/dev/null <<'EOF'
[Unit]
Description=My LLM service
After=network-online.target gpu-power-limit.service
Wants=gpu-power-limit.service

[Service]
User=dispot
WorkingDirectory=/home/dispot/src/my-service
ExecStart=/home/dispot/src/my-service/.venv/bin/python serve.py --port 8100
Restart=on-failure
RestartSec=10
TimeoutStartSec=600      # если сервис долго грузит модель
TimeoutStopSec=180       # сколько ждать graceful-остановки
OOMScoreAdjust=-500      # при нехватке памяти OOM-killer уберёт его последним

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
```

Проверка: `sudo systemctl start myservice.service`, `systemctl status myservice.service`,
`journalctl -u myservice.service -f`. **Enable** (`sudo systemctl enable ...`) —
только если ясно, что сервис не конфликтует за GPU/RAM с другими при загрузке.

### 1.2 User-юнит — шаблон

```bash
mkdir -p ~/.config/systemd/user
tee ~/.config/systemd/user/myservice.service >/dev/null <<'EOF'
[Unit]
Description=My service (user)

[Service]
WorkingDirectory=%h/src/my-service
ExecStart=%h/src/my-service/run.sh
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
loginctl enable-linger $USER   # чтобы юнит жил без открытой сессии
systemctl --user start myservice.service
```

### 1.3 Смена концепции: user-юнит → system-юнит (пример Maya)

```bash
# 1) остановить и удалить user-юнит
systemctl --user stop maya-maya-m.service
rm ~/.config/systemd/user/maya-maya-m.service
systemctl --user daemon-reload

# 2) создать system-юнит (содержимое — по шаблону §1.1; полный пример Maya — §5)
#    ...
# 3) перечитать конфигурацию systemd
sudo systemctl daemon-reload
```

Если юнит уже был добавлен в панель как user-юнит, удалите его в Config Panel
(снимите галочку / удалите из `extra_units`) и добавьте заново — scope
определится автоматически (system).

---

## 2. Показать юнит на панели

### 2.1 «Поиск сервисов» (Config Panel)

`/config` → «Поиск сервисов» → список загруженных юнитов обоих менеджеров
(system и user) → отметить нужные → «Сохранить». Сохранённые юниты хранятся в
настройках панели (SQLite), правки yaml не требуют; карточки появляются на
Статусе со state/uptime и действиями.

### 2.2 «Добавить юнит по имени»

Тот же блок в Config Panel, поле под списком. Работает и для юнитов, которых в
`list-units` нет — ни разу не стартовавших или остановленных и выгруженных:
панель проверяет имя через `systemctl show` сначала в user-менеджере, затем в
системном, определяет scope и сохраняет юнит. Имя — точное, с суффиксом:
`maya-maya-m.service`.

### 2.3 Запись в `services.yaml` (полные данные)

Для основных сервисов добавьте блок в `/etc/llm-panel/services.yaml`
(шаблон — `deploy/services.yaml.example`, реальные примеры — `deploy/examples/`):

```yaml
services:
  - id: my-service                    # латиница/цифры/дефис, уникален
    name: "My service (человекочитаемо)"
    type: generic                     # vllm | llamacpp | generic
    unit: myservice.service           # ТОЧНОЕ имя юнита (регистр сохраняется)
    scope: system                     # для user-юнитов: user
    managed: adopted
    edit_mode: manage_only
    port: 8100
    bind_host: 127.0.0.1
    health:
      kind: http                      # или tcp (port)
      url: http://127.0.0.1:8100/health
      timeout_s: 3
      interval_s: 15
    startup_timeout: 600              # 30..3600, сколько ждать готовности
    stop_timeout: 60                  # 1..300, согласовать с TimeoutStopSec юнита
    gpus: []                          # плановая привязка по ПОЛНЫМ UUID (nvidia-smi -L)
    cuda_addressing: uuid             # или index (как сервис фактически адресует карты)
    notes: >-
      ExecStart, env, особенности.
```

После правки — `sudo systemctl restart llm-panel` (или проверить конфиг:
`.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml check-config`).
Поля GPU и health-эндпоинт — этап 2, для простой карточки достаточно
`id/unit/scope`.

> YAML-блок и юниты, добавленные через UI (§2.1/§2.2), не мешают друг другу:
> реестр приоритетен — юнит, уже описанный в `services.yaml`, повторно добавить
> из UI нельзя.

---

## 3. Allowlist для system-юнитов

Управление system-юнитами из панели идёт через root-owned helper
`/usr/local/sbin/llm-panel-systemd`; без записи юнита в allowlist кнопки
start/stop/restart отклоняются с подсказкой «внесите юнит в allowlist»
(чтение статуса работает и без него — панель читает systemctl напрямую).

Внесение — только от root. Удобно объединить с уже существующей записью:

```bash
sudo python3 - <<'EOF'
import json
p = "/etc/llm-panel/allowlist.json"
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["units"]["myservice.service"] = {
    "actions": ["start", "stop", "restart", "enable", "disable", "reset-failed",
                "status", "journal", "journal-follow"],
    # «только просмотр»: ["status", "journal"] + "note": "почему"
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
EOF
```

Проверка: `sudo -n /usr/local/sbin/llm-panel-systemd status myservice.service`
(код 0), затем кнопки на карточке. User-юниты в allowlist вносить не нужно.

Действия из настроек сервиса и страницы «Конфиг юнита» (v0.3.18/0.3.19):

- `reset-failed` («Сбросить статус failed») — per-unit действие. Чтобы не
  перечислять его в каждом `actions`, разрешите глобально: ключ allowlist
  верхнего уровня `"reset_failed": true` действует для ВСЕХ юнитов,
  внесённых в allowlist (текущих и добавляемых в будущем).
- `daemon-reload` («Daemon reload») — операция над менеджером ЦЕЛИКОМ
  (перечитывает все юнит-файлы), в per-unit `actions` не входит:
  разрешается одним ключом `"daemon_reload": true` — этого достаточно для
  всех юнитов сразу. User-юнитам это не нужно — `systemctl --user
  daemon-reload` выполняется панелью напрямую.
- `unit-cat` / `unit-write` («Конфиг юнита»: просмотр/правка юнит-файла
  /etc/systemd/system/<unit>) — строго per-unit (правка root-файла опаснее):
  добавляйте в `actions` только юнитам, которым действительно нужна правка
  из UI. Перед каждой записью helper делает резервную копию в
  /etc/systemd/system/.panel-backups/; создание НОВЫХ юнитов через панель
  запрещено.

Править allowlist.json вручную не обязательно (v0.3.20): для этого есть
инструмент `deploy/allowlist_tool.py` (backup + валидация + атомарная
запись при каждом изменении). Примеры:

```bash
cd ~/llm-panel
# добавить недостающие юниты из /etc/llm-panel/services.yaml (сначала план):
sudo .venv/bin/python deploy/allowlist_tool.py sync-services --dry-run
sudo .venv/bin/python deploy/allowlist_tool.py sync-services
# разрешить «Конфиг юнита» юниту:
sudo .venv/bin/python deploy/allowlist_tool.py grant vllm.service unit-cat unit-write
# глобальные ключи (один раз на менеджер):
sudo .venv/bin/python deploy/allowlist_tool.py set-global reset_failed true
sudo .venv/bin/python deploy/allowlist_tool.py set-global daemon_reload true
# показать текущее состояние:
sudo .venv/bin/python deploy/allowlist_tool.py list
```

После правки allowlist перезапускать панель не нужно (allowlist читается
helper'ом при каждом действии), но полезно проверить файл: `... check`.

---

## 4. Чек-лист добавления нового сервиса

1. Создать юнит (§1.1 или §1.2), `daemon-reload`, тестовый старт, журнал.
2. Решить вопрос с GPU/памятью: не пересекается ли юнит с работающими
   (`ss -ltnp`, `nvidia-smi`, сводка RAM)? Не включать `enable` до решения.
3. Внести в allowlist (§3) — для system-юнитов с кнопками.
4. Добавить в панель: «Поиск сервисов» или «по имени» (§2.1/§2.2), для
   постоянных сервисов — блок в `services.yaml` (§2.3).
5. Оформить порты/health/gpus по фактическому поведению; notes — для потомков.

---

## 5. Пример: Maya (system-юнит, 2026-10-28)

Контекст: Maya-M (GLM-5.3-Flash) в `/home/dispot/src/project-maya`, конфиг
`maya-maya-m.json`, веб-морда и API на одном порту 8010 (loopback), движок
пиннует почти всю свободную RAM (`MAYA_GLM_RAM_HEADROOM_GB=10`), старт ~6,5 мин,
graceful-остановка до 150 с (QUIT движку, освобождение VRAM/RAM).

### 5.1 Юнит

```bash
sudo tee /etc/systemd/system/maya-maya-m.service >/dev/null <<'EOF'
[Unit]
Description=Project Maya (Maya-M) - LLM station service
After=network-online.target gpu-power-limit.service
Wants=gpu-power-limit.service

[Service]
User=dispot
WorkingDirectory=/home/dispot/src/project-maya
Environment=MAYA_GLM_RAM_HEADROOM_GB=10
ExecStart=/home/dispot/src/project-maya/.venv/bin/python maya.py --config maya-maya-m.json --port 8010
Restart=on-failure
RestartSec=10
TimeoutStartSec=900
TimeoutStopSec=180
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
```

### 5.2 Allowlist (system-юнит — действия через helper)

```bash
sudo python3 - <<'EOF'
import json
p = "/etc/llm-panel/allowlist.json"
with open(p, encoding="utf-8") as f:
    d = json.load(f)
d["units"]["maya-maya-m.service"] = {
    "actions": ["start", "stop", "restart", "enable", "disable", "reset-failed",
                "status", "journal", "journal-follow"],
}
with open(p, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
EOF
```

### 5.3 Тестовый старт

```bash
sudo systemctl stop vllm.service          # vllm держит карты 0,1 — без этого
                                          # Maya упадёт «pack: out of memory»
sudo systemctl start maya-maya-m.service
journalctl -u maya-maya-m.service -f      # «ready: http://127.0.0.1:8010/v1»
```

`enable` отложен до решения о политике одновременности (этап 2): при загрузке
станции Maya и vllm сцепятся за карты 0/1.

### 5.4 Панель

`/config` → «Поиск сервисов» или «Добавить юнит по имени» → `maya-maya-m.service`.
Постоянный блок реестра — `deploy/examples/services.yaml.station-llm.yaml`
(id `maya`: порт 8010, tcp-health, startup_timeout 900, stop_timeout 180,
все три GPU по UUID, `cuda_addressing: index`, `ram_exclusive: true`).
