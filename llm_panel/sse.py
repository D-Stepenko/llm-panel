"""SSE-хаб: один общий сборщик данных для всех подключённых клиентов (ТЗ §3, §5.1)."""

from __future__ import annotations

import asyncio


class Hub:
    """Шина рассылки снапшотов подписчикам SSE. Лёгкая: нет дедлайнов и буферов на страницы."""

    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict) -> None:
        for q in tuple(self._subs):
            # событие предназначено для конкретного клиента
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # медленный клиент — пропускаем итерацию, он получит следующий тик
                pass

    @property
    def client_count(self) -> int:
        return len(self._subs)
