# llm-panel — инструкция по развертыванию

Универсальная установка на сервер с systemd и NVIDIA GPU (Ubuntu 22.04/24.04+
проверены). Все пути/имена пользователей — параметры; никакие значения конкретного
сервера не зашиты в код. Для истории установки на станции-LLM см. `docs/STATION-LLM.md`.

## 1. Требования

- Linux + systemd; Python 3.11+ (3.12 рекомендован); NVIDIA GPU + драйвер с NVML
  (без GPU панель тоже работает — GPU-блок покажет «нет данных»).
- Пользователь (далее `paneluser`) с правом `sudo` для шагов установки. Панель сама
  работает НЕ от root.
- Доступ в LAN для веб-доступа через Caddy (панель слушает только `127.0.0.1`).

## 2. Обследование сервера (read-only, до изменений)

```bash
git clone https://github.com/D-Stepenko/llm-panel.git
cd llm-panel
bash scripts/survey.sh > survey.out 2>&1      # ничего не меняет, только читает
bash scripts/survey2.sh                        # уточнения при необходимости
```

Из вывода берутся: фактические имена юнитов (`systemctl list-units --type=service`),
порты и адреса слушания, health-эндпоинты, GPU-UUID (`nvidia-smi -L`).

## 3. Быстрая установка (install.sh)

```bash
sudo bash deploy/install.sh --user paneluser
```

Параметры:

| Параметр | По умолчанию | Назначение |
|---|---|---|
| `--user` | обязателен | пользователь, от которого работает панель |
| `--dir` | `~paneluser/llm-panel` | каталог кода и `.venv` |
| `--config-dir` | `/etc/llm-panel` | каталог конфигурации |
| `--data-dir` | `/var/lib/llm-panel` | каталог данных (БД), права 0700 |
| `--with-sudoers` | выкл | установить sudoers-правило (см. §4.6 — требуется явное решение) |
| `--no-start` | выкл | не запускать панель сразу |

Скрипт: создаёт venv в домашнем каталоге пользователя (не системный Python),
ставит зависимости, создаёт каталоги, копирует шаблоны конфигов (**существующие
файлы никогда не перезаписываются**), устанавливает helper (root:root 0755),
инициализирует БД, генерирует systemd-юнит из шаблона, `daemon-reload`,
`enable --now`.

## 4. Ручная установка (то же самое по шагам)

