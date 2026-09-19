"""
jogadores_web.py
================
Interface de rotulagem do cobrador e do goleiro de cada penalti (Flask).

O que a logica ja resolveu (times, placar, rodada, temporada, regiao do chute,
gol/nao-gol, agrupamento dos angulos) vem preenchido. Aqui se preenche so o
que exige olhar o video: quem bateu, quem defendeu, o numero das camisas, o
lado do goleiro e o minuto.

A peca central e o ZOOM: numero de camisa em transmissao 720p tem poucos
pixels de altura. Clicar no frame recorta aquele ponto na resolucao nativa e
amplia - sem isso a rotulagem seria adivinhacao.

Identificacao pelo NUMERO (elencos.py)
--------------------------------------
O nome do jogador nao e digitado: quem confere le o numero na camisa e o
elenco do Transfermarkt daquele clube naquela temporada responde quem e.

    - a lista destaca em laranja SO os penaltis com duvida (video com 2
      cobradores); os demais o elenco resolve sozinho
    - no penalti com duvida, os candidatos aparecem como cartoes grandes
      "#26 Fabio Santos" / "#77 Jo" - clicar em um preenche tudo
    - digitar qualquer numero resolve o nome ao vivo, com o aviso de
      ambiguidade quando a camisa trocou de dono no meio da temporada
    - os goleiros do time adversario viram botoes com numero e nome
    - "preencher as resolvidas" grava de uma vez os que nao tem ambiguidade

Toda escrita passa por jogadores.py, que so grava dentro de
_dataset_jogadores/ e nunca toca no labels.csv.

    python jogadores.py web          # http://127.0.0.1:5006
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import threading
import webbrowser
from collections import defaultdict
from typing import Any

from flask import Flask, Response, abort, jsonify, request, send_file

import navbar
import elencos as E          # numero da camisa -> jogador (elencos do scraping)
import jogadores as J
import validador as V

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_ctx: dict[str, Any] = {}


def p() -> J.PathsJog:
    return _ctx["paths"]


def rotulos_por_id() -> dict[str, dict]:
    """Linhas do labels.csv de origem, indexadas por row_id (somente leitura)."""
    return _ctx["rotulos"]


def _erro(msg: str, code: int = 400):
    return jsonify({"erro": msg}), code


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/fila")
def api_fila():
    linhas = J.carregar(p())
    temporada = request.args.get("temporada", "")
    status = request.args.get("status", "pendente")
    clube = request.args.get("clube", "")
    ident = request.args.get("ident", "")            # resolvido | duvida | sem_dados
    busca = request.args.get("busca", "").lower()

    # veredito da identificacao automatica, um por penalti (consulta barata)
    vered = {r["penalti_uid"]: E.veredito_rapido(r) for r in linhas}

    sel = [r for r in linhas
           if (not temporada or r["temporada"] == temporada)
           and (status == "todos" or r["status_rotulo"] == status)
           and (not clube or clube in (r["mandante"], r["visitante"]))
           and (not ident or vered[r["penalti_uid"]]["veredito"] == ident)
           and (not busca or busca in r["video_file"].lower())]

    temporadas: dict[str, int] = defaultdict(int)
    clubes: dict[str, int] = defaultdict(int)
    por_status: dict[str, int] = defaultdict(int)
    por_ident: dict[str, int] = defaultdict(int)
    for r in linhas:
        temporadas[r["temporada"]] += 1
        por_status[r["status_rotulo"]] += 1
        clubes[r["mandante"]] += 1
        clubes[r["visitante"]] += 1
        por_ident[vered[r["penalti_uid"]]["veredito"]] += 1

    return jsonify({
        # .get: fonte_cobrador e coluna nova - linhas gravadas antes nao a tem
        "itens": [{**{k: r.get(k, "") for k in ("penalti_uid", "temporada", "rodada",
                                        "mandante", "visitante", "region", "is_goal",
                                        "n_angulos", "status_rotulo", "camisa_cobrador",
                                        "camisa_goleiro", "nome_cobrador", "fonte_cobrador")},
                   "ident": vered[r["penalti_uid"]]["veredito"],
                   "n_candidatos": vered[r["penalti_uid"]]["n_candidatos"]}
                  for r in sel],
        "total": len(linhas), "filtrados": len(sel),
        "temporadas": dict(sorted(temporadas.items())),
        "clubes": dict(sorted(clubes.items())),
        "por_status": dict(por_status),
        "por_ident": dict(por_ident),
    })


@app.get("/api/penalti/<uid>")
def api_penalti(uid: str):
    linhas = J.carregar(p())
    i = next((k for k, r in enumerate(linhas) if r["penalti_uid"] == uid), None)
    if i is None:
        return _erro("penalti nao encontrado", 404)
    reg = linhas[i]

    # todos os angulos do lance, do labels.csv de origem
    idx = rotulos_por_id()
    angulos = []
    for rid in reg["rotulos_ids"].split(";"):
        l = idx.get(rid)
        if not l:
            continue
        fps = V.fps_estimado(l) or V.FPS_ESPERADO
        angulos.append({
            "row_id": rid,
            "camera_type": l.get("camera_type", ""),
            "region": l.get("region", ""),
            "is_goal": V.is_goal(l),
            "inicio_frame": int(V.num(l, "inicio_frame")),
            "chute_frame": int(V.num(l, "chute_frame")),
            "inicio_time_s": round(V.num(l, "inicio_time_s"), 3),
            "chute_time_s": round(V.num(l, "chute_time_s"), 3),
            "fps": round(fps, 3),
            "clip_existe": os.path.isfile(l.get("clip_path", "")),
        })
    # a visao do cobrador costuma mostrar melhor a camisa de quem bate
    prioridade = {"visão cobrador": 0, "visão do torcedor": 1, "visão goleiro": 2}
    angulos.sort(key=lambda a: prioridade.get(a["camera_type"], 9))

    elenco = J.carregar_elenco(p()).get(reg["temporada"], {})
    ident = E.identificar(reg)
    return jsonify({
        "reg": reg,
        "angulos": angulos,
        "video_disponivel": bool(reg["video_path"]) and os.path.isfile(reg["video_path"]),
        "anterior": linhas[i - 1]["penalti_uid"] if i > 0 else None,
        "proximo": linhas[i + 1]["penalti_uid"] if i < len(linhas) - 1 else None,
        # elenco aprendido a mao: continua valendo como rede de seguranca para
        # os jogos que o scraping nao cobre
        "elenco_mandante": elenco.get(reg["mandante"], {}),
        "elenco_visitante": elenco.get(reg["visitante"], {}),
        # identificacao pelos elencos reais do Transfermarkt
        "ident": ident,
        "elencos": {
            "mandante": E.elenco_por_numero(ident["clube_mandante"], reg["temporada"]),
            "visitante": E.elenco_por_numero(ident["clube_visitante"], reg["temporada"]),
        },
        "vocab": {"pes": J.PES, "lados": J.LADOS_GOLEIRO,
                  "desfechos": J.DESFECHOS, "periodos": J.PERIODOS},
    })


@app.get("/api/numero")
def api_numero():
    """
    Numero da camisa -> jogador, no elenco daquele clube naquela temporada.

    E o coracao da validacao por numero: quem confere le so o numero no video e
    o nome vem daqui, sem digitar nada. Quando a camisa trocou de dono no meio
    da temporada, devolve os dois e deixa a escolha na tela.
    """
    uid = request.args.get("uid", "")
    lado = request.args.get("lado", "")            # mandante | visitante
    numero = request.args.get("numero", "")
    reg = next((r for r in J.carregar(p()) if r["penalti_uid"] == uid), None)
    if reg is None:
        return _erro("penalti nao encontrado", 404)
    if lado not in ("mandante", "visitante"):
        return _erro("lado deve ser 'mandante' ou 'visitante'")

    clube = E.clube_transfermarkt(reg[lado])
    res = E.resolver_numero(numero, clube, reg["temporada"])
    return jsonify({**res, "clube": clube, "lado": lado,
                    "time_arquivo": reg[lado]})


@app.post("/api/aplicar-sugestao")
def api_aplicar_sugestao():
    """Grava a identificacao automatica deste penalti (so quando nao ha duvida)."""
    d = request.get_json(force=True)
    try:
        reg = J.aplicar_sugestao(p(), d.get("penalti_uid", ""),
                                 bool(d.get("sobrescrever")))
    except J.ValidacaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True, "status": reg["status_rotulo"],
                    "nome_cobrador": reg.get("nome_cobrador", ""),
                    "camisa_cobrador": reg.get("camisa_cobrador", "")})


@app.post("/api/aplicar-sugestoes")
def api_aplicar_sugestoes():
    """Mesma coisa, para toda a base de uma vez."""
    d = request.get_json(force=True) if request.data else {}
    res = J.aplicar_sugestoes(p(), bool(d.get("sobrescrever")))
    return jsonify({"ok": True, "aplicados": len(res["aplicados"]),
                    "pulados": res["pulados"], "total": res["total"]})


@app.post("/api/rotular")
def api_rotular():
    d = request.get_json(force=True)
    uid = d.pop("penalti_uid", "")
    try:
        reg = J.salvar_rotulo(p(), uid, d)
    except J.ValidacaoErro as e:
        return _erro(str(e))
    return jsonify({"ok": True, "status": reg["status_rotulo"],
                    "goleiro_acertou_lado": reg["goleiro_acertou_lado"]})


@app.get("/api/progresso")
def api_progresso():
    linhas = J.carregar(p())
    c = sum(1 for r in linhas if r["status_rotulo"] == "completo")
    pa = sum(1 for r in linhas if r["status_rotulo"] == "parcial")
    return jsonify({"total": len(linhas), "completos": c, "parciais": pa,
                    "pendentes": len(linhas) - c - pa, "pasta": p().dir})


# ---------------------------------------------------------------------------
# Midia
# ---------------------------------------------------------------------------
def _video_de(nome: str) -> str:
    caminho = _ctx["indice"].get(nome, "")
    if not caminho or not os.path.isfile(caminho):
        abort(404)
    return caminho


def _range(caminho: str) -> Response:
    tam = os.path.getsize(caminho)
    tipo = mimetypes.guess_type(caminho)[0] or "video/mp4"
    m = re.match(r"bytes=(\d*)-(\d*)", request.headers.get("Range", ""))
    if not m:
        return send_file(caminho, mimetype=tipo, conditional=True)
    ini = int(m.group(1)) if m.group(1) else 0
    fim = min(int(m.group(2)) if m.group(2) else ini + 4 * 1024 * 1024 - 1, tam - 1)
    if ini >= tam:
        return Response(status=416, headers={"Content-Range": f"bytes */{tam}"})
    with open(caminho, "rb") as f:
        f.seek(ini)
        dados = f.read(fim - ini + 1)
    return Response(dados, status=206, mimetype=tipo, headers={
        "Content-Range": f"bytes {ini}-{fim}/{tam}",
        "Accept-Ranges": "bytes", "Content-Length": str(len(dados))})


@app.get("/midia/video")
def midia_video():
    return _range(_video_de(request.args.get("nome", "")))


_cache: dict[tuple, bytes] = {}
_lock = threading.Lock()


def _ler_frame(caminho: str, n: int):
    import cv2
    with _lock:                                  # VideoCapture nao e thread-safe
        cap = cv2.VideoCapture(caminho)
        if not cap.isOpened():
            abort(500)
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(n, 0))
        ok, img = cap.read()
        cap.release()
    if not ok:
        abort(404)
    return img


def _jpeg(img, q: int = 92) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        abort(500)
    return buf.tobytes()


@app.get("/midia/frame")
def midia_frame():
    """Frame exato. O <video> do navegador nao garante o frame N."""
    nome = request.args.get("nome", "")
    n = int(request.args.get("n", 0) or 0)
    chave = ("f", nome, n)
    if chave in _cache:
        return Response(_cache[chave], mimetype="image/jpeg")
    img = _ler_frame(_video_de(nome), n)
    dados = _jpeg(img, 88)
    if len(_cache) > 300:
        _cache.clear()
    _cache[chave] = dados
    return Response(dados, mimetype="image/jpeg")


@app.get("/api/videoinfo")
def api_videoinfo():
    """Resolucao da fonte: diz ao usuario ate onde o zoom ainda tem pixel real."""
    import cv2
    caminho = _video_de(request.args.get("nome", ""))
    chave = ("i", caminho)
    if chave in _ctx.setdefault("info", {}):
        return jsonify(_ctx["info"][chave])
    cap = cv2.VideoCapture(caminho)
    info = {"largura": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "altura": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "frames": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
            "fps": round(cap.get(cv2.CAP_PROP_FPS), 3)}
    cap.release()
    _ctx["info"][chave] = info
    return jsonify(info)


@app.get("/midia/zoom")
def midia_zoom():
    """
    Recorte do frame na resolucao nativa, ampliado e com nitidez.

    E o que torna o numero da camisa legivel: em vez de esticar a imagem ja
    reduzida da tela, recorta os pixels originais e so entao amplia. A mascara
    de nitidez no fim recupera a borda dos algarismos, que a interpolacao
    suaviza - e a diferenca entre ler "11" e chutar entre "11" e "17".
    """
    import cv2
    nome = request.args.get("nome", "")
    n = int(request.args.get("n", 0) or 0)
    cx = float(request.args.get("cx", 0.5))
    cy = float(request.args.get("cy", 0.5))
    fator = max(1.5, min(float(request.args.get("fator", 4)), 16))
    nitidez = request.args.get("nitidez", "1") != "0"

    chave = ("z", nome, n, round(cx, 4), round(cy, 4), fator, nitidez)
    if chave in _cache:
        return Response(_cache[chave], mimetype="image/jpeg")

    img = _ler_frame(_video_de(nome), n)
    h, w = img.shape[:2]
    cw, ch = max(int(w / fator), 16), max(int(h / fator), 16)
    x = min(max(int(cx * w) - cw // 2, 0), max(w - cw, 0))
    y = min(max(int(cy * h) - ch // 2, 0), max(h - ch, 0))
    corte = img[y:y + ch, x:x + cw]

    # LANCZOS4 preserva a borda dos numeros melhor que a interpolacao linear
    escala = min(1280 / max(corte.shape[1], 1), 4.0)
    if escala > 1:
        corte = cv2.resize(corte, None, fx=escala, fy=escala, interpolation=cv2.INTER_LANCZOS4)
    if nitidez:
        borrado = cv2.GaussianBlur(corte, (0, 0), 1.6)
        corte = cv2.addWeighted(corte, 1.7, borrado, -0.7, 0)     # unsharp mask

    dados = _jpeg(corte, 95)
    if len(_cache) > 300:
        _cache.clear()
    _cache[chave] = dados
    return Response(dados, mimetype="image/jpeg")


@app.get("/")
def index():
    return Response(PAGINA.replace("<!--NAV-->", navbar.barra("jogadores")),
                    mimetype="text/html")


# ---------------------------------------------------------------------------
PAGINA = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cobrador &amp; Goleiro — Brasileirão</title>
<style>
 :root{--bg:#11111b;--pn:#181825;--pn2:#1e1e2e;--bd:#313244;--tx:#cdd6f4;--tx2:#9399b2;
       --az:#89b4fa;--lj:#fab387;--vd:#a6e3a1;--vm:#f38ba8;--am:#f9e2af;--rx:#cba6f7}
 *{box-sizing:border-box}
 body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);
      color:var(--tx);height:100vh;overflow:hidden}
  /* desconta a barra de navegacao do topo (navbar.py publica --nav-h) */
 #app{display:grid;grid-template-columns:270px 1fr 350px;
      height:calc(100vh - var(--nav-h,0px))}
 .col{overflow-y:auto;padding:12px}.col+.col{border-left:1px solid var(--bd)}
 h1{font-size:14px;margin:0 0 10px}
 h2{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--tx2);
    margin:14px 0 6px;font-weight:600}h2:first-child{margin-top:0}
 select,input,textarea,button{font:inherit;color:var(--tx);background:var(--pn2);
    border:1px solid var(--bd);border-radius:6px;padding:6px 8px}
 button{cursor:pointer;transition:.12s}button:hover{border-color:var(--az)}
 button:disabled{opacity:.35;cursor:not-allowed}
 .filtros{display:grid;grid-template-columns:1fr 1fr;gap:5px;margin-bottom:8px}
 .filtros>*{width:100%;font-size:12px;padding:5px}
 .item{padding:7px 9px;border:1px solid var(--bd);border-radius:7px;margin-bottom:5px;
       cursor:pointer;background:var(--pn);font-size:12px}
 .item:hover{border-color:var(--az)}.item.sel{border-color:var(--az);background:#1e2438}
 .item.completo{border-left:3px solid var(--vd)}.item.parcial{border-left:3px solid var(--am)}
 /* duvida = o elenco nao decide sozinho; e o unico caso que precisa da sua atencao */
 .item.duvida{border-left:3px solid var(--lj);background:#231c14}
 .tagduv{color:var(--lj);font-weight:700}
 .cand{display:grid;gap:5px;margin-top:6px}
 .cand button{display:grid;grid-template-columns:52px 1fr;gap:9px;align-items:center;
              padding:8px;text-align:left;border-color:#4a3520}
 .cand button:hover{border-color:var(--lj)}
 .cand button.on{background:var(--lj);color:#11111b;border-color:var(--lj)}
 .cand .n{font-size:22px;font-weight:800;text-align:center;line-height:1}
 .cand small{display:block;font-size:10px;opacity:.8}
 .aviso{font-size:11px;background:#231c14;border:1px solid #4a3520;color:var(--lj);
        padding:7px 9px;border-radius:6px;margin-top:6px}
 .ok-auto{font-size:11px;background:#16241c;border:1px solid #2c5a3f;color:var(--vd);
          padding:7px 9px;border-radius:6px;margin-top:6px}
 .gks{display:flex;gap:4px;flex-wrap:wrap;margin-top:5px}
 .gks button{font-size:11px;padding:4px 8px}
 .gks button.on{background:var(--az);color:#11111b;border-color:var(--az);font-weight:700}
 .achado{font-size:12px;margin-top:4px;min-height:17px}
 .item .jogo{font-size:11px;color:var(--tx2);margin-top:2px;white-space:nowrap;
             overflow:hidden;text-overflow:ellipsis}
 .chip{font-size:11px;color:var(--tx2);padding:3px 7px;background:var(--pn);
       border-radius:5px;border:1px solid var(--bd);white-space:nowrap}
 .chip b{color:var(--tx)}
 #quadro{position:relative;background:#000;border-radius:8px;overflow:hidden;
         display:flex;align-items:center;justify-content:center;min-height:220px}
 #quadro img,#quadro video{max-width:100%;max-height:46vh;display:block;cursor:crosshair}
 #mira{position:absolute;width:26px;height:26px;border:2px solid var(--am);
       border-radius:50%;pointer-events:none;transform:translate(-50%,-50%);display:none;
       box-shadow:0 0 0 9999px rgba(0,0,0,.15)}
 #lupa{margin-top:8px;background:#000;border-radius:8px;overflow:hidden;min-height:60px;
       display:flex;align-items:center;justify-content:center;border:1px solid var(--bd)}
 #lupa img{max-width:100%;max-height:30vh;display:block;image-rendering:auto}
 #lupa .vazio{color:#585b70;padding:26px;font-size:12px}
 .barra{display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin:8px 0}
 .barra button{padding:4px 9px;font-size:12px}
 .abas{display:flex;gap:4px;margin-bottom:6px;flex-wrap:wrap}
 .abas button{font-size:12px;padding:4px 10px;background:transparent;border-color:transparent}
 .abas button.on{background:var(--pn2);border-color:var(--bd)}
 .times{display:grid;grid-template-columns:1fr 1fr;gap:6px}
 .times button{padding:9px 6px;font-size:12px;line-height:1.3;text-align:center}
 .times button.on{background:var(--az);color:#11111b;border-color:var(--az);font-weight:700}
 .times small{display:block;font-size:10px;opacity:.75}
 .camisa{display:grid;grid-template-columns:74px 1fr;gap:6px;align-items:center}
 .camisa input.num{font-size:26px;font-weight:800;text-align:center;padding:6px}
 .opcoes{display:flex;gap:4px;flex-wrap:wrap}
 .opcoes button{padding:5px 10px;font-size:12px;flex:1}
 .opcoes button.on{background:var(--az);color:#11111b;border-color:var(--az);font-weight:700}
 .lados button.on{background:var(--rx);color:#11111b;border-color:var(--rx)}
 .g2{display:grid;grid-template-columns:1fr 1fr;gap:6px}
 .g3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}
 label.mini{font-size:10px;color:var(--tx2);display:block;margin-bottom:2px}
 .salvar{width:100%;padding:11px;margin-top:12px;background:#1e3a2a;border-color:var(--vd);
         color:var(--vd);font-weight:700}
 .dica{font-size:11px;color:var(--tx2);background:var(--pn);padding:6px 9px;
       border-radius:6px;border-left:2px solid var(--az);margin-top:6px}
 #toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--pn2);
        border:1px solid var(--bd);padding:9px 16px;border-radius:8px;opacity:0;
        transition:.2s;pointer-events:none;z-index:99}
 #toast.on{opacity:1}#toast.erro{border-color:var(--vm);color:var(--vm)}
 #toast.bom{border-color:var(--vd);color:var(--vd)}
 kbd{background:var(--pn2);border:1px solid var(--bd);border-radius:4px;padding:0 4px;font-size:10px}
 .rodape{font-size:11px;color:var(--tx2);margin-top:12px;padding-top:9px;border-top:1px solid var(--bd)}
</style></head><body>
<!--NAV-->
<div id="app">
 <div class="col">
  <h1>Cobrador &amp; Goleiro</h1>
  <div class="chip" id="prog" style="display:block;margin-bottom:8px"></div>
  <div class="filtros">
   <select id="f-temp"><option value="">todas temporadas</option></select>
   <select id="f-status">
     <option value="pendente">pendentes</option><option value="todos">todos</option>
     <option value="parcial">parciais</option><option value="completo">completos</option></select>
   <select id="f-clube" style="grid-column:1/3"><option value="">todos os clubes</option></select>
   <select id="f-ident" style="grid-column:1/3">
     <option value="">identificação: todas</option>
     <option value="duvida">só as com DÚVIDA (escolher pelo número)</option>
     <option value="resolvido">só as já resolvidas pelo elenco</option>
     <option value="sem_dados">sem dados do scraping</option></select>
  </div>
  <div id="idresumo" class="dica" style="margin:0 0 8px"></div>
  <div id="lista"></div>
 </div>

 <div class="col">
  <div id="cab"></div>
  <div class="abas" id="abas"></div>
  <div id="quadro"><div style="color:#585b70;padding:50px">selecione um pênalti</div>
    <div id="mira"></div></div>
  <div class="barra" id="ctrl"></div>
  <div class="barra" id="ctrl2"></div>
  <div id="lupa"><div class="vazio">clique no frame para ampliar e ler o número da camisa</div></div>
 </div>

 <div class="col" id="form"></div>
</div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
let fila=[],at=null,angIdx=0,frame=0,modo='frame',fator=4,nitidez=true,ultimoClique=null,form={};

function toast(m,t){const e=$('#toast');e.textContent=m;e.className='on '+(t||'');
  clearTimeout(e._h);e._h=setTimeout(()=>e.className='',2600);}
async function api(u,o){const r=await fetch(u,o);const j=await r.json().catch(()=>({erro:'resposta inválida'}));
  if(!r.ok)throw new Error(j.erro||('HTTP '+r.status));return j;}

async function carregarFila(){
  const q=new URLSearchParams({temporada:$('#f-temp').value,status:$('#f-status').value,
                              clube:$('#f-clube').value,ident:$('#f-ident').value});
  const d=await api('/api/fila?'+q); fila=d.itens;
  const pi=d.por_ident||{};
  $('#idresumo').innerHTML=
    `identificação pelos elencos: <b style="color:var(--vd)">${pi.resolvido||0}</b> resolvidas · `+
    `<b style="color:var(--am)">${pi.duvida||0}</b> com dúvida · ${pi.sem_dados||0} sem dados` +
    ((pi.resolvido||0) ? ` · <button style="padding:2px 7px;font-size:11px"
        onclick="aplicarTodas()">preencher as resolvidas</button>` : '');
  if(!$('#f-temp').dataset.ok){
    for(const [t,n] of Object.entries(d.temporadas))
      $('#f-temp').insertAdjacentHTML('beforeend',`<option value="${t}">${t} (${n})</option>`);
    for(const [c,n] of Object.entries(d.clubes))
      $('#f-clube').insertAdjacentHTML('beforeend',`<option value="${c}">${c} (${n})</option>`);
    $('#f-temp').dataset.ok='1';
  }
  // so a DUVIDA fica em destaque: o resto o elenco ja resolve sozinho
  $('#lista').innerHTML=fila.map(i=>`
    <div class="item ${i.status_rotulo} ${i.ident==='duvida'&&!i.camisa_cobrador?'duvida':''}
         ${at&&at.reg.penalti_uid===i.penalti_uid?'sel':''}"
         onclick="abrir('${i.penalti_uid}')">
      <div><b>${i.penalti_uid}</b> · ${i.temporada}${i.rodada?' R'+i.rodada:''}
        ${i.camisa_cobrador
          ? `<span style="color:var(--vd)">#${i.camisa_cobrador} ${i.nome_cobrador||''}</span>`
          : (i.ident==='duvida'
             ? `<span class="tagduv">? ${i.n_candidatos} cobradores</span>`
             : (i.ident==='sem_dados'?'<span style="color:var(--tx2)">sem elenco</span>':''))}
        ${i.n_angulos>1?`<span style="color:var(--tx2)">·${i.n_angulos}âng</span>`:''}</div>
      <div class="jogo">${i.mandante} x ${i.visitante}</div>
    </div>`).join('')||'<div style="color:#585b70;padding:16px 4px">nada com esses filtros</div>';
  const pr=await api('/api/progresso');
  $('#prog').innerHTML=`<b>${pr.completos}</b>/${pr.total} completos · ${pr.parciais} parciais`;
}

async function abrir(uid){
  at=await api('/api/penalti/'+uid); angIdx=0; ultimoClique=null;
  frame=at.angulos.length?at.angulos[0].chute_frame:0;
  const r=at.reg;
  $('#cab').innerHTML=`
   <div style="display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin-bottom:7px">
     <b style="font-size:16px">${r.penalti_uid}</b>
     <span class="chip">${r.temporada}${r.rodada?' · rodada '+r.rodada:''}</span>
     <span class="chip"><b>${r.mandante}</b> ${r.placar_final_mandante} x
       ${r.placar_final_visitante} <b>${r.visitante}</b></span>
     <span class="chip">${r.region_label}</span>
     <span class="chip" style="color:${r.is_goal==='True'?'var(--vd)':'var(--vm)'}">
       ${r.is_goal==='True'?'GOL':'não foi gol'}</span>
     <span style="margin-left:auto;display:flex;gap:4px">
       <button onclick="ir('anterior')" ${!at.anterior?'disabled':''}>&larr;</button>
       <button onclick="ir('proximo')" ${!at.proximo?'disabled':''}>&rarr;</button></span>
   </div>`;
  $('#abas').innerHTML=at.angulos.map((a,i)=>
    `<button class="${i===angIdx?'on':''}" onclick="setAng(${i})">${a.camera_type}</button>`).join('')
    + `<button id="ab-vid" onclick="setModo('video')">vídeo completo</button>`;
  form={}; render(); renderForm(); carregarFila();
}
function ir(d){if(at[d])abrir(at[d]);}
function ang(){return at.angulos[angIdx]||{};}
function setAng(i){angIdx=i;frame=ang().chute_frame;modo='frame';ultimoClique=null;
  document.querySelectorAll('#abas button').forEach((b,k)=>b.classList.toggle('on',k===i));render();}
function setModo(m){modo=m;document.querySelectorAll('#abas button').forEach(b=>b.classList.remove('on'));
  if(m==='video')$('#ab-vid').classList.add('on');render();}

function render(){
  if(!at)return;
  const nome=encodeURIComponent(at.reg.video_file), a=ang();
  if(!at.video_disponivel){
    $('#quadro').innerHTML='<div style="color:var(--vm);padding:50px">vídeo original não encontrado</div>';
    $('#ctrl').innerHTML='';$('#ctrl2').innerHTML='';return;
  }
  if(modo==='video'){
    $('#quadro').innerHTML=`<video id="mid" src="/midia/video?nome=${nome}" controls></video><div id="mira"></div>`;
    const v=$('#mid'); v.addEventListener('loadedmetadata',()=>{v.currentTime=Math.max(a.inicio_time_s-3,0);},{once:true});
    $('#ctrl').innerHTML=`<button onclick="$('#mid').currentTime=Math.max(${a.inicio_time_s}-3,0)">início</button>
      <button onclick="$('#mid').currentTime=${a.chute_time_s}">chute</button>
      <button onclick="$('#mid').playbackRate=0.25">0.25x</button>
      <button onclick="$('#mid').playbackRate=0.5">0.5x</button>
      <button onclick="$('#mid').playbackRate=1">1x</button>`;
    $('#ctrl2').innerHTML='';return;
  }
  $('#quadro').innerHTML=`<img id="mid" src="/midia/frame?nome=${nome}&n=${frame}" onclick="clicou(event)"><div id="mira"></div>`;
  $('#ctrl').innerHTML=`
    <button onclick="passo(-25)">&laquo;25</button><button onclick="passo(-5)">&laquo;5</button>
    <button onclick="passo(-1)">&lsaquo;1</button>
    <span class="chip">frame <b id="fn">${frame}</b> · ${(frame/(a.fps||30)).toFixed(2)}s</span>
    <button onclick="passo(1)">1&rsaquo;</button><button onclick="passo(5)">5&raquo;</button>
    <button onclick="passo(25)">25&raquo;</button>
    <button onclick="frame=ang().inicio_frame;render()">início</button>
    <button onclick="frame=ang().chute_frame;render()">chute</button>`;
  $('#ctrl2').innerHTML=`<span class="chip">zoom</span>` +
    [2,3,4,6,8,12].map(f=>`<button class="${f===fator?'on':''}"
       style="${f===fator?'background:var(--az);color:#11111b;font-weight:700':''}"
       onclick="fator=${f};render();aplicarZoom()">${f}x</button>`).join('') +
    `<button onclick="nitidez=!nitidez;render();aplicarZoom()"
       style="${nitidez?'background:var(--rx);color:#11111b;font-weight:700':''}">nitidez</button>
     <span class="chip" id="recorte">clique no jogador para ler a camisa</span>`;
  infoVideo();
  if(ultimoClique) aplicarZoom(); else
    $('#lupa').innerHTML='<div class="vazio">clique no frame para ampliar e ler o número da camisa</div>';
}
function passo(d){frame=Math.max(frame+d,0);
  $('#mid').src=`/midia/frame?nome=${encodeURIComponent(at.reg.video_file)}&n=${frame}`;
  $('#fn').textContent=frame; if(ultimoClique)aplicarZoom();}
function clicou(e){
  const r=e.target.getBoundingClientRect();
  ultimoClique={cx:(e.clientX-r.left)/r.width, cy:(e.clientY-r.top)/r.height};
  const m=$('#mira'); m.style.display='block';
  m.style.left=(e.clientX-$('#quadro').getBoundingClientRect().left)+'px';
  m.style.top=(e.clientY-$('#quadro').getBoundingClientRect().top)+'px';
  aplicarZoom();
}
function aplicarZoom(){
  if(!ultimoClique)return;
  const q=new URLSearchParams({nome:at.reg.video_file,n:frame,
    cx:ultimoClique.cx.toFixed(4),cy:ultimoClique.cy.toFixed(4),fator,
    nitidez:nitidez?1:0});
  $('#lupa').innerHTML=`<img src="/midia/zoom?${q}">`;
}
async function infoVideo(){
  // mostra ate onde o zoom ainda tem pixel de verdade
  try{
    const i=await api('/api/videoinfo?nome='+encodeURIComponent(at.reg.video_file));
    const el=$('#recorte'); if(!el)return;
    const cw=Math.round(i.largura/fator), ch=Math.round(i.altura/fator);
    el.innerHTML=`fonte <b>${i.largura}x${i.altura}</b> · recorte <b>${cw}x${ch}</b> px reais`;
  }catch(e){}
}

// ---------- formulário ----------
function v(c){return form[c]!==undefined?form[c]:(at.reg[c]||'');}
function set(c,val){form[c]=val;renderForm();}
function renderForm(){
  if(!at){$('#form').innerHTML='';return;}
  const r=at.reg, voc=at.vocab;
  const opc=(campo,lista,cls='')=>`<div class="opcoes ${cls}">`+lista.map(o=>
    `<button class="${v(campo)===o?'on':''}" onclick="set('${campo}','${o}')">${o}</button>`).join('')+`</div>`;
  const elencoDe=lado=>lado==='mandante'?at.elenco_mandante:at.elenco_visitante;
  const clubeCob=v('time_cobrador')==='mandante'?r.mandante:(v('time_cobrador')==='visitante'?r.visitante:'');
  const clubeGol=v('time_cobrador')==='mandante'?r.visitante:(v('time_cobrador')==='visitante'?r.mandante:'');
  const elCob=v('time_cobrador')?elencoDe(v('time_cobrador')):{};
  const elGol=v('time_cobrador')?elencoDe(v('time_cobrador')==='mandante'?'visitante':'mandante'):{};

  const id=at.ident||{}, cands=id.candidatos||[];
  const ladoGol=v('time_cobrador')==='mandante'?'visitante':(v('time_cobrador')==='visitante'?'mandante':'');
  const gks=(id.goleiros_possiveis||[]).filter(g=>!ladoGol||g.lado===ladoGol);

  $('#form').innerHTML=`
   <h2>Quem bateu</h2>
   ${identificacaoHTML(id,cands)}
   <div class="times">
     <button class="${v('time_cobrador')==='mandante'?'on':''}" onclick="set('time_cobrador','mandante')">
       ${r.mandante}<small>mandante</small></button>
     <button class="${v('time_cobrador')==='visitante'?'on':''}" onclick="set('time_cobrador','visitante')">
       ${r.visitante}<small>visitante</small></button>
   </div>
   <div class="camisa" style="margin-top:7px">
     <div><label class="mini">camisa</label>
       <input class="num" id="c-cob" type="number" min="1" max="99" value="${v('camisa_cobrador')}"
              oninput="numMudou('cob')"></div>
     <div><label class="mini">nome do cobrador${clubeCob?' ('+clubeCob+')':''}</label>
       <input id="n-cob" list="l-cob" value="${v('nome_cobrador')}" placeholder="digite o número ao lado"
              oninput="form.nome_cobrador=this.value;form.fonte_cobrador='manual'">
       <datalist id="l-cob">${Object.entries(elCob).map(([n,nm])=>
         `<option value="${nm}">${n}</option>`).join('')}</datalist>
       <div class="achado" id="ach-cob"></div></div>
   </div>
   ${chipsNumero(cobradorAtual())}
   <label class="mini" style="margin-top:7px">pé</label>${opc('pe_cobrador',voc.pes)}

   <h2>Quem defendeu</h2>
   ${gks.length?`<label class="mini">goleiros do ${ladoGol==='mandante'?r.mandante:r.visitante}
      no elenco de ${r.temporada} — clique no que apareceu no vídeo</label>
     <div class="gks">${gks.map(g=>`<button class="${v('camisa_goleiro')===g.numero?'on':''}"
       data-n="${esc(g.numero)}" data-nome="${esc(g.nome)}"
       onclick="usarGoleiro(this.dataset.n,this.dataset.nome)"
       >#${esc(g.numero)} ${esc(g.nome)}</button>`).join('')}
     </div>`
    :`<div class="dica">${v('time_cobrador')?'nenhum goleiro no elenco desta temporada'
        :'escolha primeiro o time do cobrador'}</div>`}
   <div class="camisa" style="margin-top:6px">
     <div><label class="mini">camisa</label>
       <input class="num" id="c-gol" type="number" min="1" max="99" value="${v('camisa_goleiro')}"
              oninput="numMudou('gol')"></div>
     <div><label class="mini">nome do goleiro${clubeGol?' ('+clubeGol+')':''}</label>
       <input id="n-gol" list="l-gol" value="${v('nome_goleiro')}" placeholder="opcional"
              oninput="form.nome_goleiro=this.value;form.fonte_goleiro='manual'">
       <datalist id="l-gol">${Object.entries(elGol).map(([n,nm])=>
         `<option value="${nm}">${n}</option>`).join('')}</datalist>
       <div class="achado" id="ach-gol"></div></div>
   </div>
   <label class="mini" style="margin-top:7px">para que lado o goleiro foi</label>
   ${opc('lado_goleiro',voc.lados,'lados')}
   <div class="dica">a bola foi para <b>${r.lado_chute||'?'}</b> (${r.altura_chute||'?'}) —
     mesmo referencial${v('lado_goleiro')&&v('lado_goleiro')!=='nd'
       ?(v('lado_goleiro')===r.lado_chute?' · <span style="color:var(--vd)">acertou o lado</span>'
                                         :' · <span style="color:var(--vm)">foi para o outro lado</span>'):''}</div>

   <h2>Desfecho</h2>${opc('desfecho',voc.desfechos)}
   <div class="dica">pela lógica: <b>${r.desfecho_derivado}</b></div>

   <h2>Momento do jogo</h2>
   <div class="g3">
     <div><label class="mini">período</label>
       <select onchange="form.periodo=this.value" style="width:100%">
         ${['',...voc.periodos].map(o=>`<option ${v('periodo')===o?'selected':''}>${o}</option>`).join('')}
       </select></div>
     <div><label class="mini">minuto</label>
       <input type="number" min="0" max="130" value="${v('minuto')}" oninput="form.minuto=this.value"></div>
     <div><label class="mini">placar no lance</label>
       <div style="display:flex;gap:3px">
        <input type="number" min="0" style="width:100%" value="${v('placar_mandante_momento')}"
               oninput="form.placar_mandante_momento=this.value" title="${r.mandante}">
        <input type="number" min="0" style="width:100%" value="${v('placar_visitante_momento')}"
               oninput="form.placar_visitante_momento=this.value" title="${r.visitante}"></div></div>
   </div>

   <h2>Observação</h2>
   <textarea rows="2" style="width:100%" oninput="form.observacao=this.value"
     placeholder="o que você viu...">${v('observacao')}</textarea>

   <button class="salvar" onclick="salvar()">Salvar e ir para o próximo <kbd>Ctrl+S</kbd></button>
   <div class="rodape">
     <kbd>←</kbd><kbd>→</kbd> pênalti · <kbd>,</kbd><kbd>.</kbd> frame · <kbd>Z</kbd> zoom+ ·
     <kbd>1..4</kbd> ângulo<br><br>
     Grava em <b>penaltis_jogadores.csv</b>. O labels.csv original nunca é tocado.
   </div>`;
}
// ---------- identificação pelos elencos ----------
// Só a DÚVIDA aparece em destaque. Quando o vídeo tem um cobrador só, o elenco
// já responde e o card é apenas uma confirmação verde.
function identificacaoHTML(id,cands){
  if(!id.disponivel)
    return `<div class="dica">elencos do Transfermarkt indisponíveis — preencha à mão</div>`;
  const av=(id.avisos||[]).length?`<div class="aviso">${id.avisos.map(esc).join('<br>')}</div>`:'';
  if(id.veredito==='resolvido'&&id.cobrador){
    const c=id.cobrador, g=id.goleiro;
    return `<div class="ok-auto">
      <b>identificado pelo elenco:</b> ${c.numero?'#'+esc(c.numero)+' ':''}${esc(c.nome)}
      <small style="color:var(--tx2)">(${esc(c.clube)} · ${esc(c.lado)||'lado ?'})</small>
      ${g?`<br>goleiro: ${g.numero?'#'+esc(g.numero)+' ':''}${esc(g.nome)}`:''}
      <br><button style="margin-top:5px;padding:3px 9px;font-size:11px"
        onclick="usarSugestao()">preencher com estes</button>
      <span style="color:var(--tx2);font-size:10px">só 1 cobrador neste vídeo</span>
     </div>${av}`;
  }
  if(id.veredito==='duvida')
    return `<div class="aviso"><b>DÚVIDA:</b> este vídeo tem ${cands.length} cobradores.
      Leia o número na camisa e escolha:</div>
      <div class="cand">${cands.map((c,i)=>`
        <button class="${v('camisa_cobrador')===c.numero&&v('nome_cobrador')===c.nome?'on':''}"
                onclick="usarCandidato(${i})">
          <span class="n">${esc(c.numero)||'?'}</span>
          <span><b>${esc(c.nome)}</b><small>${esc(c.clube)} · ${esc(c.lado)||'lado ?'}
            ${c.n_penaltis>1?' · '+c.n_penaltis+' pênaltis neste jogo':''}
            ${c.camisa_ambigua?' · camisas '+c.numeros.join(', ')+' na temporada':''}</small></span>
        </button>`).join('')}</div>${av}`;
  return `<div class="dica">este jogo não está no dataset do scraping —
     digite o número e o elenco resolve, se houver</div>${av}`;
}
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// Jogador que trocou de camisa na temporada: o dataset guarda os dois numeros
// e so o video diz qual ele veste neste jogo.
function cobradorAtual(){
  const id=at.ident||{}, nome=v('nome_cobrador');
  return (id.candidatos||[]).find(c=>c.nome===nome) || (nome?null:id.cobrador);
}
function chipsNumero(c){
  if(!c||(c.numeros||[]).length<2)return '';
  return `<div class="gks"><span class="mini" style="align-self:center">
    ${esc(c.nome)} usou ${c.numeros.length} camisas em ${esc(at.reg.temporada)} —
    qual está no vídeo?</span>${c.numeros.map(n=>`<button class="${v('camisa_cobrador')===n?'on':''}"
      data-n="${n}" onclick="usarNumeroCobrador(this.dataset.n)">#${n}</button>`).join('')}</div>`;
}
function usarNumeroCobrador(n){
  form.camisa_cobrador=n; form.fonte_cobrador='numero'; renderForm();
}
function usarCandidato(i){
  const c=at.ident.candidatos[i];
  Object.assign(form,{time_cobrador:c.lado||form.time_cobrador||'',
    camisa_cobrador:c.numero, nome_cobrador:c.nome, pe_cobrador:c.pe||v('pe_cobrador'),
    fonte_cobrador:'numero'});
  renderForm(); toast(`#${c.numero} ${c.nome}`,'bom');
}
function usarSugestao(){
  const c=at.ident.cobrador, g=at.ident.goleiro;
  if(c)Object.assign(form,{time_cobrador:c.lado||'',camisa_cobrador:c.numero,
    nome_cobrador:c.nome,pe_cobrador:c.pe||v('pe_cobrador'),fonte_cobrador:'scraping'});
  if(g)Object.assign(form,{camisa_goleiro:g.numero||'',nome_goleiro:g.nome,
    fonte_goleiro:'scraping'});
  renderForm(); toast('preenchido pelo elenco','bom');
}
function usarGoleiro(num,nome){
  Object.assign(form,{camisa_goleiro:num,nome_goleiro:nome,fonte_goleiro:'numero'});
  renderForm();
}
async function aplicarTodas(){
  if(!confirm('Preencher cobrador e goleiro de TODOS os pênaltis sem ambiguidade?\n'+
              'Os que têm dúvida não são tocados. Só preenche campo vazio.'))return;
  try{
    const r=await api('/api/aplicar-sugestoes',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    toast(`${r.aplicados} pênaltis preenchidos`,'bom');
    await carregarFila(); if(at)abrir(at.reg.penalti_uid);
  }catch(e){toast(e.message,'erro');}
}

// número da camisa -> jogador, no elenco real daquele clube/temporada
async function numMudou(qual){
  const inp=$(qual==='cob'?'#c-cob':'#c-gol'), campo=qual==='cob'?'camisa_cobrador':'camisa_goleiro';
  form[campo]=inp.value;
  const lado=v('time_cobrador'); if(!lado)return;
  const ladoBusca=qual==='cob'?lado:(lado==='mandante'?'visitante':'mandante');
  const alvo=$(qual==='cob'?'#n-cob':'#n-gol'), cx=$('#ach-'+qual);
  const campoNome=qual==='cob'?'nome_cobrador':'nome_goleiro';

  // fallback imediato: o elenco aprendido a mao, enquanto a resposta nao chega
  const el=qual==='cob'?(lado==='mandante'?at.elenco_mandante:at.elenco_visitante)
                       :(lado==='mandante'?at.elenco_visitante:at.elenco_mandante);
  if(el[inp.value]&&!alvo.value){alvo.value=el[inp.value];form[campoNome]=el[inp.value];}
  if(!inp.value){cx.innerHTML='';return;}

  clearTimeout(window._bn); const numero=inp.value;
  window._bn=setTimeout(async()=>{
    try{
      const r=await api(`/api/numero?uid=${at.reg.penalti_uid}&lado=${ladoBusca}&numero=${numero}`);
      if(r.ok&&r.jogadores.length===1){
        const j=r.jogadores[0];
        alvo.value=j.nome; form[campoNome]=j.nome;
        form[qual==='cob'?'fonte_cobrador':'fonte_goleiro']='numero';
        cx.innerHTML=`<span style="color:var(--vd)">✓ ${esc(j.nome)}</span>
          <span style="color:var(--tx2)">· ${esc(j.posicao||'')}</span>`;
      }else if(r.ok){                       // a camisa trocou de dono na temporada
        cx.innerHTML=`<span style="color:var(--am)">${r.jogadores.length} jogadores usaram a #${numero}:</span> `+
          r.jogadores.map(j=>`<button style="padding:1px 6px;font-size:11px"
            data-nome="${esc(j.nome)}" onclick="escolherNome('${qual}',this.dataset.nome)"
            >${esc(j.nome)}</button>`).join(' ');
      }else if((r.aproximados||[]).length){
        cx.innerHTML=`<span style="color:var(--am)">só em outra temporada:</span> `+
          r.aproximados.map(j=>`<button style="padding:1px 6px;font-size:11px"
            data-nome="${esc(j.nome)}" onclick="escolherNome('${qual}',this.dataset.nome)"
            >${esc(j.nome)} (${j.temporada_encontrada})</button>`).join(' ');
      }else{
        cx.innerHTML=`<span style="color:var(--tx2)">${esc(r.erro||'não encontrado')}</span>`;
      }
    }catch(e){cx.innerHTML=`<span style="color:var(--vm)">${esc(e.message)}</span>`;}
  },260);
}
function escolherNome(qual,nome){
  const alvo=$(qual==='cob'?'#n-cob':'#n-gol');
  alvo.value=nome; form[qual==='cob'?'nome_cobrador':'nome_goleiro']=nome;
  form[qual==='cob'?'fonte_cobrador':'fonte_goleiro']='numero';
  $('#ach-'+qual).innerHTML=`<span style="color:var(--vd)">✓ ${esc(nome)}</span>`;
}
async function salvar(){
  const campos=['time_cobrador','camisa_cobrador','nome_cobrador','pe_cobrador','camisa_goleiro',
    'nome_goleiro','lado_goleiro','desfecho','periodo','minuto','placar_mandante_momento',
    'placar_visitante_momento','observacao','fonte_cobrador','fonte_goleiro'];
  const corpo={penalti_uid:at.reg.penalti_uid};
  campos.forEach(c=>corpo[c]=v(c));
  try{
    const r=await api('/api/rotular',{method:'POST',headers:{'Content-Type':'application/json'},
                                      body:JSON.stringify(corpo)});
    toast('salvo · '+r.status,'bom');
    const prox=at.proximo; await carregarFila(); if(prox)abrir(prox);
  }catch(e){toast(e.message,'erro');}
}
document.addEventListener('keydown',e=>{
  if(e.ctrlKey&&e.key.toLowerCase()==='s'){e.preventDefault();salvar();return;}
  if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return;
  if(!at)return;
  const k=e.key.toLowerCase();
  if(k==='arrowleft'){e.preventDefault();ir('anterior');}
  if(k==='arrowright'){e.preventDefault();ir('proximo');}
  if(k===','){passo(-1);} if(k==='.'){passo(1);}
  if(k==='z'){fator=fator>=12?2:fator+2;render();}
  if(/^[1-4]$/.test(k)&&+k<=at.angulos.length)setAng(+k-1);
});
['f-temp','f-status','f-clube','f-ident'].forEach(i=>$('#'+i).onchange=carregarFila);
carregarFila().then(()=>{if(fila.length)abrir(fila[0].penalti_uid);});
</script></body></html>
"""


