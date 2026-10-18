# donedone

**Type a goal on your phone, close the app, get pinged when it is actually done.**
donedone takes a one-line goal and has Nemotron write its own *definition of done*: 8 to 14
machine-checkable checks (computed CSS values, measured animation timings, vision-judged
screenshots, console/HTML checks) plus the hardware those checks need. It then provisions that
hardware itself: a real Vercel Sandbox microVM with a full Linux desktop (Xvfb + headed Chromium + noVNC) the agent operates like a person, a real Lambda A100 box for GPU-kernel goals, and a simulated macOS /
remote-desktop adapters behind the same interface. Inside the sandbox an agent team builds, drives
the result like a user, and verifies every check independently. A controller decides each round
whether to **continue**, **replan**, **stop**, or **escalate** (ping a human). You walk away, and
an ntfy push tells you when it finished or needs you.

Demo goal: *"Recreate the feel and motion of JetBrains CLion IDE: colors, easing curves, timings."*

## Architecture

```
phone/curl ──POST /plan──▶ Vercel Function (FastAPI, app.py)
                              │  Super VL: goal → {checks[], environment{}}   (≈5–20 s)
                              ◀── spec JSON (user edits, then)
           ──POST /run───▶ Vercel Function
                              │  provisioner picks adapters from environment{}
                              │  VercelSandboxAdapter (REAL): create_sandbox(ports=[8080], env=creds)
                              │  RemoteDesktopAdapter (REAL): Xvfb desktop + noVNC on :6080 | LambdaGPUAdapter (REAL): A100 over SSH | MacOSAdapter (MOCK)
                              │  writes runner/ + spec.json + bootstrap.sh → create_process(detached)
                              ◀── {job_id, sandbox_name, live_url, environment_plan}   (≈10–20 s)
                                            │
                              Vercel Sandbox microVM (Ubuntu, Chromium via Playwright, 35 min)
                                 bootstrap.sh: pip install playwright openai; playwright install --with-deps chromium
                                 python runner/loop.py
                                   Builder  = Lightning  (tools: write_file, run_shell)
                                   UserSim  = Lightning  ("preset slave": click/hover/type/wait/screenshot/measure)
                                   Verifier = Super VL   (screenshots/frames vs criteria → pass/fail + evidence)
                                   Controller = deterministic + Super VL re-plan (continue/replan/stop/escalate)
                                 serves /workspace/out on :8080 (status.json, report.md, screenshots)
                                 POST ntfy.sh/<topic> on done | escalate
           ──GET /jobs/{id}──▶ Vercel Function → get_sandbox(name) → reads status.json
```

Job state lives in the sandbox itself (sandbox name = job id). There is no database.

| Path | What it is |
|---|---|
| `framework/spec.py` | Pydantic `Spec` / `Check` / `Environment`: the definition of done |
| `framework/compiler.py` | Super VL prompt: goal (+ optional reference image) → `Spec` |
| `framework/llm.py` | Crusoe Managed Inference client (OpenAI-compatible), model ids |
| `framework/adapters/`, `framework/provisioner.py` | environment needs → adapters → environment plan |
| `app.py` | FastAPI server (`/plan`, `/run`, `/jobs/{id}`, `/jobs/{id}/stop`), deployed as a Vercel Function |
| `runner/` | copied into the sandbox: builder, user-sim, verifier, controller loop |
| `cli.py` | terminal client for the server |
| `tests/` | offline tests + a real recorded spec (`tests/fixture_spec.json`) |

## How it maps to the "Done Means Done" criteria

- **The agent writes its own definition of done.** `framework/compiler.py` turns the goal into a
  `Spec`. Each check has a `kind` (`dom` | `timing` | `visual` | `code`), literal user-sim `steps`,
  and a numeric pass condition (`selector` + `property` + `expected`, a ms range, or a visual score
  threshold). Design facts (hex colors, easing curves, durations) are written down explicitly so
  the builder and verifier work from the same numbers. You can edit the JSON before running.
- **Independent verification.** The builder never grades itself. `dom` / `timing` / `code` checks
  are deterministic Python assertions over evidence that Playwright collects (`getComputedStyle`,
  ~60 fps frame sampling, console logs). `visual` checks are scored by Super VL from screenshots
  against the rubric. A separate Lightning user-simulator performs the clicks and hovers, so
  "it works" means a simulated user saw it work.
- **Drift / loop detection.** The controller switches to `replan` when the same set of checks
  fails two rounds in a row, when the score has not improved in two rounds, or when the builder
  writes output that is at least 90% identical to the previous round.
- **Decisions.** Every round ends with one of `continue` (score improving), `replan` (Super VL
  rewrites the builder brief from the evidence), `stop` (all checks pass, or the budget ran out with
  score >= 0.8, reported as "done with caveats"), or `escalate` (two replans failed, or a check
  cannot be verified in this environment). The decision and the reason go into `status.json`.
- **Walk-away operation.** phone → Vercel Function → sandbox → ntfy push. The function only
  provisions and returns in seconds. The loop runs detached in the microVM, and the phone gets a
  push on `stop` / `escalate` with the score and the live URL.

## Best use of Nemotron

Both models are served by **Crusoe Managed Inference** (OpenAI-compatible,
`https://api.inference.crusoecloud.com/v1`), with each model used where it is strongest:

- **Nemotron 3.5 Super VL** (`nvidia/NVIDIA-Nemotron-3.5-Super-VL`, 120B-A12B, vision): the
  *spec compiler* (goal + optional reference screenshot → JSON definition of done, in JSON mode),
  the *visual judge* (screenshots vs rubric → score + reason), and the *re-planner* (evidence →
  new builder brief when the loop stalls).
