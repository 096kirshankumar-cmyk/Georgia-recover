"""FastAPI web dashboard: recover readable text / build corrected PDFs from
garbled (mojibake) PDFs.

Features (the "dashboard" the user asked for):
  * import (upload) one or many PDFs in one go
  * each upload becomes an independent, isolated *job* (own temp dir, own
    log, own output) -> processing several PDFs one after another never
    interferes with each other
  * a single background worker processes jobs sequentially
  * live per-job log streamed to the bottom of the dashboard (SSE)
  * corrected PDF / recovered text downloads named after the original file
    (e.g. "Anaesthesia_ed8.pdf" -> "Anaesthesia_ed8_corrected.pdf")

Run locally:   uvicorn app:app --host 0.0.0.0 --port 8000
Deploy: Railway (Dockerfile). The reference Georgia font is downloaded lazily.
"""
import asyncio
import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
import uuid

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import (HTMLResponse, StreamingResponse, JSONResponse)
from fastapi.middleware.cors import CORSMiddleware

from recover_pdf import recover, ensure_ref_font
from make_corrected_pdf import correct_pdf

app = FastAPI(title="PDF Text Recovery Dashboard", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# Reference Georgia font resolved lazily (downloaded once on first use).
_FONT_PATH = os.environ.get("GEORGIA_FONT_PATH")
_FONT_LOCK = threading.Lock()
_REF = {"path": None}


def ref_font():
    with _FONT_LOCK:
        if _REF["path"] is None:
            _REF["path"] = ensure_ref_font(_FONT_PATH)
        return _REF["path"]


# --------------------------------------------------------------------------- #
#  Persistent storage on the Railway volume (/volume) so jobs and their output
#  files survive redeploys/restarts — not just page refreshes. Falls back to a
#  local ./data dir when no volume is mounted (local dev / non-Railway).
# --------------------------------------------------------------------------- #
def _storage_dir():
    for cand in (os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"),
                 os.environ.get("VOLUME_DIR"),
                 "/volume"):
        if cand:
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".wtest")
                with open(probe, "w") as f:
                    f.write("1")
                os.remove(probe)
                return cand
            except Exception:
                continue
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d


STORAGE = _storage_dir()
OUTPUTS = os.path.join(STORAGE, "outputs")
os.makedirs(OUTPUTS, exist_ok=True)
STATE = os.path.join(STORAGE, "jobs.json")


# --------------------------------------------------------------------------- #
#  Job manager: each job is isolated (own temp dir, log, output), processed
#  one-at-a-time by a single background worker. No cross-job shared state.
# --------------------------------------------------------------------------- #
_Q = queue.Queue()      # job ids awaiting processing
_JOBS = {}              # job_id -> Job


class Job:
    def __init__(self, job_id, filename, mode):
        self.id = job_id
        self.filename = filename
        self.mode = mode
        self.status = "queued"          # queued | processing | done | error
        self.logs = []                  # list of log lines
        self.lock = threading.Lock()
        self.workdir = os.path.join(OUTPUTS, job_id)
        os.makedirs(self.workdir, exist_ok=True)
        self.input_path = None
        self.output_path = None         # primary output (corrected pdf or text)
        self.result_name = None         # primary download filename
        self.output_type = None         # 'pdf' | 'txt'
        self.outputs = []               # list of dicts {name, type, path}
        self.error = None
        self.created = time.time()
        self.downloads = 0
        self._log(f"[{mode}] queued — waiting for the worker slot")

    def _log(self, msg):
        with self.lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def snapshot(self):
        with self.lock:
            return {
                "id": self.id,
                "filename": self.filename,
                "mode": self.mode,
                "status": self.status,
                "log_count": len(self.logs),
                "tail": self.logs[-8:],
                "result_name": self.result_name,
                "output_type": self.output_type,
                "error": self.error,
                "downloads": self.downloads,
                "created": self.created,
                "outputs": [{"name": o["name"], "type": o["type"]}
                            for o in self.outputs],
            }


