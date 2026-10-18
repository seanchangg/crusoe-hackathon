"""Adapter protocol: each adapter decides if an Environment needs it, then provisions (real or simulated)."""
from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel

from ..spec import Environment, Spec


class Provisioned(BaseModel):
    adapter: str
    real: bool
    status: Literal["ready", "simulated", "failed"]
    detail: str
    connect_info: dict = {}
    live_url: str | None = None


@runtime_checkable
class Adapter(Protocol):
    name: str

    def wanted(self, env: Environment) -> bool: ...

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned: ...
