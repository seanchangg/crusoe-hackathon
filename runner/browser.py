"""Playwright wrapper: one Chromium page (1280x800) on the built site, HEADED on the X desktop
($DISPLAY, e.g. Xvfb :99 watched through noVNC) when available, headless otherwise.

Two instrument layers:
  * Playwright (deterministic): measure_css, is_visible, console_errors, record_frames, sample_css.
  * Desktop (acts like a human on the X display): desk_move/desk_click/desk_type/desk_key via xdotool,
    desk_screenshot via ImageMagick `import` (ffmpeg x11grab fallback), desk_record via ffmpeg x11grab
    (mp4 + a labelled 3x3 contact-sheet PNG for vision models that do not take video).

The site directory is served by a tiny no-cache http.server on 127.0.0.1:8081 (a thread),
so relative assets (style.css, app.js) resolve the same way they would on a real host.

record_frames() is a page.screenshot() loop; achieved fps is reported (on a 4-vCPU
sandbox clipped element screenshots run ~15-30 fps, full-page ~5-10 fps).
sample_css() is the precise timing probe: a requestAnimationFrame loop *inside* the page
records (t, computed value, visible) every frame plus the time of the triggering input event.
"""
from __future__ import annotations

import functools
import http.server
import io
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORT = {"width": 1280, "height": 800}

_RGB = re.compile(r"rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)")


def normalize_color(value: str | None) -> str | None:
    """rgb()/rgba()/#abc -> lowercase #rrggbb (or #rrggbbaa when alpha<1, 'transparent' when 0)."""
    if value is None:
        return None
    v = value.strip().lower()
    m = _RGB.search(v)
    if m:
        r, g, b = (max(0, min(255, round(float(x)))) for x in m.group(1, 2, 3))
        a = m.group(4)
        alpha = 1.0
        if a is not None:
            alpha = float(a[:-1]) / 100 if a.endswith("%") else float(a)
        if alpha <= 0:
            return "transparent"
        hx = f"#{r:02x}{g:02x}{b:02x}"
        return hx if alpha >= 1 else hx + f"{round(alpha * 255):02x}"
    m = re.fullmatch(r"#([0-9a-f]{3})", v)
    if m:
        return "#" + "".join(c * 2 for c in m.group(1))
    m = re.fullmatch(r"#([0-9a-f]{6})(ff)?", v)
    if m:
        return "#" + m.group(1)
    return v


def normalize_value(value: str | None) -> str | None:
    """Replace every rgb()/rgba() inside a computed value with hex (handles shorthands too)."""
    if value is None:
        return None
    return _RGB.sub(lambda m: normalize_color(m.group(0)) or m.group(0), value.strip())


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        # Empty favicon so a missing one never shows up as a console error.
        if self.path.split("?")[0] == "/favicon.ico" and not os.path.exists(
                os.path.join(self.directory, "favicon.ico")):
            self.send_response(204)
            self.end_headers()
            return None
        return super().send_head()


def start_site_server(site_dir: str, port: int = 8081) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(_QuietHandler, directory=site_dir)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


_SAMPLER_JS = """
([sel, prop]) => {
  const S = window.__dd = {samples: [], trig: null, start: performance.now()};
  const mark = () => { if (S.trig === null) S.trig = performance.now(); };
  ['mouseover', 'pointerdown', 'keydown', 'click', 'focusin'].forEach(
      ev => document.addEventListener(ev, mark, true));
  const tick = () => {
    const el = document.querySelector(sel);
    let v = null, vis = false;
    if (el) {
      const cs = getComputedStyle(el);
      v = prop ? cs.getPropertyValue(prop) : null;
      const r = el.getBoundingClientRect();
      vis = cs.display !== 'none' && cs.visibility !== 'hidden' &&
            parseFloat(cs.opacity) > 0.01 && r.width > 0 && r.height > 0;
    }
    S.samples.push([performance.now(), v, vis]);
    if (S.samples.length < 6000) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
"""


