"""
validador_web.py
================
Interface de revisao no navegador (Flask) para os pontos de atencao do dataset.

Toda a leitura e escrita passa pelo validador.py - inclusive o _guard() que
impede escrita no labels.csv original. Este arquivo e so a camada de tela: se
uma regra de negocio precisar mudar, muda la, e a web acompanha.

    python validador.py web              # abre em http://127.0.0.1:5005
    python validador_web.py              # equivalente

O que da para fazer na tela
---------------------------
    - assistir ao lance no VIDEO ORIGINAL, no instante certo, em camera lenta
    - andar frame a frame (frames exatos, decodificados pelo OpenCV)
    - comparar os angulos do mesmo lance lado a lado
    - clicar na grade do gol para corrigir a regiao
    - marcar gol/nao-gol, trocar a camera, reposicionar inicio/chute
    - aprovar sem alteracao, descartar linha duplicada, desfazer
"""

from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import threading
import webbrowser
from collections import defaultdict
from typing import Any, Optional

from flask import Flask, Response, abort, jsonify, request, send_file

import validador as V

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_ctx: dict[str, Any] = {}          # paths + source_dir + indice de videos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def paths() -> V.Paths:
    return _ctx["paths"]


def indice() -> dict[str, str]:
    return _ctx["indice"]


def _linha_json(l: dict, i: int) -> dict:
    fps = V.fps_estimado(l) or V.FPS_ESPERADO
    return {
        "n": i,
        "row_id": V.row_id(l),
        "penalty_id": l.get("penalty_id", ""),
        "camera_type": l.get("camera_type", ""),
        "region": l.get("region", ""),
        "region_label": l.get("region_label", ""),
        "is_goal": V.is_goal(l),
        "inicio_frame": int(V.num(l, "inicio_frame")),
        "chute_frame": int(V.num(l, "chute_frame")),
        "inicio_time_s": round(V.num(l, "inicio_time_s"), 3),
        "chute_time_s": round(V.num(l, "chute_time_s"), 3),
        "dur_s": round(V.duracao_s(l), 3),
        "dur_frames": V.duracao_frames(l),
        "fps": round(fps, 3),
        "clip_existe": os.path.isfile(l.get("clip_path", "")),
        "clip_nome": os.path.basename(l.get("clip_path", "")),
    }


def _erro(msg: str, code: int = 400):
    return jsonify({"erro": msg}), code


# ---------------------------------------------------------------------------
# API - fila e detalhe
# ---------------------------------------------------------------------------
@app.get("/api/fila")
def api_fila():
    _, _, revisoes = V.carregar(paths())
    prio = request.args.get("prioridade", "")
    tipo = request.args.get("tipo", "")
    status = request.args.get("status", "pendente")
    busca = request.args.get("busca", "").lower()

    sel = [r for r in revisoes
           if (not prio or r["prioridade"] == prio)
           and (not tipo or r["tipo"] == tipo)
           and (status == "todos" or r["status"] == status)
           and (not busca or busca in r["video_file"].lower())]

    tipos: dict[str, int] = defaultdict(int)
    por_status: dict[str, int] = defaultdict(int)
    for r in revisoes:
        tipos[r["tipo"]] += 1
        por_status[r["status"]] += 1

    return jsonify({
        "itens": [{k: r[k] for k in ("id_revisao", "prioridade", "tipo", "status",
                                     "descricao", "video_file", "n_linhas", "t_chute_s")}
                  for r in sel],
        "total": len(revisoes),
        "filtrados": len(sel),
        "tipos": dict(sorted(tipos.items())),
        "status": dict(por_status),
        "pendentes": por_status.get("pendente", 0),
    })


@app.get("/api/ponto/<ident>")
def api_ponto(ident: str):
    try:
        _, linhas, revisoes = V.carregar(paths())
        ponto = V.achar_revisao(revisoes, ident)
    except V.ValidacaoErro as e:
        return _erro(str(e), 404)

    alvo = V.linhas_do_ponto(ponto, linhas)
    video_path = indice().get(ponto["video_file"])
    ordem = [r["id_revisao"] for r in revisoes]
    pos = ordem.index(ponto["id_revisao"])

    return jsonify({
        "ponto": {k: ponto[k] for k in ("id_revisao", "prioridade", "tipo", "status",
                                        "descricao", "video_file", "lance_id",
                                        "decisao", "nota", "revisado_em")},
        "linhas": [_linha_json(l, i) for i, l in enumerate(alvo, 1)],
        "video_disponivel": bool(video_path),
        "video_path": video_path or "",
        "anterior": ordem[pos - 1] if pos > 0 else None,
        "proximo": ordem[pos + 1] if pos < len(ordem) - 1 else None,
        "regioes": [{"code": c, "label": V.REGION_LABELS[c]} for c in V.REGION_LABELS],
        "cameras": sorted(V.VALID_CAMERAS),
    })


