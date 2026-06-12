# Troubleshooting (especially macOS)

**If anything breaks:** copy the error message you saw and hand this file + the
error to an AI coding assistant (Claude Code, Codex, Cursor, …) — or just paste the
error into ChatGPT/Claude. Each problem below lists the exact **symptom**, the
**cause**, and the **fix** so it can be resolved in one step.

> ⚠️ **macOS note for the maintainer / the next AI:** this project was **developed
> and tested on Windows.** The cross-platform code paths were written and reasoned
> through carefully (process-kill is branched `os.name=="nt"` → `taskkill` else
> `os.killpg`; the headless worker uses `start_new_session` on POSIX; line endings
> forced to LF via `.gitattributes`; the server runs on port **5050** to dodge the
> macOS-AirPlay-on-5000 clash), **but they have NOT been executed on a real Mac.**
> So if something misbehaves on macOS, it's most likely one of the environment
> items below. The known *unverified-on-Mac* risk areas, for an AI to check first:
> **(a)** SSL root certs (python.org Python — issue #1), **(b)** Gatekeeper /
> lost exec bit on the launcher (#2, #3), **(c)** the `/usr/bin/python3` Xcode stub
> (#4), **(d)** Playwright Chromium download on Apple Silicon (#7). Pure-Python
> logic (the scraper tiers, budget guard, matching) is OS-agnostic and safe.

> Tip for the AI assistant reading this: the app is a Flask web UI (`app.py`) that
> shells out to `pipeline.py`. It is pure Python + Playwright, no native build
> step. Launchers: `run.bat` (Windows), `run.command` (macOS double-click),
> `run.sh` (Linux). Default port **5050**. Secrets/caps live in
> `secrets.local.json` (gitignored). Most macOS issues are environment setup
> (certs, Gatekeeper, exec bit), not code.

## First, collect these (helps any assistant diagnose fast)
```bash
sw_vers                      # macOS version
python3 --version            # must be 3.11+
which python3
cat secrets.local.json 2>/dev/null | grep -v APIFY_TOKEN   # caps only, NOT the token
```

---

## macOS issues

### 1. SSL: `CERTIFICATE_VERIFY_FAILED` during a run
**Symptom:** the run starts but every lookup fails; log shows
`SSLCertVerificationError` / `certificate verify failed: unable to get local issuer certificate`.
**Cause:** the python.org Python installer does **not** install root certificates by
default on macOS, so `requests`/`urllib` can't verify HTTPS.
**Fix (one time):**
```bash
# Run the certificate installer that ships with Python (adjust 3.11 to your version):
/Applications/Python\ 3.11/Install\ Certificates.command
# If that file isn't there:
python3 -m pip install --upgrade certifi
```
Then restart the app.

### 2. "run.command" can't be opened — unidentified developer
**Symptom:** `"run.command" cannot be opened because it is from an unidentified
developer` or `Apple cannot check it for malicious software`.
**Cause:** macOS Gatekeeper blocks downloaded scripts (quarantine flag).
**Fix:** **right-click** `run.command` → **Open** → **Open** (only needed once). Or:
```bash
xattr -d com.apple.quarantine run.command run.sh
```

### 3. Double-clicking does nothing / "permission denied"
**Symptom:** double-clicking `run.command` opens Terminal that closes instantly, or
`permission denied: ./run.command`.
**Cause:** downloading via "Download ZIP" strips the executable bit.
**Fix:**
```bash
cd /path/to/the/unzipped/folder
chmod +x run.command run.sh
```
Or just run it without the bit: in Terminal type `bash ` (with a space), drag
`run.command` into the window, press Enter.

### 4. `python3: command not found`, OR an "Install Command Line Developer Tools" popup, OR venv fails
**Symptom:** the launcher says Python isn't installed; **or** a macOS dialog pops up
offering to "Install Command Line Developer Tools"; **or** `python3 -m venv .venv`
errors / hangs.
**Cause:** macOS no longer ships real Python — but it *does* ship a stub at
`/usr/bin/python3` that only triggers the Xcode tools prompt. So `python3` can look
"present" while not actually working.
**Fix:** install **Python 3.11+** from <https://www.python.org/downloads/> (the
regular macOS .pkg installer — this is the recommended path; it also sets up certs).
Then re-run the launcher. Verify with `python3 --version` (should print 3.11+).
(Homebrew also works: `brew install python@3.12`.) Avoid relying on the
`/usr/bin/python3` stub.

### 5. `zsh: bad interpreter: /usr/bin/env: no such file` or `^M` errors
**Symptom:** `bad interpreter` or stray `^M` when running the script.
**Cause:** the script got Windows (CRLF) line endings. (The included
`.gitattributes` prevents this, but a bad copy/paste can reintroduce it.)
**Fix:**
```bash
sed -i '' 's/\r$//' run.command run.sh
```

### 6. Port already in use / page won't load
**Symptom:** `OSError: [Errno 48] Address already in use`, or the browser opens a
blank/AirPlay page.
**Cause:** another app holds port 5050 (or, on the old 5000, macOS AirPlay
Receiver). 
**Fix:** pick another port:
```bash
PORT=8080 python app.py          # then open http://127.0.0.1:8080
```
(You can also turn off AirPlay Receiver in System Settings → General → AirDrop &
Handoff, but changing the port is simpler.)

### 7. Playwright / Chromium: `Executable doesn't exist`
**Symptom:** the **headless** step logs `Executable doesn't exist at
.../chromium-XXXX/...` or `playwright install`.
**Cause:** the headless browser wasn't downloaded (the free JS-page fallback).
**Fix:**
```bash
source .venv/bin/activate
python -m playwright install chromium
```
Apple Silicon (M1/M2/M3) is supported natively — no Rosetta needed. The headless
step is optional; the app still works without it (it just skips JS-only pages).

### 8. macOS firewall prompt on first run
**Symptom:** "Do you want the application 'Python' to accept incoming network
connections?"
**Fix:** click **Allow**. The server is local-only (`127.0.0.1`); nothing is
exposed to the internet.

### 9. Paid tiers do nothing, or a run stops early
**Symptom:** Indeed/LinkedIn/Google/Apify return nothing; or a run halts with
"stopped early — budget cap reached".
**Cause:** no Apify token saved, **or the run hit the $15 spend cap** (by design).
**Fix:** paste a token (free at apify.com → Settings → Integrations). If it stopped
on the cap and you want it to go further, **raise the "Spend cap per run" field**
on the page — nothing spends past your cap without you raising it first. The token
and cap live in `secrets.local.json` (`APIFY_RUN_CAP_USD`).
**Note on the ledger:** the per-run cap is tracked **in memory**, so it holds even
if `apify_spend.json` is reset/edited. The authoritative spend figure is your real
Apify usage (`GET https://api.apify.com/v2/users/me/limits`), not that local file.

### 10. Google Jobs tier seems expensive / returns odd companies
**Symptom:** the Google Jobs tier costs more than the others, or its results
include other employers.
**Cause:** Google Jobs is ~$0.02/job (vs ~$0.0001 for Indeed) and matches loosely.
**By design** it's employer-filtered and only runs on larger companies (≥100
employees) under a $6 tier cap — so it stays cheap. This is expected; nothing to fix.

---

## Works on every OS
- A run **can never overspend** — it stops the instant it would exceed your
  **$15-per-run cap** (default), shown live on the page. Raise the cap yourself if a
  run needs more.
- Output for each run is saved under `outputs/` and downloadable from the page.
- Your uploaded role list replaces the shortlist only if you tick the box; the old
  one is archived in `roles_archive/`, never deleted.

## If you're still stuck
Give the AI assistant: (a) this file, (b) the exact error text from the black log
box on the page (or the Terminal window), and (c) the output of the "collect these"
commands at the top. That's enough to pinpoint and fix nearly anything.