4.1. Код и venv:
```bash
sudo git clone https://github.com/D-Stepenko/llm-panel.git /home/paneluser/llm-panel
sudo chown -R paneluser:paneluser /home/paneluser/llm-panel
cd /home/paneluser/llm-panel
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

4.2. Каталоги:
```bash
sudo mkdir -p /etc/llm-panel /var/lib/llm-panel
sudo chown paneluser:paneluser /var/lib/llm-panel && chmod 700 /var/lib/llm-panel
```

4.3. Конфигурация (шаблоны → рабочие файлы, затем отредактировать):
```bash
sudo install -m 644 deploy/config.yaml.example    /etc/llm-panel/config.yaml
sudo install -m 644 deploy/services.yaml.example  /etc/llm-panel/services.yaml
sudo install -m 644 deploy/allowlist.json.example /etc/llm-panel/allowlist.json
```
- `config.yaml`: `trusted_hosts` — адрес/имя, по которому панель открывают из браузера;
  `models.roots` — пути дисковой сводки (дальше меняется из UI);
  `systemd.panel_unit` — юнит самой панели: карточка llm-panel на Статусе наравне
  с сервисами (пустая строка — отключить; действия требуют записи в allowlist).
- `services.yaml`: юниты ВАШЕГО сервера (см. §6). systemd **user-юниты**
  (`~/.config/systemd/user/*.service`) не видны системному менеджеру — для них
  укажите `scope: user`; чтение/действия/журнал панель выполняет напрямую
  `systemctl --user` / `journalctl --user` (helper и allowlist не нужны;
  юнит должен принадлежать тому же пользователю, от которого работает панель).
- `allowlist.json`: те же юниты + разрешённые действия (включая `llm-panel.service`,
  если нужна карточка самой панели с действиями).

4.4. Helper:
```bash
sudo install -o root -g root -m 755 deploy/helper/llm-panel-systemd /usr/local/sbin/llm-panel-systemd
```
Проверить, что ни один каталог в пути не доступен `paneluser` на запись.

4.5. БД и пароль (обязательно с той же конфигурацией, что у панели):
```bash
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml init-db
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml set-password
```

4.6. **sudoers — отдельное решение владельца (ТЗ §12.5).** Без него управление
сервисами из панели невозможно (чтение статусов работает, кнопки — нет). Право ровно
одно: запуск root-owned helper с фиксированным путём, NOPASSWD. Риск: безпарольная
root-операция на фикс. путь; второй рубеж проверок — сам helper (allowlist).
```bash
sudo sed "s/^dispot /paneluser /" deploy/sudoers.llm-panel | sudo tee /etc/sudoers.d/llm-panel >/dev/null
sudo chmod 0440 /etc/sudoers.d/llm-panel && sudo visudo -c
# проверка: sudo -n /usr/local/sbin/llm-panel-systemd status <ваш-юнит>.service
# откат:    sudo rm /etc/sudoers.d/llm-panel && sudo visudo -c
```

4.7. systemd-юнит панели (генерируется из шаблона):
```bash
sed -e "s|@@PANEL_USER@@|paneluser|g" -e "s|@@PANEL_GROUP@@|paneluser|g" \
    -e "s|@@PANEL_DIR@@|/home/paneluser/llm-panel|g" \
    -e "s|@@CONFIG_FILE@@|/etc/llm-panel/config.yaml|g" \
    deploy/llm-panel.service.template | sudo tee /etc/systemd/system/llm-panel.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now llm-panel
```
**Внимание:** не добавляйте `NoNewPrivileges=yes` — флаг делает sudo невозможным,
управление сервисами сломается с «helper завершился с кодом 1» (реальный случай
2026-10-28). `ProtectSystem=strict` требует явного `ReadWritePaths=` на каталог данных.

## 5. Публикация через Caddy (HTTPS)

Отдельный сайт-блок с собственным портом (под-путь `/panel/*` не используется —
абсолютные пути панели `/login`, `/static/…` под ним не работают):
```caddy
192.168.1.10:8444 {
    tls internal
    @lan remote_ip 192.168.0.0/16
    handle @lan {
        reverse_proxy 127.0.0.1:8443
    }
}
```
```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```
UFW для 8443 (upstream) не открывать; 8444 — только с адресов LAN.

## 6. Настройка под свой сервер (юниты, GPU, диски)

1. `nvidia-smi -L` → полные UUID карт (НЕ индексы: индексы меняются между загрузками).
2. Заполнить `services.yaml`: по одному блоку на юнит (шаблон с комментариями —
   `deploy/services.yaml.example`, реальный пример — `deploy/examples/`).
   Имена юнитов писать точно как в systemctl (регистр сохраняется).
3. Заполнить `allowlist.json` теми же юнитами; для юнитов «только просмотр» оставить
   `status`+`journal`.
4. Диски сводки — в Панели настроек (`/config`) или стартово в `models.roots`.
5. GPU/CPU/RAM определяются автоматически, настраивать не нужно.
6. Проверка конфигурации: `.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml check-config`

Добавление нового сервиса как systemd-юнита (system или user) и показ его на
панели — пошагово в `docs/UNITS.md`; быстрое добавление юнита без правки yaml —
«Поиск сервисов» / «Добавить юнит по имени» в Config Panel.

## 7. Обновление версии

```bash
cd /home/paneluser/llm-panel
git pull                                    # конфиги в /etc и БД в /var/lib не трогаются
.venv/bin/pip install -r requirements.txt   # если requirements менялся
.venv/bin/python -m llm_panel --config /etc/llm-panel/config.yaml check-config
sudo systemctl restart llm-panel
```
Если git-доступа с сервера нет — скопируйте изменённые файлы из репозитория вручную
(`.venv` не заменять), затем `restart`.

## 8. Аварийные процедуры

- Остановить панель, не трогая LLM-сервисы: `sudo systemctl disable --now llm-panel`
  (панель не «владеет» сервисами — их юниты не остановятся).
- Восстановление доступа (блокировка входа):
  `.venv/bin/llm-panel --config /etc/llm-panel/config.yaml lockout-reset`
- Сброс пароля: `llm-panel set-password` (интерактивно; все сессии инвалидируются).
- Бэкап: `panel.db` (WAL) + `/etc/llm-panel/{config,services}.yaml`, `allowlist.json`.
  Восстановление: остановить панель, скопировать файлы с теми же правами.
- Откат helper/sudoers: удалить файлы, `visudo -c`, `daemon-reload`.

## 9. Проверки после установки

```bash
systemctl status llm-panel --no-pager          # панель стартовала от paneluser
ss -ltnp | grep 8443                           # слушает ТОЛЬКО 127.0.0.1
curl -sk https://<IP>:8444/ -o /dev/null -w '%{http_code}\n'   # через Caddy — 302/200
curl -m 3 http://<IP>:8443/                    # ожидание: отказ/таймаут (извне недоступна)
sudo -n /usr/local/sbin/llm-panel-systemd status <ваш-юнит>.service    # успех
sudo -n /usr/local/sbin/llm-panel-systemd start stranger.service       # ожидание: ОТКАЗ (rc=3)
```
В UI: вход, статусы, Старт/Стоп на разрешённом юните, `/audit` содержит записи,
`/config` применяет настройки без рестарта.
