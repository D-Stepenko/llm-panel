"""Мастер «модель → сервис» (этап 3, v0.3.5): готовый сниппет services.yaml.

Панель НИЧЕГО не пишет: root-owned /etc/llm-panel/services.yaml и
systemd-юниты правит администратор ОС (ТЗ §5.5). Мастер — READ-ONLY
генератор:
  1. модель из каталога (/models — скан model_roots);
  2. движок: llamacpp (GGUF) | vllm (safetensors) — предвыбор по kind;
  3. карты — ПОЛНЫЕ UUID из NVML-снапшота; занятость (план/факт) — из
     матрицы (build_matrix), чтобы выбрать свободные сразу;
  4. порт — свободный из cfg.ports_min..ports_max: занятые в реестре
     (services.yaml + extra_defs) + bind-проба на 127.0.0.1;
  5. на выходе — валидный YAML-блок services.yaml, шаблон systemd-юнита
     и шаги установки. Ничего не исполняется и не сохраняется.
"""

from __future__ import annotations

import pathlib
import re
import socket

ENGINES = ("llamacpp", "vllm")
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+\.service$")
GPU_UUID_RE = re.compile(r"^GPU-[0-9a-fA-F-]{20,}$")   # как в registry (ТЗ §2)


def slugify(name: str) -> str:
    """Имя модели → база id/юнита: 'Qwen3.8-27B-FP8' → 'qwen3-8b-fp8'."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:48] or "model"


def suggest_unit(model_name: str, engine: str) -> str:
    prefix = "llama" if engine == "llamacpp" else "vllm"
    return f"{prefix}-{slugify(model_name)}.service"


# --------------------------------------------------------------- порты -------

def used_ports(state) -> set[int]:
    """Порты, занятые описанными сервисами (services.yaml + extra_defs)."""
    used: set[int] = set()
    for svc in tuple(state.registry.services) + tuple(state.extra_defs.values()):
        p = getattr(svc, "port", None)
        if p:
            used.add(int(p))
    return used


def _port_busy(port: int) -> bool:
    """Bind-проба на 127.0.0.1: порт занят реальным слушателем?"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        finally:
            s.close()
    except OSError:
        return True
    return False


def free_port(state, preferred: str = "") -> tuple[int | None, str]:
    """Свободный порт из cfg.ports_min..ports_max.

    preferred — значение формы (пусто/невалидно → авто-выбор первого
    свободного). Возвращает (порт | None, ключ_ошибки i18n).
    """
    lo, hi = int(state.cfg.ports_min), int(state.cfg.ports_max)
    used = used_ports(state)
    want: int | None = None
    if (preferred or "").strip():
        try:
            want = int(str(preferred).strip())
        except ValueError:
            return None, "wizard_err_port"
        if not (lo <= want <= hi):
            return None, "wizard_err_range"
    if want is not None:
        if want in used or _port_busy(want):
            return None, "wizard_err_busy"
        return want, ""
    for p in range(lo, hi + 1):
        if p not in used and not _port_busy(p):
            return p, ""
    return None, "wizard_err_nofree"


# ------------------------------------------------------------ GPU/матрица ----

def gpu_context(state) -> dict:
    """Карты из NVML-снапшота + занятость (план/факт) из матрицы конфликтов."""
    from .matrix import build_matrix

    snap = getattr(state.collector, "snapshot", None)
    defs = tuple(state.registry.services) + tuple(state.extra_defs.values())
    data = build_matrix(defs, snap.services if snap else None,
                        snap.gpus if snap else None)
    options = []
    for g in data["gpus"]:
        options.append({
            "uuid": g["uuid"],
            "index": g.get("index"),
            "name": g.get("name") or "",
            "mem_total_mb": g.get("mem_total_mb"),
            "planned_by": data["gpu_planned"].get(g["uuid"], []),
            "actual_by": data["gpu_actual"].get(g["uuid"], []),
        })
    return {"gpus": options, "rows": data["rows"]}


def valid_gpus(state, raw_list) -> list[str]:
    """Выбранные карты формы: только UUID, реально присутствующие в NVML."""
    known = {g["uuid"] for g in gpu_context(state)["gpus"]}
    out: list[str] = []
    for v in raw_list:
        u = str(v).strip()
        if u in known and u not in out:
            out.append(u)
    return out


