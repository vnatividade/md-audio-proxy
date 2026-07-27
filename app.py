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
import json
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

HERE = os.path.dirname(os.path.abspath(__file__))

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


# ------------------------------- PWA --------------------------------------
_MANIFEST = {
    "name": "Meus Áudios",
    "short_name": "Meus Áudios",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#14110E",
    "theme_color": "#14110E",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}

_SW_JS = """
const C = 'mdaudio-v1';
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  e.respondWith(
    fetch(req).then(res => {
      if (req.mode === 'navigate') { const cp = res.clone(); caches.open(C).then(c => c.put('/', cp)); }
      return res;
    }).catch(() => caches.match(req).then(m => m || caches.match('/')))
  );
});
"""


@app.get("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(_MANIFEST), mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(_SW_JS, mimetype="application/javascript")


@app.get("/icon-192.png")
def icon_192():
    return send_file(os.path.join(HERE, "icon-192.png"))


@app.get("/icon-512.png")
def icon_512():
    return send_file(os.path.join(HERE, "icon-512.png"))


@app.get("/apple-touch-icon.png")
def apple_icon():
    return send_file(os.path.join(HERE, "apple-touch-icon.png"))


# Landing (vitrine pública, direção "Lampião" — design/atelier/design-brief.md).
# Arquivo separado para não inchar o app; se faltar, / volta ao comportamento antigo.
try:
    with open(os.path.join(HERE, "landing.html"), encoding="utf-8") as _f:
        LANDING_HTML = _f.read()
except OSError:
    LANDING_HTML = None

# Transformação de texto colado (PIP-721), extraída pra arquivo próprio pra ser
# testável com `node --test` (PIP-722) sem duplicar a lógica. .replace() em vez
# de .format() porque o CSS/JS ao redor está cheio de chaves literais.
with open(os.path.join(HERE, "transform.js"), encoding="utf-8") as _f:
    TRANSFORM_JS = _f.read()


@app.get("/")
def index():
    # Quem já tem acesso (cookie md_auth, ?token= ou header) cai direto no app —
    # inclui a PWA instalada e o fluxo de link com token. Visitante vê a vitrine.
    if LANDING_HTML is None or _user_ok(request):
        return Response(INDEX_HTML, mimetype="text/html")
    return Response(LANDING_HTML, mimetype="text/html")


@app.get("/app")
def app_view():
    # Rota fixa do app (destino do CTA da landing); o formulário de token vive aqui.
    return Response(INDEX_HTML, mimetype="text/html")


INDEX_HTML = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Meus Áudios</title>
<meta name="theme-color" content="#14110E">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Meus Áudios">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<script>try{document.documentElement.dataset.theme=localStorage.getItem('mdaudio_theme')||'auto';}catch(e){document.documentElement.dataset.theme='auto';}</script>
<style>
  :root {
    color-scheme: dark;
    --bg:#14110E; --card:#1E1A15; --border:#33291F; --text:#EFE7DA; --muted:#A79B88;
    --field:#100D09; --btn2:#2E2820; --accent:#E8A33D; --accent-ink:#1A1206;
    --err:#ff6b6b; --ok:#8FD19E; --pin:#F2C15A;
    --display:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,'Times New Roman',serif;
  }
  :root[data-theme="light"] {
    color-scheme: light;
    --bg:#F5EFE4; --card:#FFFFFF; --border:#E6DBC8; --text:#2A2320; --muted:#6B6152;
    --field:#FBF7EF; --btn2:#EEE4D3; --accent:#B4741A; --accent-ink:#FFFFFF;
    --err:#c0392b; --ok:#2F8F4E; --pin:#B8860B;
  }
  @media (prefers-color-scheme: light) {
    :root[data-theme="auto"] {
      color-scheme: light;
      --bg:#F5EFE4; --card:#FFFFFF; --border:#E6DBC8; --text:#2A2320; --muted:#6B6152;
      --field:#FBF7EF; --btn2:#EEE4D3; --accent:#B4741A; --accent-ink:#FFFFFF;
      --err:#c0392b; --ok:#2F8F4E; --pin:#B8860B;
    }
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,system-ui,sans-serif;
    background:var(--bg); color:var(--text); display:flex; min-height:100vh;
    align-items:flex-start; justify-content:center; padding:24px;
    transition:background .2s ease, color .2s ease; }
  .card { width:100%; max-width:440px; margin-top:16px; padding:30px 28px;
    border:1px solid var(--border); border-radius:20px;
    background:
      radial-gradient(300px 150px at 50% -22px, rgba(232,163,61,.16), transparent 72%),
      var(--card);
    box-shadow:0 14px 44px rgba(0,0,0,.22); }
  h1 { font-family:var(--display); font-size:28px; font-weight:600; letter-spacing:.2px;
    margin:0 0 5px; }
  p.sub { margin:0 0 16px; color:var(--muted); font-size:13.5px; line-height:1.5; }
  label { display:block; font-size:13px; color:var(--muted); margin:16px 0 6px; }
  input[type=text], input[type=password], input[type=file], select, textarea { width:100%; padding:12px;
    background:var(--field); border:1px solid var(--border); border-radius:10px;
    color:var(--text); font-size:15px; }
  textarea { font-family:inherit; resize:vertical; min-height:120px; line-height:1.45; }
  input[type=range] { width:100%; accent-color:var(--accent); margin-top:4px; }
  .rangeval { color:var(--accent); font-weight:600; }
  button { width:100%; margin-top:22px; padding:14px; border:0; border-radius:12px;
    background:var(--accent); color:var(--accent-ink); font-size:16px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .trow { display:flex; gap:8px; }
  .trow input { flex:1 1 auto; }
  .paste { width:auto; margin:0; padding:0 16px; background:var(--btn2); color:var(--text);
    font-size:14px; font-weight:500; border-radius:10px; flex:0 0 auto; }
  .settings { display:flex; align-items:center; gap:10px; margin:0 0 8px; }
  .seg { display:inline-flex; background:var(--field); border:1px solid var(--border);
    border-radius:10px; overflow:hidden; }
  .seg button { width:auto; margin:0; padding:13px 14px; background:transparent; color:var(--muted);
    font-size:13px; font-weight:600; border-radius:0; min-height:44px; }
  .seg button.on { background:var(--accent); color:var(--accent-ink); }
  .previewmeta { font-size:13px; color:var(--accent); font-weight:600; margin:0 0 8px; }
  .status { margin-top:18px; font-size:14px; min-height:24px; display:flex;
    align-items:center; gap:9px; color:var(--muted); }
  .spinner { width:18px; height:18px; border:3px solid var(--border);
    border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite;
    flex:0 0 auto; display:none; }
  .spinner.on { display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .status.err { color:var(--err); }
  .conn { margin:2px 0 4px; font-size:13px; color:var(--ok); display:none; }
  .conn a { color:var(--muted); margin-left:8px; }
  .hidden { display:none !important; }
  audio { width:100%; margin-top:18px; }
  .pctl { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap; align-items:center; }
  .pbtn { width:auto; margin:0; padding:9px 14px; background:var(--btn2); color:var(--text);
    font-size:14px; font-weight:600; border-radius:10px; }
  .pbtn.save { margin-left:auto; }
  .hist { margin-top:26px; }
  .hist h2 { font-size:14px; color:var(--muted); margin:0 0 10px; font-weight:600; }
  .hitem { display:flex; align-items:center; gap:10px; padding:10px 12px;
    background:var(--field); border:1px solid var(--border); border-radius:12px; margin-bottom:8px; }
  .hinfo { flex:1 1 auto; min-width:0; }
  .hname { font-size:14px; color:var(--text); white-space:nowrap; overflow:hidden;
    text-overflow:ellipsis; }
  .hdate { font-size:12px; color:var(--muted); margin-top:2px; }
  .hbtns { display:flex; gap:5px; flex:0 0 auto; }
  .hbtn { width:32px; margin:0; padding:8px 0; background:var(--btn2); color:var(--text); font-size:14px; }
  .hbtn.del { background:transparent; color:var(--muted); }
  .hbtn.on { color:var(--pin); }
  #histsearch { margin-bottom:12px; }
  .renameinp { width:100%; padding:6px 8px; background:var(--field); border:1px solid var(--border);
    border-radius:8px; color:var(--text); font-size:14px; }
  /* facelift */
  #btn { box-shadow:0 6px 18px rgba(232,163,61,.22); }
  .secondary { background:var(--btn2); color:var(--text); margin-top:12px; font-size:15px; box-shadow:none; }
  .hist h2 { font-family:var(--display); font-size:16px; color:var(--text); }
  .hitem.pinned { border-left:3px solid var(--pin); }
  .hitem { transition:border-color .15s ease; }
</style>
</head>
<body>
  <div class="card">
    <h1>Meus Áudios</h1>
    <p class="sub">Cole um texto ou suba um arquivo .md, e ouça em PT-BR.<br>
      Dica: escreva <b>pausa de 5 segundos</b> (ou <b>pausa de 1 minuto</b>) no texto pra inserir silêncio — vale pro que você cola também.<br>
      O áudio é gerado no servidor pessoal (Mac Studio).</p>
    <div class="settings">
      <div class="seg" id="themeseg">
        <button type="button" data-theme="auto">Auto</button>
        <button type="button" data-theme="light">Claro</button>
        <button type="button" data-theme="dark">Escuro</button>
      </div>
    </div>
    <div class="conn" id="conn">Conectado neste aparelho<a href="#" id="forget">trocar token</a></div>
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
      <input type="text" id="title" enterkeyhint="done" placeholder="ex: Capítulo 3 — se vazio, usa o nome do arquivo ou &quot;Texto colado&quot;">
      <label for="voice">Voz</label>
      <select id="voice" name="voice">
        <option value="pm_santa">Masculina — Santa</option>
        <option value="pm_alex">Masculina — Alex</option>
        <option value="pf_dora">Feminina — Dora</option>
      </select>
      <label for="speed">Velocidade da fala: <span class="rangeval" id="speedval">1,0×</span></label>
      <input type="range" id="speed" name="speed" min="0.8" max="1.3" step="0.05" value="1.0">
      <button type="button" id="preview" class="secondary">Ouvir prévia da voz</button>
      <label for="pastetext">Colar texto (opcional se enviar arquivo)</label>
      <div class="settings">
        <div class="seg" id="pastemodeseg">
          <button type="button" data-mode="text" class="on">Texto cru</button>
          <button type="button" data-mode="markdown">Markdown cru</button>
        </div>
      </div>
      <textarea id="pastetext" rows="6"
        placeholder="Cole aqui uma resposta de IA, anotação ou markdown — sem precisar salvar arquivo antes."></textarea>
      <div id="previewToggleWrap" class="hidden">
        <button type="button" id="previewToggle" class="secondary">Ver prévia do texto falado</button>
      </div>
      <div id="previewPanel" class="hidden">
        <label for="previewText">Prévia — é isto que será enviado, edite se quiser</label>
        <div class="previewmeta" id="previewMeta"></div>
        <textarea id="previewText" rows="8"></textarea>
      </div>
      <label for="file">Arquivo Markdown (.md)</label>
      <input type="file" id="file" name="file" accept=".md,.markdown,text/markdown,text/plain">
      <button id="btn" type="submit">Gerar áudio</button>
    </form>
    <div class="status" id="status"><span class="spinner" id="spin"></span><span id="stxt"></span></div>
    <div id="player"></div>
    <div class="hist">
      <div id="histhead" class="hidden">
        <h2>Histórico</h2>
        <input type="text" id="histsearch" placeholder="Buscar no histórico"
               enterkeyhint="search" autocapitalize="off" autocorrect="off" spellcheck="false">
      </div>
      <div id="histlist"></div>
    </div>
  </div>
<script>
__MDAUDIO_TRANSFORM_JS__
const f = document.getElementById('f');
const btn = document.getElementById('btn');
const spin = document.getElementById('spin');
const stxt = document.getElementById('stxt');
const statusEl = document.getElementById('status');
const player = document.getElementById('player');
const histListEl = document.getElementById('histlist');
const histHeadEl = document.getElementById('histhead');
const histSearchEl = document.getElementById('histsearch');
const tokenEl = document.getElementById('token');
const titleEl = document.getElementById('title');
const fileEl = document.getElementById('file');
const pasteEl = document.getElementById('pastetext');
const pasteModeSeg = document.getElementById('pastemodeseg');
const previewToggleWrap = document.getElementById('previewToggleWrap');
const previewToggle = document.getElementById('previewToggle');
const previewPanel = document.getElementById('previewPanel');
const previewMeta = document.getElementById('previewMeta');
const previewTextEl = document.getElementById('previewText');
const voiceEl = document.getElementById('voice');
const speedEl = document.getElementById('speed');
const speedValEl = document.getElementById('speedval');
const tokenWrap = document.getElementById('tokenwrap');
const conn = document.getElementById('conn');
const JOB_KEY = 'mdaudio_job';
const TOK_KEY = 'mdaudio_token';
const MAX_PASTE_BYTES = 2 * 1024 * 1024;
let authed = false;
let pasteMode = 'text';
let pasteModeTouched = false;
let previewOpen = false;
let previewEdited = false;
let previewDebounce = null;
const MDA_CHARS_PER_MIN_1X = 825; // ~150 palavras/min * ~5.5 chars/palavra, ponto de partida pra afinar ouvindo

// auto-detecção por densidade de sintaxe markdown (cabeçalho, lista, cerca,
// ênfase, citação) — o override manual do usuário sempre vence (ver abaixo)
function detectPasteMode(text) {
  const lines = text.split('\\n').filter(l => l.trim());
  if (!lines.length) return 'text';
  let hits = 0;
  for (const l of lines) {
    if (
      /^#{1,6}\\s/.test(l) || /^\\s*(?:[-*+]|\\d+\\.)\\s+/.test(l) ||
      /^\\s{0,3}```/.test(l) || /^\\s{0,3}>/.test(l) ||
      /(\\*\\*|__)[^*_]+\\1/.test(l) || /\\[[^\\]]*\\]\\([^)]*\\)/.test(l)
    ) hits++;
  }
  return (hits / lines.length) >= 0.15 ? 'markdown' : 'text';
}
function applyPasteMode(m) {
  pasteMode = m;
  [...pasteModeSeg.children].forEach(b => b.classList.toggle('on', b.dataset.mode === m));
}

// duração é estimativa deliberada: não conhece o ritmo real do Kokoro, só
// soma as pausas explícitas (essas sim exatas) com uma taxa de fala aproximada
function mdaEstimateSeconds(text, speed) {
  const re = /\\[?\\s*(?:pausa|pause)(?:\\s+de)?\\s+(\\d+(?:[.,]\\d+)?)\\s*(minutos?|mins?|min|m|segundos?|segs?|seg|s)\\b\\.?\\s*\\]?/gi;
  let pauseSeconds = 0, spokenLen = text.length, m;
  while ((m = re.exec(text))) {
    const num = parseFloat(m[1].replace(',', '.'));
    const secs = m[2].toLowerCase().startsWith('m') ? num * 60 : num;
    pauseSeconds += Math.max(0.1, Math.min(secs, 300));
    spokenLen -= m[0].length;
  }
  const sp = Math.max(0.5, Math.min(speed || 1, 2.0));
  return Math.max(0, Math.round(pauseSeconds + (Math.max(0, spokenLen) / MDA_CHARS_PER_MIN_1X) * 60 / sp));
}
function mdaFmtDuration(totalSeconds) {
  const mm = Math.floor(totalSeconds / 60), ss = totalSeconds % 60;
  return mm > 0 ? (mm + ' min ' + ss + ' s') : (ss + ' s');
}
// conta os marcadores presentes no texto ATUAL da prévia (não num estado à
// parte) — assim o resumo bate mesmo depois de o usuário editar à mão
function mdaCountMarkersInText(text) {
  const defs = [
    ['código', /\\[código omitido\\]/g],
    ['link', /\\[link omitido\\]/g],
    ['imagem', /\\[imagem omitida\\]/g],
    ['tabela', /\\[tabela omitida\\]/g],
  ];
  const counts = {};
  for (const [label, re] of defs) {
    const n = (text.match(re) || []).length;
    if (n) counts[label] = n;
  }
  return counts;
}
function renderPreviewMeta(text, speed) {
  const counts = mdaCountMarkersInText(text);
  const bits = [];
  const entries = Object.entries(counts);
  if (entries.length) {
    bits.push(entries.map(([k, n]) => n + ' ' + k + (n > 1 ? 's' : '') + ' omitido' + (n > 1 && k !== 'tabela' ? 's' : '')).join(', '));
  } else {
    bits.push('Nenhuma omissão');
  }
  bits.push('duração estimada: ' + mdaFmtDuration(mdaEstimateSeconds(text, speed)) + ' (estimativa)');
  previewMeta.textContent = bits.join(' · ');
}
// recalcula a transformação; force=true ignora edição manual do usuário no
// preview (troca de modo invalida a edição anterior, feita sob o modo velho)
function recomputeAll(force) {
  if (!pasteEl.value.trim()) {
    previewToggleWrap.classList.add('hidden');
    previewPanel.classList.add('hidden');
    previewOpen = false;
    return;
  }
  previewToggleWrap.classList.remove('hidden');
  if (previewEdited && !force) return;
  const result = mdaudioTransform(pasteEl.value, pasteMode);
  if (!titleTouched && result.title) titleEl.value = result.title;
  if (previewOpen || force) {
    previewTextEl.value = result.speakable;
    renderPreviewMeta(result.speakable, parseFloat(speedEl.value));
    if (force) previewEdited = false;
  }
}
previewToggle.addEventListener('click', () => {
  previewOpen = !previewOpen;
  previewPanel.classList.toggle('hidden', !previewOpen);
  previewToggle.textContent = previewOpen ? 'Ocultar prévia' : 'Ver prévia do texto falado';
  if (previewOpen) recomputeAll(true);
});
previewTextEl.addEventListener('input', () => {
  previewEdited = true;
  renderPreviewMeta(previewTextEl.value, parseFloat(speedEl.value));
});
pasteModeSeg.addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  pasteModeTouched = true;
  applyPasteMode(b.dataset.mode);
  recomputeAll(true);
});
pasteEl.addEventListener('input', () => {
  if (!pasteModeTouched) applyPasteMode(detectPasteMode(pasteEl.value));
  clearTimeout(previewDebounce);
  previewDebounce = setTimeout(() => recomputeAll(false), 400);
});

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
speedEl.addEventListener('input', () => {
  speedValEl.textContent = fmtSpeed(parseFloat(speedEl.value));
  if (previewOpen) renderPreviewMeta(previewTextEl.value, parseFloat(speedEl.value));
});

const themeSeg = document.getElementById('themeseg');
function applyTheme(t){
  if (!['auto','light','dark'].includes(t)) t = 'auto';
  document.documentElement.dataset.theme = t;
  [...themeSeg.children].forEach(b => b.classList.toggle('on', b.dataset.theme === t));
  try { localStorage.setItem('mdaudio_theme', t); } catch(e){}
  const tc = document.querySelector('meta[name="theme-color"]');
  if (tc) requestAnimationFrame(() => tc.setAttribute('content', getComputedStyle(document.body).backgroundColor));
}
themeSeg.addEventListener('click', e => { const b = e.target.closest('button'); if (b) applyTheme(b.dataset.theme); });
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
const DBN='mdaudio_db', STORE='audios', HMAX=20;
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
    const unpinned = all.filter(x=>!x.pinned);   // fixados nunca são removidos
    for (const old of unpinned.slice(HMAX)) await histDel(old.id);
  } catch(e){}
}
let _histItems = [];
async function renderHistory(){
  _histItems = await histAll();
  histHeadEl.classList.toggle('hidden', _histItems.length === 0);
  renderList();
}
function renderList(){
  const q = (histSearchEl.value || '').trim().toLowerCase();
  let items = _histItems.slice();
  if (q) items = items.filter(it => (it.title || '').toLowerCase().includes(q));
  items.sort((a, b) => (b.pinned?1:0) - (a.pinned?1:0) || b.ts - a.ts);  // fixados no topo
  if (!items.length) { histListEl.innerHTML = q ? '<div class="hdate">Nada encontrado.</div>' : ''; return; }
  histListEl.innerHTML = items.map(it =>
    '<div class="hitem'+(it.pinned?' pinned':'')+'" data-id="'+it.id+'">' +
      '<div class="hinfo"><div class="hname">'+esc(it.title)+'</div>' +
      '<div class="hdate">'+relDate(it.ts)+'</div></div>' +
      '<div class="hbtns">' +
        '<button class="hbtn" data-act="play" title="Ouvir">▶</button>' +
        '<button class="hbtn'+(it.pinned?' on':'')+'" data-act="pin" title="Fixar">'+(it.pinned?'★':'☆')+'</button>' +
        '<button class="hbtn" data-act="rename" title="Renomear">✎</button>' +
        '<button class="hbtn" data-act="save" title="Salvar">⬇</button>' +
        '<button class="hbtn del" data-act="del" title="Remover">✕</button>' +
      '</div>' +
    '</div>').join('');
}
histSearchEl.addEventListener('input', renderList);
histSearchEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); histSearchEl.blur(); } });

histListEl.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-act]'); if (!b) return;
  const row = e.target.closest('.hitem'); const id = row.getAttribute('data-id');
  const act = b.getAttribute('data-act');
  if (act === 'del') { await histDel(id); renderHistory(); return; }
  const rec = await histGet(id);
  if (!rec) { setStatus('Item indisponível.', { err:true }); return; }
  if (act === 'play') {
    if (rec.blob) mountPlayer(player, rec.blob, rec.id, rec.title);
  } else if (act === 'save') {
    if (rec.blob) saveAudio(rec.blob, sanitize(rec.title) + '.mp3');
  } else if (act === 'pin') {
    rec.pinned = !rec.pinned; await histPut(rec); renderHistory();
  } else if (act === 'rename') {
    const nameEl = row.querySelector('.hname');
    const cur = rec.title || '';
    nameEl.innerHTML = '<input class="renameinp" type="text" enterkeyhint="done">';
    const inp = nameEl.querySelector('input'); inp.value = cur; inp.focus(); inp.select();
    let done = false;
    const commit = async () => {
      if (done) return; done = true;
      rec.title = (inp.value.trim() || cur); await histPut(rec); renderHistory();
    };
    inp.addEventListener('keydown', ev => { if (ev.key === 'Enter') { ev.preventDefault(); commit(); } });
    inp.addEventListener('blur', commit);
  }
});

function fmtTime(s){ s = Math.max(0, Math.floor(s||0)); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }

// player com velocidade de reprodução, pular ±15s, salvar e retomar posição
function mountPlayer(container, blob, id, title){
  const url = URL.createObjectURL(blob);
  container.innerHTML =
    '<audio id="pl" controls src="'+url+'"></audio>' +
    '<div class="pctl">' +
      '<button type="button" class="pbtn" id="back15">« 15s</button>' +
      '<button type="button" class="pbtn" id="rate">1×</button>' +
      '<button type="button" class="pbtn" id="fwd15">15s »</button>' +
      '<button type="button" class="pbtn save" id="savebtn">⬇ Salvar</button>' +
    '</div>';
  const a = container.querySelector('#pl');
  const rates = [1, 1.25, 1.5, 2];
  let rate = parseFloat(localStorage.getItem('mdaudio_rate') || '1') || 1;
  if (!rates.includes(rate)) rate = 1;
  const rbtn = container.querySelector('#rate');
  function applyRate(){ a.playbackRate = rate; rbtn.textContent = (rate+'').replace('.', ',') + '×'; }
  applyRate();
  rbtn.addEventListener('click', () => {
    rate = rates[(rates.indexOf(rate) + 1) % rates.length];
    try { localStorage.setItem('mdaudio_rate', rate); } catch(e){}
    applyRate();
  });
  container.querySelector('#back15').addEventListener('click', () => { a.currentTime = Math.max(0, a.currentTime - 15); });
  container.querySelector('#fwd15').addEventListener('click', () => { a.currentTime = Math.min((a.duration||1e9), a.currentTime + 15); });
  container.querySelector('#savebtn').addEventListener('click', () => saveAudio(blob, sanitize(title) + '.mp3'));

  // retomar de onde parou (posição por item, guardada no localStorage)
  if (id) {
    const POS = 'mdpos:' + id;
    const saved = parseFloat(localStorage.getItem(POS) || '0');
    a.addEventListener('loadedmetadata', () => {
      applyRate();
      if (saved > 5 && saved < a.duration - 5) { a.currentTime = saved; setStatus('Continuando de ' + fmtTime(saved)); }
    }, { once:true });
    let last = 0;
    a.addEventListener('timeupdate', () => {
      const t = a.currentTime;
      if (Math.abs(t - last) >= 5) { last = t; try { localStorage.setItem(POS, t); } catch(e){} }
    });
    a.addEventListener('pause', () => { try { localStorage.setItem(POS, a.currentTime); } catch(e){} });
    a.addEventListener('ended', () => { try { localStorage.removeItem(POS); } catch(e){} });
  }
  a.play().catch(()=>{});
}

function showResult(blob, title, id){ mountPlayer(player, blob, id, title); }

async function poll(job_id, token, isResume, title, isPreview){
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
      setStatus(isPreview ? 'Prévia pronta — ajuste ou gere o áudio completo.' : 'Pronto!');
      showResult(blob, isPreview ? 'Prévia' : (title || 'áudio'), isPreview ? null : job_id);
      try { localStorage.removeItem(JOB_KEY); } catch(e){}
      if (!isPreview) { await histPut({ id: job_id, title: title || 'áudio', ts: Date.now(), blob }); renderHistory(); }
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

  const pasted = pasteEl.value;
  const usingPaste = pasted.trim().length > 0; // colar sempre ganha do arquivo, se os dois vierem preenchidos
  const hasFile = !!fileEl.files[0];
  if (!usingPaste && !hasFile) {
    setStatus('Cole um texto ou escolha um arquivo .md.', { err:true });
    return;
  }

  // texto colado sempre passa pela transformação — o preview, quando aberto,
  // é a fonte da verdade (edição do usuário vence); se nunca foi aberto, a
  // transformação roda aqui mesmo, silenciosamente, do mesmo jeito
  let sendText = pasted;
  if (usingPaste) {
    if (previewOpen) {
      sendText = previewTextEl.value;
      if (!sendText.trim()) {
        setStatus('A prévia está vazia — edite o texto antes de gerar.', { err:true });
        return;
      }
    } else {
      const result = mdaudioTransform(pasted, pasteMode);
      if (result.empty) {
        setStatus('O texto colado não sobra com conteúdo falável depois da limpeza — abra a prévia e edite antes de gerar.', { err:true });
        return;
      }
      sendText = result.speakable;
    }
  }
  if (usingPaste && new TextEncoder().encode(sendText).length > MAX_PASTE_BYTES) {
    setStatus('Texto passa de 2 MB depois da transformação — reduza antes de gerar.', { err:true });
    return;
  }

  const fname = usingPaste ? 'colado.md' : ((fileEl.files[0] && fileEl.files[0].name) || 'audio');
  const title = (titleEl.value.trim()) || (usingPaste ? 'Texto colado' : fname.replace(/\\.[^.]+$/, ''));
  savePrefs();
  setBusy(true); setStatus('Enviando…', { loading:true });
  try {
    const data = new FormData(f);
    if (usingPaste) data.set('file', new File([sendText], fname, { type:'text/markdown' }));
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

async function doPreview(){
  const txt = 'Esta é uma prévia da voz e da velocidade selecionadas. Se gostar, gere o áudio completo.';
  savePrefs();
  player.innerHTML = '';
  setBusy(true); setStatus('Gerando prévia…', { loading:true });
  try {
    const fd = new FormData();
    fd.append('voice', voiceEl.value);
    fd.append('speed', speedEl.value);
    fd.append('token', tokenEl.value);
    fd.append('file', new File([txt], 'previa.md', { type:'text/markdown' }));
    const r = await fetch('/synthesize', { method:'POST', body:fd, credentials:'same-origin' });
    if (!r.ok) {
      let m = 'Erro ' + r.status; try { const j = await r.json(); if (j && j.error) m = j.error; } catch(e){}
      setStatus(m, { err:true }); setBusy(false); return;
    }
    authed = true; showTokenField(false);
    const { job_id } = await r.json();
    poll(job_id, tokenEl.value, false, 'Prévia', true);
  } catch (err) { setStatus('Falha: ' + err, { err:true }); setBusy(false); }
}
document.getElementById('preview').addEventListener('click', doPreview);

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

// título vem do nome do arquivo; se o usuário editar, respeita a edição
let titleTouched = false;
titleEl.addEventListener('input', () => { titleTouched = true; });
fileEl.addEventListener('change', () => {
  const fn = fileEl.files[0] && fileEl.files[0].name;
  if (fn && !titleTouched) titleEl.value = fn.replace(/\\.[^.]+$/, '');
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('input, textarea, button, a, select, label, option')) {
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

  applyTheme(document.documentElement.dataset.theme || 'auto');
  loadPrefs();
  renderHistory();

  if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(()=>{}); }

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

INDEX_HTML = INDEX_HTML.replace("__MDAUDIO_TRANSFORM_JS__", TRANSFORM_JS)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
