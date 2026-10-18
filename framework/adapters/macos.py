"""MOCK: a real macOS device (MacStadium / AWS EC2 mac2) for native macOS/iOS targets."""
from __future__ import annotations

from ..spec import Environment, Spec
from .base import Provisioned


class MacOSAdapter:
    name = "macos"

    def wanted(self, env: Environment) -> bool:
        return env.needs_macos

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned:
        return Provisioned(
            adapter=self.name, real=False, status="simulated",
            detail=("would provision AWS EC2 mac2.metal (Apple M2, 24 GB, macOS 15 + Xcode 16) via dedicated host, "
                    "ETA ~8 min; inject CRUSOE_API_KEY/NTFY_TOPIC through SSM Parameter Store, "
                    "runner pulled over SSH, screen captured with screencapture + XCUITest"),
            connect_info={"ssh": f"ec2-user@mac-{job_id}.example.internal", "vnc_port": 5900})
