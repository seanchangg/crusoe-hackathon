"""REAL: a full Linux desktop inside the job's sandbox (Xvfb :99 + headed Chromium + x11vnc + noVNC).

The sandbox adapter exposes port 6080 and the runner's bootstrap starts the desktop stack; this adapter
reports the live noVNC URL so a human can watch the agent operate the machine.
"""
from __future__ import annotations

from ..spec import Environment, Spec
from .base import Provisioned


class RemoteDesktopAdapter:
    name = "remote_desktop"

    def wanted(self, env: Environment) -> bool:
        return env.needs_browser or env.needs_desktop

    async def provision(self, spec: Spec, job_id: str, creds: dict[str, str]) -> Provisioned:
        from . import vercel_sandbox as vs
        url = await vs.live_url(job_id, vs.NOVNC_PORT)
        watch = f"{url}/vnc.html?autoconnect=1&resize=scale" if url else None
        return Provisioned(
            adapter=self.name, real=True, status="ready" if url else "failed",
            detail=("Xvfb :99 1280x800 + Chromium (headed) + x11vnc + noVNC on sandbox job-" + job_id +
                    " port 6080; user-simulator acts via xdotool, motion recorded with ffmpeg x11grab"),
            connect_info={"display": ":99", "novnc_port": vs.NOVNC_PORT, "vnc_port": 5900},
            live_url=watch)
