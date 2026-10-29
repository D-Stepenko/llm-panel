"""HF-загрузки моделей (этап 3, v0.3.1): фоновые задачи `hf download`.

Безопасность:
  * инструмент ищется абсолютным путём (shutil.which) — у systemd-юнита панели
    PATH урезан; нет ни `hf`, ни `huggingface-cli` → None, UI показывает
    уведомление с командой установки;
  * репозиторий строго по маске owner/name (без опций и шелл-метасимволов);
  * каталог назначения — только ВНУТРИ одного из model_roots (проверка
    resolve + os.path.commonpath), подкаталог создаётся самим загрузчиком;
  * максимум 3 одновременных задач, хвост вывода — 12 строк; токены/секреты
    не вводятся в панель (HF_TOKEN, если нужен, — в окружении станции).

Задачи живут в памяти панели и теряются при её рестарте (сам файлы загрузки
на диске остаются; докачка — на совесть повторного запуска).
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import time
import uuid
from collections import deque

MAX_CONCURRENT = 3
TAIL_LINES = 12

REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
SUBPATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-/ ]*$")   # без «..» (см. resolve)
HF_INSTALL_CMD = 'pip3 install --user -U "huggingface_hub[cli]"'

INSTALL_HINT = ("Если система ответит «externally-managed-environment» — "
                "добавьте флаг --break-system-packages к команде.")


def find_tool() -> str | None:
    """Абсолютный путь к hf / huggingface-cli (или None — UI покажет уведомление)."""
    for name in ("hf", "huggingface-cli"):
        p = shutil.which(name)
        if p:
            return p
    return None


def validate_repo(repo: str) -> bool:
    return bool(REPO_RE.match(repo.strip()))


def resolve_dest(root: str, subpath: str) -> tuple[str | None, str]:
    """root (из model_roots) + необязательный подкаталог → путь внутри root.

    Возвращает (путь | None, ключ ошибки). «..» и символические выходы
    отбраковываются проверкой общего префикса.
    """
    sub = (subpath or "").strip().strip("/")
    if sub and (not SUBPATH_RE.match(sub) or ".." in sub.split("/")):
        return None, "hf_err_subpath"
    base = os.path.realpath(root)
    dest = os.path.realpath(os.path.join(base, sub)) if sub else base
    if dest != base and not dest.startswith(base + os.sep):
        return None, "hf_err_subpath"
    if not os.path.isdir(base):
        return None, "hf_err_root_missing"
    return dest, ""


@dataclasses.dataclass
class HFJob:
    id: str
    repo: str
    dest: str
    status: str = "running"          # running | done | stopped | error
    exit_code: int | None = None
    started: float = dataclasses.field(default_factory=time.time)
    finished: float | None = None
    tail: deque = dataclasses.field(default_factory=lambda: deque(maxlen=TAIL_LINES))
    proc: object | None = None       # asyncio subprocess
    note: str = ""


def active_count(jobs: dict) -> int:
    return sum(1 for j in jobs.values() if j.status == "running")


def tail_text(job: HFJob) -> str:
    return "\n".join(job.tail)


async def start_job(state, tool: str, repo: str, dest: str) -> HFJob:
    """Запуск `hf download <repo> --local-dir <dest>` как фоновой задачи."""
    import asyncio

    jid = uuid.uuid4().hex[:8]
    os.makedirs(dest, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        tool, "download", repo.strip(), "--local-dir", dest,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=8192,
    )
    job = HFJob(id=jid, repo=repo.strip(), dest=dest, proc=proc)
    state.hf_jobs[jid] = job
    asyncio.create_task(_pump(proc, job), name=f"hf-{jid}")
    return job


async def _pump(proc, job: HFJob) -> None:
    """Читает вывод загрузчика в хвост задачи (строки прогресса HF)."""
    import asyncio

    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.strip():
                job.tail.append(line[-400:])
    except asyncio.CancelledError:
        return
    except Exception as e:  # noqa: BLE001 — ошибка чтения не должна ронять панель
        job.note = f"{type(e).__name__}: {e}"
    rc = await proc.wait()
    job.exit_code = rc
    job.finished = time.time()
    if rc == 0:
        job.status = "done"
    elif rc < 0 or job.status == "stopping":
        job.status = "stopped"
    else:
        job.status = "error"
        if not job.tail:
            job.tail.append(f"загрузчик завершился с кодом {rc}")


def request_stop(job: HFJob) -> bool:
    """Попросить задачу остановиться (SIGTERM; SIGKILL запрещён — ТЗ §4.2)."""
    if job.proc is None or job.status != "running":
        return False
    job.status = "stopping"
    try:
        job.proc.terminate()
    except ProcessLookupError:
        pass
    return True