class _Tee:
    """Redirect the recovery code's print() output into a job's live log too."""
    def __init__(self, job, real):
        self.job, self.real = job, real
        self.buf = ""

    def write(self, s):
        self.real.write(s)
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            line = line.rstrip()
            if line:
                self.job._log(line)

    def flush(self):
        self.real.flush()


def _safe_stem(name):
    stem = os.path.splitext(os.path.basename(name))[0]
    stem = re.sub(r"[^\w\-]+", "_", stem).strip("_")
    return stem or "document"


def run_job(job):
    job.status = "processing"
    job._log("Starting…")
    real_out, real_err = sys.stdout, sys.stderr
    tee = _Tee(job, real_out)
    sys.stdout, sys.stderr = tee, tee
    try:
        try:
            ref = ref_font()
            job._log(f"Reference font ready: {os.path.basename(ref)}")
        except Exception as e:
            job._log(f"WARN: reference font problem ({e}) — will retry lazily.")

        stem = _safe_stem(job.filename)
        inpath = os.path.join(job.workdir, "input.pdf")
        with open(inpath, "wb") as f:
            f.write(job.bytes)
        job.input_path = inpath
        job._log(f"Saved input as {job.filename} ({len(job.bytes)/1024:.0f} KB)")

        def _register(path, name, otype):
            job.outputs.append({"name": name, "type": otype, "path": path})
            # primary output = first one registered (corrected pdf takes priority)
            if job.output_path is None:
                job.output_path = path
                job.result_name = name
                job.output_type = otype

        if job.mode in ("corrected_pdf", "both"):
            job._log("Building corrected PDF (same layout, fixed text layer)…")
            out = os.path.join(job.workdir, stem + "_corrected.pdf")
            correct_pdf(inpath, out, ref_font=ref, add_cmap=True, verbose=False)
            _register(out, stem + "_corrected.pdf", "pdf")
            job._log("Corrected PDF ready ✓")

        if job.mode in ("text", "both"):
            job._log("Recovering plain text from glyph outlines…")
            txt = os.path.join(job.workdir, stem + "_recovered.txt")
            recover(inpath, output_txt=txt, ref_font=ref, verbose=False)
            _register(txt, stem + "_recovered.txt", "txt")
            job._log("Recovered text ready ✓")

        job.status = "done"
        job._log("Job complete ✓")
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job._log(f"ERROR: {e}")
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def _worker():
    while True:
        job_id = _Q.get()
        job = _JOBS.get(job_id)
        if job is not None:
            try:
                run_job(job)
            except Exception as e:  # last-resort guard
                job.status = "error"
                job.error = str(e)
            _save_state()
        _Q.task_done()


_STATE_LOCK = threading.Lock()