class Browser:
    def __init__(self, site_dir: str = "site", shots_dir: str = "out/shots", port: int = 8081):
        self.site_dir = str(Path(site_dir).resolve())
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._srv = start_site_server(self.site_dir, port)
        self._pw = sync_playwright().start()
        self.display = os.environ.get("DISPLAY", "")
        self.desktop = False
        self.desktop_note = ""
        self._browser = None
        self._ctx = None
        self.page = None
        self._persistent = False
        want_headed = bool(self.display) and os.environ.get("HEADLESS", "0") != "1"
        if want_headed:
            try:
                # Persistent context: its first window honours --kiosk (no tab strip/URL bar, origin at 0,0)
                # even without a window manager; windows from browser.new_context() would not.
                import tempfile
                self._ctx = self._pw.chromium.launch_persistent_context(
                    tempfile.mkdtemp(prefix="dd-chrome-"), headless=False,
                    **({"no_viewport": True} if os.environ.get("KIOSK_NO_VIEWPORT", "1") == "1" else {"viewport": VIEWPORT}),
                    args=[f"--window-size={VIEWPORT['width']},{VIEWPORT['height']}", "--window-position=0,0",
                          "--kiosk", "--no-sandbox", "--disable-infobars", "--no-first-run",
                          "--no-default-browser-check", "--disable-features=Translate"])
                self._persistent = True
                self.desktop = bool(shutil.which("xdotool"))
                self.desktop_note = f"headed Chromium (kiosk) on {self.display}" + \
                    ("" if self.desktop else " (xdotool missing: desktop tools fall back to Playwright input)")
            except Exception as e:  # noqa: BLE001
                self._ctx = None
                self.desktop_note = f"headed launch failed ({str(e).splitlines()[0][:120]}); headless fallback"
        if self._ctx is None:
            self._browser = self._pw.chromium.launch(headless=True)
            self.desktop_note = self.desktop_note or "headless (no DISPLAY)"
        print(f"[browser] {self.desktop_note}; desktop tools={'on' if self.desktop else 'off'}", flush=True)
        self._recs: list[dict] = []
        self._errors: list[str] = []
        self._shot_seq = 0
        self.new_page()

    # ---- lifecycle -------------------------------------------------------
    def new_page(self) -> None:
        """(Re)create the working page. Headed: keep the single kiosk window unless it was closed."""
        if self._persistent:
            if self.page is not None and not self.page.is_closed():
                self._errors = []
                return
            self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        else:
            if self._ctx:
                self._ctx.close()
            self._ctx = self._browser.new_context(viewport=VIEWPORT)
            self.page = self._ctx.new_page()
        self.page.set_default_timeout(5000)
        self._errors = []
        self.page.on("console", lambda m: self._errors.append(m.text) if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self._errors.append(f"pageerror: {e}"))

    def close(self) -> None:
        try:
            self.finish_recordings()
        except Exception:
            pass
        for fn in (lambda: self._ctx.close(), lambda: self._browser and self._browser.close(), self._pw.stop,
                   self._srv.shutdown):
            try:
                fn()
            except Exception:
                pass

    # ---- user-sim tools --------------------------------------------------
    def goto(self, path: str = "/") -> str:
        """Fresh page load (resets hover state and console log)."""
        path = (path or "/").strip()
        if "${" in path:  # 'goto ${DEPLOY_URL}' from a spec step
            try:
                import creds
                path, unresolved = creds.resolve_templates(path)
            except Exception:  # noqa: BLE001
                unresolved = ["?"]
            if unresolved:
                raise RuntimeError(f"cannot resolve ${{{unresolved[0]}}} (site/deploy.json not written yet?)")
        if re.match(r"^https?://", path, re.I):
            url = path  # deployed app / external page: navigate directly
        elif re.match(r"^[\w.-]+\.(?:app|com|dev|io|net|org|sh)(?:/|$)", path):
            url = "https://" + path  # bare host like donedone-job-x.vercel.app
        else:
            # Local site: normalise whatever the spec wrote ('file:///site/index.html', 'site/index.html',
            # './index.html', '/') onto the served root, so the page the builder wrote is what gets tested.
            local = re.sub(r"^file://", "", path, flags=re.I)
            local = re.sub(r"^/*(?:vercel/sandbox/)?(?:site/)?", "", local)
            if local in ("", "index.html", "./index.html", "."):
                local = ""
            url = self.base + "/" + local.lstrip("/")
        self._errors = []
        self._park_at(VIEWPORT["width"] - 2, VIEWPORT["height"] - 2)
        remote = not url.startswith(self.base)
        self.page.goto(url, wait_until="load", timeout=20000 if remote else 5000)
        self.page.wait_for_timeout(400 if remote else 150)
        return f"loaded {url} title={self.page.title()!r}"

    def click(self, selector: str) -> str:
        self.page.click(selector, timeout=3000)
        return f"clicked {selector}"

    def hover(self, selector: str) -> str:
        self.page.hover(selector, timeout=3000)
        return f"hovering {selector}"

    def type_text(self, text: str) -> str:
        self.page.keyboard.type(text)
        return f"typed {len(text)} chars"

    def press(self, key: str) -> str:
        self.page.keyboard.press(key)
        return f"pressed {key}"

    def wait_ms(self, ms: int | float) -> str:
        ms = max(0, min(int(ms), 10000))
        self.page.wait_for_timeout(ms)
        return f"waited {ms}ms"

    def screenshot(self, name: str = "shot") -> str:
        self._shot_seq += 1
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "shot"
        path = self.shots_dir / f"{safe}.png"
        self.page.screenshot(path=str(path))
        return str(path)

    def measure_css(self, selector: str, prop: str) -> str | None:
        """Computed style value; colors normalised to hex. 'visible' pseudo-prop -> 'visible'/'hidden'."""
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return None
        if prop in ("visible", "is_visible"):
            return "visible" if loc.is_visible() else "hidden"
        raw = loc.evaluate("(el, p) => getComputedStyle(el).getPropertyValue(p)", prop)
        return normalize_value(raw)

    def text_content(self, selector: str) -> str | None:
        """element.textContent of the first match (None when nothing matches)."""
        loc = self.page.locator(selector)
        if loc.count() == 0:
            try:
                loc.first.wait_for(state="attached", timeout=3000)
            except Exception:  # noqa: BLE001
                return None
        return loc.first.text_content(timeout=3000)

    def is_visible(self, selector: str) -> bool:
        loc = self.page.locator(selector).first
        return loc.count() > 0 and loc.is_visible()

    def bbox(self, selector: str | None):
        if not selector:
            return None
        loc = self.page.locator(selector).first
        if loc.count() == 0:
            return None
        return loc.bounding_box()

    # ---- desktop (X display) tools: act like a human -----------------------
    def _x(self, *args: str, timeout: float = 10) -> str:
        env = {**os.environ, "DISPLAY": self.display}
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"{args[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
        return r.stdout

    def screen_offset(self) -> tuple[float, float]:
        """Screen position of the page's (0,0).

        Calibrated empirically once per page: move the real pointer to a known screen point and read
        clientX/Y from a trusted mousemove event (window metrics lie under kiosk + viewport emulation).
        Falls back to window.screenX/outer-inner deltas.
        """
        if not self.desktop:
            return 0.0, 0.0
        key = id(self.page)
        if getattr(self, "_offset_key", None) == key:
            return self._offset
        off = None
        try:
            self.page.evaluate("() => { window.__ddmv = null; document.addEventListener('mousemove',"
                               " e => { if (e.isTrusted) window.__ddmv = [e.screenX, e.screenY, e.clientX, e.clientY]; },"
                               " {capture: true}); }")
            for sx, sy in ((640, 450), (643, 452)):
                self._x("xdotool", "mousemove", str(sx), str(sy))
                time.sleep(0.08)
            ev = self.page.evaluate("() => window.__ddmv")
            if ev:
                off = (643 - ev[2], 452 - ev[3])
        except Exception as e:  # noqa: BLE001
            print(f"[browser] pointer calibration failed: {e}", flush=True)
        if off is None:
            o = self.page.evaluate("() => [window.screenX, window.screenY, window.outerWidth - window.innerWidth,"
                                   " window.outerHeight - window.innerHeight]")
            off = (o[0] + max(0, o[2]) / 2, o[1] + max(0, o[3]))
        self._offset_key, self._offset = key, off
        print(f"[browser] screen offset of page origin: {off}", flush=True)
        return off

    def locate(self, selector: str) -> dict | None:
        """CSS selector -> screen coordinates {x, y (center), left, top, width, height}."""
        bb = self.bbox(selector)
        if not bb:
            return None
        ox, oy = self.screen_offset() if self.desktop else (0, 0)
        return {"x": round(ox + bb["x"] + bb["width"] / 2), "y": round(oy + bb["y"] + bb["height"] / 2),
                "left": round(ox + bb["x"]), "top": round(oy + bb["y"]),
                "width": round(bb["width"]), "height": round(bb["height"])}

    def desk_move(self, x: int, y: int) -> str:
        if not self.desktop:
            self.page.mouse.move(x, y)
            return f"(headless) mouse at {x},{y}"
        self._x("xdotool", "mousemove", str(int(x)), str(int(y)))
        time.sleep(0.03)  # (no --sync: it blocks forever when the pointer is already there)
        return f"mouse at {x},{y}"

    def desk_click(self, x: int, y: int, button: int = 1) -> str:
        if not self.desktop:
            self.page.mouse.click(x, y, button={1: "left", 2: "middle", 3: "right"}.get(int(button), "left"))
            return f"(headless) clicked {x},{y}"
        self._x("xdotool", "mousemove", str(int(x)), str(int(y)), "click", str(int(button)))
        return f"clicked button {button} at {x},{y}"

    def desk_type(self, text: str) -> str:
        if not self.desktop:
            self.page.keyboard.type(text)
            return f"(headless) typed {len(text)} chars"
        self._x("xdotool", "type", "--delay", "25", "--", text, timeout=60)
        return f"typed {len(text)} chars"

    @staticmethod
    def xdo_key(key: str) -> str:
        """'Control+Shift+A' / 'Enter' (Playwright style) -> 'ctrl+shift+a' / 'Return' (xdotool keysyms)."""
        m = {"control": "ctrl", "ctrl": "ctrl", "shift": "shift", "alt": "alt", "meta": "super", "cmd": "super",
             "enter": "Return", "return": "Return", "escape": "Escape", "esc": "Escape", "tab": "Tab",
             "backspace": "BackSpace", "delete": "Delete", "space": "space", "arrowup": "Up", "arrowdown": "Down",
             "arrowleft": "Left", "arrowright": "Right", "pageup": "Prior", "pagedown": "Next", "home": "Home",
             "end": "End"}
        parts = [p for p in re.split(r"\+", key.strip()) if p]
        return "+".join(m.get(p.lower(), p.lower() if len(p) == 1 else p) for p in parts)

    def desk_key(self, key: str) -> str:
        if not self.desktop:
            self.page.keyboard.press(key)
            return f"(headless) pressed {key}"
        self._x("xdotool", "key", "--clearmodifiers", self.xdo_key(key))
        return f"pressed {self.xdo_key(key)}"

    def desk_screenshot(self, name: str = "desk") -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "desk"
        path = self.shots_dir / f"{safe}.png"
        if self.desktop:
            try:
                if shutil.which("import"):
                    self._x("import", "-window", "root", str(path))
                else:
                    self._x("ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-video_size",
                            f"{VIEWPORT['width']}x{VIEWPORT['height']}", "-i", self.display, "-frames:v", "1", str(path))
                return str(path)
            except Exception as e:  # noqa: BLE001
                print(f"[browser] desk_screenshot fallback: {e}", flush=True)
        self.page.screenshot(path=str(path))
        return str(path)

    def desk_record(self, name: str = "clip", duration_ms: int = 1500) -> dict:
        """Start an x11grab recording in the BACKGROUND (returns immediately) so the triggering action that
        follows is captured. finish_recordings() waits for it and builds the contact sheet."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:60] or "clip"
        dur = max(0.3, min(float(duration_ms) / 1000, 15))
        mp4 = self.shots_dir / f"{safe}.mp4"
        rec = {"name": safe, "mp4": str(mp4), "duration_s": dur, "sheet": None, "proc": None, "error": None}
        if not (self.desktop and shutil.which("ffmpeg")):
            rec["error"] = "desktop recording unavailable"
            self._recs.append(rec)
            return rec
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-framerate", "30", "-video_size",
               f"{VIEWPORT['width']}x{VIEWPORT['height']}", "-i", self.display, "-t", f"{dur:.2f}",
               "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(mp4)]
        rec["proc"] = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                       env={**os.environ, "DISPLAY": self.display})
        rec["t0"] = time.time()
        time.sleep(0.15)  # let x11grab open the display before the action
        self._recs.append(rec)
        return rec

    def focus_box(self, selector: str | None) -> dict | None:
        """Screen region (>= 640x400, even dims) centred on an element: what a vision model should look at.
        A full 1280x800 frame shrinks small controls (a 47x27 button) below what the judge can see."""
        loc = self.locate(selector) if selector else None
        if not loc:
            return None
        W, H = VIEWPORT["width"], VIEWPORT["height"]
        w = min(W, max(640, loc["width"] + 240)) // 2 * 2
        h = min(H, max(400, loc["height"] + 240)) // 2 * 2
        x = max(0, min(W - w, loc["x"] - w // 2))
        y = max(0, min(H - h, loc["y"] - h // 2))
        return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}

    def finish_recordings(self, focus: dict | None = None) -> list[dict]:
        """Wait for pending clips; return [{name, mp4, sheet, focus, error}] (paths) and clear the queue.
        focus (from focus_box) crops the contact sheet to that region at native resolution."""
        out = []
        for rec in self._recs:
            p = rec.pop("proc", None)
            if p is not None:
                try:
                    _, err = p.communicate(timeout=rec["duration_s"] + 20)
                    if p.returncode != 0:
                        rec["error"] = (err or b"").decode()[:200] or f"ffmpeg rc={p.returncode}"
                except subprocess.TimeoutExpired:
                    p.kill()
                    rec["error"] = "ffmpeg timeout"
                if not rec["error"] and Path(rec["mp4"]).exists():
                    try:
                        rec["sheet"] = contact_sheet(rec["mp4"], str(self.shots_dir / f"{rec['name']}_sheet.png"),
                                                     rec["duration_s"], crop=focus)
                        rec["focus"] = focus
                    except Exception as e:  # noqa: BLE001
                        rec["error"] = f"contact sheet failed: {e}"
            rec.pop("t0", None)
            out.append({k: rec.get(k) for k in ("name", "mp4", "sheet", "error", "duration_s", "focus")})
        self._recs = []
        return out

    def park_mouse(self, avoid: list[str] | None = None) -> None:
        """Move the mouse to a viewport corner that is not over any of the `avoid` elements."""
        boxes = [b for b in (self.bbox(sel) for sel in (avoid or [])) if b]
        W, H = VIEWPORT["width"], VIEWPORT["height"]
        for x, y in ((W - 2, H - 2), (2, H - 2), (W - 2, 2), (W // 2, H - 2), (2, 2)):
            if not any(b["x"] - 4 <= x <= b["x"] + b["width"] + 4 and b["y"] - 4 <= y <= b["y"] + b["height"] + 4
                       for b in boxes):
                self._park_at(x, y)
                return
        self._park_at(W - 2, H - 2)

    def _park_at(self, x: int, y: int) -> None:
        if self.desktop:  # the real X pointer must also leave the element, or :hover sticks
            try:
                ox, oy = self.screen_offset()
                self._x("xdotool", "mousemove", str(int(ox + x)), str(int(oy + y)))
                # NO page.mouse.move here: a CDP mouse position competes with the real X pointer and Chromium
                # flips :hover between the two on every repaint/screenshot (seen as a flickering transition).
                return
            except Exception:
                pass
        self.page.mouse.move(x, y)

    def mark_trigger(self) -> None:
        """Fallback trigger timestamp (used when no input event reaches the document listener)."""
        self.page.evaluate("() => { if (window.__dd) window.__dd.pre = performance.now(); }")

    def record_frames(self, selector: str | None = None, duration_ms: int = 600, fps: int = 30,
                      prefix: str = "frame", save: bool = True):
        """Screenshot burst. Returns (frames=[(t_ms, png_path|bytes)], achieved_fps).

        Clips to the element's bbox (padded) when a selector is given; this is what keeps fps up.
        """
        clip = None
        bb = self.bbox(selector)
        if bb and bb["width"] > 0 and bb["height"] > 0:
            pad = 8
            clip = {"x": max(0, bb["x"] - pad), "y": max(0, bb["y"] - pad),
                    "width": min(VIEWPORT["width"], bb["width"] + 2 * pad),
                    "height": min(VIEWPORT["height"], bb["height"] + 2 * pad)}
        frames = []
        interval = 1.0 / max(1, fps)
        t0 = time.perf_counter()
        i = 0
        while (now := time.perf_counter()) - t0 < duration_ms / 1000:
            png = self.page.screenshot(clip=clip, animations="allow", caret="initial") if clip \
                else self.page.screenshot(animations="allow", caret="initial")
            t = (now - t0) * 1000
            if save:
                p = self.shots_dir / f"{prefix}_{i:03d}.png"
                p.write_bytes(png)
                frames.append((round(t, 1), str(p), png))
            else:
                frames.append((round(t, 1), None, png))
            i += 1
            spare = interval - (time.perf_counter() - now)
            if spare > 0:
                time.sleep(spare)
        elapsed = (time.perf_counter() - t0) or 1e-6
        achieved = round(len(frames) / elapsed, 1)
        return frames, achieved

    def clip_frames(self, mp4: str, selector: str | None = None, fps: int = 30, prefix: str = "cf"):
        """Frames of a desktop clip (cropped to the element's screen box) as [(t_ms, path, png_bytes)].

        Used instead of record_frames() on the headed desktop: page.screenshot() in headed Chromium perturbs
        layout/:hover under the real pointer (the transition being measured flickers)."""
        loc = self.locate(selector) if selector else None
        vf = f"fps={fps}"
        if loc and loc["width"] > 0 and loc["height"] > 0:
            pad = 8
            x, y = max(0, loc["left"] - pad), max(0, loc["top"] - pad)
            w = min(VIEWPORT["width"] - x, loc["width"] + 2 * pad)
            h = min(VIEWPORT["height"] - y, loc["height"] + 2 * pad)
            vf += f",crop={w}:{h}:{x}:{y}"
        tmp = self.shots_dir / f"{prefix}_frames"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-vf", vf, str(tmp / "f_%03d.png")],
                       check=True, timeout=60)
        files = sorted(tmp.glob("f_*.png"))
        return [(round(i * 1000 / fps, 1), str(f), f.read_bytes()) for i, f in enumerate(files)]

    def console_errors(self) -> list[str]:
        return [e for e in self._errors if "favicon" not in e.lower()]

    # ---- precise in-page timing probe -----------------------------------
    def start_sampler(self, selector: str, prop: str | None) -> None:
        self.page.evaluate(_SAMPLER_JS, [selector, prop])

    def read_sampler(self) -> dict:
        return self.page.evaluate("() => window.__dd || null") or {}


def frame_settle_ms(frames, threshold: float = 0.002) -> float | None:
    """Time (ms) of the last frame that differs from its predecessor, i.e. when pixels stop changing.

    Uses PIL mean abs diff when available, else raw byte inequality.
    """
    if len(frames) < 2:
        return None
    try:
        from PIL import Image, ImageChops, ImageStat

        def diff(a: bytes, b: bytes) -> float:
            ia, ib = Image.open(io.BytesIO(a)).convert("RGB"), Image.open(io.BytesIO(b)).convert("RGB")
            if ia.size != ib.size:
                return 1.0
            st = ImageStat.Stat(ImageChops.difference(ia, ib))
            return sum(st.mean) / (3 * 255)
    except Exception:  # PIL missing
        def diff(a: bytes, b: bytes) -> float:
            return 0.0 if a == b else 1.0
    last_change = None
    for (t0, _, a), (t1, _, b) in zip(frames, frames[1:]):
        if diff(a, b) > threshold:
            last_change = t1
    return last_change if last_change is not None else 0.0


def _crop_vf(crop: dict | None) -> str:
    return f"crop={crop['w']}:{crop['h']}:{crop['x']}:{crop['y']}," if crop else ""


def crop_video(mp4: str, crop: dict | None, out_mp4: str | None = None) -> str:
    """Crop a clip to the focus region (for native-video judging). Returns the input path when crop is None."""
    if not crop:
        return mp4
    out = out_mp4 or str(Path(mp4).with_name(Path(mp4).stem + "_focus.mp4"))
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-vf", _crop_vf(crop).rstrip(","),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-an", out],
                   check=True, timeout=60)
    return out


def contact_sheet(mp4: str, out_png: str, duration_s: float, n: int = 9, cols: int = 3,
                  crop: dict | None = None) -> str:
    """Sample n frames evenly across a clip and tile them (cols x rows) with timestamp labels.

    crop: focus region (x,y,w,h) -> tiles at native resolution of that region (max 640 wide) so small
    controls stay legible; otherwise the full frame scaled to 426 px wide.
    PIL path labels each tile with its time; without PIL falls back to ffmpeg's tile filter (no labels).
    """
    tmp = Path(out_png).with_suffix("")
    tmp.mkdir(parents=True, exist_ok=True)
    rate = n / max(duration_s, 0.1)
    tile_w = min(640, crop["w"]) if crop else 426
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-vf",
                    f"{_crop_vf(crop)}fps={rate:.4f},scale={tile_w}:-2",
                    "-frames:v", str(n), str(tmp / "f_%02d.png")], check=True, timeout=60)
    frames = sorted(tmp.glob("f_*.png"))
    if not frames:
        raise RuntimeError("no frames extracted")
    try:
        from PIL import Image, ImageDraw
        ims = [Image.open(f).convert("RGB") for f in frames]
        w, h = ims[0].size
        rows = (len(ims) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * w, rows * h), (0, 0, 0))
        d = ImageDraw.Draw(sheet)
        for i, im in enumerate(ims):
            x, y = (i % cols) * w, (i // cols) * h
            sheet.paste(im, (x, y))
            label = f"t={int((i + 0.5) / rate * 1000)}ms"
            d.rectangle([x, y, x + 8 * len(label) + 8, y + 16], fill=(0, 0, 0))
            d.text((x + 4, y + 2), label, fill=(255, 255, 0))
        sheet.save(out_png)
    except ImportError:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp4, "-vf",
                        f"{_crop_vf(crop)}fps={rate:.4f},scale={tile_w}:-2,tile={cols}x{(n + cols - 1) // cols}",
                        "-frames:v", "1",
                        out_png], check=True, timeout=60)
    shutil.rmtree(tmp, ignore_errors=True)
    return out_png
