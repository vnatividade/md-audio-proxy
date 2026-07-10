"""
Fachada Railway (fila de jobs). NÃO faz TTS — só recebe upload, enfileira,
e serve o mp3 que o worker local (Mac Studio) devolve.

Fluxo:
  navegador  --POST /synthesize-->  cria job (pending), devolve job_id
  Mac worker --GET  /jobs/next  -->  pega job pending (vira processing)
  Mac worker --POST /jobs/<id>/result --> entrega mp3 (vira done)
  navegador  --GET  /result/<id>  -->  baixa o mp3 quando pronto
"""
import os
import io
import time
import uuid
import threading

from flask import Flask, request, send_file, abort, jsonify, Response

app = Flask(__name__)

APP_TOKEN = os.environ.get("APP_TOKEN", "")           # token do usuário (navegador)
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")     # token do canal Railway<->Mac
JOB_TTL = int(os.environ.get("JOB_TTL", "1800"))      # jobs somem após 30 min
PROCESSING_TIMEOUT = int(os.environ.get("PROCESSING_TIMEOUT", "600"))
MAX_MD_BYTES = int(os.environ.get("MAX_MD_BYTES", str(2 * 1024 * 1024)))

_jobs = {}            # id -> dict
_lock = threading.Lock()
_last_worker_seen = [0.0]   # timestamp do último poll do worker


def _now():
    return time.time()


def _cleanup():
    now = _now()
    for k in list(_jobs.keys()):
        j = _jobs[k]
        if now - j["created"] > JOB_TTL:
            _jobs.pop(k, None)
        elif j["status"] == "processing" and now - j["updated"] > PROCESSING_TIMEOUT:
            # worker sumiu no meio: volta pra fila
            j["status"] = "pending"
            j["updated"] = now


def _user_ok(req):
    if not APP_TOKEN:
        return False
    supplied = (
        req.headers.get("X-App-Token")
        or req.form.get("token")
        or req.args.get("token")
        or ""
    )
    return supplied == APP_TOKEN


def _worker_ok(req):
    if not WORKER_TOKEN:
        return False
    return req.headers.get("X-Worker-Token", "") == WORKER_TOKEN