def _save_state():
    """Persist job metadata to the volume so jobs survive redeploys."""
    data = {"jobs": []}
    with _STATE_LOCK:
        for j in _JOBS.values():
            data["jobs"].append({
                "id": j.id,
                "filename": j.filename,
                "mode": j.mode,
                "status": j.status,
                "error": j.error,
                "created": j.created,
                "downloads": j.downloads,
                "outputs": j.outputs,
                "output_path": j.output_path,
                "result_name": j.result_name,
                "output_type": j.output_type,
            })
    try:
        with open(STATE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _load_state():
    """Restore jobs (and their downloadable outputs) from the volume."""
    if not os.path.exists(STATE):
        return
    try:
        with open(STATE) as f:
            data = json.load(f)
    except Exception:
        return
    for s in data.get("jobs", []):
        job_id = s.get("id")
        if not job_id or job_id in _JOBS:
            continue
        j = Job(job_id, s.get("filename", "unknown.pdf"), s.get("mode", "corrected_pdf"))
        j.created = s.get("created", time.time())
        j.downloads = s.get("downloads", 0)
        j.outputs = s.get("outputs", [])
        j.output_path = s.get("output_path")
        j.result_name = s.get("result_name")
        j.output_type = s.get("output_type")
        if j.outputs:
            j.output_path = j.output_path or j.outputs[0].get("path")
            j.result_name = j.result_name or j.outputs[0].get("name")
            j.output_type = j.output_type or j.outputs[0].get("type")
        if s.get("status") in ("done", "error"):
            j.status = s["status"]
            j.error = s.get("error")
            j._log(f"Restored from storage — outputs still available ({j.status})")
        else:
            # an interrupted (queued/processing) job can't resume: mark it
            j.status = "error"
            j.error = "Interrupted by a server restart — please upload this PDF again."
            j._log("Interrupted by server restart; please re-upload.")
        _JOBS[job_id] = j


threading.Thread(target=_worker, daemon=True).start()


def _create_job(filename, data, mode):
    job_id = uuid.uuid4().hex[:12]
    job = Job(job_id, filename, mode)
    job.bytes = data
    _JOBS[job_id] = job
    _Q.put(job_id)
    _save_state()
    return job


_load_state()


# --------------------------------------------------------------------------- #
#  Frontend dashboard (self-contained HTML; inline CSS/JS, no external CDN so
#  it renders everywhere including sandboxed previews).
# --------------------------------------------------------------------------- #
_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Text Recovery Dashboard</title>
<style>
  :root{
    --bg:#0f172a; --panel:#1e293b; --panel2:#273549; --line:#334155;
    --text:#e2e8f0; --muted:#94a3b8; --accent:#38bdf8; --accent2:#818cf8;
    --ok:#34d399; --err:#f87171; --warn:#fbbf24; --run:#38bdf8;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}
  .wrap{max-width:1080px;margin:0 auto;padding:24px 20px 320px}
  header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
  .logo{width:44px;height:44px;border-radius:12px;flex:none;
        background:linear-gradient(135deg,var(--accent),var(--accent2));
        display:flex;align-items:center;justify-content:center;font-weight:800;font-size:20px;color:#0f172a}
  h1{font-size:22px;margin:0;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:13px;margin-top:2px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;
        padding:20px;margin-bottom:18px}
  h2{font-size:15px;margin:0 0 14px;color:var(--accent)}
  /* ---- import ---- */
  #drop{position:relative;border:2px dashed var(--line);border-radius:14px;
        padding:34px 20px;text-align:center;cursor:pointer;transition:.15s;
        background:var(--panel2)}
  #drop.drag,#drop:hover{border-color:var(--accent);background:#33415566}
  #drop .big{font-size:34px}
  #drop .hint{color:var(--muted);margin-top:6px;font-size:13px}
  #fileInput{display:none}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
  .chip{background:var(--panel2);border:1px solid var(--line);border-radius:999px;
        padding:6px 12px;font-size:12px;color:var(--text)}
  .opts{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}
  .opt{flex:1;min-width:150px;border:1px solid var(--line);border-radius:12px;
       padding:12px 14px;cursor:pointer;background:var(--panel2);transition:.15s}
  .opt.sel{border-color:var(--accent);background:#38bdf81f}
  .opt .t{font-weight:600;font-size:13px}
  .opt .d{color:var(--muted);font-size:11.5px;margin-top:3px}
  .btn{background:linear-gradient(135deg,var(--accent),var(--accent2));color:#0f172a;
       border:0;border-radius:12px;padding:12px 22px;font-weight:700;font-size:14px;
       cursor:pointer;margin-top:18px}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.ghost{background:transparent;border:1px solid var(--accent);color:var(--accent)}
  /* ---- jobs ---- */
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
  .job{background:var(--panel2);border:1px solid var(--line);border-radius:14px;padding:16px}
  .job .name{font-weight:600;font-size:13.5px;word-break:break-all}
  .job .meta{color:var(--muted);font-size:11.5px;margin-top:3px}
  .badge{display:inline-block;border-radius:999px;padding:3px 10px;font-size:11px;
         font-weight:700;margin-top:8px}
  .b-queued{background:#fbbf2422;color:var(--warn)}
  .b-processing{background:#38bdf822;color:var(--run)}
  .b-done{background:#34d39922;color:var(--ok)}
  .b-error{background:#f8717122;color:var(--err)}
  .progress{height:6px;background:var(--line);border-radius:999px;margin-top:12px;overflow:hidden}
  .progress .bar{height:100%;width:0%;border-radius:999px;
        background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .4s}
  .job .dl{display:inline-block;margin-top:12px;background:var(--ok);color:#052e1f;
        border-radius:10px;padding:8px 14px;font-weight:700;font-size:12.5px;text-decoration:none}
  .job .err{color:var(--err);font-size:11.5px;margin-top:8px;word-break:break-all}
  .job .viewlog{margin-top:12px;font-size:11.5px;color:var(--accent);cursor:pointer;background:none;border:0}
  .empty{color:var(--muted);font-size:13px;text-align:center;padding:20px}
  /* ---- live log ---- */
  #termwrap{position:fixed;left:0;right:0;bottom:0;z-index:20;
        background:#020617;border-top:1px solid var(--line)}
  #termhead{display:flex;align-items:center;justify-content:space-between;
        padding:8px 16px;background:#0b1220;border-bottom:1px solid var(--line);cursor:pointer}
  #termhead .tt{font-size:12px;font-weight:600;color:var(--accent)}
  #termhead .tt .live{color:var(--ok);animation:blink 1.2s infinite}
  @keyframes blink{50%{opacity:.3}}
  #termbody{height:150px;overflow:auto;padding:10px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
        font-size:12px;line-height:1.6;white-space:pre-wrap;color:#cbd5e1}
  #termbody.closed{height:0;padding:0}
  #termind{display:inline-block;margin-left:8px;font-size:11px;color:var(--muted)}
  #termbody .log-line{display:block}
  #termbody .info{color:#7dd3fc}
  #termbody .ok{color:#34d399}
  #termbody .warn{color:#fbbf24}
  #termbody .err{color:#f87171}
  #termempty{color:#475569}
  .clearbtn{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;
        font-size:11px;padding:4px 10px;cursor:pointer}
  .mode-note{font-size:12px;color:var(--muted);margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">PDF</div>
    <div>
      <h1>PDF Text Recovery Dashboard</h1>
      <div class="sub">Recover readable text &amp; build corrected PDFs from mojibake documents</div>
    </div>
  </header>

  <div class="card">
    <h2>Import PDFs</h2>
    <div id="drop">
      <div class="big">📂</div>
      <div><strong>Drop PDFs here</strong> or click to choose files</div>
      <div class="hint">You can select multiple PDFs — each is processed as its own isolated job.</div>
      <input type="file" id="fileInput" accept=".pdf" multiple>
    </div>
    <div class="chips" id="chips"></div>
    <div class="opts">
      <div class="opt sel" data-mode="corrected_pdf" onclick="pickMode(this)">
        <div class="t">📄 Corrected PDF</div>
        <div class="d">Same visual layout, fixed text layer</div>
      </div>
      <div class="opt" data-mode="text" onclick="pickMode(this)">
        <div class="t">📝 Recovered text</div>
        <div class="d">Plain .txt extracted from glyph outlines</div>
      </div>
      <div class="opt" data-mode="both" onclick="pickMode(this)">
        <div class="t">🔀 Both</div>
        <div class="d">Corrected PDF <b>and</b> recovered text</div>
      </div>
    </div>
    <button class="btn" id="startBtn" disabled onclick="startAll()">Start processing</button>
    <div class="mode-note" id="modeNote">Each job runs independently — your previous files stay untouched while the next one processes.</div>
  </div>

  <div class="card">
    <h2>Jobs</h2>
    <div class="grid" id="grid"></div>
    <div class="empty" id="empty">No jobs yet — import a PDF to get started.</div>
  </div>
</div>

<!-- live log terminal pinned to the bottom -->
<div id="termwrap">
  <div id="termhead" onclick="toggleTerm()">
    <div class="tt"><span class="live">●</span> LIVE LOG&nbsp; <span id="termTitle">— select a job</span><span id="termind"></span></div>
    <div style="display:flex;gap:8px;align-items:center">
      <button class="clearbtn" onclick="event.stopPropagation();clearTerm()">Clear</button>
      <button class="clearbtn" id="collapseBtn" onclick="event.stopPropagation();toggleTerm()">Hide ▾</button>
    </div>
  </div>
  <div id="termbody"><div id="termempty">Waiting for a job to stream its log…</div></div>
</div>

<script>
const $=id=>document.getElementById(id);
let selectedFiles=[]; let mode='corrected_pdf'; let jobs={}; let es=null; let termOpen=true;
const pad=b=>('0'+b).slice(-2);
const ts=()=>{const d=new Date();return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds());};

/* ---- import ---- */
const drop=$('drop'), fin=$('fileInput');
drop.addEventListener('click',()=>fin.click());
drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('drag');});
drop.addEventListener('dragleave',()=>drop.classList.remove('drag'));
drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('drag');
   addFiles([...e.dataTransfer.files]);});
fin.addEventListener('change',()=>addFiles([...fin.files]));
function addFiles(fs){
  const ok=fs.filter(f=>/\.pdf$/i.test(f.name));
  selectedFiles=selectedFiles.concat(ok);
  renderChips(); $('startBtn').disabled=selectedFiles.length===0;
}
function renderChips(){
  $('chips').innerHTML=selectedFiles.map((f,i)=>
    `<span class="chip">${f.name} <b style="cursor:pointer;color:var(--err)" onclick="removeChip(${i})">×</b></span>`).join('');
}
function removeChip(i){selectedFiles.splice(i,1);renderChips();$('startBtn').disabled=selectedFiles.length===0;}
function pickMode(el){mode=el.dataset.mode;[...document.querySelectorAll('.opt')].forEach(o=>o.classList.remove('sel'));el.classList.add('sel');}

async function startAll(){
  if(!selectedFiles.length)return;
  $('startBtn').disabled=true;
  for(const f of selectedFiles){
    const fd=new FormData(); fd.append('file',f); fd.append('mode',mode);
    try{
      const r=await fetch('/api/jobs',{method:'POST',body:fd});
      const j=await r.json(); jobs[j.id]=j; renderJobs(); refresh(j.id);
    }catch(e){console.error(e);}
  }
  selectedFiles=[]; renderChips();
  $('startBtn').disabled=true;
  $('empty').style.display='none';
}

/* ---- jobs ---- */
async function refresh(id){
  try{
    const r=await fetch('/api/jobs/'+id); const j=await r.json();
    jobs[id]=j; renderJobs();
  }catch(e){}
}
function renderJobs(){
  const ids=Object.keys(jobs);
  $('empty').style.display=ids.length?'none':'block';
  $('grid').innerHTML=ids.length?ids.map(id=>{
    const j=jobs[id];
    let extra='';
    if(j.status==='processing') extra=`<div class="progress"><div class="bar" style="width:60%"></div></div>`;
    if(j.status==='done'&&j.outputs&&j.outputs.length){
      const btns=j.outputs.map(o=>{
        const label=o.type==='pdf'?'⬇ Download corrected PDF':'⬇ Download text';
        return `<a class="dl" href="/api/jobs/${j.id}/download?name=${encodeURIComponent(o.name)}">${label}</a>`;
      }).join(' ');
      extra=`<div style="margin-top:12px">${btns}</div>`;
    }else if(j.status==='done'){
      extra=`<a class="dl" href="/api/jobs/${j.id}/download">⬇ Download</a>`;
    }
    if(j.status==='error') extra=`<div class="err">${j.error}</div>`;
    const mname={'corrected_pdf':'Corrected PDF','text':'Text','both':'PDF + Text'}[j.mode]||j.mode;
    return `<div class="job">
      <div class="name">${esc(j.filename)}</div>
      <div class="meta">${mname} · ${fmtDate(j.created)}</div>
      <span class="badge b-${j.status}">${j.status}</span>${j.downloads?` <span style="color:var(--muted);font-size:11px">· ${j.downloads}×</span>`:''}
      ${extra}
      <div><button class="viewlog" onclick="stream('${j.id}','${esc(j.filename)}')">⌕ view live log</button></div>
    </div>`;
  }).join(''):'';
}
function fmtDate(t){return new Date(t*1000).toLocaleTimeString();}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

/* ---- live log (SSE) ---- */
function openTerm(){
  $('termwrap').style.display='block';
  $('termbody').classList.remove('closed');
  $('collapseBtn').textContent='Hide ▾';
  $('termind').textContent='';
  termOpen=true;
}
function toggleTerm(){
  if(termOpen){
    $('termbody').classList.add('closed');
    $('collapseBtn').textContent='Show ▴';
    $('termind').textContent='log hidden';
    termOpen=false;
  }else{
    openTerm();
  }
}
function stream(id, name){
  if(es)es.close();
  $('termTitle').textContent=' — '+name;
  $('termbody').innerHTML='';
  $('termempty').style.display='none';
  openTerm();
  es=new EventSource('/api/jobs/'+id+'/stream');
  es.onmessage=ev=>{
    const d=JSON.parse(ev.data);
    if(d.logs&&d.logs.length) appendLog(d.logs);
    // status transition -> re-fetch the full job (includes the outputs list)
    // so the download button appears immediately even if the final SSE
    // event is lost behind the proxy.
    if(d.status){refresh(id);}
  };
  es.onerror=()=>{ if(es){es.close();es=null;refresh(id);} };
}
function appendLog(lines){
  const tb=$('termbody'), el=document.createElement('div');
  el.innerHTML=lines.map(l=>{
    let cls='info';
    if(/✓|complete/i.test(l))cls='ok';
    if(/warn/i.test(l))cls='warn';
    if(/error/i.test(l))cls='err';
    return `<span class="log-line ${cls}">${esc(l)}</span>`;
  }).join('');
  tb.appendChild(el);
  tb.scrollTop=tb.scrollHeight;
}
function clearTerm(){const tb=$('termbody');tb.innerHTML=`<div id="termempty">Waiting for a job…</div>`;}

/* ---- boot: reload existing jobs so a page refresh never loses them ---- */
async function init(){
  try{
    const r=await fetch('/api/jobs');
    const d=await r.json();
    (d.jobs||[]).forEach(j=>jobs[j.id]=j);
    renderJobs();
    // auto-open the live log for the most recent unfinished job
    const active=[...Object.values(jobs)]
      .filter(j=>j.status==='queued'||j.status==='processing')
      .sort((a,b)=>b.created-a.created);
    if(active.length) stream(active[0].id, active[0].filename);
  }catch(e){console.error('init failed',e);}
}
/* Poll fallback: keeps statuses/outputs fresh even if SSE is unavailable.
   Also refreshes done jobs so a missed SSE 'done' event can't leave a card
   without its download button. */
let _lastRefresh={};
setInterval(async()=>{
  const ids=Object.keys(jobs);
  const now=Date.now();
  for(const id of ids){
    const j=jobs[id];
    const isTerminal=(j.status==='done'||j.status==='error');
    // active jobs: always refresh; terminal jobs: refresh once after finish
    if(!isTerminal || _lastRefresh[id]===undefined){
      try{const r=await fetch('/api/jobs/'+id);if(r.ok){jobs[id]=await r.json();_lastRefresh[id]=now;}}
      catch(e){}
    }
  }
  renderJobs();
},2000);
init();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return _INDEX_HTML


# --------------------------------------------------------------------------- #
#  API
# --------------------------------------------------------------------------- #
@app.get("/api/jobs")
def list_jobs():
    jobs = []
    for j in _JOBS.values():
        s = j.snapshot()
        jobs.append(s)
    jobs.sort(key=lambda s: s["created"])
    return {"jobs": jobs}


@app.post("/api/jobs")
def create_job(file: UploadFile = File(...), mode: str = Form("corrected_pdf")):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    if mode not in ("corrected_pdf", "text", "both"):
        mode = "corrected_pdf"
    data = file.file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    job = _create_job(file.filename, data, mode)
    return job.snapshot()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = _JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Job not found")
    s = j.snapshot()
    s["finished"] = s["status"] in ("done", "error")
    return s


@app.get("/api/jobs/{job_id}/stream")
async def job_stream(job_id: str):
    j = _JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Job not found")

    async def gen():
        sent = 0
        last_status = None
        while True:
            with j.lock:
                logs = j.logs[sent:]
                sent = len(j.logs)
                status = j.status
                done = status in ("done", "error")
            payload = {"logs": logs, "status": status, "finished": done}
            if last_status != status:
                payload["status_changed"] = True
                last_status = status
            yield f"data: {json.dumps(payload)}\n\n"
            if done:
                yield f"data: {json.dumps({'final': True})}\n\n"
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, name: str = None):
    j = _JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Job not found")
    if j.status != "done":
        raise HTTPException(409, "Job is not finished yet")
    # pick the requested output by name, else the primary output
    target = None
    if name:
        for o in j.outputs:
            if o["name"] == name and os.path.exists(o["path"]):
                target = o
                break
    if target is None:
        if j.output_path and os.path.exists(j.output_path):
            target = {"name": j.result_name, "type": j.output_type,
                      "path": j.output_path}
    if target is None:
        raise HTTPException(409, "No finished output available yet")
    with open(target["path"], "rb") as f:
        payload = f.read()
    with j.lock:
        j.downloads += 1
    media = "application/pdf" if target["type"] == "pdf" else "text/plain"
    filename = target["name"]
    return StreamingResponse(
        io.BytesIO(payload), media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    j = _JOBS.get(job_id)
    if j is None:
        raise HTTPException(404, "Job not found")
    if j.status == "processing":
        raise HTTPException(409, "Job is still processing")
    # drop from memory; files remain in /tmp (cleaned by OS)
    _JOBS.pop(job_id, None)
    return {"ok": True}


# Backward-compatible simple endpoints -------------------------------------- #
@app.post("/recover")
def recover_text(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    data = file.file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data); path = tmp.name
    try:
        text, _ = recover(path, ref_font=ref_font(), verbose=False)
    except Exception as e:
        raise HTTPException(500, f"Recovery failed: {e}")
    finally:
        os.unlink(path)
    return text


@app.post("/correct-pdf")
def correct_pdf_endpoint(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a .pdf file")
    data = file.file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data); inpath = tmp.name
    outpath = tempfile.mktemp(suffix="_corrected.pdf")
    try:
        correct_pdf(inpath, outpath, ref_font=ref_font(), verbose=False)
        with open(outpath, "rb") as f:
            payload = f.read()
    except Exception as e:
        raise HTTPException(500, f"PDF correction failed: {e}")
    finally:
        os.unlink(inpath)
        if os.path.exists(outpath):
            os.unlink(outpath)
    stem = _safe_stem(file.filename)
    return StreamingResponse(io.BytesIO(payload), media_type="application/pdf",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{stem}_corrected.pdf"'})


@app.get("/health")
def health():
    return {"status": "ok"}
