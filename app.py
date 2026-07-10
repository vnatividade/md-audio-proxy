"""
Fachada Railway (fila de jobs). NÃO faz TTS — só recebe upload, enfileira,
e serve o mp3 que o worker local (Mac Studio) devolve.

Fluxo:
  navegador  --POST /synthesize-->  cria job (pending), devolve job_id
  Mac worker --GET  /jobs/next  -->  pega job pending (vira processing)
  Mac worker --POST /jobs/<id>/result --> entrega mp3 (vira done)
  navegador  --GET  /result/<id>  -->  baixa o mp3 quando pronto

Auth do usuário: token via form/header/arg OU cookie de dispositivo (md_auth).
"""
import os
import io
import time
import uuid
import threading

from flask import Flask, request, send_file, abort, jsonify, Response
from werkzeug.exceptions import HTTPException

app = Flask(__name__)


@app.errorhandler(HTTPException)
def _json_error(e):
    # respostas de erro em JSON limpo (nada de página HTML crua no front)
    return jsonify({"error": e.description, "code": e.code}), e.code


APP_TOKEN = os.environ.get("APP_TOKEN", "")           # token do usuário (navegador)
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")     # token do canal Railway<->Mac
JOB_TTL = int(os.environ.get("JOB_TTL", "1800"))      # jobs somem após 30 min
PROCESSING_TIMEOUT = int(os.environ.get("PROCESSING_TIMEOUT", "600"))
MAX_MD_BYTES = int(os.environ.get("MAX_MD_BYTES", str(2 * 1024 * 1024)))

COOKIE_NAME = "md_auth"
COOKIE_MAXAGE = int(os.environ.get("COOKIE_MAXAGE", str(365 * 24 * 3600)))  # 1 ano

ALLOWED_VOICES = {"pm_santa", "pm_alex", "pf_dora"}

_jobs = {}            # id -> dict
_lock = threading.Lock()
_last_worker_seen = [0.0]


def _now():
    return time.time()


def _cleanup():
    now = _now()
    for k in list(_jobs.keys()):
        j = _jobs[k]
        if now - j["created"] > JOB_TTL:
            _jobs.pop(k, None)
        elif j["status"] == "processing" and now - j["updated"] > PROCESSING_TIMEOUT:
            j["status"] = "pending"
            j["updated"] = now


def _user_ok(req):
    if not APP_TOKEN:
        return False
    supplied = (
        req.headers.get("X-App-Token")
        or req.form.get("token")
        or req.args.get("token")
        or req.cookies.get(COOKIE_NAME)
        or ""
    )
    return supplied == APP_TOKEN


def _set_auth_cookie(resp):
    resp.set_cookie(
        COOKIE_NAME, APP_TOKEN,
        max_age=COOKIE_MAXAGE, httponly=True, secure=True, samesite="Lax",
    )
    return resp


def _worker_ok(req):
    if not WORKER_TOKEN:
        return False
    return req.headers.get("X-Worker-Token", "") == WORKER_TOKEN


@app.after_request
def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ----------------------------- usuário ------------------------------------
@app.post("/synthesize")
def synthesize():
    if not _user_ok(request):
        abort(403, "Token inválido")
    up = request.files.get("file")
    if up is None or up.filename == "":
        abort(400, "Nenhum arquivo enviado")
    raw = up.read(MAX_MD_BYTES + 1)
    if len(raw) > MAX_MD_BYTES:
        abort(413, "Arquivo grande demais")
    text = raw.decode("utf-8", "ignore")
    if not text.strip():
        abort(400, "Arquivo vazio")

    voice = request.form.get("voice") if request.form.get("voice") in ALLOWED_VOICES else None
    try:
        speed = max(0.5, min(float(request.form.get("speed")), 2.0))
    except (TypeError, ValueError):
        speed = None

    jid = uuid.uuid4().hex[:16]
    name = os.path.splitext(os.path.basename(up.filename))[0] or "audio"
    with _lock:
        _cleanup()
        _jobs[jid] = {
            "status": "pending", "text": text, "name": name,
            "voice": voice, "speed": speed,
            "result": None, "error": None,
            "created": _now(), "updated": _now(),
        }
    return _set_auth_cookie(jsonify({"job_id": jid, "name": name}))


