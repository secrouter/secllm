"""GPU discovery + worker placement — how SecLLM spreads model workers across GPUs.

Turns "load N models" into concrete GPU assignments: which device(s) each worker is pinned to
(``CUDA_VISIBLE_DEVICES``) and what fraction of each it reserves (vLLM ``--gpu-memory-utilization``),
so several models coexist on one big GPU *or* spread one-per-GPU across many — without any of them
OOMing a card that's already full.

:func:`plan_placement` is a **pure function** of the current allocation, so it unit-tests with a
synthetic GPU list (no GPU / nvidia-smi needed). :func:`detect_gpus` is the only impure part and
degrades to ``[]`` on any host without a working ``nvidia-smi`` (CPU box, mock backend, driver
down), so the supervisor simply falls back to the historical single-GPU-unmanaged launch there.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Gpu:
    index: int
    name: str
    total_mib: int
    free_mib: int


@dataclass
class Placement:
    """Where a worker runs: the device indices to pin (``CUDA_VISIBLE_DEVICES``) and the per-GPU
    memory fraction to hand vLLM. ``gpus`` has one entry for a single-GPU model, ``tp`` entries for
    a tensor-parallel one (each spanned GPU reserves the same fraction)."""

    gpus: list[int]
    memory_fraction: float

    @property
    def visible_devices(self) -> str:
        return ",".join(str(g) for g in self.gpus)


def detect_gpus() -> list[Gpu]:
    """The NVIDIA GPUs on this host, via ``nvidia-smi``. Returns ``[]`` when nvidia-smi is absent or
    errors — a CPU/mock host, or a driver that isn't up — so GPU management is strictly opt-in on
    the presence of real hardware and never a hard requirement."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(  # noqa: S603
            [exe, "--query-gpu=index,name,memory.total,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus: list[Gpu] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            gpus.append(Gpu(index=int(parts[0]), name=parts[1],
                            total_mib=int(float(parts[2])), free_mib=int(float(parts[3]))))
        except ValueError:
            continue
    return gpus


def parse_tensor_parallel(vllm_args: list[str]) -> int:
    """The ``--tensor-parallel-size`` a model requests (default 1). A TP model spans that many
    GPUs, so the scheduler must place it on that many cards at once."""
    for i, a in enumerate(vllm_args):
        if a in ("--tensor-parallel-size", "-tp") and i + 1 < len(vllm_args):
            try:
                return max(1, int(vllm_args[i + 1]))
            except ValueError:
                return 1
        if a.startswith("--tensor-parallel-size="):
            try:
                return max(1, int(a.split("=", 1)[1]))
            except ValueError:
                return 1
    return 1


def plan_placement(*, need: float, tp: int, gpu_indices: list[int],
                   allocated: dict[int, float], cap: float) -> Placement | None:
    """Pick GPU(s) for a worker that reserves ``need`` fraction on each of ``tp`` GPUs.

    Chooses the least-allocated GPUs that still have room (``allocated[g] + need <= cap``), so
    workers spread across cards first and pack onto a shared card only up to ``cap``. Returns
    ``None`` when fewer than ``tp`` GPUs have room — the caller then surfaces a clear "no capacity"
    error instead of launching a worker that would OOM a full card. ``need`` is clamped to ``cap``
    (a model can't reserve more of a GPU than the cap permits)."""
    if tp < 1 or len(gpu_indices) < tp:
        return None
    need = min(need, cap)
    # GPUs with room, least-loaded first; ties broken by index for deterministic, stable placement.
    candidates = sorted(
        (allocated.get(g, 0.0), g) for g in gpu_indices
        if allocated.get(g, 0.0) + need <= cap + 1e-9
    )
    if len(candidates) < tp:
        return None
    chosen = sorted(g for _, g in candidates[:tp])
    return Placement(gpus=chosen, memory_fraction=round(need, 4))
