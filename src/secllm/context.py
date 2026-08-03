"""Shared application context."""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import Catalog
from .config import Config
from .health import HealthMonitor
from .supervisor import Supervisor


@dataclass
class Context:
    config: Config
    catalog: Catalog
    supervisor: Supervisor
    health: HealthMonitor
