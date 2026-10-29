"""Каталог моделей (этап 3, v0.3.0): READ-ONLY скан каталогов с моделями.

Источники — model_roots (Config Panel → «Disks summary paths», иначе
models.roots из config.yaml). Ничего не скачивает и не пишет: только
метаданные файлов для страницы /models (загрузки HF и мастер
«модель → сервис» — следующие подэтапы).

Что считается моделью:
  * каталог с *.gguf (одиночный или шарды «…-00001-of-00003.gguf» —
    группируются в одну запись);
  * каталог с config.json (HF-формат, safetensors) — читаются
    model_type/torch_dtype/quantization_config.

Ограничения: глубина обхода, кап количества записей, скрытые каталоги
(.git, .cache) пропускаются; недоступные каталоги — в errors, не падение.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import re
import shutil

MAX_DEPTH_DEFAULT = 3
MAX_ENTRIES_DEFAULT = 500
GGM_SHARD_RE = re.compile(r"-\d{5}-of-\d{5}")
QUANT_RE = re.compile(
    r"\b(IQ\d+[_A-Za-z0-9]*|Q\d+[_A-Za-z0-9]*|FP8|FP16|F16|BF16|INT8|MXFP4|AWQ|GPTQ)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass
class ModelEntry:
    name: str            # отображаемое имя (каталог или база шард-группы)
    path: str            # каталог модели
    root: str            # корень, в котором найдено
    kind: str            # gguf | safetensors
    size_bytes: int      # суммарный размер файлов модели
    n_files: int
    family: str          # эвристика по имени (Qwen3, Llama, …)
    quant: str           # Q4_K_M / FP8 / … (пусто — не определён)
    dtype: str           # torch_dtype из config.json (safetensors)
    model_type: str      # model_type из config.json (safetensors)


def _dir_size(p: pathlib.Path) -> tuple[int, int]:
    total = 0
    count = 0
    try:
        for f in p.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                    count += 1
                except OSError:
                    pass
    except OSError:
        pass
    return total, count


def _family(name: str) -> str:
    token = re.split(r"[-_.\s]+", name.strip(), maxsplit=1)[0]
    return token or name


def _quant_from_name(name: str) -> str:
    m = QUANT_RE.search(name)
    return m.group(1).upper() if m else ""


def _gguf_base(fname: str) -> str:
    """base для шард-группы: 'model-00001-of-00003.gguf' → 'model'."""
    stem = fname[:-5] if fname.lower().endswith(".gguf") else fname
    return GGM_SHARD_RE.sub("", stem).rstrip("-. ")


def _gguf_entry(d: pathlib.Path, ggufs: list[str], root: str) -> ModelEntry:
    """Одна запись на каталог с GGUF.

    Один шард-набор (или одиночный файл) → имя по файлу, квант из него.
    Несколько РАЗНЫХ наборов (например talker+tokenizer, vocab+mmproj) →
    одна запись с именем КАТАЛОГА: иначе каждая вспомогательная GGUF-часть
    выглядела отдельной «моделью».
    """
    groups: dict[str, list[str]] = {}
    for n in ggufs:
        groups.setdefault(_gguf_base(n), []).append(n)
    if len(groups) == 1:
        base = sorted(groups)[0]
        files = groups[base]
        name = base
        quant = _quant_from_name(base)
    else:
        files = sorted(n for fs in groups.values() for n in fs)
        name = d.name
        quants = [q for q in (_quant_from_name(b) for b in groups) if q]
        quant = (max(set(quants), key=quants.count) if quants else "") \
            or _quant_from_name(name)
    size = 0
    for n in files:
        try:
            size += (d / n).stat().st_size
        except OSError:
            pass
    return ModelEntry(
        name=name, path=str(d), root=root, kind="gguf",
        size_bytes=size, n_files=len(files),
        family=_family(name), quant=quant,
        dtype="", model_type="",
    )


def _safetensors_info(d: pathlib.Path) -> tuple[str, str]:
    """(model_type, dtype) из config.json; пусто, если не читается."""
    model_type = dtype = ""
    try:
        cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        model_type = str(cfg.get("model_type") or "")
        dtype = str(cfg.get("torch_dtype") or "")
        q = cfg.get("quantization_config")
        if isinstance(q, dict) and q.get("method"):
            dtype = dtype or str(q["method"])
    except (OSError, ValueError):
        pass
    return model_type, dtype


def scan_models(roots, max_depth: int = MAX_DEPTH_DEFAULT,
                max_entries: int = MAX_ENTRIES_DEFAULT) -> dict:
    """Скан корней → {models, roots, ts, errors}. Только чтение, без side-effect'ов."""
    models: list[ModelEntry] = []
    errors: list[str] = []
    roots_info: list[dict] = []
    seen: set[str] = set()      # realpath каталога: симлинки и вложенные корни — без дублей

    for raw in roots or []:
        root = str(raw).strip()
        if not root:
            continue
        p = pathlib.Path(root)
        if not p.is_dir():
            roots_info.append({"path": root, "exists": False, "free_bytes": None})
            continue
        try:
            free = shutil.disk_usage(p).free
        except OSError:
            free = None
        roots_info.append({"path": root, "exists": True, "free_bytes": free})

        try:
            it = _walk(p, max_depth)
        except OSError as e:
            errors.append(f"{root}: {type(e).__name__}: {e}")
            continue
        for d in it:
            try:
                rp = os.path.realpath(d)
            except OSError:
                rp = str(d)
            if rp in seen:      # симлинк или уже посещённый (вложенный корень)
                continue
            seen.add(rp)
            if len(models) >= max_entries:
                errors.append(f"Достигнут лимит записей ({max_entries}) — каталог показан не весь")
                break
            try:
                names = sorted(os.listdir(d))
            except OSError:
                continue
            ggufs = [n for n in names if n.lower().endswith(".gguf")]
            if ggufs:
                models.append(_gguf_entry(d, ggufs, root))
            elif "config.json" in names:
                model_type, dtype = _safetensors_info(d)
                size, n_files = _dir_size(d)
                name = d.name
                models.append(ModelEntry(
                    name=name, path=str(d), root=root, kind="safetensors",
                    size_bytes=size, n_files=n_files,
                    family=_family(name), quant=_quant_from_name(name),
                    dtype=dtype, model_type=model_type,
                ))

    return {
        "models": models,
        "roots": roots_info,
        "errors": errors,
        "ts": __import__("time").time(),
    }


def _walk(root: pathlib.Path, max_depth: int):
    """Итерация каталогов глубины ≤ max_depth (сами корневые — тоже)."""
    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        yield d
        if depth >= max_depth:
            continue
        try:
            for child in d.iterdir():
                if child.is_dir() and not child.name.startswith("."):
                    stack.append((child, depth + 1))
        except OSError:
            continue


def human_bytes(n: int | None) -> str:
    """Человекочитаемый размер (для шаблонов — как фильтр)."""
    if n is None:
        return ""
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ", "ПБ"):
        if value < 1024 or unit == "ПБ":
            return f"{value:.1f} {unit}" if unit not in ("Б",) else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.1f} ПБ"
