"""
revisor_web.py
==============
Interface de REVISAO COMPLETA no navegador (Flask).

Mostra todos os rotulos da base contra o VIDEO ORIGINAL, com filtro por tipo
de chute (fora cima / fora direita / fora esquerda / cada canto do gol), para
conferir um a um e corrigir o que estiver errado.

    python run.py                    # o lancador do projeto: sobe tudo e abre aqui
    python revisao.py web            # so esta tela, em http://127.0.0.1:5007
    python revisor_web.py            # equivalente

Toda leitura e escrita passa pelo revisao.py - inclusive o _guard() que impede
escrita nos labels.csv originais. Este arquivo e so a camada de tela: regra de
negocio muda la, a web acompanha.

O que da para fazer
-------------------
    - filtrar por regiao (fora_cima, fora_direita, fora_esquerda, ...),
      status, competicao, camera, gol/nao-gol e nome do video
    - assistir ao lance no video original, no instante do chute, em camera lenta
    - andar frame a frame (frames exatos, decodificados pelo OpenCV)
    - ver a tira de frames em volta do chute (onde a bola foi, de relance)
    - comparar com o clip recortado e com os PNGs exportados
    - clicar na grade do gol para corrigir a regiao, trocar camera, marcar
      gol/nao-gol, reposicionar inicio/chute, anotar observacao
    - aprovar sem alteracao, descartar rotulo ruim, desfazer, exportar

Nada disso toca o dado original: tudo vai para
<output_base>/_revisao_manual/revisao.csv (a camada de correcoes) e os CSVs
corrigidos saem em _revisao_manual/export/ quando voce clicar em Exportar.
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import threading
import webbrowser
from typing import Any, Optional

from flask import Flask, Response, abort, jsonify, request, send_file

import navbar
import revisao as R

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_ctx: dict[str, Any] = {}          # paths + source_dir + indice de videos


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def paths() -> R.Paths:
    return _ctx["paths"]


def indice() -> dict[str, str]:
    return _ctx["indice"]


def _erro(msg: str, code: int = 400):
    return jsonify({"erro": msg}), code


def _item(r: dict) -> dict:
    """Versao enxuta de um rotulo, para a lista da esquerda."""
    a = r["atual"]
    return {
        "uid":          r["uid"],
        "video_file":   a.get("video_file", ""),
        "penalty_id":   a.get("penalty_id", ""),
        "competicao":   r["competicao"],
        "region":       a.get("region", ""),
        "region_label": a.get("region_label", ""),
        "camera_type":  a.get("camera_type", ""),
        "is_goal":      R.is_goal(a),
        "chute_time_s": round(R.num(a, "chute_time_s"), 2),
        "status":       r["status"],
        "alterado":     bool(r["alteracoes"]),
        "revisado_em":  r["revisado_em"],
    }


def _detalhe(r: dict, base: R.Base) -> dict:
    a, o = r["atual"], r["original"]
    fps = R.fps_da_linha(a)
    video_path = indice().get(a.get("video_file", ""))
    t_chute = R.num(a, "chute_time_s")

    # Outros rotulos do mesmo video proximos no tempo: quase sempre replays do
    # MESMO penalti em outro angulo. Divergencia entre eles e erro de rotulagem.
    irmaos = [
        {"uid": x["uid"], "region": x["atual"].get("region", ""),
         "camera_type": x["atual"].get("camera_type", ""),
         "is_goal": R.is_goal(x["atual"]),
         "chute_time_s": round(R.num(x["atual"], "chute_time_s"), 2),
         "status": x["status"]}
        for x in base.registros
        if x["atual"].get("video_file") == a.get("video_file")
        and x["uid"] != r["uid"]
        and abs(R.num(x["atual"], "chute_time_s") - t_chute) <= R.FPS_ESPERADO
    ]

    return {
        "reg": {
            **_item(r),
            "competicao_label": r["competicao_label"],
            "inicio_frame":  int(R.num(a, "inicio_frame")),
            "chute_frame":   int(R.num(a, "chute_frame")),
            "inicio_time_s": round(R.num(a, "inicio_time_s"), 3),
            "chute_time_s":  round(t_chute, 3),
            "dur_s":         round(R.duracao_s(a), 3),
            "fps":           round(fps, 3),
            "observations":  a.get("observations", ""),
            "nota":          r["nota"],
            "motivo":        r["motivo"],
            "alteracoes":    r["alteracoes"],
            "revisado_em":   r["revisado_em"],
            "csv":           r["csv"],
            "linha_csv":     r["n_csv"],
        },
        "original": {
            "region":       o.get("region", ""),
            "region_label": o.get("region_label", ""),
            "camera_type":  o.get("camera_type", ""),
            "is_goal":      R.is_goal(o),
            "inicio_frame": int(R.num(o, "inicio_frame")),
            "chute_frame":  int(R.num(o, "chute_frame")),
        },
        "video_disponivel": bool(video_path),
        "clip_existe":      os.path.isfile(a.get("clip_path", "")),
        "png_inicio":       os.path.isfile(a.get("frame_inicio_path", "")),
        "png_chute":        os.path.isfile(a.get("frame_chute_path", "")),
        "irmaos":           sorted(irmaos, key=lambda x: x["chute_time_s"]),
        "regioes":          [{"code": c, "label": R.REGION_LABELS[c]} for c in R.REGION_LABELS],
        "cameras":          sorted(R.VALID_CAMERAS),
    }


# ---------------------------------------------------------------------------
# API - leitura
# ---------------------------------------------------------------------------
@app.get("/api/base")
def api_base():
    base = R.carregar(paths())
    sel = R.filtrar(
        base.registros,
        regiao=request.args.get("regiao", ""),
        grupo=request.args.get("grupo", ""),
        status=request.args.get("status", "todos"),
        competicao=request.args.get("competicao", ""),
        camera=request.args.get("camera", ""),
        gol=request.args.get("gol", ""),
        busca=request.args.get("busca", ""),
    )
    return jsonify({
        "itens":     [_item(r) for r in sel],
        "total":     len(base.registros),
        "filtrados": len(sel),
        "resumo":    R.resumo(base.registros),
        "resumo_filtro": R.resumo(sel),
    })


@app.get("/api/registro/<path:uid>")
def api_registro(uid: str):
    base = R.carregar(paths())
    try:
        return jsonify(_detalhe(base.get(uid), base))
    except R.RevisaoErro as e:
        return _erro(str(e), 404)


@app.get("/api/status")
def api_status():
    base = R.carregar(paths())
    r = R.resumo(base.registros)
    log = R.ler_auditoria(paths())
    return jsonify({
        "total":      r["total"],
        "por_status": r["status"],
        "revisados":  r["total"] - r["status"].get("pendente", 0),
        "edicoes":    sum(1 for a in log
                          if a["acao"] in ("editar", "descartar") and not a.get("desfeito")),
        "pode_desfazer": any(not a.get("desfeito")
                             and a["acao"] in ("editar", "aprovar", "descartar", "reverter")
                             for a in log),
        "originais":  R.conferir_originais(paths()),
        "pasta_revisao": paths().dir,
        "overlay":    paths().overlay,
    })


# ---------------------------------------------------------------------------
# API - mutacoes (tudo delegado ao revisao.py)
# ---------------------------------------------------------------------------
@app.post("/api/editar")
def api_editar():
    d = request.get_json(force=True)
    try:
        mudancas = R.editar(
            paths(), d["uid"],
            region=d.get("region") or None,
            camera=d.get("camera") or None,
            gol=d.get("gol") if isinstance(d.get("gol"), bool) else None,
            inicio_frame=d.get("inicio_frame"),
            chute_frame=d.get("chute_frame"),
            fps=float(d["fps"]) if d.get("fps") else None,
            observations=d.get("observations"),
            nota=d.get("nota", ""))
    except R.RevisaoErro as e:
        return _erro(str(e))
    except (KeyError, TypeError, ValueError) as e:
        return _erro(f"requisicao invalida: {e}")
    return jsonify({"ok": True, "mudancas": mudancas})


@app.post("/api/aprovar")
def api_aprovar():
    d = request.get_json(force=True)
    try:
        R.aprovar(paths(), d["uid"], d.get("nota", ""))
    except R.RevisaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True})


@app.post("/api/descartar")
def api_descartar():
    d = request.get_json(force=True)
    try:
        R.descartar(paths(), d["uid"], d.get("motivo", ""))
    except R.RevisaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True})


@app.post("/api/reverter")
def api_reverter():
    d = request.get_json(force=True)
    try:
        R.reverter(paths(), d["uid"])
    except R.RevisaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True})


@app.post("/api/desfazer")
def api_desfazer():
    reg = R.desfazer(paths())
    if reg is None:
        return _erro("nada para desfazer")
    return jsonify({"ok": True, "acao": reg["acao"], "uid": reg.get("uid", "")})


@app.post("/api/exportar")
def api_exportar():
    res = R.exportar(paths())
    return jsonify({"ok": True, "pasta": res["pasta"], "linhas": res["total_linhas"],
                    "descartados": res["total_descartados"],
                    "arquivos": [os.path.basename(a["arquivo"]) for a in res["arquivos"]]})


# ---------------------------------------------------------------------------
# Midia: video original (com Range), frame exato, tira de frames, clip e PNGs
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


def _caminho_do_video(nome: str) -> str:
    """So serve o que esta no indice - nome de arquivo, nunca caminho livre."""
    caminho = indice().get(nome)
    if not caminho or not os.path.isfile(caminho):
        abort(404)
    return caminho


@app.get("/midia/video")
def midia_video():
    return _servir_com_range(_caminho_do_video(request.args.get("nome", "")))


_frame_cache: dict[tuple, bytes] = {}
_tira_cache: dict[tuple, list] = {}
_lock = threading.Lock()           # VideoCapture nao e thread-safe


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        abort(503)


def _jpeg(cv2, img, largura: Optional[int] = None, q: int = 88) -> bytes:
    if largura and img.shape[1] > largura:
        h = int(img.shape[0] * largura / img.shape[1])
        img = cv2.resize(img, (largura, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        abort(500)
    return buf.tobytes()


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
        n = max(int(request.args.get("n", 0)), 0)
    except ValueError:
        abort(400)
    caminho = _caminho_do_video(nome)

    chave = (nome, n)
    if chave in _frame_cache:
        return Response(_frame_cache[chave], mimetype="image/jpeg")

    cv2 = _cv2()
    with _lock:
        cap = cv2.VideoCapture(caminho)
        if not cap.isOpened():
            abort(500)
        cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ok, img = cap.read()
        cap.release()
    if not ok:
        abort(404)

    dados = _jpeg(cv2, img)
    if len(_frame_cache) > 400:
        _frame_cache.clear()
    _frame_cache[chave] = dados
    return Response(dados, mimetype="image/jpeg")


@app.get("/midia/tira")
def midia_tira():
    """
    Tira de frames em volta do chute, numa unica requisicao.

    Abrir um VideoCapture por miniatura seria lento demais (seek + decode a
    cada chamada); aqui a captura abre uma vez, le sequencialmente e devolve
    todas as miniaturas juntas. E a leitura que mais resolve na validacao:
    da para ver a bola saindo do pe e onde ela termina.
    """
    nome = request.args.get("nome", "")
    try:
        centro = max(int(request.args.get("centro", 0)), 0)
        antes  = min(max(int(request.args.get("antes", 4)), 0), 30)
        depois = min(max(int(request.args.get("depois", 14)), 0), 60)
        passo  = min(max(int(request.args.get("passo", 2)), 1), 30)
        fps    = float(request.args.get("fps", R.FPS_ESPERADO)) or R.FPS_ESPERADO
    except ValueError:
        abort(400)
    caminho = _caminho_do_video(nome)

    chave = (nome, centro, antes, depois, passo)
    if chave in _tira_cache:
        return jsonify({"frames": _tira_cache[chave]})

    cv2 = _cv2()
    ini = max(centro - antes * passo, 0)
    fim = centro + depois * passo
    quero = list(range(ini, fim + 1, passo))

    with _lock:
        cap = cv2.VideoCapture(caminho)
        if not cap.isOpened():
            abort(500)
        cap.set(cv2.CAP_PROP_POS_FRAMES, ini)
        frames, n = [], ini
        alvo = set(quero)
        while n <= fim:
            ok, img = cap.read()
            if not ok:
                break
            if n in alvo:
                frames.append({
                    "n": n,
                    "t": round(n / fps, 2),
                    "chute": n == centro,
                    "img": "data:image/jpeg;base64," +
                           base64.b64encode(_jpeg(cv2, img, largura=260, q=72)).decode(),
                })
            n += 1
        cap.release()

    if len(_tira_cache) > 40:
        _tira_cache.clear()
    _tira_cache[chave] = frames
    return jsonify({"frames": frames})


def _arquivo_do_registro(uid: str, campo: str) -> str:
    """Caminho gravado no rotulo, restrito a pasta de saida por seguranca."""
    base = R.carregar(paths())
    try:
        reg = base.get(uid)
    except R.RevisaoErro:
        abort(404)
    caminho = os.path.abspath(reg["atual"].get(campo, ""))
    if not caminho.startswith(paths().output_base) or not os.path.isfile(caminho):
        abort(404)
    return caminho


@app.get("/midia/clip")
def midia_clip():
    return _servir_com_range(_arquivo_do_registro(request.args.get("uid", ""), "clip_path"))


@app.get("/midia/png")
def midia_png():
    qual = request.args.get("qual", "chute")
    campo = "frame_inicio_path" if qual == "inicio" else "frame_chute_path"
    return send_file(_arquivo_do_registro(request.args.get("uid", ""), campo),
                     mimetype="image/png")


# ---------------------------------------------------------------------------
# Pagina
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    # a barra comum entra aqui: a pagina fica servivel sozinha, e o run.py
    # so precisa definir as portas no ambiente para os links casarem
    return Response(PAGINA.replace("<!--NAV-->", navbar.barra("revisor")),
                    mimetype="text/html")


PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Revisor de Pênaltis</title>
<style>
  :root{
    --bg:#11111b; --painel:#181825; --painel2:#1e1e2e; --borda:#313244;
    --txt:#cdd6f4; --txt2:#9399b2; --azul:#89b4fa; --laranja:#fab387;
    --verde:#a6e3a1; --vermelho:#f38ba8; --amarelo:#f9e2af; --roxo:#cba6f7;
  }
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
       background:var(--bg);color:var(--txt);height:100vh;overflow:hidden}
    /* desconta a barra de navegacao do topo (navbar.py publica --nav-h) */
  #app{display:grid;grid-template-columns:330px 1fr 330px;
       height:calc(100vh - var(--nav-h,0px))}
  .col{overflow-y:auto;padding:12px}
  .col+.col{border-left:1px solid var(--borda)}
  h1{font-size:15px;margin:0 0 8px;letter-spacing:.3px}
  h2{font-size:11px;text-transform:uppercase;letter-spacing:.8px;
     color:var(--txt2);margin:14px 0 6px;font-weight:600}
  h2:first-child{margin-top:0}
  select,input,textarea,button{font:inherit;color:var(--txt);
     background:var(--painel2);border:1px solid var(--borda);border-radius:6px;padding:6px 8px}
  button{cursor:pointer;transition:.12s}
  button:hover{border-color:var(--azul)}
  button:disabled{opacity:.4;cursor:not-allowed}
  .chip{display:inline-block;background:var(--painel2);border:1px solid var(--borda);
        border-radius:999px;padding:1px 9px;font-size:11px;color:var(--txt2)}

  /* ---- filtros ---- */
  .filtros{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px}
  .filtros select,.filtros input{width:100%;font-size:12px;padding:5px 6px}
  .atalhos{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px}
  .atalhos button{font-size:11px;padding:4px 8px;border-radius:999px}
  .atalhos button.on{background:var(--laranja);color:#11111b;border-color:var(--laranja);font-weight:700}
  .barra-prog{height:6px;background:var(--painel2);border-radius:99px;overflow:hidden;margin:6px 0 10px}
  .barra-prog div{height:100%;background:var(--verde);transition:.3s}

  /* ---- lista ---- */
  .item{background:var(--painel);border:1px solid var(--borda);border-left-width:3px;
        border-radius:6px;padding:6px 8px;margin-bottom:5px;cursor:pointer;font-size:12px}
  .item:hover{border-color:var(--azul)}
  .item.sel{border-color:var(--azul);background:#1c2233}
  .item.ok{border-left-color:var(--verde)}
  .item.corrigido{border-left-color:var(--amarelo)}
  .item.descartado{border-left-color:var(--vermelho);opacity:.55}
  .item.pendente{border-left-color:var(--borda)}
  /* ja revisado: fundo mais claro e texto discreto - a lista mostra o
     progresso de relance, sem precisar ler item por item */
  .item.feito{background:#14161f}
  .item.feito .vid,.item.feito .reg{opacity:.75}
  .item .top{display:flex;gap:6px;align-items:center}
  .selo{font-size:11px;font-weight:700;white-space:nowrap;letter-spacing:.2px}
  .legenda{font-size:11px;color:var(--txt2);margin:0 0 8px;display:flex;gap:9px;flex-wrap:wrap}
  .item .reg{color:var(--laranja);font-weight:600}
  .item .reg.gol{color:var(--azul)}
  .item .vid{color:var(--txt2);font-size:11px;white-space:nowrap;overflow:hidden;
             text-overflow:ellipsis;margin-top:2px}

  /* ---- palco ---- */
  .abas{display:flex;gap:4px;margin-bottom:8px}
  .abas button{font-size:12px;padding:5px 10px}
  .abas button.on{background:var(--azul);color:#11111b;border-color:var(--azul);font-weight:600}
  #palco{position:relative;background:#000;border:1px solid var(--borda);border-radius:8px;
         overflow:hidden;display:flex;align-items:center;justify-content:center;
         min-height:300px;max-height:56vh}

  /* ---- régua das traves: sobreposição de conferência, nunca gravada ---- */
  #regua{position:absolute;pointer-events:none;z-index:3}
  #regua.calibrando{pointer-events:auto;cursor:crosshair}
  #regua .quad{fill:none;stroke:var(--laranja);stroke-width:2}
  #regua .div{stroke:var(--laranja);stroke-width:1.5;stroke-opacity:.7}
  #regua .marcada{fill:var(--azul);fill-opacity:.22;stroke:var(--azul);stroke-width:1.5}
  /* contorno no próprio texto: legível tanto no gramado quanto na rede */
  #regua text{font:600 10px system-ui,sans-serif;text-anchor:middle;fill:#fff;
              paint-order:stroke;stroke:#000;stroke-width:2.6;stroke-opacity:.6}
  #regua text.fora{fill:var(--laranja);font-weight:700}
  #regua .alca{fill:var(--laranja);stroke:#11111b;stroke-width:2;cursor:grab;
               pointer-events:auto}
  #regua .alca:hover{fill:#fff}
  #regua .pino{fill:var(--laranja);stroke:#11111b;stroke-width:2}
  #regua-info{position:absolute;left:8px;top:8px;z-index:4;pointer-events:none;
              background:rgba(17,17,27,.88);border:1px solid var(--borda);border-radius:7px;
              padding:5px 9px;font-size:11px;color:var(--txt);max-width:70%}
  #regua-info b{color:var(--laranja)}
  #palco video,#palco img{max-width:100%;max-height:56vh;display:block}
  .barra{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:8px 0}
  .barra button{font-size:12px;padding:5px 9px}
  .tira{display:flex;gap:4px;overflow-x:auto;padding-bottom:6px}
  .tira figure{margin:0;flex:0 0 auto;cursor:pointer;border:2px solid transparent;border-radius:5px}
  .tira figure.chute{border-color:var(--laranja)}
  .tira figure.sel{border-color:var(--azul)}
  .tira img{width:150px;display:block;border-radius:3px}
  .tira figcaption{font-size:10px;color:var(--txt2);text-align:center}
  table.ang{width:100%;border-collapse:collapse;font-size:11px;margin-top:4px}
  table.ang th{color:var(--txt2);text-align:left;font-weight:600;padding:3px 5px}
  table.ang td{padding:3px 5px;border-top:1px solid var(--borda);cursor:pointer}
  table.ang tr:hover td{background:var(--painel)}

  /* ---- editor ---- */
  .grade{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
  .grade button{padding:11px 4px;font-size:11px}
  .grade button.on{background:var(--azul);color:#11111b;font-weight:700;border-color:var(--azul)}
  .fora{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-top:5px}
  .fora button{padding:9px 4px;font-size:11px;border-color:#4a3520;color:var(--laranja)}
  .fora button.on{background:var(--laranja);color:#11111b;font-weight:700}
  .dupla{display:grid;grid-template-columns:1fr 1fr;gap:5px}
  .dupla button.on-sim{background:var(--verde);color:#11111b;font-weight:700;border-color:var(--verde)}
  .dupla button.on-nao{background:var(--vermelho);color:#11111b;font-weight:700;border-color:var(--vermelho)}
  .linha-campo{display:grid;grid-template-columns:70px 1fr;gap:6px;align-items:center;margin-bottom:5px}
  .linha-campo label{font-size:11px;color:var(--txt2)}
  .linha-campo input{width:100%}
  .acoes{display:grid;gap:5px;margin-top:14px}
  .acoes button{padding:9px}
  .b-ok{background:#1e3a2a;border-color:var(--verde);color:var(--verde);font-weight:600}
  .b-salvar{background:#1e2a3a;border-color:var(--azul);color:var(--azul);font-weight:600}
  .b-desc{border-color:#4a1f2b;color:var(--vermelho)}
  .diff{font-size:11px;color:var(--amarelo);background:#2a2416;border:1px solid #4a4020;
        border-radius:6px;padding:6px 8px;margin-bottom:8px;line-height:1.5}
  .aviso{background:#4a1f2b;color:var(--vermelho);padding:8px;border-radius:6px;
         font-size:11px;margin-bottom:8px}
  .rodape{font-size:11px;color:var(--txt2);margin-top:14px;padding-top:10px;
          border-top:1px solid var(--borda);line-height:1.8}
  kbd{background:var(--painel2);border:1px solid var(--borda);border-radius:4px;
      padding:0 5px;font-size:10px;font-family:inherit}
  #toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);
         background:var(--painel2);border:1px solid var(--borda);padding:10px 18px;
         border-radius:8px;opacity:0;transition:.2s;pointer-events:none;z-index:99;max-width:70vw}
  #toast.on{opacity:1}
  #toast.erro{border-color:var(--vermelho);color:var(--vermelho)}
  #toast.bom{border-color:var(--verde);color:var(--verde)}
</style>
</head>
<body>
<!--NAV-->
<div id="app">
  <!-- ================= coluna 1: filtros + lista ================= -->
  <div class="col">
    <h1>Revisor de Pênaltis</h1>
    <div id="prog-txt" class="chip" style="display:block"></div>
    <div class="barra-prog"><div id="prog-bar" style="width:0"></div></div>

    <div class="atalhos" id="atalhos"></div>
    <div class="filtros">
      <select id="f-status">
        <option value="todos">todos os status</option>
        <option value="pendente">pendentes</option>
        <option value="ok">conferidos</option>
        <option value="corrigido">corrigidos</option>
        <option value="descartado">descartados</option>
      </select>
      <select id="f-comp"><option value="">todas competições</option></select>
      <select id="f-regiao"><option value="">todas as regiões</option></select>
      <select id="f-gol">
        <option value="">gol e não-gol</option>
        <option value="sim">só gol</option>
        <option value="nao">só não-gol</option>
      </select>
      <select id="f-camera" style="grid-column:1/3"><option value="">todas as câmeras</option></select>
      <input id="f-busca" placeholder="filtrar por vídeo..." style="grid-column:1/3">
    </div>
    <div id="cont-lista" class="chip" style="display:block;margin-bottom:6px"></div>
    <div class="legenda" id="legenda"></div>
    <div id="lista"></div>
    <div style="text-align:center;margin:8px 0">
      <button id="mais" onclick="limite+=300;renderLista()" style="display:none">mostrar mais</button>
    </div>
  </div>

  <!-- ================= coluna 2: vídeo ================= -->
  <div class="col">
    <div id="cab"></div>
    <div class="abas">
      <button id="ab-video" class="on" onclick="modo('video')">Vídeo original</button>
      <button id="ab-frame" onclick="modo('frame')">Frame a frame</button>
      <button id="ab-clip" onclick="modo('clip')">Clip recortado</button>
      <button id="ab-png" onclick="modo('png')">PNGs exportados</button>
    </div>
    <div id="palco"><div style="color:#585b70;padding:60px">selecione um rótulo à esquerda</div></div>
    <div class="barra" id="controles"></div>
    <h2 id="tira-tit" style="display:none">Tira de frames em volta do chute</h2>
    <div class="tira" id="tira"></div>
    <div id="irmaos"></div>
  </div>

  <!-- ================= coluna 3: editor ================= -->
  <div class="col" id="editor">
    <div style="color:#585b70;padding:20px 0">nada selecionado</div>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const FORA = ['fora_esquerda','fora_cima','fora_direita'];
let itens = [], atual = null, modoAtual = 'video', frameAtual = 0, limite = 300;
let statusGlobal = {}, filtroRegiao = '', sel = {region:'', gol:false};
let tiraCfg = {antes:4, depois:14, passo:2};

function toast(msg, tipo){
  const t = $('#toast'); t.textContent = msg; t.className = 'on ' + (tipo||'');
  clearTimeout(t._h); t._h = setTimeout(()=>t.className='', 2800);
}
async function api(url, opts){
  const r = await fetch(url, opts);
  const j = await r.json().catch(()=>({erro:'resposta inválida'}));
  if(!r.ok) throw new Error(j.erro || ('HTTP '+r.status));
  return j;
}
const esc = s => String(s??'').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// ---------- selo de revisão ----------
// Um rótulo já conferido tem que se anunciar sozinho: ícone + cor, no mesmo
// lugar em toda a tela (lista, cabeçalho e tabela de ângulos).
const SELO = {
  pendente:   {ic:'○', txt:'a conferir',cor:'var(--txt2)'},
  ok:         {ic:'✓', txt:'conferido', cor:'var(--verde)'},
  corrigido:  {ic:'✎', txt:'corrigido', cor:'var(--amarelo)'},
  descartado: {ic:'✕', txt:'descartado',cor:'var(--vermelho)'},
};
function selo(status, comTexto, quando){
  const s = SELO[status] || SELO.pendente;
  const t = quando ? ` title="revisado em ${esc(quando)}"` : '';
  return `<span class="selo" style="color:${s.cor}"${t}>${s.ic}${comTexto?' '+s.txt:''}</span>`;
}
const revisado = st => st && st !== 'pendente';

// ---------- filtros rápidos por tipo de chute ----------
function renderAtalhos(res){
  const r = res.regiao || {};
  const n = c => r[c] || 0;
  const chips = [
    ['', 'tudo', res.total],
    ['fora_cima', 'fora cima', n('fora_cima')],
    ['fora_direita', 'fora direita', n('fora_direita')],
    ['fora_esquerda', 'fora esquerda', n('fora_esquerda')],
    [FORA.join(','), 'fora (todas)', FORA.reduce((s,c)=>s+n(c),0)],
    ['__gol__', 'gol (todas)', (res.grupo||{}).gol||0],
  ];
  $('#atalhos').innerHTML = chips.map(([v,rot,q])=>
    `<button class="${filtroRegiao===v?'on':''}" onclick="setRegiaoFiltro('${v}')">${rot} <b>${q}</b></button>`
  ).join('');
}
function setRegiaoFiltro(v){ filtroRegiao = v; $('#f-regiao').value = ''; carregar(); }

// ---------- lista ----------
async function carregar(manterSel){
  const q = new URLSearchParams({
    regiao: filtroRegiao === '__gol__' ? '' : (filtroRegiao || $('#f-regiao').value),
    grupo:  filtroRegiao === '__gol__' ? 'gol' : '',
    status: $('#f-status').value, competicao: $('#f-comp').value,
    camera: $('#f-camera').value, gol: $('#f-gol').value, busca: $('#f-busca').value});
  const d = await api('/api/base?'+q);
  itens = d.itens;
  renderAtalhos(d.resumo);
  preencherSelects(d.resumo);
  // a legenda vira o placar do recorte atual: quantos ja levaram cada selo
  const st = d.resumo_filtro.status || {};
  $('#cont-lista').innerHTML = `<b>${d.filtrados}</b> de ${d.total} rótulos`;
  $('#legenda').innerHTML = Object.keys(SELO).map(k =>
    `<span style="opacity:${st[k]?1:.45}">${selo(k, true)} <b>${st[k]||0}</b></span>`).join('');
  limite = Math.max(limite, 300);
  renderLista();
  await atualizarStatus();
  if(!manterSel && itens.length && (!atual || !itens.some(i=>i.uid===atual.reg.uid)))
    abrir(itens[0].uid);
}
function preencherSelects(res){
  const mk = (id, mapa, rotulo) => {
    const s = $('#'+id); if(s.dataset.pronto) return; s.dataset.pronto = '1';
    for(const [k,v] of Object.entries(mapa)){
      const o = document.createElement('option');
      o.value = k; o.textContent = `${rotulo?rotulo(k):k} (${v})`; s.appendChild(o);
    }
  };
  mk('f-comp', res.competicao); mk('f-regiao', res.regiao); mk('f-camera', res.camera);
}
function renderLista(){
  const vis = itens.slice(0, limite);
  // uid vai em data-uid (nome de video com aspas quebraria um onclick inline);
  // o clique e capturado por delegacao la embaixo.
  $('#lista').innerHTML = vis.map(i => `
    <div class="item ${i.status} ${revisado(i.status)?'feito':''} ${atual&&atual.reg.uid===i.uid?'sel':''}"
         data-uid="${esc(i.uid)}">
      <div class="top">
        ${selo(i.status, false, i.revisado_em)}
        <span class="reg ${i.region.startsWith('gol')?'gol':''}">${esc(i.region)}</span>
        <span style="color:${i.is_goal?'var(--verde)':'var(--txt2)'}">${i.is_goal?'GOL':'—'}</span>
        <span style="margin-left:auto;color:var(--txt2)">${i.chute_time_s}s</span>
      </div>
      <div class="vid">${esc(i.video_file)} · #${i.penalty_id}</div>
    </div>`).join('') || '<div style="color:#585b70;padding:20px 4px">nada com esses filtros</div>';
  $('#mais').style.display = itens.length > limite ? 'inline-block' : 'none';
}
async function atualizarStatus(){
  const s = await api('/api/status'); statusGlobal = s;
  const pct = 100 * s.revisados / Math.max(s.total,1);
  $('#prog-bar').style.width = pct + '%';
  const mudou = s.originais.filter(o=>!o.intacto).length;
  $('#prog-txt').innerHTML =
    `<b>${s.revisados}/${s.total}</b> revisados · ${s.edicoes} alteração(ões) · ` +
    (mudou ? '<span style="color:var(--amarelo)">origem mudou (backup novo gravado)</span>'
           : '<span style="color:var(--verde)">originais intactos</span>');
}

// ---------- detalhe ----------
function idx(){ return itens.findIndex(i => atual && i.uid === atual.reg.uid); }
function vizinho(d){
  // idx() < 0 = o rotulo aberto saiu do filtro (ex.: era fora_cima e virou
  // fora_direita); nesse caso o "proximo" e o comeco/fim da lista atual.
  const at = idx();
  const i = at < 0 ? (d > 0 ? 0 : itens.length - 1) : at + d;
  if(i >= 0 && i < itens.length){ if(i >= limite){ limite = i + 100; renderLista(); }
    abrir(itens[i].uid); }
}
async function abrir(uid){
  atual = await api('/api/registro/' + encodeURIComponent(uid));
  const r = atual.reg;
  frameAtual = r.chute_frame;
  sel = {region: r.region, gol: r.is_goal};
  reguaAoTrocarLance();
  const i = idx();
  $('#cab').innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:5px">
      <b style="font-size:15px">${esc(r.region_label || r.region)}</b>
      <span class="chip">${esc(r.competicao_label)}</span>
      <span class="chip">pênalti ${esc(r.penalty_id)}</span>
      <span class="chip">${selo(r.status, true)}${r.revisado_em
        ? ' <span style="color:var(--txt2)">em ' + esc(r.revisado_em.replace('T',' ')) + '</span>' : ''}</span>
      <span style="margin-left:auto;display:flex;gap:4px;align-items:center">
        <span class="chip" ${i<0?'style="color:var(--amarelo)"':''}>${
          i<0 ? 'fora do filtro · '+itens.length : (i+1)+'/'+itens.length}</span>
        <button onclick="vizinho(-1)" ${i===0||!itens.length?'disabled':''}>&larr;</button>
        <button onclick="vizinho(1)" ${(i>=0&&i>=itens.length-1)||!itens.length?'disabled':''}>&rarr;</button>
      </span>
    </div>
    <div class="chip" style="display:block;margin-bottom:8px">${esc(r.video_file)}</div>
    ${!atual.video_disponivel?'<div class="aviso">vídeo original não encontrado no índice — use o clip ou os PNGs</div>':''}`;
  renderLista(); render(); renderEditor();
}

function modo(m){ modoAtual = m;
  ['video','frame','clip','png'].forEach(x=>$('#ab-'+x).classList.toggle('on', x===m)); render(); }

function render(){
  if(!atual) return;
  const r = atual.reg, nome = encodeURIComponent(r.video_file), uid = encodeURIComponent(r.uid);

  if(modoAtual === 'frame'){
    $('#palco').innerHTML = `<img id="mid" src="/midia/frame?nome=${nome}&n=${frameAtual}">`;
    $('#controles').innerHTML = `
      <button onclick="passo(-10)">&laquo; 10</button>
      <button onclick="passo(-1)">&lsaquo; 1</button>
      <span class="chip">frame <b id="fnum">${frameAtual}</b> · <span id="ftime">${(frameAtual/r.fps).toFixed(2)}</span>s</span>
      <button onclick="passo(1)">1 &rsaquo;</button>
      <button onclick="passo(10)">10 &raquo;</button>
      <button onclick="irFrame(atual.reg.inicio_frame)">início</button>
      <button onclick="irFrame(atual.reg.chute_frame)">chute</button>
      <button onclick="usarFrame('chute')">marcar chute aqui</button>
      <button onclick="usarFrame('inicio')">marcar início aqui</button>`;
  } else if(modoAtual === 'clip'){
    $('#palco').innerHTML = atual.clip_existe
      ? `<video id="mid" src="/midia/clip?uid=${uid}" controls autoplay loop muted></video>`
      : '<div style="color:var(--vermelho);padding:60px">clip não encontrado no disco</div>';
    $('#controles').innerHTML = velocidades();
  } else if(modoAtual === 'png'){
    $('#palco').innerHTML = `<div style="display:flex;gap:6px;background:#000">
      ${atual.png_inicio?`<figure style="margin:0"><img src="/midia/png?uid=${uid}&qual=inicio" style="max-height:52vh">
        <figcaption style="color:#9399b2;font-size:11px;text-align:center">início</figcaption></figure>`:''}
      ${atual.png_chute?`<figure style="margin:0"><img src="/midia/png?uid=${uid}&qual=chute" style="max-height:52vh">
        <figcaption style="color:#9399b2;font-size:11px;text-align:center">chute</figcaption></figure>`:''}
      ${!atual.png_inicio&&!atual.png_chute?'<div style="color:var(--vermelho);padding:60px">PNGs não encontrados</div>':''}
    </div>`;
    $('#controles').innerHTML = '';
  } else {
    if(!atual.video_disponivel){
      $('#palco').innerHTML = '<div style="color:#585b70;padding:60px">sem vídeo original</div>';
      $('#controles').innerHTML = '';
    } else {
      $('#palco').innerHTML = `<video id="mid" src="/midia/video?nome=${nome}" controls preload="metadata"></video>`;
      const v = $('#mid');
      v.addEventListener('loadedmetadata', ()=>{ v.currentTime = Math.max(r.inicio_time_s-1.5,0); }, {once:true});
      $('#controles').innerHTML = `
        <button onclick="irPara(atual.reg.inicio_time_s-1.5)">início -1.5s</button>
        <button onclick="irPara(atual.reg.chute_time_s)">momento do chute</button>
        <button onclick="pular(-1)">-1s</button><button onclick="pular(1)">+1s</button>
        ${velocidades()}
        <span class="chip">início ${r.inicio_time_s}s · chute ${r.chute_time_s}s · ${r.fps} fps</span>`;
    }
  }
  carregarTira();
  renderIrmaos();
  // a régua é redesenhada por cima da mídia nova; vídeo/imagem só têm tamanho
  // depois de carregar, então redesenha de novo no primeiro quadro
  const mid=reguaMidia();
  if(mid) mid.addEventListener(mid.tagName==='VIDEO'?'loadeddata':'load',
                               reguaDesenhar,{once:true});
  reguaDesenhar();
}
const velocidades = () => `<button onclick="vel(0.25)">0.25x</button>
  <button onclick="vel(0.5)">0.5x</button><button onclick="vel(1)">1x</button>`;

function irPara(t){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.currentTime=Math.max(t,0); }
function pular(d){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.currentTime=Math.max(v.currentTime+d,0); }
function vel(x){ const v=$('#mid'); if(v&&v.tagName==='VIDEO') v.playbackRate=x; }
function irFrame(n){ frameAtual = Math.max(n,0); if(modoAtual!=='frame') return modo('frame');
  $('#mid').src = `/midia/frame?nome=${encodeURIComponent(atual.reg.video_file)}&n=${frameAtual}`;
  $('#fnum').textContent = frameAtual; $('#ftime').textContent = (frameAtual/atual.reg.fps).toFixed(2);
  marcarTira(); }
function passo(d){ irFrame(frameAtual + d); }
function usarFrame(qual){
  $('#ed-'+qual).value = frameAtual;
  toast(`frame ${frameAtual} anotado como ${qual} — clique em Salvar para gravar`);
}

// ---------- régua das traves (E mostra, R recolhe) ----------
// Sobreposição de conferência: marca as 4 traves e divide o gol em 9 regiões
// iguais por cima do frame. NÃO é gravada em lugar nenhum — nem no CSV, nem no
// vídeo. Os 4 pontos ficam no localStorage do navegador, por vídeo e câmera,
// para a régua continuar onde você a colocou enquanto anda pelos frames.
const NS='http://www.w3.org/2000/svg';
const PEDIDOS=['trave ESQUERDA em CIMA','trave ESQUERDA em BAIXO',
               'trave DIREITA em BAIXO','trave DIREITA em CIMA'];
const CELS=[['gol_topo_esquerdo','gol_topo_centro','gol_topo_direito'],
            ['gol_meio_esquerdo','gol_meio_centro','gol_meio_direito'],
            ['gol_baixo_esquerdo','gol_baixo_centro','gol_baixo_direito']];
const CURTO={esquerdo:'esq',direito:'dir',centro:'centro'};
let regua={modo:'off',pontos:[],arrastando:-1,nomes:true}; // off | calibrando | on
try{ regua.nomes = localStorage.getItem('regua:nomes') !== '0'; }catch(e){}

const reguaChave=()=>atual?`regua|${atual.reg.video_file}|${atual.reg.camera_type}`:'';
function reguaLer(){
  try{const p=JSON.parse(localStorage.getItem(reguaChave())||'null');
      return Array.isArray(p)&&p.length===4?p:null;}catch(e){return null;}
}
function reguaGravar(){ try{localStorage.setItem(reguaChave(),JSON.stringify(regua.pontos));}catch(e){} }
const reguaMidia=()=>$('#palco video')||$('#palco img');

function reguaAlternar(){          // tecla E
  if(!atual) return;
  if(regua.modo==='off'){
    const p=reguaLer();
    if(p){ regua.pontos=p; regua.modo='on'; }
    else { regua.pontos=[]; regua.modo='calibrando'; }
  }else{                            // já visível: E recomeça a marcação
    regua.pontos=[]; regua.modo='calibrando';
  }
  reguaDesenhar();
}
function reguaNomes(){             // tecla T
  if(regua.modo!=='on') return;    // em calibracao os pedidos precisam aparecer
  regua.nomes=!regua.nomes;
  try{ localStorage.setItem('regua:nomes', regua.nomes?'1':'0'); }catch(e){}
  reguaDesenhar();
  toast(regua.nomes ? 'nomes das regiões à mostra'
                    : 'só os traços da régua — T traz os nomes de volta');
}
function reguaRecolher(){          // tecla R
  if(regua.modo==='off') return;
  regua.modo='off'; regua.arrastando=-1; reguaDesenhar();
}
function reguaAoTrocarLance(){     // outro rótulo = outro enquadramento
  if(regua.modo==='off') return;
  const p=reguaLer();
  if(p){ regua.pontos=p; regua.modo='on'; } else { regua.modo='off'; regua.pontos=[]; }
}

// bilinear nos 4 cantos: dividir cada aresta em 3 e ligar dá exatamente a
// divisão "igual" que o olho espera, já acompanhando a perspectiva do quadro
function reguaPonto(u,v){
  const [TL,BL,BR,TR]=regua.pontos;
  const cima ={x:TL.x+(TR.x-TL.x)*u, y:TL.y+(TR.y-TL.y)*u};
  const baixo={x:BL.x+(BR.x-BL.x)*u, y:BL.y+(BR.y-BL.y)*u};
  return {x:cima.x+(baixo.x-cima.x)*v, y:cima.y+(baixo.y-cima.y)*v};
}

function reguaDesenhar(){
  const palco=$('#palco');
  const velho=$('#regua'), velhoInfo=$('#regua-info');
  if(velho) velho.remove();
  if(velhoInfo) velhoInfo.remove();
  if(!palco||regua.modo==='off') return;
  const m=reguaMidia(); if(!m) return;
  const rp=palco.getBoundingClientRect(), rm=m.getBoundingClientRect();
  if(!rm.width||!rm.height) return;              // vídeo ainda sem metadados

  const svg=document.createElementNS(NS,'svg');
  svg.id='regua';
  svg.setAttribute('viewBox',`0 0 ${rm.width} ${rm.height}`);
  Object.assign(svg.style,{left:(rm.left-rp.left)+'px',top:(rm.top-rp.top)+'px',
                           width:rm.width+'px',height:rm.height+'px'});
  const px=p=>({x:p.x*rm.width, y:p.y*rm.height});
  const cria=(tag,attrs)=>{const e=document.createElementNS(NS,tag);
    for(const k in attrs) e.setAttribute(k,attrs[k]); svg.appendChild(e); return e;};

  if(regua.modo==='calibrando'){
    svg.classList.add('calibrando');
    regua.pontos.forEach(p=>{const q=px(p); cria('circle',{cx:q.x,cy:q.y,r:5,class:'pino'});});
    if(regua.pontos.length>1){
      cria('polyline',{class:'div',fill:'none',
        points:regua.pontos.map(p=>{const q=px(p);return `${q.x},${q.y}`;}).join(' ')});
    }
    svg.addEventListener('click',ev=>{
      const r=svg.getBoundingClientRect();
      regua.pontos.push({x:(ev.clientX-r.left)/r.width,y:(ev.clientY-r.top)/r.height});
      if(regua.pontos.length===4){ regua.modo='on'; reguaGravar(); }
      reguaDesenhar();
    });
  }else{
    const cantos=regua.pontos.map(px);
    // [TL, BL, BR, TR] -> contorno na ordem do gol
    cria('path',{class:'quad',d:`M${cantos[0].x},${cantos[0].y} L${cantos[3].x},${cantos[3].y} `
      +`L${cantos[2].x},${cantos[2].y} L${cantos[1].x},${cantos[1].y} Z`});

    // célula da região que está selecionada no editor, em destaque
    for(let l=0;l<3;l++) for(let c=0;c<3;c++){
      if(CELS[l][c]!==sel.region) continue;
      const q=[[c/3,l/3],[(c+1)/3,l/3],[(c+1)/3,(l+1)/3],[c/3,(l+1)/3]]
        .map(([u,v])=>px(reguaPonto(u,v)));
      cria('polygon',{class:'marcada',points:q.map(p=>`${p.x},${p.y}`).join(' ')});
    }
    for(const t of [1/3,2/3]){     // divisões internas
      const a=px(reguaPonto(t,0)), b=px(reguaPonto(t,1));
      cria('line',{class:'div',x1:a.x,y1:a.y,x2:b.x,y2:b.y});
      const c=px(reguaPonto(0,t)), d=px(reguaPonto(1,t));
      cria('line',{class:'div',x1:c.x,y1:c.y,x2:d.x,y2:d.y});
    }
    // T tira todo o texto e deixa so os tracos laranja sobre o lance
    if(regua.nomes){
      for(let l=0;l<3;l++) for(let c=0;c<3;c++){
        const q=px(reguaPonto((c+0.5)/3,(l+0.5)/3));
        const partes=CELS[l][c].split('_');
        const t=cria('text',{x:q.x,y:q.y+3});
        t.textContent=`${partes[1]} ${CURTO[partes[2]]||partes[2]}`;
      }
      // fora do gol: onde as três regiões externas ficam neste enquadramento
      const meioEsq=px(reguaPonto(0,.5)), meioDir=px(reguaPonto(1,.5)), meioCima=px(reguaPonto(.5,0));
      const fora=(x,y,txt,anchor)=>{const t=cria('text',{x,y,class:'fora'});
        if(anchor)t.setAttribute('text-anchor',anchor); t.textContent=txt;};
      fora(Math.max(meioEsq.x-8,26),meioEsq.y,'fora esq','end');
      fora(Math.min(meioDir.x+8,rm.width-26),meioDir.y,'fora dir','start');
      fora(meioCima.x,Math.max(meioCima.y-9,12),'fora cima');
    }

    // alças para ajustar sem remarcar tudo
    cantos.forEach((q,i)=>{
      const a=cria('circle',{cx:q.x,cy:q.y,r:6,class:'alca'});
      a.addEventListener('pointerdown',ev=>{ev.preventDefault();regua.arrastando=i;});
    });
  }
  palco.appendChild(svg);

  if(regua.modo==='on' && !regua.nomes) return;   // frame limpo: só os traços
  const info=document.createElement('div');
  info.id='regua-info';
  info.innerHTML=regua.modo==='calibrando'
    ? `clique na <b>${PEDIDOS[regua.pontos.length]}</b>
       <span style="color:var(--txt2)">(${regua.pontos.length}/4 · <kbd>R</kbd> cancela)</span>`
    : `régua das traves · arraste as alças <span style="color:var(--txt2)">
       <kbd>E</kbd> remarcar · <kbd>T</kbd> tira os nomes · <kbd>R</kbd> recolher ·
       não é gravada</span>`;
  palco.appendChild(info);
}

// arrastar alça: os eventos vivem na janela para o ponteiro poder sair do palco
addEventListener('pointermove',ev=>{
  if(regua.arrastando<0) return;
  const svg=$('#regua'); if(!svg) return;
  const r=svg.getBoundingClientRect();
  regua.pontos[regua.arrastando]={
    x:Math.min(Math.max((ev.clientX-r.left)/r.width,0),1),
    y:Math.min(Math.max((ev.clientY-r.top)/r.height,0),1)};
  reguaDesenhar();
});
addEventListener('pointerup',()=>{
  if(regua.arrastando<0) return;
  regua.arrastando=-1; reguaGravar();
});
addEventListener('resize',()=>{ if(regua.modo!=='off') reguaDesenhar(); });

// ---------- tira de frames ----------
async function carregarTira(){
  if(!atual || !atual.video_disponivel){ $('#tira').innerHTML=''; $('#tira-tit').style.display='none'; return; }
  const r = atual.reg;
  $('#tira-tit').style.display = 'block';
  $('#tira').innerHTML = '<div style="color:#585b70;padding:10px">decodificando frames...</div>';
  const q = new URLSearchParams({nome:r.video_file, centro:r.chute_frame, fps:r.fps, ...tiraCfg});
  try{
    const d = await api('/midia/tira?'+q);
    $('#tira').innerHTML = d.frames.map(f=>`
      <figure data-n="${f.n}" class="${f.chute?'chute':''}" onclick="irFrame(${f.n})">
        <img src="${f.img}"><figcaption>${f.n} · ${f.t}s${f.chute?' · chute':''}</figcaption>
      </figure>`).join('');
    marcarTira();
  }catch(e){ $('#tira').innerHTML = `<div style="color:var(--vermelho);padding:10px">${e.message}</div>`; }
}
function marcarTira(){
  document.querySelectorAll('#tira figure').forEach(f=>
    f.classList.toggle('sel', +f.dataset.n === frameAtual));
}

// ---------- outros ângulos do mesmo lance ----------
function renderIrmaos(){
  const irs = atual.irmaos;
  if(!irs.length){ $('#irmaos').innerHTML = ''; return; }
  const div = new Set(irs.map(i=>i.region).concat(atual.reg.region)).size > 1;
  $('#irmaos').innerHTML = `
    <h2>${irs.length} outro(s) rótulo(s) no mesmo lance
        ${div?'<span style="color:var(--vermelho)"> — regiões divergentes!</span>':''}</h2>
    <table class="ang"><tr><th>região</th><th>câmera</th><th>gol</th><th>chute</th><th>status</th></tr>
    ${irs.map(i=>`<tr data-uid="${esc(i.uid)}">
      <td style="color:var(--laranja)">${esc(i.region)}</td><td>${esc(i.camera_type)}</td>
      <td style="color:${i.is_goal?'var(--verde)':'var(--vermelho)'}">${i.is_goal?'SIM':'não'}</td>
      <td>${i.chute_time_s}s</td><td>${selo(i.status, true)}</td></tr>`).join('')}</table>`;
}

// ---------- editor ----------
function renderEditor(){
  const r = atual.reg, o = atual.original;
  const lab = c => (atual.regioes.find(x=>x.code===c)||{label:c}).label;
  const bt = c => `<button class="gol ${sel.region===c?'on':''}" data-reg="${c}"
      onclick="setReg('${c}')">${lab(c).replace('Gol - ','')}</button>`;
  const mudou = [];
  if(o.region !== r.region) mudou.push(`região: ${o.region} → ${r.region}`);
  if(o.is_goal !== r.is_goal) mudou.push(`gol: ${o.is_goal} → ${r.is_goal}`);
  if(o.camera_type !== r.camera_type) mudou.push(`câmera: ${o.camera_type} → ${r.camera_type}`);
  if(o.inicio_frame !== r.inicio_frame) mudou.push(`início: ${o.inicio_frame} → ${r.inicio_frame}`);
  if(o.chute_frame !== r.chute_frame) mudou.push(`chute: ${o.chute_frame} → ${r.chute_frame}`);

  $('#editor').innerHTML = `
    ${mudou.length?`<div class="diff"><b>alterado em relação ao original</b><br>${mudou.map(esc).join('<br>')}</div>`:''}
    ${r.status==='descartado'?`<div class="aviso">descartado: ${esc(r.motivo)}<br>
       <button onclick="reverter()" style="margin-top:6px">restaurar</button></div>`:''}

    <h2>Região do chute</h2>
    <div class="grade">
      ${bt('gol_topo_esquerdo')}${bt('gol_topo_centro')}${bt('gol_topo_direito')}
      ${bt('gol_meio_esquerdo')}${bt('gol_meio_centro')}${bt('gol_meio_direito')}
      ${bt('gol_baixo_esquerdo')}${bt('gol_baixo_centro')}${bt('gol_baixo_direito')}
    </div>
    <div class="fora">
      ${FORA.map(c=>`<button data-reg="${c}" class="${sel.region===c?'on':''}"
        onclick="setReg('${c}')">${lab(c).replace('Fora - ','Fora ')}</button>`).join('')}
    </div>

    <h2>Foi gol?</h2>
    <div class="dupla">
      <button class="${sel.gol?'on-sim':''}" onclick="setGol(true)">SIM</button>
      <button class="${!sel.gol?'on-nao':''}" onclick="setGol(false)">NÃO</button>
    </div>

    <h2>Câmera</h2>
    <select id="ed-camera" style="width:100%">
      ${atual.cameras.map(c=>`<option ${c===r.camera_type?'selected':''}>${esc(c)}</option>`).join('')}
    </select>

    <h2>Frames</h2>
    <div class="linha-campo"><label>início</label><input id="ed-inicio" type="number" value="${r.inicio_frame}"></div>
    <div class="linha-campo"><label>chute</label><input id="ed-chute" type="number" value="${r.chute_frame}"></div>
    <div class="linha-campo"><label>fps</label><input id="ed-fps" type="number" step="0.001" value="${r.fps}"></div>

    <h2>Observação (vai para o CSV)</h2>
    <textarea id="ed-obs" rows="2" style="width:100%">${esc(r.observations)}</textarea>
    <h2>Nota da revisão (só no backup)</h2>
    <textarea id="ed-nota" rows="2" style="width:100%" placeholder="o que você viu no vídeo...">${esc(r.nota)}</textarea>

    <div class="acoes">
      <button class="b-ok" onclick="aprovar()">Está correto, próximo <kbd>A</kbd></button>
      <button class="b-salvar" onclick="salvar()">Salvar correção <kbd>S</kbd></button>
      <button class="b-desc" onclick="descartar()">Descartar rótulo</button>
      <button onclick="reverter()" ${mudou.length||r.status!=='pendente'?'':'disabled'}>Voltar ao original</button>
      <button onclick="desfazer()" ${statusGlobal.pode_desfazer?'':'disabled'}>Desfazer última ação <kbd>Z</kbd></button>
      <button onclick="exportar()">Exportar CSVs revisados</button>
    </div>

    <div class="rodape">
      <kbd>←</kbd><kbd>→</kbd> rótulo anterior/próximo · <kbd>A</kbd> aprovar · <kbd>S</kbd> salvar<br>
      <kbd>7</kbd><kbd>8</kbd><kbd>9</kbd> / <kbd>4</kbd><kbd>5</kbd><kbd>6</kbd> /
      <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> grade do gol (layout do teclado numérico)<br>
      <kbd>J</kbd> fora esquerda · <kbd>I</kbd> fora cima · <kbd>L</kbd> fora direita<br>
      <kbd>F</kbd> frame a frame · <kbd>,</kbd><kbd>.</kbd> frame anterior/próximo<br>
      <kbd>E</kbd> régua das traves (4 cliques) · <kbd>T</kbd> só os traços ·
      <kbd>R</kbd> recolhe a régua<br><br>
      Grava em <b>_revisao_manual/revisao.csv</b>.<br>Os labels.csv originais nunca são escritos.
    </div>`;
}
function setReg(c){
  sel.region = c;
  document.querySelectorAll('[data-reg]').forEach(b=>b.classList.toggle('on', b.dataset.reg===c));
  if(c.startsWith('fora')) setGol(false);
  reguaDesenhar();                       // a célula marcada segue a escolha
}
function setGol(v){
  sel.gol = v;
  const bs = document.querySelectorAll('.dupla button');
  bs[0].className = v?'on-sim':''; bs[1].className = v?'':'on-nao';
}

// ---------- ações ----------
async function salvar(){
  try{
    const r = await api('/api/editar', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid, region: sel.region, camera: $('#ed-camera').value,
        gol: sel.gol, inicio_frame: parseInt($('#ed-inicio').value),
        chute_frame: parseInt($('#ed-chute').value), fps: parseFloat($('#ed-fps').value),
        observations: $('#ed-obs').value, nota: $('#ed-nota').value})});
    toast('salvo: ' + r.mudancas.join(' · '), 'bom');
    const uid = atual.reg.uid; await carregar(true); await abrir(uid);
  }catch(e){ toast(e.message, 'erro'); }
}
async function aprovar(){
  try{
    await api('/api/aprovar', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid, nota: $('#ed-nota').value})});
    toast('conferido, sem alteração', 'bom');
    // guarda o proximo ANTES de recarregar: com o filtro em "pendentes" o item
    // aprovado sai da lista e os indices andam um para tras.
    const i = idx(), prox = i >= 0 && i+1 < itens.length ? itens[i+1].uid : null;
    await carregar(true);
    if(prox && itens.some(x=>x.uid===prox)) abrir(prox);
    else if(itens.length) abrir(itens[Math.min(Math.max(i,0), itens.length-1)].uid);
  }catch(e){ toast(e.message, 'erro'); }
}
async function descartar(){
  const motivo = prompt('Por que descartar este rótulo?\n(fica registrado; dá para desfazer)');
  if(!motivo) return;
  try{
    await api('/api/descartar', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid, motivo})});
    toast('descartado', 'bom');
    const uid = atual.reg.uid; await carregar(true); await abrir(uid);
  }catch(e){ toast(e.message, 'erro'); }
}
async function reverter(){
  try{
    await api('/api/reverter', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid})});
    toast('rótulo restaurado ao original', 'bom');
    const uid = atual.reg.uid; await carregar(true); await abrir(uid);
  }catch(e){ toast(e.message, 'erro'); }
}
async function desfazer(){
  try{
    const r = await api('/api/desfazer', {method:'POST'});
    toast('desfeito: ' + r.acao, 'bom');
    const uid = atual ? atual.reg.uid : null; await carregar(true); if(uid) await abrir(uid);
  }catch(e){ toast(e.message, 'erro'); }
}
async function exportar(){
  try{
    const r = await api('/api/exportar', {method:'POST'});
    toast(`exportado: ${r.linhas} linhas (${r.descartados} descartadas) em ${r.pasta}`, 'bom');
  }catch(e){ toast(e.message, 'erro'); }
}

// ---------- teclado ----------
const GRADE = {'7':'gol_topo_esquerdo','8':'gol_topo_centro','9':'gol_topo_direito',
               '4':'gol_meio_esquerdo','5':'gol_meio_centro','6':'gol_meio_direito',
               '1':'gol_baixo_esquerdo','2':'gol_baixo_centro','3':'gol_baixo_direito',
               'j':'fora_esquerda','i':'fora_cima','l':'fora_direita'};
document.addEventListener('keydown', e=>{
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || !atual) return;
  const k = e.key.toLowerCase();
  if(k==='arrowleft'){ e.preventDefault(); vizinho(-1); }
  else if(k==='arrowright'){ e.preventDefault(); vizinho(1); }
  else if(k==='a') aprovar();
  else if(k==='s') salvar();
  else if(k==='z') desfazer();
  else if(k==='f') modo(modoAtual==='frame'?'video':'frame');
  else if(k==='e'){ e.preventDefault(); reguaAlternar(); }
  else if(k==='r'){ e.preventDefault(); reguaRecolher(); }
  else if(k==='t'){ e.preventDefault(); reguaNomes(); }
  else if(k===',') { if(modoAtual!=='frame') modo('frame'); else passo(-1); }
  else if(k==='.') { if(modoAtual!=='frame') modo('frame'); else passo(1); }
  else if(GRADE[k]) setReg(GRADE[k]);
});

// clique na lista e na tabela de angulos, por delegacao
['#lista','#irmaos'].forEach(s => $(s).addEventListener('click', e=>{
  const el = e.target.closest('[data-uid]');
  if(el) abrir(el.dataset.uid);
}));

['f-status','f-comp','f-regiao','f-camera','f-gol'].forEach(id=>
  $('#'+id).onchange = ()=>{ if(id==='f-regiao') filtroRegiao=''; carregar(); });
$('#f-busca').oninput = ()=>{ clearTimeout(window._b); window._b = setTimeout(carregar, 300); };
carregar();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def preparar(p: R.Paths, source_dir: str) -> None:
    """Backup, indice e contexto. Separado do servir() para o run.py reusar."""
    # O backup dos originais e a PRIMEIRA coisa que acontece: nada de abrir a
    # tela de edicao sem uma copia de seguranca no disco.
    R.garantir_backup(p)
    _ctx["paths"] = p
    _ctx["source_dir"] = source_dir
    _ctx["indice"] = R.indexar_videos(p, source_dir)


def servir(p: R.Paths, source_dir: str, host: str = "127.0.0.1",
           porta: int = 5007, abrir_navegador: bool = True) -> int:
    preparar(p, source_dir)

    base = R.carregar(p)
    res = R.resumo(base.registros)
    pend = res["status"].get("pendente", 0)
    url = f"http://{host}:{porta}"

    print(f"\n  Revisor de Penaltis  ->  {url}")
    print(f"  {res['total']} rotulos ({pend} pendente(s)) em "
          f"{len(base.fontes)} competicao(oes)")
    print(f"  fora_cima {res['regiao'].get('fora_cima', 0)} · "
          f"fora_direita {res['regiao'].get('fora_direita', 0)} · "
          f"fora_esquerda {res['regiao'].get('fora_esquerda', 0)}")
    print(f"  backup/estado: {p.dir}")
    print(f"  originais    : nunca sao escritos (export sai em {p.dir_export})\n")

    if abrir_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=porta, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Interface web de revisao completa (Flask)")
    ap.add_argument("--output-base", default=R.DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=R.DEFAULT_SOURCE_DIR)
    ap.add_argument("--porta", type=int, default=5007)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    raise SystemExit(servir(R.Paths(a.output_base), a.source_dir, a.host, a.porta,
                            not a.sem_navegador))
