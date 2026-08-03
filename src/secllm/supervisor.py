"""Worker supervisor — the lifecycle of model worker subprocesses.

Each loaded model is one worker (a ``vllm serve`` or mock subprocess) on its own port.
The supervisor starts/stops/reloads them and, when at ``max_loaded`` capacity (a single GPU
is usually 1), evicts the oldest to make room — i.e. loading a new model *switches* to it.
"""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .backends import build_launch_command, worker_base_url
from .catalog import Catalog
from .config import Config


@dataclass
class Worker:
    model_id: str
    host: str
    port: int
    process: subprocess.Popen
    state: str = "starting"  # starting | healthy | unhealthy | stopped | error
    started_at: float = field(default_factory=time.time)
    loaded_seq: int = 0
    last_health: float | None = None
    consecutive_failures: int = 0
    restarts: int = 0
    error: str = ""

    @property
    def base_url(self) -> str:
        return worker_base_url(self.host, self.port)

    @property
    def uptime_s(self) -> float:
        return time.time() - self.started_at

    def process_alive(self) -> bool:
        return self.process.poll() is None


class Supervisor:
    def __init__(self, cfg: Config, catalog: Catalog) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.workers: dict[str, Worker] = {}
        self._seq = 0
        self._logdir = cfg.data_dir / "logs"
        self._logdir.mkdir(parents=True, exist_ok=True)

    # ---- lifecycle ---------------------------------------------------------

    def load(self, model_id: str) -> Worker:
        if model_id in self.workers and self.workers[model_id].state != "stopped":
            return self.workers[model_id]
        model = self.catalog.get(model_id)
        if not model:
            raise KeyError(f"unknown model {model_id!r}")

        # Capacity: evict the oldest active worker(s) until there's room (switch semantics).
        active = [w for w in self.workers.values() if w.state != "stopped"]
        while active and len(active) >= self.cfg.max_loaded:
            oldest = min(active, key=lambda w: w.loaded_seq)
            self.unload(oldest.model_id)
            active = [w for w in self.workers.values() if w.state != "stopped"]

        port = self._alloc_port()
        cmd = build_launch_command(self.cfg, model, port)
        log = open(self._logdir / f"{model_id}.log", "ab", buffering=0)  # noqa: SIM115
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.cfg.data_dir)
        )
        self._seq += 1
        worker = Worker(
            model_id=model_id, host=self.cfg.worker_host, port=port,
            process=proc, loaded_seq=self._seq,
        )
        self.workers[model_id] = worker
        return worker

    def unload(self, model_id: str) -> None:
        worker = self.workers.pop(model_id, None)
        if not worker:
            return
        if worker.process.poll() is None:
            worker.process.terminate()
            try:
                worker.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                worker.process.kill()
        worker.state = "stopped"

    def reload(self, model_id: str) -> Worker:
        if not self.catalog.get(model_id):
            raise KeyError(f"unknown model {model_id!r}")
        prior = self.workers.get(model_id)
        restarts = (prior.restarts + 1) if prior else 0
        self.unload(model_id)
        worker = self.load(model_id)
        worker.restarts = restarts
        return worker

    def stop_all(self) -> None:
        for model_id in list(self.workers):
            self.unload(model_id)

    # ---- accessors ---------------------------------------------------------

    def get(self, model_id: str) -> Worker | None:
        return self.workers.get(model_id)

    def list(self) -> list[Worker]:
        return list(self.workers.values())

    def ready(self, model_id: str) -> Worker | None:
        """Return the worker for a model only if it is loaded and healthy."""
        worker = self.workers.get(model_id)
        return worker if worker and worker.state == "healthy" else None

    # ---- internals ---------------------------------------------------------

    def _alloc_port(self) -> int:
        used = {w.port for w in self.workers.values()}
        base = self.cfg.worker_port_base
        for port in range(base, base + 200):
            if port in used:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((self.cfg.worker_host, port))
                    return port
                except OSError:
                    continue
        raise RuntimeError("no free worker port available")