# ---------------------------------------------------------------------------
def preparar(paths: J.PathsJog, source_dir: str) -> None:
    """Indice de videos e rotulos de origem. Separado para o run.py reusar."""
    _ctx["paths"] = paths
    _ctx["indice"] = J.indice_videos(paths, source_dir)
    _, origem = V.ler_csv(paths.csv_origem)
    _ctx["rotulos"] = {V.row_id(l): l for l in origem}


def servir(paths: J.PathsJog, source_dir: str, host: str = "127.0.0.1",
           porta: int = 5006, abrir_navegador: bool = True) -> int:
    if not os.path.isfile(paths.csv):
        print("Dataset nao construido. Rode antes:  python jogadores.py construir")
        return 1

    preparar(paths, source_dir)

    linhas = J.carregar(paths)
    pend = sum(1 for r in linhas if r["status_rotulo"] != "completo")
    url = f"http://{host}:{porta}"
    print(f"\n  Cobrador & Goleiro (Brasileirao)  ->  {url}")
    print(f"  {pend} de {len(linhas)} penaltis a rotular")
    print(f"  gravando em: {paths.csv}")
    print(f"  origem (so leitura): {paths.csv_origem}\n")
    if abrir_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=porta, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Interface de rotulagem cobrador/goleiro")
    ap.add_argument("--output-base", default=V.DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=V.DEFAULT_SOURCE_DIR)
    ap.add_argument("--porta", type=int, default=5006)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    raise SystemExit(servir(J.PathsJog(a.output_base), a.source_dir, a.host, a.porta,
                            not a.sem_navegador))