def gpu_conflicts(state, gpus: list[str]) -> list[str]:
    """Предупреждения по выбранным картам: кто уже «запланирован/занят»."""
    ctx = gpu_context(state)
    names = {g["uuid"]: (g["name"] or g["uuid"][-12:]) for g in ctx["gpus"]}
    warns: list[str] = []
    for u in gpus:
        planned = next((g["planned_by"] for g in ctx["gpus"]
                        if g["uuid"] == u), [])
        actual = next((g["actual_by"] for g in ctx["gpus"] if g["uuid"] == u), [])
        label = names.get(u, u)
        if planned:
            warns.append(f"{label}: уже запланирована для «{', '.join(planned)}» "
                         "(запуск обоих одновременно — конфликт привязки)")
        if actual:
            warns.append(f"{label}: сейчас занята «{', '.join(actual)}» "
                         "(факт, NVML)")
    return warns


# ------------------------------------------------------- генерация сниппета --

def build_service(entry, engine: str, gpus: list[str], port: int, unit: str,
                  addressing: str, notes: str = "") -> dict:
    """Dict для services.yaml (валидация реестром — registry._parse_service)."""
    svc = {
        "id": slugify(entry.name),
        "name": f"{engine} — {entry.name}",
        "type": engine,
        "unit": unit,
        "managed": "adopted",
        "edit_mode": "manage_only",
        "port": int(port),
        "bind_host": "127.0.0.1",
        "health": {
            "kind": "http",
            "url": f"http://127.0.0.1:{int(port)}/health",
            "timeout_s": 3,
            "interval_s": 10,
        },
        "startup_timeout": 600,
        "gpus": list(gpus),
        "cuda_addressing": addressing,
    }
    if notes:
        svc["notes"] = notes
    return svc


def render_yaml(svc: dict) -> str:
    """Валидный YAML-блок для вставки в /etc/llm-panel/services.yaml."""
    import yaml
    return yaml.dump({"services": [svc]}, sort_keys=False,
                     allow_unicode=True, default_flow_style=False, width=100)


def unit_template(unit: str, entry, engine: str, port: int,
                  gpus: list[str], indices: tuple[int, ...]) -> str:
    """Шаблон systemd-юнита под выбранный движок (правит владелец станции)."""
    model_path = entry.path
    if engine == "llamacpp":
        exec_line = (f"ExecStart=/usr/local/bin/llama-server -m {model_path}/<model.gguf> "
                     f"--host 127.0.0.1 --port {port} -ngl 99 -fa on --jinja")
        env_line = (f'Environment=CUDA_VISIBLE_DEVICES="{",".join(gpus)}"'
                    if gpus else "# Environment=CUDA_VISIBLE_DEVICES= (карты не выбраны)")
        comment = "# адресация карт — по ПОЛНЫМ UUID (cuda_addressing: uuid)"
    else:
        exec_line = (f"ExecStart=<conda-env>/bin/vllm serve {model_path} "
                     f"--host 127.0.0.1 --port {port} --tensor-parallel-size "
                     f"{max(1, len(gpus))}")
        idx = ",".join(str(i) for i in indices) if indices else "<индексы>"
        env_line = f'Environment=CUDA_VISIBLE_DEVICES="{idx}"'
        comment = ("# vLLM адресует карты ПО ИНДЕКСАМ: индексы выше — на момент "
                   "запуска выведены заново из NVML (см. /matrix)")
    lines = [
        f"# /etc/systemd/system/{unit} — ШАБЛОН от мастера «модель → сервис».",
        "# Заполните фактические пути (бинарь движка, User) — панель юнит не пишет.",
        comment,
        "",
        "[Unit]",
        f"Description={engine} — {entry.name}",
        "After=network-online.target",
        "",
        "[Service]",
        "User=dispot",
        "WorkingDirectory=/home/dispot",
        exec_line,
        env_line,
        "Restart=on-failure",
        "RestartSec=30",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ]
    return "\n".join(lines)