@app.get("/api/status")
def api_status():
    _, linhas, revisoes = V.carregar(paths())
    _, descartes = V.ler_csv(paths().descartes)
    log = V.ler_auditoria(paths())
    por_status: dict[str, int] = defaultdict(int)
    for r in revisoes:
        por_status[r["status"]] += 1

    with open(paths().manifest, "r", encoding="utf-8") as f:
        man = json.load(f)
    original_intacto = (os.path.isfile(paths().csv_original)
                        and V.sha256(paths().csv_original) == man["sha256_original"])

    return jsonify({
        "linhas_trabalho": len(linhas),
        "linhas_original": man["n_linhas"],
        "descartadas": len(descartes),
        "edicoes": sum(1 for a in log if a["acao"] in ("editar", "descartar",
                                                       "corrigir-tracos") and not a.get("desfeito")),
        "pode_desfazer": any(not a.get("desfeito") and a["acao"] in ("editar", "descartar")
                             for a in log),
        "por_status": dict(por_status),
        "total": len(revisoes),
        "original_intacto": original_intacto,
        "csv_original": paths().csv_original,
        "pasta_validacao": paths().dir,
    })


# ---------------------------------------------------------------------------
# API - mutacoes (tudo delegado ao validador.py)
# ---------------------------------------------------------------------------
@app.post("/api/editar")
def api_editar():
    d = request.get_json(force=True)
    try:
        mudancas = V.aplicar_edicao(
            paths(), d["id_revisao"], int(d.get("linha", 1)),
            region=d.get("region", "") or "",
            camera=d.get("camera", "") or "",
            gol=d.get("gol") if isinstance(d.get("gol"), bool) else None,
            inicio_frame=d.get("inicio_frame"),
            chute_frame=d.get("chute_frame"),
            fps=float(d.get("fps") or V.FPS_ESPERADO),
            nota=d.get("nota", ""))
    except V.ValidacaoErro as e:
        return _erro(str(e))
    except (KeyError, TypeError, ValueError) as e:
        return _erro(f"requisicao invalida: {e}")
    return jsonify({"ok": True, "mudancas": mudancas})


@app.post("/api/ok")
def api_ok():
    d = request.get_json(force=True)
    try:
        V.marcar_ok(paths(), d["id_revisao"], d.get("nota", ""))
    except V.ValidacaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True})


@app.post("/api/descartar")
def api_descartar():
    d = request.get_json(force=True)
    try:
        rid = V.aplicar_descarte(paths(), d["id_revisao"], int(d.get("linha", 1)),
                                 d.get("motivo", ""))
    except V.ValidacaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True, "row_id": rid})


@app.post("/api/desfazer")
def api_desfazer():
    try:
        reg = V.aplicar_desfazer(paths())
    except V.ValidacaoErro as e:
        return _erro(str(e))
    if reg is None:
        return _erro("nada para desfazer")
    return jsonify({"ok": True, "acao": reg["acao"], "row_id": reg.get("row_id", ""),
                    "id_revisao": reg.get("id_revisao", "")})


@app.post("/api/corrigir-tracos")
def api_corrigir_tracos():
    n = V.aplicar_corrigir_tracos(paths())
    return jsonify({"ok": True, "n": n})


# ---------------------------------------------------------------------------
# Midia: video original (com Range), clip recortado e frame exato
# ---------------------------------------------------------------------------
def _servir_com_range(caminho: str) -> Response:
    """
    Range requests na mao: sem isso o <video> nao consegue buscar no meio de um
    arquivo grande - ele baixaria tudo antes de tocar.
    """
    tamanho = os.path.getsize(caminho)
    faixa = request.headers.get("Range", "")
    tipo = mimetypes.guess_type(caminho)[0] or "video/mp4"

    m = re.match(r"bytes=(\d*)-(\d*)", faixa)
    if not m:
        return send_file(caminho, mimetype=tipo, conditional=True)

    ini = int(m.group(1)) if m.group(1) else 0
    fim = int(m.group(2)) if m.group(2) else min(ini + 4 * 1024 * 1024 - 1, tamanho - 1)
    fim = min(fim, tamanho - 1)
    if ini >= tamanho:
        return Response(status=416, headers={"Content-Range": f"bytes */{tamanho}"})

    with open(caminho, "rb") as f:
        f.seek(ini)
        dados = f.read(fim - ini + 1)

    return Response(dados, status=206, mimetype=tipo, headers={
        "Content-Range": f"bytes {ini}-{fim}/{tamanho}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(len(dados)),
        "Cache-Control": "no-cache",
    })