- **Nemotron 3.5 Lightning** (`nvidia/Nemotron-3.5-Lightning-30B-A3B`, 30B-A3B): the *builder*
  (tool calls: `write_file`, `read_file`, `run_shell`) and the preset *user-simulator* that drives
  the browser (`goto`, `click`, `hover`, `type`, `wait_ms`, `screenshot`, `measure_css`,
  `record_frames`). It needs many short tool-calling steps per round, which suits a 3B-active MoE
  with sub-second tool calls.

Thinking is turned off (`chat_template_kwargs.enable_thinking=false`) for the tight inner-loop calls.

## Quickstart

```bash
uv sync
cp .env.example .env          # set CRUSOE_API_KEY and NTFY_TOPIC
```

| Env var | Purpose |
|---|---|
| `CRUSOE_API_KEY` | Crusoe Managed Inference key (both Nemotron models) |
| `NTFY_TOPIC` | ntfy.sh topic. Subscribe to it in the ntfy phone app to get pushes |
| `DONEDONE_URL` | CLI only: server base URL (default `http://localhost:8787`) |
| `VERCEL_OIDC_TOKEN` (or `VERCEL_TOKEN` + `VERCEL_TEAM_ID` + `VERCEL_PROJECT_ID`) | sandbox auth when running locally. Automatic on Vercel |

**Local:**

```bash
npx vercel link                       # once
npx vercel env pull .env.vercel       # sandbox OIDC token for local runs
uv run uvicorn app:app --port 8787
```

**Deploy:**

```bash
npx vercel env add CRUSOE_API_KEY
npx vercel env add NTFY_TOPIC
npx vercel deploy --prod
export DONEDONE_URL=https://<your-deployment>.vercel.app
```

**Tests:** `uv run pytest -q`

## CLI

```bash
# goal -> definition of done (table of checks + environment needs), saved to spec.json
uv run python cli.py plan "Recreate the feel and motion of JetBrains CLion IDE: colors, easing curves, timings" --out spec.json
uv run python cli.py plan "<goal>" --offline          # no server: calls the compiler directly
uv run python cli.py plan "<goal>" --image ref.png    # attach a reference screenshot

# edit spec.json by hand if you want (drop checks, change expected values, fix design facts)

uv run python cli.py run spec.json --ntfy my-topic --watch   # environment plan (REAL vs SIMULATED), live_url, then live progress
uv run python cli.py status <job_id>
uv run python cli.py stop <job_id>

# the one-liner: plan, run, and watch with no prompt
uv run python cli.py go "<goal>" --yes
```

Without the CLI:

```bash
curl -s -X POST $DONEDONE_URL/plan -H 'content-type: application/json' -d '{"goal":"..."}' > spec.json
curl -s -X POST $DONEDONE_URL/run  -H 'content-type: application/json' -d "{\"spec\": $(cat spec.json), \"ntfy_topic\": \"my-topic\"}"
curl -s $DONEDONE_URL/jobs/<job_id>
```

## What is real and what is mocked

| Piece | Status |
|---|---|
| Spec compiler (Super VL on Crusoe) | real |
| Vercel Sandbox microVM (Ubuntu, 4 vCPU / 8 GB) | real, created per job |
| Linux desktop inside the sandbox: Xvfb :99, headed Chromium, x11vnc + noVNC (watch live on :6080) | real |
| User-simulator acting on that desktop (xdotool mouse/keyboard, ffmpeg x11grab clips judged by Super VL) | real |
| Lambda Labs A100 box for GPU-kernel goals (SSH, fixed independent harness: correctness vs PyTorch, TFLOPS, speedup) | real |
| Playwright inside the sandbox as the measuring instrument (computed styles, frame-settle timing, console errors) | real |
| Builder / user-sim / verifier / controller loop | real, runs in the sandbox |
| ntfy push notifications | real |
| macOS device adapter | **simulated**: returns a plan (e.g. a hosted M-series Mac + Xcode) behind the same adapter interface |


The environment plan returned by `/run` marks each adapter as `real` / `simulated` so the demo never
presents a mock as real hardware.

## Limits and assumptions

- **Vercel Hobby:** a sandbox session lasts at most 45 minutes, so the loop budget is capped at
  about 30 minutes (and a fixed number of rounds). Vercel Functions time out at 300 s, which is why
  the function only provisions and the loop runs detached in the sandbox. Pro allows longer sessions.
- **First-run cost:** installing Playwright and Chromium in a fresh sandbox takes about 90 s before
  round 1 starts.
- **There is no real CLion in the sandbox.** The CLion colors, easing curves and timings come from
  the model's knowledge, so they can be wrong, and two runs of `plan` can give different hex values
  (the recorded fixture says editor `#252526`, a later run said `#2b2b2b`). The checks verify that the build matches *the spec*, not CLion
  itself. Edit `design_facts` and `expected` in the spec, or attach a reference screenshot with
  `--image`, to pin them down.
- Visual checks depend on a model judge and are less repeatable than the dom/timing checks. That is
  why the compiler is told to use at most 4 of them.
- Job state is only as durable as the sandbox. When the sandbox stops, `/jobs/{id}` returns `unknown`.
- **Secrets:** keys live in `.env` / `.env.vercel` (gitignored) and Vercel env vars, and are passed
  into the sandbox as env vars. Rotate the Crusoe key and Vercel tokens after the hackathon.

## Roadmap / stretch

- noVNC desktop: headed Chromium under Xvfb in the sandbox, exposed on `:6080`, linked from the status page.
- Video judging: record the interaction with Playwright, send the clip to Super VL to judge motion
  directly instead of from frame samples.
- Snapshot caching: snapshot the sandbox after the first bootstrap and start later jobs from it to
  skip the Playwright install.
- Turn the simulated macOS adapter into a real one (hosted M-series Mac).
