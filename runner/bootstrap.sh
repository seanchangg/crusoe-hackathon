#!/usr/bin/env bash
# Sandbox entrypoint. cwd=/vercel/sandbox (runner/ + spec.json here).
# Order: serve out/ on :8080 (status page) -> apt desktop packages -> pip openai/playwright -> chromium
#        -> Xvfb :99 + x11vnc :5900 + noVNC :6080 -> runner/loop.py (headed Chromium on :99).
# Env in: CRUSOE_API_KEY NTFY_TOPIC JOB_ID LIVE_URL(:8080) NOVNC_URL(:6080) MAX_ROUNDS MAX_MINUTES
#         + broker secrets (VERCEL_TOKEN VERCEL_TEAM_ID GITHUB_TOKEN GPU_HOST GPU_USER GPU_SSH_KEY_B64 ...) and
#         SECRETS_LEDGER (metadata only). out/log.txt is served publicly: NEVER echo a secret value here
#         (only "set"/"unset", names and the ledger's masked field).
set -u
cd "$(dirname "$0")/.."
mkdir -p out out/shots site
exec > >(tee -a out/log.txt) 2>&1

T0=$(date +%s)
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
status() {  # status <phase> <message>
  python3 - "$1" "$2" <<'PY'
import json, os, sys, datetime
p = "out/status.json"
try:
    d = json.load(open(p))
except Exception:
    d = {}
d.update({"job_id": os.environ.get("JOB_ID", "local"), "phase": sys.argv[1], "message": sys.argv[2],
          "round": d.get("round", 0), "max_rounds": int(os.environ.get("MAX_ROUNDS", "6") or 6),
          "score": d.get("score", 0.0), "history": d.get("history", []), "checks": d.get("checks", []),
          "decision": d.get("decision"), "live_url": os.environ.get("LIVE_URL", ""),
          "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
d["elapsed_s"] = int(__import__("time").time()) - int(os.environ.get("T0", "0") or 0)
open(p + ".tmp", "w").write(json.dumps(d, indent=2)); os.replace(p + ".tmp", p)
PY
}
export T0
fail() {
  echo "[bootstrap] FAILED: $1"
  status failed "bootstrap failed: $1"
  if [ -n "${NTFY_TOPIC:-}" ]; then
    curl -s -m 15 -H "Title: donedone ${JOB_ID:-local}: failed score=0" -H "Click: ${LIVE_URL:-}" \
      -d "bootstrap failed: $1" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null \
    || python3 -c "import urllib.request,os,sys;urllib.request.urlopen(urllib.request.Request('https://ntfy.sh/'+os.environ['NTFY_TOPIC'],data=sys.argv[1].encode(),headers={'Title':'donedone '+os.environ.get('JOB_ID','local')+': failed score=0','Click':os.environ.get('LIVE_URL','')}),timeout=15)" "bootstrap failed: $1" || true
  fi
  exit 1
}

echo "[bootstrap] $(now) job=${JOB_ID:-local} python=$(python3 --version 2>&1) arch=$(uname -m)"
status bootstrap "starting status server"
cp runner/status_page.html out/index.html

# 1. Live URL works immediately.
python3 -m http.server 8080 --directory out >/dev/null 2>&1 &
echo "[bootstrap] status server pid $! on :8080"

# Does this spec need a browser at all? (same rule as agents.needs_browser: gpu-only / file-exists-only -> no)
NEEDS_BROWSER=$(python3 - <<'PY'
import json, re
try:
    s = json.load(open("spec.json"))
except Exception:
    print(1); raise SystemExit
fe = re.compile(r"[\w./-]+\.(?:html|css|js|py|json|txt|md|toml|ya?ml|sh)\s+exists")
fp = re.compile(r"\bfile\s+exists\s*[:=]?\s*[`'\"]?[\w./-]+")
def file_check(c):
    return any(fp.search(t.lower()) or fe.search(t.lower())
               for t in ((c.get("expected") or ""), (c.get("criteria") or "")))
need = any(c.get("kind") in ("dom", "timing", "visual")
           or (c.get("kind") == "code" and not file_check(c))
           for c in s.get("checks", []))
print(1 if need else 0)
PY
)
echo "[bootstrap] needs_browser=$NEEDS_BROWSER gpu_host=${GPU_HOST:+set} vercel_token=${VERCEL_TOKEN:+set}"
# Credential ledger summary (names / status / masked only).
python3 - <<'PY' || true
import json, os
try:
    led = json.loads(os.environ.get("SECRETS_LEDGER") or "[]")
except Exception:
    led = []
for e in led if isinstance(led, list) else []:
    if isinstance(e, dict):
        print(f"[bootstrap] secret {e.get('name')}: {e.get('status')} (source {e.get('source')}, {e.get('masked') or '-'})")
PY

# GPU SSH key from the broker -> /tmp/gpu_key (0600). Never echoed.
if [ -n "${GPU_SSH_KEY_B64:-}" ] && [ ! -s /tmp/gpu_key ]; then
  ( umask 077; printf '%s' "$GPU_SSH_KEY_B64" | base64 -d > /tmp/gpu_key 2>/dev/null ) \
    && { [ -z "$(tail -c1 /tmp/gpu_key)" ] || echo >> /tmp/gpu_key; chmod 600 /tmp/gpu_key; echo "[bootstrap] wrote /tmp/gpu_key (600)"; } \
    || { rm -f /tmp/gpu_key; echo "[bootstrap] GPU_SSH_KEY_B64 is not valid base64; /tmp/gpu_key not written"; }
fi

# 2. Desktop packages (each group independent so one 404/missing package does not sink the rest).
export DEBIAN_FRONTEND=noninteractive
sudo -E apt-get update -q >/dev/null 2>&1 || echo "[bootstrap] apt-get update failed"
for pair in "ssh:openssh-client" "curl:curl"; do
  cmd=${pair%%:*}; pkg=${pair#*:}
  command -v "$cmd" >/dev/null && continue
  sudo -E apt-get install -y -q --no-install-recommends "$pkg" >/dev/null 2>&1 \
    || sudo dnf install -y -q "$( [ "$pkg" = openssh-client ] && echo openssh-clients || echo "$pkg" )" >/dev/null 2>&1 \
    && echo "[bootstrap] installed $pkg" || echo "[bootstrap] install FAILED (ignored): $pkg"
done
# Node/npx for `npx vercel deploy` (present in the universal image; best-effort install otherwise).
if ! command -v npx >/dev/null; then
  sudo -E apt-get install -y -q --no-install-recommends nodejs npm >/dev/null 2>&1 \
    || sudo dnf install -y -q nodejs npm >/dev/null 2>&1 || true
fi
echo "[bootstrap] node=$(node --version 2>/dev/null || echo missing) npx=$(command -v npx >/dev/null && echo ok || echo missing)"
# Pre-fetch the Vercel CLI in the background so the builder's first deploy fits its 180 s run_shell timeout.
VERCEL_WARM_PID=""
if [ -n "${VERCEL_TOKEN:-}" ] && command -v npx >/dev/null; then
  ( timeout 150 npx -y vercel --version >out/vercel_warm.log 2>&1 \
      && echo "[bootstrap] vercel CLI ready: $(tail -1 out/vercel_warm.log)" \
      || echo "[bootstrap] vercel CLI pre-fetch failed (builder will retry)" ) &
  VERCEL_WARM_PID=$!
fi
if [ "$NEEDS_BROWSER" = 1 ]; then
  status bootstrap "installing desktop packages (Xvfb, x11vnc, noVNC, xdotool, ffmpeg)"
  for grp in "xvfb x11vnc xdotool" "novnc websockify" "ffmpeg" "imagemagick" "fonts-jetbrains-mono" "fonts-dejavu-core"; do
    sudo -E apt-get install -y -q --no-install-recommends $grp >/dev/null 2>&1 \
      && echo "[bootstrap] apt ok: $grp" || echo "[bootstrap] apt FAILED (ignored): $grp"
  done
  echo "[bootstrap] desktop packages done after $(( $(date +%s) - T0 ))s"
fi

# 3. Python deps (PEP 668 aware), fallback to a venv.
status bootstrap "installing openai + playwright"
PY=python3
if ! python3 -m pip install -q --break-system-packages openai playwright pillow 2>&1 | tail -3; then :; fi
if ! python3 -c "import openai, playwright" 2>/dev/null; then
  echo "[bootstrap] system pip failed; trying venv"
  python3 -m venv .venv && .venv/bin/pip install -q openai playwright pillow || fail "pip install openai playwright"
  PY=.venv/bin/python
fi
$PY -c "import openai, playwright" || fail "openai/playwright not importable"
echo "[bootstrap] deps ok after $(( $(date +%s) - T0 ))s"

# 4. Chromium + system libs (browser jobs only).
if [ "$NEEDS_BROWSER" = 1 ]; then
status bootstrap "installing chromium"
if ! $PY -m playwright install --with-deps chromium 2>&1 | tail -5; then :; fi
if ! $PY -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); b.close(); p.stop()" 2>/dev/null; then
  echo "[bootstrap] chromium not launchable yet; installing deps with sudo"
  sudo -E "$(command -v $PY || echo $PY)" -m playwright install-deps chromium 2>&1 | tail -5 \
    || sudo -E env PATH="$PATH" $PY -m playwright install-deps chromium 2>&1 | tail -5
  $PY -m playwright install chromium 2>&1 | tail -3
  $PY -c "from playwright.sync_api import sync_playwright as s; p=s().start(); b=p.chromium.launch(); b.close(); p.stop()" \
    || fail "chromium does not launch after install-deps"
fi
echo "[bootstrap] chromium ok after $(( $(date +%s) - T0 ))s"
fi

# 5. Virtual desktop: Xvfb :99 -> x11vnc :5900 -> websockify/noVNC :6080 (public URL in $NOVNC_URL).
DESKTOP=unavailable
start_desktop() {
  command -v Xvfb >/dev/null || { echo "[bootstrap] Xvfb missing"; return 1; }
  Xvfb :99 -screen 0 1280x800x24 -nolisten tcp >out/xvfb.log 2>&1 &
  for i in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
  [ -e /tmp/.X11-unix/X99 ] || { echo "[bootstrap] Xvfb did not start"; cat out/xvfb.log; return 1; }
  export DISPLAY=:99
  if command -v x11vnc >/dev/null; then
    x11vnc -display :99 -forever -shared -nopw -quiet -rfbport 5900 -bg -o out/x11vnc.log >/dev/null 2>&1 \
      || echo "[bootstrap] x11vnc failed (desktop still usable, just not watchable)"
  fi
  NOVNC_WEB=/usr/share/novnc
  [ -d "$NOVNC_WEB" ] || NOVNC_WEB=$(dirname "$(find / -name vnc.html -path '*novnc*' 2>/dev/null | head -1)" 2>/dev/null)
  if command -v websockify >/dev/null && [ -n "$NOVNC_WEB" ] && [ -d "$NOVNC_WEB" ]; then
    websockify --web="$NOVNC_WEB" 6080 localhost:5900 >out/websockify.log 2>&1 &
    echo "[bootstrap] noVNC on :6080 (web=$NOVNC_WEB)"
  else
    echo "[bootstrap] websockify/noVNC missing; live desktop view unavailable"
  fi
  return 0
}
if [ "$NEEDS_BROWSER" != 1 ]; then DESKTOP=skipped; unset DISPLAY
elif start_desktop; then DESKTOP=ready; else unset DISPLAY; fi
export DESKTOP NOVNC_URL="${NOVNC_URL:-}"
python3 - <<PY
import json, os
p = "out/status.json"
d = json.load(open(p))
d["desktop"] = os.environ.get("DESKTOP")
d["novnc_url"] = os.environ.get("NOVNC_URL", "")
json.dump(d, open(p, "w"), indent=2)
PY
echo "[bootstrap] desktop=$DESKTOP DISPLAY=${DISPLAY:-none} novnc_url=${NOVNC_URL:-unset}"

# 5b. Record the WHOLE session (best-effort). 60 s segments stay playable if the sandbox dies mid-job;
#     finish_session() stops ffmpeg with SIGINT, concatenates into out/session.mp4, lists it in status.json.
REC_PID=""
if [ "$DESKTOP" = ready ] && command -v ffmpeg >/dev/null; then
  ffmpeg -nostdin -loglevel error -f x11grab -framerate 15 -video_size 1280x800 -i :99 \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -g 30 -f segment -segment_time 60 -reset_timestamps 1 \
    out/session_%03d.mp4 >out/session_rec.log 2>&1 &
  REC_PID=$!
  echo "[bootstrap] session recorder pid $REC_PID"
fi
SESSION_DONE=0
finish_session() {
  [ "$SESSION_DONE" = 1 ] && return 0
  SESSION_DONE=1
  [ -n "$REC_PID" ] || return 0
  kill -INT "$REC_PID" 2>/dev/null
  for i in $(seq 1 50); do kill -0 "$REC_PID" 2>/dev/null || break; sleep 0.2; done
  kill -9 "$REC_PID" 2>/dev/null || true
  ls out/session_[0-9]*.mp4 >/dev/null 2>&1 || { echo "[bootstrap] no session segments"; return 0; }
  ( cd out && for f in session_[0-9]*.mp4; do echo "file '$f'"; done > session_list.txt \
    && ffmpeg -nostdin -loglevel error -y -f concat -safe 0 -i session_list.txt -c copy session.mp4 ) \
    || { echo "[bootstrap] session concat failed (segments kept)"; return 0; }
  python3 - <<'PY' || true
import json, os
p = "out/status.json"
try:
    d = json.load(open(p))
except Exception:
    d = {}
d["session_video"] = "session.mp4"
open(p + ".tmp", "w").write(json.dumps(d, indent=2)); os.replace(p + ".tmp", p)
PY
  echo "[bootstrap] session video: out/session.mp4 ($(du -h out/session.mp4 | cut -f1))"
}
trap finish_session EXIT

# 6. The loop (writes status.json itself from here on and pings ntfy on done/escalate/failed).
#    If the headed browser cannot start on :99, browser.py falls back to headless automatically.
[ -n "$VERCEL_WARM_PID" ] && wait "$VERCEL_WARM_PID" 2>/dev/null
status building "bootstrap done in $(( $(date +%s) - T0 ))s; starting loop"
$PY runner/loop.py
RC=$?
echo "[bootstrap] loop exited rc=$RC after $(( $(date +%s) - T0 ))s"
finish_session || true
# keep serving out/ until the sandbox times out so the live URL stays up
wait