@app.get("/result/<jid>")
def result(jid):
    if not _user_ok(request):
        abort(403, "Token inválido")
    j = _jobs.get(jid)
    if j is None:
        abort(404, "Job não encontrado (pode ter expirado)")
    if j["status"] == "done":
        return send_file(
            io.BytesIO(j["result"]),
            mimetype="audio/mpeg",
            as_attachment=False,
            download_name=f"{j['name']}.mp3",
        )
    if j["status"] == "error":
        abort(500, j["error"] or "Falha no processamento")
    worker_online = (_now() - _last_worker_seen[0]) < 20
    return jsonify({"status": j["status"], "worker_online": worker_online}), 202


@app.get("/me")
def me():
    return {"authed": _user_ok(request)}


@app.post("/login")
def login():
    if not _user_ok(request):
        abort(403, "Token inválido")
    return _set_auth_cookie(jsonify({"authed": True}))


@app.post("/logout")
def logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ----------------------------- worker (Mac) -------------------------------
@app.get("/jobs/next")
def jobs_next():
    if not _worker_ok(request):
        abort(403)
    _last_worker_seen[0] = _now()
    with _lock:
        _cleanup()
        for jid, j in _jobs.items():
            if j["status"] == "pending":
                j["status"] = "processing"
                j["updated"] = _now()
                return jsonify({"id": jid, "text": j["text"], "name": j["name"],
                                "voice": j.get("voice"), "speed": j.get("speed")})
    return ("", 204)


@app.post("/jobs/<jid>/result")
def jobs_result(jid):
    if not _worker_ok(request):
        abort(403)
    j = _jobs.get(jid)
    if j is None:
        abort(404)
    j["result"] = request.get_data()
    j["status"] = "done"
    j["updated"] = _now()
    return jsonify({"ok": True})


@app.post("/jobs/<jid>/error")
def jobs_error(jid):
    if not _worker_ok(request):
        abort(403)
    j = _jobs.get(jid)
    if j is None:
        abort(404)
    body = request.get_json(silent=True) or {}
    j["error"] = str(body.get("error", "erro desconhecido"))[:500]
    j["status"] = "error"
    j["updated"] = _now()
    return jsonify({"ok": True})


# ------------------------------- geral ------------------------------------
@app.get("/health")
def health():
    worker_online = (_now() - _last_worker_seen[0]) < 20
    with _lock:
        pending = sum(1 for j in _jobs.values() if j["status"] == "pending")
    return {"ok": True, "worker_online": worker_online, "pending": pending}


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


