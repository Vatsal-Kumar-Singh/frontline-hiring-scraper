"""Simple web UI for the Frontline-Hiring Signal Scraper.

For non-technical users: double-click run.bat (Windows) or run.sh (Mac/Linux) —
it installs everything and opens this page in your browser. Upload an Apollo
company CSV, pick the date window + minimum role count, choose which lookup tiers
to use, and click Run. A live progress bar shows the phase and a log; download the
enriched CSV when done.

Free tiers (static + headless) always run. The paid tiers (Indeed, LinkedIn,
Apify) need a free Apify API token, which you paste once below. Every run is hard-
capped (default $15) so it can never overspend.

Runs the proven pipeline.py as a subprocess and streams its progress.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import (Flask, jsonify, render_template_string, request,
                   send_file, abort)
from werkzeug.utils import secure_filename

import budget

ROOT = Path(__file__).resolve().parent
UPLOADS = ROOT / "uploads"
OUTPUTS = ROOT / "outputs"
ROLES_ARCHIVE = ROOT / "roles_archive"
ROLES_FILE = ROOT / "roles.txt"
SECRETS = ROOT / "secrets.local.json"
for d in (UPLOADS, OUTPUTS, ROLES_ARCHIVE):
    d.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64 MB uploads

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

DATE_LABELS = [
    ("1", "Posted in the last 1 day"),
    ("7", "Posted in the last 7 days"),
    ("15", "Posted in the last 15 days"),
    ("30", "Posted in the last 30 days"),
    ("all", "All currently-open roles"),
]

# Paid lookup tiers, cheapest first. key -> (pipeline flag, label, blurb, default-on)
TIERS = [
    ("indeed", "--indeed", "Indeed sweep",
     "Cheapest & highest-yield (~$0.0001/job). Finds companies by name on Indeed.", True),
    ("harvest", "--harvest", "ATS slug harvest",
     "Pennies. Reads a company's real ATS for free (from Indeed) + caches it.", True),
    ("linkedin", "--linkedin", "LinkedIn sweep",
     "Cheap (~$0.0009/job). Recovers companies Indeed couldn't find.", True),
    ("apify", "--apify", "Apify ATS actors",
     "Most expensive. Only for locked platforms (Paycom/Dayforce/ADP/iCIMS).", False),
]


def _roles_count() -> int:
    try:
        return sum(1 for ln in ROLES_FILE.read_text(encoding="utf-8").splitlines()
                   if ln.strip())
    except OSError:
        return 0


def _has_token() -> bool:
    if os.environ.get("APIFY_TOKEN", "").strip():
        return True
    try:
        return bool(json.loads(SECRETS.read_text(encoding="utf-8")).get("APIFY_TOKEN"))
    except (OSError, ValueError):
        return False


def _read_secrets() -> dict:
    try:
        return json.loads(SECRETS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_secrets(data: dict) -> None:
    SECRETS.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _save_token(token: str) -> None:
    """Persist the Apify token to the gitignored secrets file (merge, keep caps)."""
    data = _read_secrets()
    data["APIFY_TOKEN"] = token.strip()
    data.setdefault("APIFY_MONTHLY_CAP_USD", 65)
    data.setdefault("APIFY_RUN_CAP_USD", 15)
    _write_secrets(data)


def _save_caps(run_cap, monthly_cap) -> None:
    """Persist the user-chosen spend limits (clamped sane). Monthly is never below
    per-run so a single run can always use its full budget."""
    data = _read_secrets()
    try:
        rc = max(0.0, float(run_cap))
        mc = max(rc, float(monthly_cap))
        data["APIFY_RUN_CAP_USD"] = rc
        data["APIFY_MONTHLY_CAP_USD"] = mc
        _write_secrets(data)
    except (ValueError, TypeError):
        pass


def _budget_info() -> dict:
    return {"spent": budget.spent(), "monthly_cap": budget.cap(),
            "run_cap": budget.run_cap()}


# --------------------------------------------------------------- run a job -----
def _reader(job_id: str, proc: subprocess.Popen):
    """Read pipeline stdout, parse PROGRESS|{...} lines, update job state."""
    job = JOBS[job_id]
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if line.startswith("PROGRESS|"):
            try:
                ev = json.loads(line[len("PROGRESS|"):])
            except ValueError:
                continue
            t = ev.get("type")
            with _LOCK:
                if t == "phase":
                    job["phase"] = ev.get("phase", "")
                    job["detail"] = ev.get("detail", "")
                    job["current"], job["total"] = ev.get("current", 0), ev.get("total", 0)
                elif t == "progress":
                    if ev.get("phase"):
                        job["phase"] = ev["phase"]
                    job["current"], job["total"] = ev.get("current", 0), ev.get("total", 0)
                    if ev.get("message"):
                        job["log"].append(ev["message"])
                elif t == "log":
                    job["log"].append(ev.get("message", ""))
                elif t == "done":
                    job["phase"] = "Done"
                    job["detail"] = ""
                    job["status"] = "done"
                    job["summary"] = {
                        "counts": ev.get("counts", {}),
                        "strong": ev.get("strong", 0),
                        "total": ev.get("total", 0),
                    }
                job["log"] = job["log"][-300:]
        elif line.strip():
            with _LOCK:
                job["log"].append(line)
                job["log"] = job["log"][-300:]
    proc.wait()
    with _LOCK:
        if job["status"] != "done":
            job["status"] = "error" if proc.returncode else "done"
        job["budget"] = _budget_info()


def _start_job(job_id: str, input_csv: Path, output_csv: Path, since: str,
               threshold: int, tiers: dict) -> str:
    JOBS[job_id] = {
        "status": "running", "phase": "Starting", "detail": "",
        "current": 0, "total": 0, "log": [], "summary": None,
        "output": str(output_csv), "started": datetime.now(timezone.utc).isoformat(),
        "budget": _budget_info(),
    }
    env = {**os.environ, "PROGRESS_JSON": "1", "PYTHONUNBUFFERED": "1",
           "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-u", str(ROOT / "pipeline.py"), str(input_csv),
           str(output_csv), "--since", since, "--threshold", str(threshold)]
    for key, flag, *_ in TIERS:
        if tiers.get(key):
            cmd.append(flag)
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="utf-8", errors="replace",
    )
    threading.Thread(target=_reader, args=(job_id, proc), daemon=True).start()
    return job_id


# ----------------------------------------------------------------- routes ------
@app.route("/")
def index():
    return render_template_string(
        PAGE, date_labels=DATE_LABELS, roles_count=_roles_count(),
        threshold=20, tiers=TIERS, has_token=_has_token(), budget=_budget_info())


@app.route("/run", methods=["POST"])
def run():
    csv_file = request.files.get("apollo_csv")
    if not csv_file or not csv_file.filename:
        return jsonify({"error": "Please choose an Apollo company CSV."}), 400
    since = request.form.get("since", "7")
    try:
        threshold = max(1, int(request.form.get("threshold", "20")))
    except ValueError:
        threshold = 20

    tiers = {key: request.form.get(key) == "on" for key, *_ in TIERS}

    # Persist the spend limits the user chose (applied to THIS run via the budget guard).
    if request.form.get("run_cap") or request.form.get("monthly_cap"):
        _save_caps(request.form.get("run_cap", budget.run_cap()),
                   request.form.get("monthly_cap", budget.cap()))

    # Save the Apify token if pasted (needed for any paid tier).
    token = (request.form.get("apify_token") or "").strip()
    if token:
        _save_token(token)
    if any(tiers.values()) and not _has_token():
        return jsonify({"error": "Paid tiers need an Apify API token. Paste one "
                        "(free at apify.com) or untick the paid tiers."}), 400

    # Optional: swap the shortlist roles (archive current, never delete).
    roles_file = request.files.get("roles_file")
    replace = request.form.get("replace_roles") == "on"
    roles_note = ""
    if roles_file and roles_file.filename and replace:
        if ROLES_FILE.exists():
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(ROLES_FILE, ROLES_ARCHIVE / f"roles_{ts}.txt")
        roles_file.save(ROLES_FILE)
        roles_note = "shortlist replaced (previous archived in roles_archive/)"

    job_id = uuid.uuid4().hex[:12]
    in_path = UPLOADS / f"{job_id}_{secure_filename(csv_file.filename)}"
    csv_file.save(in_path)
    out_path = OUTPUTS / f"{job_id}.enriched.csv"
    _start_job(job_id, in_path, out_path, since, threshold, tiers)
    return jsonify({"job": job_id, "roles_note": roles_note})


@app.route("/progress")
def progress():
    job = JOBS.get(request.args.get("job", ""))
    if not job:
        return jsonify({"error": "unknown job"}), 404
    with _LOCK:
        pct = int(100 * job["current"] / job["total"]) if job["total"] else 0
        return jsonify({
            "status": job["status"], "phase": job["phase"], "detail": job["detail"],
            "current": job["current"], "total": job["total"], "percent": pct,
            "log": job["log"][-40:], "summary": job["summary"],
            "budget": job.get("budget"),
            "download": job["status"] == "done" and Path(job["output"]).exists(),
        })


@app.route("/download")
def download():
    job = JOBS.get(request.args.get("job", ""))
    if not job or not Path(job["output"]).exists():
        abort(404)
    return send_file(job["output"], as_attachment=True,
                     download_name="frontline_results.csv")


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Frontline Hiring Scraper</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:780px;margin:24px auto;padding:0 16px;color:#1b1b1b}
 h1{font-size:22px;margin-bottom:2px} .sub{color:#666;margin-top:0}
 label{display:block;margin:14px 0 4px;font-weight:600}
 select,input[type=number],input[type=password]{padding:8px;font-size:15px;width:100%;box-sizing:border-box}
 .file{border:2px dashed #bbb;padding:14px;border-radius:8px;background:#fafafa}
 .row{background:#f6f7f9;border:1px solid #e3e6ea;border-radius:10px;padding:16px;margin:14px 0}
 button{margin-top:18px;background:#1761d2;color:#fff;border:0;padding:12px 22px;font-size:16px;border-radius:8px;cursor:pointer}
 button:disabled{background:#9bb6e6;cursor:default}
 .hint{color:#777;font-size:13px;margin-top:3px}
 .tier{display:flex;align-items:flex-start;gap:9px;margin:9px 0;font-weight:400}
 .tier b{font-weight:600}
 #bar{height:22px;background:#e6e9ef;border-radius:11px;overflow:hidden;margin:8px 0}
 #fill{height:100%;width:0;background:#1761d2;transition:width .3s}
 #phase{font-weight:700;font-size:16px} #detail{color:#666;font-size:13px}
 #log{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:#10141a;color:#cfe3ff;
   padding:10px;border-radius:8px;height:230px;overflow:auto;white-space:pre-wrap;margin-top:10px}
 #done{display:none;background:#e7f6ec;border:1px solid #b6e0c4;border-radius:10px;padding:16px;margin-top:14px}
 .dl{display:inline-block;margin-top:10px;background:#13863a;color:#fff;padding:10px 18px;border-radius:8px;text-decoration:none}
 .pill{display:inline-block;background:#dfe7f5;border-radius:20px;padding:2px 10px;font-size:13px;margin-right:6px}
 .bud{font-size:13px;color:#555;background:#eef3fb;border-radius:8px;padding:6px 10px;display:inline-block}
 code{background:#eee;padding:1px 5px;border-radius:4px}
</style></head><body>
<h1>Frontline Hiring Scraper</h1>
<p class="sub">Upload your Apollo company list → get, per company, how many open frontline / hourly roles they have.</p>
<p class="bud">💰 This month: <b>${{ '%.2f'|format(budget.spent) }}</b> spent &nbsp;·&nbsp;
   hard cap <b>${{ '%g'|format(budget.run_cap) }}/run</b>, ${{ '%g'|format(budget.monthly_cap) }}/month — a run can never exceed this.</p>

<form id="form">
 <div class="row">
  <label>1. Apollo company CSV</label>
  <div class="file"><input type="file" name="apollo_csv" accept=".csv" required></div>
  <div class="hint">The export from Apollo (must include a <b>Company Name</b> and <b>Website</b> column).</div>
 </div>

 <div class="row">
  <label>2. How recent should the jobs be?</label>
  <select name="since">
   {% for val,label in date_labels %}<option value="{{val}}" {% if val=='7' %}selected{% endif %}>{{label}}</option>{% endfor %}
  </select>
  <div class="hint">Recency applies to the Indeed/LinkedIn/Apify lookups; the free ATS reads return all current openings.</div>

  <label>3. Minimum frontline roles to flag a company as "strong"</label>
  <input type="number" name="threshold" value="{{threshold}}" min="1" max="500">
  <div class="hint">Currently 20+. You can lower it to 10 or 5 anytime.</div>
 </div>

 <div class="row">
  <label>4. Lookup tiers</label>
  <div class="hint" style="margin-bottom:6px">Free tiers (read a company's own careers page) always run.
   The paid tiers below are cheap and capped — leave the defaults for the best results-per-dollar.</div>
  {% for key,flag,name,blurb,on in tiers %}
  <label class="tier"><input type="checkbox" name="{{key}}" {% if on %}checked{% endif %}>
   <span><b>{{name}}</b> — {{blurb}}</span></label>
  {% endfor %}
 </div>

 <div class="row" style="background:#fff7e6;border-color:#f0d28a">
  <label>5. Apify API token {% if has_token %}<span style="color:#13863a;font-weight:400">✓ already saved</span>{% endif %}</label>
  <input type="password" name="apify_token" placeholder="{% if has_token %}leave blank to keep the saved token{% else %}apify_api_...{% endif %}" autocomplete="off">
  <div class="hint">Needed only for the paid tiers. Get one free at <code>apify.com</code> → Settings → Integrations.
   Saved locally in <code>secrets.local.json</code> (never uploaded, git-ignored).</div>
 </div>

 <div class="row">
  <label>6. Spending limits (USD)</label>
  <div style="display:flex;gap:14px;flex-wrap:wrap">
   <div style="flex:1;min-width:180px">
    <div class="hint" style="margin:0 0 3px">Max per run</div>
    <input type="number" name="run_cap" value="{{ '%g'|format(budget.run_cap) }}" min="0" step="1">
   </div>
   <div style="flex:1;min-width:180px">
    <div class="hint" style="margin:0 0 3px">Max per month</div>
    <input type="number" name="monthly_cap" value="{{ '%g'|format(budget.monthly_cap) }}" min="0" step="1">
   </div>
  </div>
  <div class="hint">A run stops the moment it would exceed the smaller of these — it can
   <b>never</b> overspend. Defaults: $15/run, $65/month.</div>
 </div>

 <div class="row">
  <label style="font-weight:600">(Optional) Swap the shortlist of role keywords (your ICP roles)</label>
  <div class="file"><input type="file" name="roles_file" accept=".txt"></div>
  <label style="font-weight:400;margin-top:10px"><input type="checkbox" name="replace_roles" checked>
   Replace the current shortlist with this file</label>
  <div class="hint">Current shortlist: <b>{{roles_count}} role keywords</b>. Your current list is
   <b>archived (not deleted)</b> in <code>roles_archive/</code> if you replace it.</div>
 </div>

 <button type="submit" id="go">Run</button>
</form>

<div class="row" id="progress" style="display:none">
 <div id="phase">Starting…</div><div id="detail"></div>
 <div id="bar"><div id="fill"></div></div>
 <div id="counter" class="hint"></div>
 <div id="budline" class="bud" style="margin-top:8px"></div>
 <div id="log"></div>
 <div id="done">
   <b>Done!</b> <span id="summary"></span><br>
   <a class="dl" id="dl" href="#">⬇ Download results CSV</a>
 </div>
</div>

<script>
const form=document.getElementById('form'), go=document.getElementById('go');
const prog=document.getElementById('progress'), fill=document.getElementById('fill');
const phase=document.getElementById('phase'), detail=document.getElementById('detail');
const counter=document.getElementById('counter'), logEl=document.getElementById('log');
const doneEl=document.getElementById('done'), summary=document.getElementById('summary'), dl=document.getElementById('dl');
const budline=document.getElementById('budline');
let timer=null;

form.addEventListener('submit', async (e)=>{
 e.preventDefault();
 go.disabled=true; go.textContent='Running…'; prog.style.display='block'; doneEl.style.display='none';
 logEl.textContent=''; fill.style.width='0%';
 const res=await fetch('/run',{method:'POST',body:new FormData(form)});
 const data=await res.json();
 if(data.error){ alert(data.error); go.disabled=false; go.textContent='Run'; prog.style.display='none'; return; }
 if(data.roles_note){ logEl.textContent='• '+data.roles_note+'\\n'; }
 poll(data.job);
});

function poll(job){
 timer=setInterval(async ()=>{
  const r=await fetch('/progress?job='+job); const s=await r.json();
  phase.textContent=s.phase; detail.textContent=s.detail||'';
  fill.style.width=(s.percent||0)+'%';
  counter.textContent=s.total?('Phase progress: '+s.current+' / '+s.total+' ('+s.percent+'%)'):'';
  if(s.budget){ budline.textContent='💰 $'+(s.budget.spent||0).toFixed(2)+' spent this month (cap $'+s.budget.run_cap+'/run)'; }
  if(s.log&&s.log.length){ logEl.textContent=s.log.join('\\n'); logEl.scrollTop=logEl.scrollHeight; }
  if(s.status==='done'||s.status==='error'){
   clearInterval(timer); go.disabled=false; go.textContent='Run';
   if(s.status==='done'){
     fill.style.width='100%'; doneEl.style.display='block';
     if(s.summary){ const c=s.summary.counts||{};
       summary.innerHTML='<span class="pill">'+(s.summary.strong||0)+' strong ('+document.querySelector('[name=threshold]').value+'+)</span>'
         +'<span class="pill">'+(c.ok||0)+' resolved</span>'
         +'<span class="pill">'+((c.unresolved||0)+(c.error||0))+' unresolved</span> of '+(s.summary.total||0); }
     if(s.download){ dl.href='/download?job='+job; }
   } else { phase.textContent='Error — see log'; }
  }
 }, 1500);
}
</script>
</body></html>"""


if __name__ == "__main__":
    # Port 5050 (not 5000 — macOS AirPlay Receiver occupies 5000). Override with
    # the PORT env var if 5050 is taken.
    port = int(os.environ.get("PORT", "5050"))
    print(f"Frontline Hiring Scraper UI -> http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
