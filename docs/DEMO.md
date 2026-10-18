# 3-minute demo script

Setup before going on stage:
- Server deployed (`npx vercel deploy --prod`) and `export DONEDONE_URL=https://<deployment>.vercel.app`,
  or running locally with `uv run uvicorn app:app --port 8787`.
- ntfy app open on the phone, subscribed to `$NTFY_TOPIC`.
- **Start a job about 10 minutes early** (`uv run python cli.py go "<goal>" --yes --no-watch`) so a run
  with a few rounds of history is ready to show. Write down its job_id and live_url.
- Terminal font large. Tabs open: a browser tab on the warm job's `live_url`, `tests/fixture_spec.json`
  in an editor.

---

### 0:00 to 0:20: the problem

> "Agents say 'done' when they are not. Our agent writes its own definition of done before it starts,
> gets its own hardware, checks its work with separate verifiers, and decides by itself when to
> keep going, change approach, stop, or ask me. I fire it from my phone and walk away."

### 0:20 to 1:00: goal → definition of done (Super VL)

```bash
uv run python cli.py plan "Recreate the feel and motion of JetBrains CLion IDE: colors, easing curves, timings" --out spec.json
```

While it runs (5 to 20 s):
> "Nemotron Super VL on Crusoe is compiling this one-line goal into a checklist software can run
> with no human: computed CSS values, animation timings measured from frames, a few
> vision-judged screenshots, and a console check. It also says what hardware the checks need."

When the table prints, point at:
- a `dom` row: "selector, CSS property, and an exact hex value"
- a `timing` row: "a millisecond range, measured at about 60 fps"
- the `visual` row: "Super VL scores the screenshot against a rubric with a threshold"
- `Environment needs: browser`: "it decided it only needs a browser, not a Mac"

> "This is plain JSON. If I disagree with a value, I edit it before running."

### 1:00 to 1:40: self-provisioning

```bash
uv run python cli.py run spec.json --ntfy $NTFY_TOPIC
```

> "The Vercel function reads the environment needs and picks adapters. The Vercel Sandbox microVM
> with a full Linux desktop (Xvfb + headed Chromium + noVNC) is real and was created just now. The macOS adapter is
> marked SIMULATED: same interface, honestly labelled. The function returns in seconds; the loop
> runs detached in the VM, so I can close my laptop."

Point at the REAL / SIMULATED column and the `live_url`.

### 1:40 to 2:30: the self-verifying loop (switch to the warm job)

```bash
uv run python cli.py status <warm_job_id>
```

> "Inside the sandbox, Nemotron Lightning builds the page with tool calls, and a second Lightning
> agent acts as the user: it hovers, clicks, waits, and screenshots. Verification is independent:
> CSS and timing checks are plain Python assertions on what the browser measured, and visual checks
> are judged by Super VL. The builder never grades itself."

Point at the history line (`r1:0.38(continue) r2:0.54(continue) r3:0.54(replan) ...`):
> "Here the same checks failed two rounds in a row, so the controller called it drift and switched to
> replan: Super VL rewrote the builder's brief from the evidence. If replans keep failing, or a check
> cannot be verified here, it escalates and pings me instead of pretending."

Open the `live_url` tab: status.json, report.md, screenshots.

### 2:30 to 3:00: walk away

Show the phone's ntfy notification (or the one from the warm job).

> "That's the whole loop: my goal, its own definition of done, its own hardware, independent checks,
> and it decides on its own: continue, replan, stop, or ask a human. Two Nemotron models: Super VL
> plans and judges, Lightning builds and plays the user."

---

## Fallback if the live sandbox is slow or down

1. **Planning without the server:** `uv run python cli.py plan "<goal>" --offline --out spec.json`
   calls the compiler directly. If Crusoe is also slow, open `tests/fixture_spec.json`: it is a real
   recorded Super VL output (13 checks: 9 dom, 2 timing, 1 visual, 1 code, plus design facts).
   `uv run pytest tests/test_spec.py -q` shows it validates.
2. **Loop:** show the recorded run under `docs/recorded/` (real sandbox + real desktop, 2026-10-17):
   `status.json` (5 rounds: 0.704 -> replan -> 0.778 -> replan -> 1.0, all 13 checks pass), `report.md`,
   `log.txt`, `shots/`, and `session.mp4` (the full desktop recording, 12 min, gitignored; keep a local copy).
   Walk through `history` (score per round + decision) and `checks` (pass/fail + evidence) exactly as
   in the 1:40 step.
3. **Provisioning:** if `/run` hangs on sandbox creation (first Playwright install is ~90 s),
   say so honestly and use the warm job's output.