INDEX_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MD → Áudio</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,system-ui,sans-serif;
    background:#0e0f13; color:#e8e8ea; display:flex; min-height:100vh;
    align-items:flex-start; justify-content:center; padding:24px; }
  .card { width:100%; max-width:440px; background:#16181f; border:1px solid #262a35;
    border-radius:18px; padding:28px; margin-top:16px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p.sub { margin:0 0 22px; color:#9aa0ae; font-size:14px; }
  label { display:block; font-size:13px; color:#c3c8d4; margin:16px 0 6px; }
  input[type=text], input[type=password], input[type=file], select { width:100%; padding:12px;
    background:#0e0f13; border:1px solid #2c313d; border-radius:10px;
    color:#e8e8ea; font-size:15px; }
  input[type=range] { width:100%; accent-color:#5b8cff; margin-top:4px; }
  .rangeval { color:#5b8cff; font-weight:600; }
  button { width:100%; margin-top:22px; padding:14px; border:0; border-radius:12px;
    background:#5b8cff; color:#fff; font-size:16px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .trow { display:flex; gap:8px; }
  .trow input { flex:1 1 auto; }
  .paste { width:auto; margin:0; padding:0 16px; background:#2c313d; color:#e8e8ea;
    font-size:14px; font-weight:500; border-radius:10px; flex:0 0 auto; }
  .status { margin-top:18px; font-size:14px; min-height:24px; display:flex;
    align-items:center; gap:9px; color:#c3c8d4; }
  .spinner { width:18px; height:18px; border:3px solid #2c313d;
    border-top-color:#5b8cff; border-radius:50%; animation:spin .8s linear infinite;
    flex:0 0 auto; display:none; }
  .spinner.on { display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .status.err { color:#ff6b6b; }
  .conn { margin:2px 0 4px; font-size:13px; color:#7fd18c; display:none; }
  .conn a { color:#9aa0ae; margin-left:8px; }
  .hidden { display:none !important; }
  audio { width:100%; margin-top:18px; }
  .save { margin-top:12px; background:#2c313d; }
  .hist { margin-top:26px; }
  .hist h2 { font-size:14px; color:#9aa0ae; margin:0 0 10px; font-weight:600; }
  .hitem { display:flex; align-items:center; gap:10px; padding:10px 12px;
    background:#0e0f13; border:1px solid #232838; border-radius:12px; margin-bottom:8px; }
  .hinfo { flex:1 1 auto; min-width:0; }
  .hname { font-size:14px; color:#e8e8ea; white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; }
  .hdate { font-size:12px; color:#7b8291; margin-top:2px; }
  .hbtns { display:flex; gap:6px; flex:0 0 auto; }
  .hbtn { width:38px; margin:0; padding:9px 0; background:#20242f; font-size:15px; }
  .hbtn.del { background:transparent; color:#7b8291; }
</style>
</head>
<body>
  <div class="card">
    <h1>MD → Áudio</h1>
    <p class="sub">Suba um arquivo .md e ouça em PT-BR.<br>
      Dica: escreva <b>pausa de 5 segundos</b> (ou <b>pausa de 1 minuto</b>) no texto pra inserir silêncio.<br>
      O áudio é gerado no servidor pessoal (Mac Studio).</p>
    <div class="conn" id="conn">🔓 Conectado neste aparelho<a href="#" id="forget">trocar token</a></div>
    <form id="f">
      <div id="tokenwrap">
        <label for="token">Token de acesso</label>
        <div class="trow">
          <input type="text" id="token" name="token" enterkeyhint="done"
                 autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
          <button type="button" id="paste" class="paste">Colar</button>
        </div>
      </div>
      <label for="title">Título (opcional)</label>
      <input type="text" id="title" enterkeyhint="done" placeholder="ex: Capítulo 3 — se vazio, usa o nome do arquivo">
      <label for="voice">Voz</label>
      <select id="voice" name="voice">
        <option value="pm_santa">Masculina — Santa</option>
        <option value="pm_alex">Masculina — Alex</option>
        <option value="pf_dora">Feminina — Dora</option>
      </select>
      <label for="speed">Velocidade da fala: <span class="rangeval" id="speedval">1,0×</span></label>
      <input type="range" id="speed" name="speed" min="0.8" max="1.3" step="0.05" value="1.0">
      <label for="file">Arquivo Markdown (.md)</label>
      <input type="file" id="file" name="file" accept=".md,.markdown,text/markdown,text/plain" required>
      <button id="btn" type="submit">Gerar áudio</button>
    </form>
    <div class="status" id="status"><span class="spinner" id="spin"></span><span id="stxt"></span></div>
    <div id="player"></div>
    <div class="hist" id="hist"></div>
  </div>
<script>
const f = document.getElementById('f');
const btn = document.getElementById('btn');
const spin = document.getElementById('spin');
const stxt = document.getElementById('stxt');
const statusEl = document.getElementById('status');
const player = document.getElementById('player');
const histEl = document.getElementById('hist');
const tokenEl = document.getElementById('token');
const titleEl = document.getElementById('title');
const fileEl = document.getElementById('file');
const voiceEl = document.getElementById('voice');
const speedEl = document.getElementById('speed');
const speedValEl = document.getElementById('speedval');
const tokenWrap = document.getElementById('tokenwrap');
const conn = document.getElementById('conn');
const JOB_KEY = 'mdaudio_job';
const TOK_KEY = 'mdaudio_token';
let authed = false;

function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function setStatus(msg, opts){
  opts = opts || {};
  stxt.textContent = msg;
  spin.classList.toggle('on', !!opts.loading);
  statusEl.classList.toggle('err', !!opts.err);
}
function setBusy(b){ btn.disabled = b; }
function fmtSpeed(v){ return (Math.round(v*100)/100).toString().replace('.', ',') + '×'; }
function loadPrefs(){
  try {
    const p = JSON.parse(localStorage.getItem('mdaudio_prefs') || '{}');
    if (p.voice) voiceEl.value = p.voice;
    if (p.speed) speedEl.value = p.speed;
  } catch(e){}
  speedValEl.textContent = fmtSpeed(parseFloat(speedEl.value));
}
function savePrefs(){
  try { localStorage.setItem('mdaudio_prefs', JSON.stringify({ voice: voiceEl.value, speed: speedEl.value })); } catch(e){}
}
speedEl.addEventListener('input', () => { speedValEl.textContent = fmtSpeed(parseFloat(speedEl.value)); });
function esc(s){ return (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function sanitize(s){ return ((s||'audio').replace(/[\\\\/:*?"<>|]+/g,'_').trim() || 'audio').slice(0,80); }
function relDate(ts){
  const d = Math.floor((Date.now()-ts)/1000);
  if (d < 60) return 'agora';
  if (d < 3600) return 'há ' + Math.floor(d/60) + ' min';
  if (d < 86400) return 'há ' + Math.floor(d/3600) + ' h';
  return new Date(ts).toLocaleDateString('pt-BR');
}

function showTokenField(show){
  tokenWrap.classList.toggle('hidden', !show);
  conn.style.display = show ? 'none' : 'block';
  if (show) tokenEl.setAttribute('required',''); else tokenEl.removeAttribute('required');
}

// salvar/baixar de forma confiável (Web Share no iOS; fallback download no desktop)
async function saveAudio(blob, filename){
  try {
    const file = new File([blob], filename, { type:'audio/mpeg' });
    if (navigator.canShare && navigator.canShare({ files:[file] })) {
      await navigator.share({ files:[file], title: filename });
      return;
    }
  } catch (e) { if (e && e.name === 'AbortError') return; }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename; document.body.appendChild(a);
  a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(url), 2000);
}

// ---------------- histórico (IndexedDB, por aparelho) ----------------
const DBN='mdaudio_db', STORE='audios', HMAX=12;
function openDB(){ return new Promise((res,rej)=>{
  const r = indexedDB.open(DBN, 1);
  r.onupgradeneeded = () => { if(!r.result.objectStoreNames.contains(STORE)) r.result.createObjectStore(STORE,{keyPath:'id'}); };
  r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
}); }
function txReq(mode, fn){ return new Promise(async (res,rej)=>{
  try { const db = await openDB(); const rq = fn(db.transaction(STORE,mode).objectStore(STORE));
    rq.onsuccess = () => res(rq.result); rq.onerror = () => rej(rq.error);
  } catch(e){ rej(e); }
}); }
async function histAll(){ let a=[]; try { a = await txReq('readonly', s=>s.getAll()) || []; } catch(e){} a.sort((x,y)=>y.ts-x.ts); return a; }
async function histGet(id){ try { return await txReq('readonly', s=>s.get(id)); } catch(e){ return null; } }
async function histDel(id){ try { await txReq('readwrite', s=>s.delete(id)); } catch(e){} }
async function histPut(item){
  try {
    await txReq('readwrite', s=>s.put(item));
    const all = await histAll();
    for (const old of all.slice(HMAX)) await histDel(old.id);
  } catch(e){}
}
async function renderHistory(){
  let items = await histAll();
  if (!items.length) { histEl.innerHTML = ''; return; }
  histEl.innerHTML = '<h2>Histórico</h2>' + items.map(it =>
    '<div class="hitem" data-id="'+it.id+'">' +
      '<div class="hinfo"><div class="hname">'+esc(it.title)+'</div>' +
      '<div class="hdate">'+relDate(it.ts)+'</div></div>' +
      '<div class="hbtns">' +
        '<button class="hbtn" data-act="play" title="Ouvir">▶</button>' +
        '<button class="hbtn" data-act="save" title="Salvar">⬇</button>' +
        '<button class="hbtn del" data-act="del" title="Remover">✕</button>' +
      '</div>' +
    '</div>').join('');
}
histEl.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  const id = e.target.closest('.hitem').getAttribute('data-id');
  const act = b.getAttribute('data-act');
  if (act === 'del') { await histDel(id); renderHistory(); return; }
  const rec = await histGet(id);
  if (!rec || !rec.blob) { setStatus('Item indisponível.', { err:true }); return; }
  if (act === 'play') {
    const url = URL.createObjectURL(rec.blob);
    player.innerHTML = '<audio controls autoplay src="'+url+'"></audio>';
  } else if (act === 'save') {
    saveAudio(rec.blob, sanitize(rec.title) + '.mp3');
  }
});

function showResult(blob, title){
  const url = URL.createObjectURL(blob);
  player.innerHTML =
    '<audio controls autoplay src="'+url+'"></audio>' +
    '<button type="button" class="save" id="savebtn">⬇ Salvar / compartilhar .mp3</button>';
  document.getElementById('savebtn').addEventListener('click', ()=>saveAudio(blob, sanitize(title)+'.mp3'));
}

async function poll(job_id, token, isResume, title){
  setBusy(true);
  while (true) {
    let rr;
    try {
      rr = await fetch('/result/' + job_id, {
        headers: token ? { 'X-App-Token': token } : {},
        credentials: 'same-origin', cache:'no-store'
      });
    } catch (e) {
      setStatus('Sem conexão… tentando de novo', { loading:true });
      await sleep(2500); continue;
    }
    if (rr.status === 200) {
      const blob = await rr.blob();
      setStatus('Pronto!');
      showResult(blob, title || 'áudio');
      try { localStorage.removeItem(JOB_KEY); } catch(e){}
      await histPut({ id: job_id, title: title || 'áudio', ts: Date.now(), blob });
      renderHistory();
      setBusy(false); return;
    }
    if (rr.status === 202) {
      let j = {}; try { j = await rr.json(); } catch(e){}
      setStatus(j.worker_online === false
        ? 'Aguardando o Mac Studio ficar disponível…'
        : 'Gerando o áudio no Mac Studio…', { loading:true });
      await sleep(1500); continue;
    }
    try { localStorage.removeItem(JOB_KEY); } catch(e){}
    if (rr.status === 404 && isResume) { setStatus(''); setBusy(false); return; }
    let msg = 'Erro ' + rr.status;
    try { const j = await rr.json(); if (j && j.error) msg = j.error; } catch(e){}
    if (rr.status === 404) msg = 'Este pedido expirou. Envie o arquivo de novo.';
    setStatus(msg, { err:true });
    setBusy(false); return;
  }
}

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  player.innerHTML = '';
  const token = tokenEl.value;
  if (token) { try { localStorage.setItem(TOK_KEY, token); } catch(e){} }
  const fname = (fileEl.files[0] && fileEl.files[0].name) || 'audio';
  const title = (titleEl.value.trim()) || fname.replace(/\\.[^.]+$/, '');
  savePrefs();
  setBusy(true); setStatus('Enviando…', { loading:true });
  try {
    const data = new FormData(f);
    const r = await fetch('/synthesize', { method:'POST', body:data, credentials:'same-origin' });
    if (!r.ok) {
      let msg = 'Erro ' + r.status; try { const j = await r.json(); if (j && j.error) msg = j.error; } catch(e){}
      setStatus(msg, { err:true }); setBusy(false); return;
    }
    authed = true; showTokenField(false);
    const { job_id } = await r.json();
    try { localStorage.setItem(JOB_KEY, JSON.stringify({ job_id, token, title, ts: Date.now() })); } catch(e){}
    poll(job_id, token, false, title);
  } catch (err) {
    setStatus('Falha: ' + err, { err:true });
    setBusy(false);
  }
});

document.getElementById('paste').addEventListener('click', async () => {
  try {
    const t = ((await navigator.clipboard.readText()) || '').trim();
    if (t) { tokenEl.value = t; setStatus('Token colado.'); tokenEl.blur(); return; }
    setStatus('Área de transferência vazia — copie o token primeiro.', { err:true });
  } catch (e) {
    tokenEl.focus(); tokenEl.select();
    setStatus('Toque e segure no campo acima e escolha "Colar".', { err:true });
  }
});
tokenEl.addEventListener('paste', () => { setTimeout(() => setStatus('Token colado.'), 0); });
tokenEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); tokenEl.blur(); } });
titleEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); titleEl.blur(); } });
document.addEventListener('click', (e) => {
  if (!e.target.closest('input, textarea, button, a')) {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  }
});

document.getElementById('forget').addEventListener('click', async (e) => {
  e.preventDefault();
  try { await fetch('/logout', { method:'POST', credentials:'same-origin' }); } catch(e){}
  try { localStorage.removeItem(TOK_KEY); } catch(e){}
  authed = false; tokenEl.value = ''; showTokenField(true); tokenEl.focus();
});

(async function init(){
  try {
    const ut = (new URLSearchParams(location.search).get('token') || '').trim();
    if (ut) {
      try {
        const r = await fetch('/login', { method:'POST', headers:{ 'X-App-Token': ut }, credentials:'same-origin' });
        if (r.ok) authed = true;
      } catch(e){}
      history.replaceState({}, '', location.pathname);
    }
  } catch(e){}

  if (!authed) {
    try {
      const me = await fetch('/me', { credentials:'same-origin', cache:'no-store' }).then(r=>r.json());
      authed = !!me.authed;
    } catch(e){ authed = false; }
  }
  showTokenField(!authed);
  if (!authed) {
    try { const t = localStorage.getItem(TOK_KEY); if (t) tokenEl.value = t; } catch(e){}
  }

  loadPrefs();
  renderHistory();

  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(JOB_KEY) || 'null'); } catch(e){}
  const fresh = saved && saved.job_id && saved.ts && (Date.now() - saved.ts) < 30*60*1000;
  if (fresh) {
    setStatus('Retomando o áudio em andamento…', { loading:true });
    poll(saved.job_id, saved.token || '', true, saved.title || 'áudio');
  } else if (saved) {
    try { localStorage.removeItem(JOB_KEY); } catch(e){}
  }
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