@app.after_request
def _no_store(resp):
    # nada de cache: garante que o navegador sempre pega o HTML/JS mais novo
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

    jid = uuid.uuid4().hex[:16]
    name = os.path.splitext(os.path.basename(up.filename))[0] or "audio"
    with _lock:
        _cleanup()
        _jobs[jid] = {
            "status": "pending", "text": text, "name": name,
            "result": None, "error": None,
            "created": _now(), "updated": _now(),
        }
    return jsonify({"job_id": jid, "name": name})


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
    # pending / processing
    worker_online = (_now() - _last_worker_seen[0]) < 20
    return jsonify({"status": j["status"], "worker_online": worker_online}), 202


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
                return jsonify({"id": jid, "text": j["text"], "name": j["name"]})
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
    align-items:center; justify-content:center; padding:24px; }
  .card { width:100%; max-width:440px; background:#16181f; border:1px solid #262a35;
    border-radius:18px; padding:28px; }
  h1 { font-size:22px; margin:0 0 4px; }
  p.sub { margin:0 0 22px; color:#9aa0ae; font-size:14px; }
  label { display:block; font-size:13px; color:#c3c8d4; margin:16px 0 6px; }
  input[type=password], input[type=file] { width:100%; padding:12px;
    background:#0e0f13; border:1px solid #2c313d; border-radius:10px;
    color:#e8e8ea; font-size:15px; }
  button { width:100%; margin-top:22px; padding:14px; border:0; border-radius:12px;
    background:#5b8cff; color:#fff; font-size:16px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.55; cursor:default; }
  .status { margin-top:18px; font-size:14px; min-height:24px; display:flex;
    align-items:center; gap:9px; color:#c3c8d4; }
  .spinner { width:18px; height:18px; border:3px solid #2c313d;
    border-top-color:#5b8cff; border-radius:50%; animation:spin .8s linear infinite;
    flex:0 0 auto; display:none; }
  .spinner.on { display:inline-block; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .status.err { color:#ff6b6b; }
  audio { width:100%; margin-top:18px; }
  a.dl { display:inline-block; margin-top:10px; color:#5b8cff; font-size:14px; }
</style>
</head>
<body>
  <div class="card">
    <h1>MD → Áudio</h1>
    <p class="sub">Suba um arquivo .md e ouça em PT-BR.<br>
      Dica: escreva <b>pausa de 5 segundos</b> (ou <b>pausa de 1 minuto</b>) no texto pra inserir silêncio.<br>
      O áudio é gerado no servidor pessoal (Mac Studio).</p>
    <form id="f">
      <label for="token">Token de acesso</label>
      <input type="password" id="token" name="token" autocomplete="current-password" required>
      <label for="file">Arquivo Markdown (.md)</label>
      <input type="file" id="file" name="file" accept=".md,.markdown,text/markdown,text/plain" required>
      <button id="btn" type="submit">Gerar áudio</button>
    </form>
    <div class="status" id="status"><span class="spinner" id="spin"></span><span id="stxt"></span></div>
    <div id="player"></div>
  </div>
<script>
const f = document.getElementById('f');
const btn = document.getElementById('btn');
const statusEl = document.getElementById('status');
const spin = document.getElementById('spin');
const stxt = document.getElementById('stxt');
const player = document.getElementById('player');
const tokenEl = document.getElementById('token');
const JOB_KEY = 'mdaudio_job';
const TOK_KEY = 'mdaudio_token';

function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }
function setStatus(msg, opts){
  opts = opts || {};
  stxt.textContent = msg;
  spin.classList.toggle('on', !!opts.loading);
  statusEl.classList.toggle('err', !!opts.err);
}
function setBusy(b){ btn.disabled = b; }

// pré-preenche o token salvo (comodidade no celular)
try { const t = localStorage.getItem(TOK_KEY); if (t) tokenEl.value = t; } catch(e){}

async function poll(job_id, token){
  setBusy(true);
  // sem prazo curto: enquanto houver job, seguimos acompanhando
  while (true) {
    let rr;
    try {
      rr = await fetch('/result/' + job_id, { headers: { 'X-App-Token': token }, cache:'no-store' });
    } catch (e) {
      setStatus('Sem conexão… tentando de novo', { loading:true });
      await sleep(2500); continue;
    }
    if (rr.status === 200) {
      const blob = await rr.blob();
      const url = URL.createObjectURL(blob);
      setStatus('Pronto!');
      player.innerHTML =
        '<audio controls autoplay src="'+url+'"></audio>' +
        '<a class="dl" href="'+url+'" download="audio.mp3">Baixar .mp3</a>';
      try { localStorage.removeItem(JOB_KEY); } catch(e){}
      setBusy(false); return;
    }
    if (rr.status === 202) {
      let j = {}; try { j = await rr.json(); } catch(e){}
      setStatus(j.worker_online === false
        ? 'Aguardando o Mac Studio ficar disponível…'
        : 'Gerando o áudio no Mac Studio…', { loading:true });
      await sleep(1500); continue;
    }
    // 404 (expirou) / 500 (erro) — encerra
    let t = ''; try { t = await rr.text(); } catch(e){}
    setStatus('Erro ' + rr.status + ': ' + t.slice(0,160), { err:true });
    try { localStorage.removeItem(JOB_KEY); } catch(e){}
    setBusy(false); return;
  }
}

f.addEventListener('submit', async (e) => {
  e.preventDefault();
  player.innerHTML = '';
  const token = tokenEl.value;
  try { localStorage.setItem(TOK_KEY, token); } catch(e){}
  setBusy(true); setStatus('Enviando…', { loading:true });
  try {
    const data = new FormData(f);
    const r = await fetch('/synthesize', { method:'POST', body:data });
    if (!r.ok) {
      const t = await r.text();
      setStatus('Erro ' + r.status + ': ' + t.slice(0,160), { err:true });
      setBusy(false); return;
    }
    const { job_id } = await r.json();
    try { localStorage.setItem(JOB_KEY, JSON.stringify({ job_id, token, ts: Date.now() })); } catch(e){}
    poll(job_id, token);
  } catch (err) {
    setStatus('Falha: ' + err, { err:true });
    setBusy(false);
  }
});

// retoma automaticamente um job em andamento se a página foi recarregada/reaberta
(function resume(){
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(JOB_KEY) || 'null'); } catch(e){}
  if (saved && saved.job_id && saved.token) {
    setStatus('Retomando o áudio em andamento…', { loading:true });
    poll(saved.job_id, saved.token);
  }
})();
</script>
</body>
</html>"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
