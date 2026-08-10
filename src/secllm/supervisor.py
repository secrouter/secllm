"""Worker supervisor — the lifecycle of model worker subprocesses.

Each loaded model is one worker (a ``vllm serve`` or mock subprocess) on its own port. The
supervisor starts/stops/reloads them and decides which GPU(s) each runs on.

By default (``max_loaded == 0``) several models coexist, bounded by real GPU capacity: the
scheduler in :mod:`secllm.gpu` packs each worker onto the least-loaded card(s) that still have
room under ``gpu_cap``, pinning it via ``CUDA_VISIBLE_DEVICES`` and telling vLLM exactly what
fraction to reserve — so loading a second model no longer evicts the first. Setting
``max_loaded`` > 0 restores the old hard ceiling (e.g. 1 = loading a new model *switches* to it
by evicting the oldest). On a mock/CPU host (no ``nvidia-smi``) there's no inventory to manage,
so placement is a no-op and workers launch exactly as before.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .backends import build_launch_command, worker_base_url
from .catalog import Catalog, Model
from .config import Config
from .gpu import Gpu, detect_gpus, parse_tensor_parallel, plan_placement


class CapacityError(RuntimeError):
    """Raised by :meth:`Supervisor.load` when the GPU inventory has no room for a model under
    the current allocation + ``gpu_cap`` — surfaced to the operator (HTTP 409) instead of
    launching a worker that would OOM an already-full card."""


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
    # 0 = no admin-supplied override — this worker is running at the catalog's own
    # vllm_args/mlx defaults. >0 = the context length (tokens) actually passed to the backend
    # via build_launch_command, overriding the catalog default for THIS worker only.
    context_length: int = 0
    # GPU placement the scheduler chose for this worker (see gpu.py): the device indices it's
    # pinned to via CUDA_VISIBLE_DEVICES (one for a single-GPU model, several for a tensor-
    # parallel one), and the per-GPU VRAM fraction it reserves. [] / 0.0 = launched unmanaged
    # (mock/CPU host, or no GPU inventory) — the historical single-GPU behaviour.
    gpus: list[int] = field(default_factory=list)
    memory_fraction: float = 0.0

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

        # GPU inventory for the co-residency scheduler. The mock backend never touches real
        # hardware, so skip detection there; otherwise ask nvidia-smi (→ [] on any non-GPU
        # host, see gpu.detect_gpus). SECLLM_GPUS, if set, restricts the usable set — filtered
        # to what was actually detected when detection found something, else trusted as-is (so
        # an operator can still pin indices on a host where detection came up empty).
        self._gpus: list[Gpu] = [] if cfg.backend == "mock" else detect_gpus()
        if cfg.gpu_devices:
            detected = {g.index for g in self._gpus}
            self._gpu_indices: list[int] = (
                [i for i in cfg.gpu_devices if i in detected] if detected else list(cfg.gpu_devices)
            )
        else:
            self._gpu_indices = [g.index for g in self._gpus]

    # ---- lifecycle ---------------------------------------------------------

    def load(self, model_id: str, context_length: int = 0) -> Worker:
        """Load ``model_id``, optionally overriding its catalog context length (tokens) for
        THIS worker only — 0 (the default) leaves the catalog's own vllm_args/mlx defaults
        untouched, exactly as before this parameter existed. Ignored if the model is already
        loaded (the existing early-return below), same as any other load-while-loaded call."""
        if model_id in self.workers and self.workers[model_id].state != "stopped":
            return self.workers[model_id]
        model = self.catalog.get(model_id)
        if not model:
            raise KeyError(f"unknown model {model_id!r}")

        # Hard-ceiling mode only (max_loaded > 0): evict the oldest active worker(s) until
        # there's room, i.e. switch semantics. With max_loaded == 0 (the default) models
        # coexist and capacity is governed by _place() against real GPU inventory instead.
        if self.cfg.max_loaded > 0:
            active = [w for w in self.workers.values() if w.state != "stopped"]
            while active and len(active) >= self.cfg.max_loaded:
                oldest = min(active, key=lambda w: w.loaded_seq)
                self.unload(oldest.model_id)
                active = [w for w in self.workers.values() if w.state != "stopped"]

        # Placement runs AFTER any eviction (so freed VRAM counts). May raise CapacityError.
        gpus, fraction = self._place(model)

        port = self._alloc_port()
        cmd = build_launch_command(self.cfg, model, port,
                                   context_length=context_length or None,
                                   memory_fraction=fraction)
        log = open(self._logdir / f"{model_id}.log", "ab", buffering=0)  # noqa: SIM115
        # Pin the child to its assigned card(s). Unmanaged (no inventory) → env=None, i.e. the
        # child inherits the parent's environment untouched, exactly as before.
        env = ({**os.environ, "CUDA_VISIBLE_DEVICES": ",".join(map(str, gpus))}
               if gpus else None)
        proc = subprocess.Popen(  # noqa: S603
            cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(self.cfg.data_dir), env=env
        )
        self._seq += 1
        worker = Worker(
            model_id=model_id, host=self.cfg.worker_host, port=port,
            process=proc, loaded_seq=self._seq, context_length=context_length,
            gpus=gpus, memory_fraction=fraction or 0.0,
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

    def reload(self, model_id: str, context_length: int = 0) -> Worker:
        """Restart ``model_id``. ``context_length`` explicitly given (>0) sets the new
        override; omitted (0) carries forward whatever override (if any) was already running
        — a plain "restart with the same config" — rather than silently resetting to the
        catalog default."""
        if not self.catalog.get(model_id):
            raise KeyError(f"unknown model {model_id!r}")
        prior = self.workers.get(model_id)
        restarts = (prior.restarts + 1) if prior else 0
        effective = context_length or (prior.context_length if prior else 0)
        self.unload(model_id)
        worker = self.load(model_id, context_length=effective)
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

    def gpu_summary(self) -> dict:
        """A compact view of GPU management for the console/API: whether this host is managed
        at all (a real inventory exists), the per-GPU cap, and each card's current committed
        allocation (sum of the VRAM fractions of the workers pinned to it)."""
        allocated = self._allocated()
        return {
            "managed": bool(self._gpu_indices),
            "cap": self.cfg.gpu_cap,
            "gpus": [
                {"index": g.index, "name": g.name,
                 "allocated": round(allocated.get(g.index, 0.0), 4)}
                for g in self._gpus
            ],
        }

    # ---- internals ---------------------------------------------------------

    def _allocated(self) -> dict[int, float]:
        """Committed VRAM fraction per GPU index across all active (non-stopped) workers —
        the live input the scheduler subtracts from ``gpu_cap`` to decide what still fits."""
        alloc: dict[int, float] = {}
        for w in self.workers.values():
            if w.state == "stopped":
                continue
            for g in w.gpus:
                alloc[g] = alloc.get(g, 0.0) + w.memory_fraction
        return alloc

    def _place(self, model: Model) -> tuple[list[int], float | None]:
        """Choose GPU(s) + a per-GPU VRAM fraction for ``model`` under the current allocation.

        Returns ``([], None)`` on an unmanaged host (no GPU inventory) — the caller then
        launches the worker exactly as before, with no pinning and vLLM's global default
        utilization. Otherwise delegates to :func:`gpu.plan_placement`; a ``None`` plan (no
        card has room) becomes a :class:`CapacityError` rather than an OOM-bound launch."""
        if not self._gpu_indices:
            return [], None
        need = model.vram_fraction or self.cfg.gpu_memory_utilization
        tp = parse_tensor_parallel(model.vllm_args)
        placement = plan_placement(
            need=need, tp=tp, gpu_indices=self._gpu_indices,
            allocated=self._allocated(), cap=self.cfg.gpu_cap,
        )
        if placement is None:
            raise CapacityError(
                f"no GPU capacity for {model.id!r}: needs {tp}×{need:.2f} VRAM under a "
                f"{self.cfg.gpu_cap:.2f} cap on GPUs {self._gpu_indices}; "
                f"current allocation {self._allocated()}"
            )
        return placement.gpus, placement.memory_fraction

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
