"""Health monitor — polls each worker's ``/health``, updates its state, and auto-restarts
workers that go unhealthy (bounded, with a startup grace so slow model loads aren't killed)."""

from __future__ import annotations

import asyncio
import time

import httpx

from .backends import HEALTH_PATH
from .config import Config
from .supervisor import Supervisor, Worker

FAILURE_THRESHOLD = 3  # consecutive failed probes before auto-restart
MAX_RESTARTS = 5  # give up (mark error) after this many auto-restarts


class HealthMonitor:
    def __init__(self, cfg: Config, supervisor: Supervisor) -> None:
        self.cfg = cfg
        self.sup = supervisor
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def check_once(self) -> None:
        """Run a single probe pass (also used directly in tests)."""
        async with httpx.AsyncClient(timeout=self.cfg.health_timeout) as client:
            for worker in list(self.sup.workers.values()):
                await self._check(client, worker)

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=self.cfg.health_timeout) as client:
            while not self._stop.is_set():
                for worker in list(self.sup.workers.values()):
                    await self._check(client, worker)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.health_interval)
                except asyncio.TimeoutError:
                    pass

    async def _check(self, client: httpx.AsyncClient, worker: Worker) -> None:
        if not worker.process_alive():
            worker.state = "error"
            worker.error = "worker process exited"
            await self._maybe_restart(worker)
            return

        try:
            resp = await client.get(worker.base_url + HEALTH_PATH)
            ok = resp.status_code == 200
        except (httpx.HTTPError, OSError):
            ok = False

        if ok:
            worker.state = "healthy"
            worker.last_health = time.time()
            worker.consecutive_failures = 0
            worker.error = ""
            return

        worker.consecutive_failures += 1
        # A model still loading (within the grace window) is expected to fail probes.
        if worker.state == "starting" and worker.uptime_s < self.cfg.startup_grace:
            return
        worker.state = "unhealthy"
        if worker.consecutive_failures >= FAILURE_THRESHOLD:
            await self._maybe_restart(worker)

    async def _maybe_restart(self, worker: Worker) -> None:
        if worker.restarts >= MAX_RESTARTS:
            worker.state = "error"
            worker.error = f"exceeded restart limit ({MAX_RESTARTS})"
            return
        # Supervisor ops are quick/sync; safe to call from the loop.
        self.sup.reload(worker.model_id)
