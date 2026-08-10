"""Unit tests for the GPU scheduler (gpu.py) — pure placement, no real GPU/nvidia-smi."""

from __future__ import annotations

from secllm.gpu import Placement, parse_tensor_parallel, plan_placement


def test_parse_tensor_parallel():
    assert parse_tensor_parallel([]) == 1
    assert parse_tensor_parallel(["--max-model-len", "16384"]) == 1
    assert parse_tensor_parallel(["--tensor-parallel-size", "2"]) == 2
    assert parse_tensor_parallel(["-tp", "4"]) == 4
    assert parse_tensor_parallel(["--tensor-parallel-size=8"]) == 8
    assert parse_tensor_parallel(["--tensor-parallel-size", "junk"]) == 1  # fail-safe → 1


def test_solo_model_takes_its_fraction():
    p = plan_placement(need=0.90, tp=1, gpu_indices=[0], allocated={}, cap=0.95)
    assert p == Placement(gpus=[0], memory_fraction=0.90)
    assert p.visible_devices == "0"


def test_single_gpu_coresidence():
    # Two 0.45 models fit together on one card (0.45 + 0.45 <= 0.95 cap).
    p1 = plan_placement(need=0.45, tp=1, gpu_indices=[0], allocated={}, cap=0.95)
    assert p1.gpus == [0]
    p2 = plan_placement(need=0.45, tp=1, gpu_indices=[0], allocated={0: 0.45}, cap=0.95)
    assert p2 is not None and p2.gpus == [0]


def test_single_gpu_no_fit_is_none():
    # A second 0.90 model can't join a card already holding 0.90 (would OOM) → None, not a bad launch.
    assert plan_placement(need=0.90, tp=1, gpu_indices=[0], allocated={0: 0.90}, cap=0.95) is None


def test_multi_gpu_spreads_least_loaded():
    # gpu0 already full; a new 0.90 model lands on the empty gpu1.
    p = plan_placement(need=0.90, tp=1, gpu_indices=[0, 1], allocated={0: 0.90}, cap=0.95)
    assert p is not None and p.gpus == [1]
    # With both empty, it picks the lowest index (deterministic).
    p2 = plan_placement(need=0.90, tp=1, gpu_indices=[0, 1], allocated={}, cap=0.95)
    assert p2.gpus == [0]


def test_tensor_parallel_spans_multiple_gpus():
    p = plan_placement(need=0.90, tp=2, gpu_indices=[0, 1, 2, 3], allocated={0: 0.90}, cap=0.95)
    # gpu0 is full → the two least-loaded remaining (1, 2).
    assert p is not None and p.gpus == [1, 2] and p.visible_devices == "1,2"


def test_tp_needs_enough_gpus():
    assert plan_placement(need=0.90, tp=2, gpu_indices=[0], allocated={}, cap=0.95) is None


def test_no_gpus_is_none():
    assert plan_placement(need=0.90, tp=1, gpu_indices=[], allocated={}, cap=0.95) is None


def test_need_clamped_to_cap():
    p = plan_placement(need=1.5, tp=1, gpu_indices=[0], allocated={}, cap=0.90)
    assert p is not None and p.memory_fraction == 0.90