@app.get("/midia/video")
def midia_video():
    """Video original. So serve o que esta no indice - nome, nunca caminho livre."""
    nome = request.args.get("nome", "")
    caminho = indice().get(nome)
    if not caminho or not os.path.isfile(caminho):
        abort(404)
    return _servir_com_range(caminho)


@app.get("/midia/clip")
def midia_clip():
    """Clip ja recortado. Restrito a pasta de saida, por seguranca."""
    _, linhas, _ = V.carregar(paths())
    rid = request.args.get("row_id", "")
    alvo = next((l for l in linhas if V.row_id(l) == rid), None)
    if not alvo:
        abort(404)
    caminho = os.path.abspath(alvo.get("clip_path", ""))
    if not caminho.startswith(paths().output_base) or not os.path.isfile(caminho):
        abort(404)
    return _servir_com_range(caminho)


_frame_cache: dict[tuple[str, int], bytes] = {}
_frame_lock = threading.Lock()


@app.get("/midia/frame")
def midia_frame():
    """
    Frame exato do video original, em JPEG.

    O <video> do navegador nao garante o frame N (ele busca pelo keyframe mais
    proximo). Para julgar onde a bola entrou, decodificar o frame pedido no
    OpenCV e a unica forma confiavel.
    """
    nome = request.args.get("nome", "")
    try:
        n = int(request.args.get("n", 0))
    except ValueError:
        abort(400)
    caminho = indice().get(nome)
    if not caminho or not os.path.isfile(caminho):
        abort(404)

    chave = (nome, n)
    if chave in _frame_cache:
        return Response(_frame_cache[chave], mimetype="image/jpeg")

    try:
        import cv2
    except ImportError:
        abort(503)

    with _frame_lock:                       # VideoCapture nao e thread-safe
        cap = cv2.VideoCapture(caminho)
        if not cap.isOpened():
            abort(500)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(n, 0))
        ok, img = cap.read()
        cap.release()
    if not ok:
        abort(404)

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        abort(500)
    dados = buf.tobytes()

    if len(_frame_cache) > 400:
        _frame_cache.clear()
    _frame_cache[chave] = dados
    return Response(dados, mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# Pagina
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return Response(PAGINA, mimetype="text/html")


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Validador de Pênaltis</title>
<style>
  :root{
    --bg:#11111b; --painel:#181825; --painel2:#1e1e2e; --borda:#313244;
    --txt:#cdd6f4; --txt2:#9399b2; --azul:#89b4fa; --laranja:#fab387;
    --verde:#a6e3a1; --vermelho:#f38ba8; --amarelo:#f9e2af; --roxo:#cba6f7;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       background:var(--bg);color:var(--txt);height:100vh;overflow:hidden}
  #app{display:grid;grid-template-columns:320px 1fr 340px;height:100vh}
  .col{overflow-y:auto;padding:12px}
  .col+.col{border-left:1px solid var(--borda)}
  h1{font-size:15px;margin:0 0 10px;letter-spacing:.3px}
  h2{font-size:12px;text-transform:uppercase;letter-spacing:.8px;
     color:var(--txt2);margin:16px 0 8px;font-weight:600}
  h2:first-child{margin-top:0}
  select,input,textarea,button{font:inherit;color:var(--txt);
     background:var(--painel2);border:1px solid var(--borda);border-radius:6px;padding:6px 8px}
  button{cursor:pointer;transition:.12s}
  button:hover{border-color:var(--azul)}
  button:disabled{opacity:.35;cursor:not-allowed}
  .filtros{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:10px}
  .filtros select,.filtros input{width:100%;font-size:12px;padding:5px 6px}
  .item{padding:8px 10px;border:1px solid var(--borda);border-radius:8px;
        margin-bottom:6px;cursor:pointer;background:var(--painel)}
  .item:hover{border-color:var(--azul)}
  .item.sel{border-color:var(--azul);background:#1e2438}
  .item.feito{opacity:.45}
  .item .top{display:flex;justify-content:space-between;gap:6px;align-items:center}
  .item .id{font-weight:700;font-size:12px}
  .item .vid{font-size:11px;color:var(--txt2);margin-top:3px;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .tag{font-size:10px;padding:1px 6px;border-radius:20px;font-weight:700;letter-spacing:.3px}
  .CRITICA{background:#4a1f2b;color:var(--vermelho)}
  .ALTA{background:#4a3520;color:var(--laranja)}
  .MEDIA{background:#4a4520;color:var(--amarelo)}
  .BAIXA{background:#2a3550;color:var(--azul)}
  .st{font-size:10px;padding:1px 5px;border-radius:4px;background:var(--painel2);color:var(--txt2)}
  .st.ok{color:var(--verde)} .st.editado{color:var(--azul)} .st.descartado{color:var(--vermelho)}

  #palco{background:#000;border-radius:8px;overflow:hidden;position:relative;
         display:flex;align-items:center;justify-content:center;min-height:300px}
  #palco video,#palco img{max-width:100%;max-height:62vh;display:block}
  .barra{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:10px 0}
  .barra button{padding:5px 10px;font-size:13px}
  .chip{font-size:11px;color:var(--txt2);padding:4px 8px;background:var(--painel);
        border-radius:6px;border:1px solid var(--borda)}
  .desc{background:var(--painel);border-left:3px solid var(--laranja);
        padding:8px 12px;border-radius:0 8px 8px 0;margin-bottom:10px}
  .abas{display:flex;gap:4px;margin-bottom:8px}
  .abas button{font-size:12px;padding:4px 12px;background:transparent;border-color:transparent}
  .abas button.on{background:var(--painel2);border-color:var(--borda)}

  table.ang{width:100%;border-collapse:collapse;font-size:12px;margin-top:8px}
  table.ang th{text-align:left;color:var(--txt2);font-weight:600;
               font-size:10px;text-transform:uppercase;padding:4px 6px}
  table.ang td{padding:5px 6px;border-top:1px solid var(--borda)}
  table.ang tr.sel td{background:#1e2438}
  table.ang tr{cursor:pointer}
  table.ang tr:hover td{background:#1a1a2a}

  .grade{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:6px}
  .grade button{padding:12px 4px;font-size:11px;text-align:center;line-height:1.2}
  .grade button.gol{border-color:#2c3550}
  .grade button.on{background:var(--azul);color:#11111b;border-color:var(--azul);font-weight:700}
  .fora{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
  .fora button{padding:9px 4px;font-size:11px;border-color:#4a3520;color:var(--laranja)}
  .fora button.on{background:var(--laranja);color:#11111b;font-weight:700}
  .dupla{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  .dupla button{padding:9px}
  .dupla button.on-sim{background:var(--verde);color:#11111b;border-color:var(--verde);font-weight:700}
  .dupla button.on-nao{background:var(--vermelho);color:#11111b;border-color:var(--vermelho);font-weight:700}
  .linha-campo{display:grid;grid-template-columns:auto 1fr;gap:6px;align-items:center;margin-bottom:6px}
  .linha-campo label{font-size:11px;color:var(--txt2)}
  .acoes{display:grid;gap:6px;margin-top:14px}
  .acoes button{padding:10px;font-weight:600}
  .b-salvar{background:#1e3a2a;border-color:var(--verde);color:var(--verde)}
  .b-ok{background:#1e2f3a;border-color:var(--azul);color:var(--azul)}
  .b-desc{background:#3a1e28;border-color:var(--vermelho);color:var(--vermelho)}
  #toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
         background:var(--painel2);border:1px solid var(--borda);padding:10px 18px;
         border-radius:8px;opacity:0;transition:.2s;pointer-events:none;z-index:99;max-width:70vw}
  #toast.on{opacity:1}
  #toast.erro{border-color:var(--vermelho);color:var(--vermelho)}
  #toast.bom{border-color:var(--verde);color:var(--verde)}
  .rodape{font-size:11px;color:var(--txt2);margin-top:14px;padding-top:10px;
          border-top:1px solid var(--borda);line-height:1.7}
  kbd{background:var(--painel2);border:1px solid var(--borda);border-radius:4px;
      padding:0 5px;font-size:10px;font-family:inherit}
  .aviso{background:#4a1f2b;color:var(--vermelho);padding:8px;border-radius:6px;
         font-size:11px;margin-bottom:8px}
</style>
</head>
<body>
<div id="app">
  <div class="col">
    <h1>Validador de Pênaltis</h1>
    <div id="barra-status" class="chip" style="display:block;margin-bottom:10px"></div>
    <div class="filtros">
      <select id="f-prio"><option value="">todas prioridades</option>
        <option>CRITICA</option><option>ALTA</option><option>MEDIA</option><option>BAIXA</option></select>
      <select id="f-status">
        <option value="pendente">pendentes</option><option value="todos">todos</option>
        <option value="ok">conferidos</option><option value="editado">editados</option>
        <option value="descartado">descartados</option></select>
      <select id="f-tipo" style="grid-column:1/3"><option value="">todos os tipos</option></select>
      <input id="f-busca" placeholder="filtrar por vídeo..." style="grid-column:1/3">
    </div>
    <div id="lista"></div>
  </div>

  <div class="col">
    <div id="cab"></div>
    <div class="abas">
      <button id="ab-video" class="on" onclick="modo('video')">Vídeo original</button>
      <button id="ab-frame" onclick="modo('frame')">Frame a frame</button>
      <button id="ab-clip" onclick="modo('clip')">Clip recortado</button>
    </div>
    <div id="palco"><div style="color:#585b70;padding:60px">selecione um ponto de atenção</div></div>
    <div class="barra" id="controles"></div>
    <div id="angulos"></div>
  </div>

  <div class="col" id="editor"></div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let fila = [], atual = null, linhaSel = 1, modoAtual = 'video', frameAtual = 0, statusGlobal = {};

function toast(msg, tipo){
  const t = $('#toast'); t.textContent = msg; t.className = 'on ' + (tipo||'');
  clearTimeout(t._h); t._h = setTimeout(()=>t.className='', 2600);
}
async function api(url, opts){
  const r = await fetch(url, opts);
  const j = await r.json().catch(()=>({erro:'resposta invalida'}));
  if(!r.ok) throw new Error(j.erro || ('HTTP '+r.status));
  return j;
}

// ---------- fila ----------
async function carregarFila(){
  const q = new URLSearchParams({prioridade:$('#f-prio').value, status:$('#f-status').value,
                                tipo:$('#f-tipo').value, busca:$('#f-busca').value});
  const d = await api('/api/fila?'+q);
  fila = d.itens;
  if(!$('#f-tipo').dataset.pronto){
    for(const [t,n] of Object.entries(d.tipos)){
      const o = document.createElement('option'); o.value=t; o.textContent=`${t} (${n})`;
      $('#f-tipo').appendChild(o);
    }
    $('#f-tipo').dataset.pronto = '1';
  }
  $('#lista').innerHTML = fila.map(i => `
    <div class="item ${i.status!=='pendente'?'feito':''} ${atual&&atual.ponto.id_revisao===i.id_revisao?'sel':''}"
         onclick="abrir('${i.id_revisao}')">
      <div class="top">
        <span class="id">${i.id_revisao}</span>
        <span class="tag ${i.prioridade}">${i.prioridade}</span>
        <span class="st ${i.status}">${i.status==='pendente'?'':i.status}</span>
      </div>
      <div style="font-size:11px;margin-top:3px">${i.tipo}${i.n_linhas>1?' · '+i.n_linhas+' ângulos':''}</div>
      <div class="vid">${i.video_file}</div>
    </div>`).join('') || '<div style="color:#585b70;padding:20px 4px">nada com esses filtros</div>';
  atualizarStatus();
}

async function atualizarStatus(){
  const s = await api('/api/status'); statusGlobal = s;
  const feitos = s.total - (s.por_status.pendente||0);
  $('#barra-status').innerHTML =
    `<b>${feitos}/${s.total}</b> revisados · ${s.edicoes} alteração(ões) · ` +
    (s.original_intacto ? '<span style="color:var(--verde)">original intacto</span>'
                        : '<span style="color:var(--vermelho)">original mudou!</span>');
}

// ---------- detalhe ----------
async function abrir(id){
  atual = await api('/api/ponto/'+id);
  linhaSel = 1; frameAtual = atual.linhas[0] ? atual.linhas[0].chute_frame : 0;
  const p = atual.ponto;
  $('#cab').innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
      <b style="font-size:16px">${p.id_revisao}</b>
      <span class="tag ${p.prioridade}">${p.prioridade}</span>
      <span class="chip">${p.tipo}</span>
      ${p.status!=='pendente'?`<span class="st ${p.status}">${p.status}</span>`:''}
      <span style="margin-left:auto;display:flex;gap:4px">
        <button onclick="irVizinho('anterior')" ${!atual.anterior?'disabled':''}>&larr;</button>
        <button onclick="irVizinho('proximo')" ${!atual.proximo?'disabled':''}>&rarr;</button>
      </span>
    </div>
    <div class="desc">${p.descricao}</div>
    <div class="chip" style="display:block;margin-bottom:8px">${p.video_file}</div>
    ${!atual.video_disponivel?'<div class="aviso">vídeo original não encontrado — use o clip recortado</div>':''}`;
  document.querySelectorAll('.item').forEach(e=>e.classList.remove('sel'));
  render(); carregarFila();
}
function irVizinho(dir){ const id = atual[dir]; if(id) abrir(id); }

function modo(m){ modoAtual = m;
  ['video','frame','clip'].forEach(x=>$('#ab-'+x).classList.toggle('on', x===m)); render(); }

function linha(){ return atual.linhas[linhaSel-1]; }

function render(){
  if(!atual) return;
  const l = linha();
  const nome = encodeURIComponent(atual.ponto.video_file);

  if(modoAtual === 'frame'){
    $('#palco').innerHTML = `<img id="mid" src="/midia/frame?nome=${nome}&n=${frameAtual}">`;
    $('#controles').innerHTML = `
      <button onclick="passo(-10)">&laquo; 10</button>
      <button onclick="passo(-1)">&lsaquo; 1</button>
      <span class="chip">frame <b id="fnum">${frameAtual}</b> · ${(frameAtual/l.fps).toFixed(2)}s</span>
      <button onclick="passo(1)">1 &rsaquo;</button>
      <button onclick="passo(10)">10 &raquo;</button>
      <button onclick="frameAtual=linha().chute_frame;render()">ir ao chute</button>
      <button onclick="frameAtual=linha().inicio_frame;render()">ir ao início</button>
      <button onclick="usarFrame('chute')" title="define este frame como o do chute">marcar chute aqui</button>
      <button onclick="usarFrame('inicio')">marcar início aqui</button>`;
  } else if(modoAtual === 'clip'){
    $('#palco').innerHTML = l.clip_existe
      ? `<video id="mid" src="/midia/clip?row_id=${encodeURIComponent(l.row_id)}" controls autoplay loop></video>`
      : '<div style="color:var(--vermelho);padding:60px">clip não encontrado no disco</div>';
    $('#controles').innerHTML = `<span class="chip">${l.clip_nome}</span>
      <button onclick="vel(0.25)">0.25x</button><button onclick="vel(0.5)">0.5x</button>
      <button onclick="vel(1)">1x</button>`;
  } else {
    if(!atual.video_disponivel){ $('#palco').innerHTML='<div style="color:#585b70;padding:60px">sem vídeo original</div>';
      $('#controles').innerHTML=''; }
    else {
      $('#palco').innerHTML = `<video id="mid" src="/midia/video?nome=${nome}" controls preload="metadata"></video>`;
      const v = $('#mid');
      v.addEventListener('loadedmetadata', ()=>{ v.currentTime = Math.max(l.inicio_time_s-2,0); }, {once:true});
      $('#controles').innerHTML = `
        <button onclick="irPara(linha().inicio_time_s-2)">início -2s</button>
        <button onclick="irPara(linha().chute_time_s)">momento do chute</button>
        <button onclick="pular(-1)">-1s</button><button onclick="pular(1)">+1s</button>
        <button onclick="vel(0.25)">0.25x</button><button onclick="vel(0.5)">0.5x</button>
        <button onclick="vel(1)">1x</button>
        <span class="chip">início ${l.inicio_time_s}s · chute ${l.chute_time_s}s</span>`;
    }
  }

  $('#angulos').innerHTML = `
    <h2>${atual.linhas.length} rótulo(s) neste lance</h2>
    <table class="ang"><tr><th>#</th><th>pen</th><th>câmera</th><th>região</th>
      <th>gol</th><th>início</th><th>chute</th><th>dur</th><th>fps</th></tr>
    ${atual.linhas.map(a=>`<tr class="${a.n===linhaSel?'sel':''}" onclick="selLinha(${a.n})">
      <td><b>${a.n}</b></td><td>${a.penalty_id}</td><td>${a.camera_type}</td>
      <td>${a.region}</td>
      <td style="color:${a.is_goal?'var(--verde)':'var(--vermelho)'}">${a.is_goal?'SIM':'não'}</td>
      <td>${a.inicio_time_s}s</td><td>${a.chute_time_s}s</td>
      <td>${a.dur_s}s</td><td>${a.fps}</td></tr>`).join('')}</table>`;
  renderEditor();
}

function selLinha(n){ linhaSel = n; frameAtual = linha().chute_frame; render(); }
function irPara(t){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.currentTime=Math.max(t,0); }
function pular(d){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.currentTime=Math.max(v.currentTime+d,0); }
function vel(r){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.playbackRate=r; }
function passo(d){ frameAtual = Math.max(frameAtual+d, 0);
  $('#mid').src = `/midia/frame?nome=${encodeURIComponent(atual.ponto.video_file)}&n=${frameAtual}`;
  $('#fnum').textContent = frameAtual; }
function usarFrame(qual){
  const campo = qual==='chute' ? 'ed-chute' : 'ed-inicio';
  $('#'+campo).value = frameAtual;
  toast(`frame ${frameAtual} anotado como ${qual} — clique em Salvar para gravar`);
}

// ---------- editor ----------
function renderEditor(){
  const l = linha();
  const g = c => atual.regioes.find(r=>r.code===c);
  const bt = c => `<button class="gol ${l.region===c?'on':''}" onclick="setReg('${c}')">${g(c).label.replace('Gol - ','')}</button>`;
  $('#editor').innerHTML = `
    <h2>Editando a linha ${linhaSel} de ${atual.linhas.length}</h2>
    <div class="chip" style="display:block;margin-bottom:10px">${l.camera_type} · pênalti ${l.penalty_id}</div>

    <h2>Região do chute</h2>
    <div class="grade">
      ${bt('gol_topo_esquerdo')}${bt('gol_topo_centro')}${bt('gol_topo_direito')}
      ${bt('gol_meio_esquerdo')}${bt('gol_meio_centro')}${bt('gol_meio_direito')}
      ${bt('gol_baixo_esquerdo')}${bt('gol_baixo_centro')}${bt('gol_baixo_direito')}
    </div>
    <div class="fora">
      ${['fora_esquerda','fora_cima','fora_direita'].map(c=>
        `<button class="${l.region===c?'on':''}" onclick="setReg('${c}')">${g(c).label.replace('Fora - ','Fora ')}</button>`).join('')}
    </div>

    <h2>Foi gol?</h2>
    <div class="dupla">
      <button class="${l.is_goal?'on-sim':''}" onclick="setGol(true)">SIM</button>
      <button class="${!l.is_goal?'on-nao':''}" onclick="setGol(false)">NÃO</button>
    </div>

    <h2>Câmera</h2>
    <select id="ed-camera" style="width:100%">
      ${atual.cameras.map(c=>`<option ${c===l.camera_type?'selected':''}>${c}</option>`).join('')}
    </select>

    <h2>Frames</h2>
    <div class="linha-campo"><label>início</label><input id="ed-inicio" type="number" value="${l.inicio_frame}"></div>
    <div class="linha-campo"><label>chute</label><input id="ed-chute" type="number" value="${l.chute_frame}"></div>
    <div class="linha-campo"><label>fps</label><input id="ed-fps" type="number" step="0.001" value="${l.fps}"></div>

    <h2>Nota</h2>
    <textarea id="ed-nota" rows="2" style="width:100%" placeholder="o que você viu no vídeo...">${atual.ponto.nota||''}</textarea>

    <div class="acoes">
      <button class="b-salvar" onclick="salvar()">Salvar correção <kbd>S</kbd></button>
      <button class="b-ok" onclick="aprovar()">Está correto, seguir <kbd>A</kbd></button>
      <button class="b-desc" onclick="descartar()">Descartar esta linha</button>
      <button onclick="desfazer()" ${statusGlobal.pode_desfazer?'':'disabled'}>Desfazer última alteração</button>
    </div>

    <div class="rodape">
      <kbd>←</kbd><kbd>→</kbd> ponto anterior/próximo · <kbd>1</kbd>..<kbd>9</kbd> escolhe o ângulo<br>
      <kbd>A</kbd> aprovar · <kbd>S</kbd> salvar · <kbd>Z</kbd> desfazer · <kbd>F</kbd> frame a frame<br>
      <kbd>,</kbd><kbd>.</kbd> frame anterior/próximo (no modo frame)<br><br>
      Tudo grava em <b>labels_validado.csv</b>. O original nunca é tocado.
    </div>`;
  window._regSel = l.region; window._golSel = l.is_goal;
}
function setReg(c){ window._regSel = c;
  document.querySelectorAll('.grade button,.fora button').forEach(b=>b.classList.remove('on'));
  event.target.classList.add('on');
  if(c.startsWith('fora')) setGolVisual(false); }
function setGol(v){ window._golSel = v; setGolVisual(v); }
function setGolVisual(v){
  const bs = document.querySelectorAll('.dupla button');
  bs[0].className = v?'on-sim':''; bs[1].className = v?'':'on-nao'; window._golSel = v; }

async function salvar(){
  const l = linha();
  try{
    const r = await api('/api/editar', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id_revisao: atual.ponto.id_revisao, linha: linhaSel,
        region: window._regSel, camera: $('#ed-camera').value, gol: window._golSel,
        inicio_frame: parseInt($('#ed-inicio').value), chute_frame: parseInt($('#ed-chute').value),
        fps: parseFloat($('#ed-fps').value), nota: $('#ed-nota').value})});
    toast('salvo: ' + r.mudancas.join(' · '), 'bom');
    await abrir(atual.ponto.id_revisao);
  }catch(e){ toast(e.message, 'erro'); }
}
async function aprovar(){
  try{
    await api('/api/ok', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id_revisao: atual.ponto.id_revisao, nota: $('#ed-nota').value})});
    toast('conferido, sem alteração', 'bom');
    const prox = atual.proximo; await carregarFila(); if(prox) abrir(prox);
  }catch(e){ toast(e.message, 'erro'); }
}
async function descartar(){
  const motivo = prompt('Por que descartar a linha '+linhaSel+'?\n(ela vai para descartes.csv, dá para desfazer)');
  if(!motivo) return;
  try{
    await api('/api/descartar', {method:'POST',headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id_revisao: atual.ponto.id_revisao, linha: linhaSel, motivo})});
    toast('linha descartada', 'bom'); await abrir(atual.ponto.id_revisao);
  }catch(e){ toast(e.message, 'erro'); }
}
async function desfazer(){
  try{ const r = await api('/api/desfazer', {method:'POST'});
    toast('desfeito: '+r.acao+' em '+r.row_id, 'bom');
    if(atual) await abrir(atual.ponto.id_revisao); else carregarFila();
  }catch(e){ toast(e.message, 'erro'); }
}

// ---------- teclado ----------
document.addEventListener('keydown', e=>{
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  if(!atual) return;
  const k = e.key.toLowerCase();
  if(k==='arrowleft')  { e.preventDefault(); irVizinho('anterior'); }
  if(k==='arrowright') { e.preventDefault(); irVizinho('proximo'); }
  if(k==='a') aprovar();
  if(k==='s') salvar();
  if(k==='z') desfazer();
  if(k==='f') modo(modoAtual==='frame'?'video':'frame');
  if(k===',' && modoAtual==='frame') passo(-1);
  if(k==='.' && modoAtual==='frame') passo(1);
  if(/^[1-9]$/.test(k) && +k <= atual.linhas.length) selLinha(+k);
});

['f-prio','f-status','f-tipo'].forEach(id=>$('#'+id).onchange = carregarFila);
$('#f-busca').oninput = () => { clearTimeout(window._b); window._b = setTimeout(carregarFila, 300); };
carregarFila().then(()=>{ if(fila.length) abrir(fila[0].id_revisao); });
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def servir(p: V.Paths, source_dir: str, host: str = "127.0.0.1",
           porta: int = 5005, abrir_navegador: bool = True) -> int:
    if not os.path.isfile(p.trabalho):
        print("Validacao nao iniciada. Rode antes:  python validador.py init")
        return 1

    _ctx["paths"] = p
    _ctx["source_dir"] = source_dir
    _ctx["indice"] = V.indexar_videos(p, source_dir)

    _, _, revisoes = V.carregar(p)
    pend = sum(1 for r in revisoes if r["status"] == "pendente")
    url = f"http://{host}:{porta}"

    print(f"\n  Validador de Penaltis  ->  {url}")
    print(f"  {pend} pendente(s) de {len(revisoes)} ponto(s) de atencao")
    print(f"  editando: {p.trabalho}")
    print(f"  original: {p.csv_original}  (nunca e escrito)\n")

    if abrir_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=porta, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Interface web de validacao (Flask)")
    ap.add_argument("--output-base", default=V.DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=V.DEFAULT_SOURCE_DIR)
    ap.add_argument("--porta", type=int, default=5005)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    raise SystemExit(servir(V.Paths(a.output_base), a.source_dir, a.host, a.porta,
                            not a.sem_navegador))
