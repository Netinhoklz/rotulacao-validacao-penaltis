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
import shutil
import subprocess
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


_FIM_CONFRONTO = (r"(?=\s+(?:MELHORES\s+MOMENTOS|GOLS|COMPACTO|"
                  r"HIGHLIGHTS|RESUMO|P[ÊE]NALTIS|JOGO\s+COMPLETO|AO\s+VIVO|"
                  r"\d+\s+RO(?:DADA)?|BRASILEIR[ÃA]O|COPA\s+DO\s+BRASIL)\b|$)")
_JOGO_RE = re.compile(
    r"^(?P<casa>.+?)\s+(?P<gols_casa>\d{1,2})\s*[xX×]\s*"
    r"(?P<gols_fora>\d{1,2})\s+(?P<fora>.+?)" + _FIM_CONFRONTO,
    re.IGNORECASE,
)
_DISPUTA_RE = re.compile(
    r"^(?P<casa>.+?)\s+(?P<gols_casa>\d{1,2})\s+(?P<pen_casa>\d{1,2})"
    r"\s*[xX×]\s*(?P<pen_fora>\d{1,2})\s+(?P<gols_fora>\d{1,2})"
    r"\s+(?P<fora>.+?)" + _FIM_CONFRONTO, re.IGNORECASE,
)


def _jogo_do_arquivo(nome: str) -> dict[str, str]:
    """Exibe o confronto do nome do vídeo sem inventar dados ausentes."""
    arquivo = os.path.basename(nome)
    stem = re.sub(r"\.f\d+$", "", os.path.splitext(arquivo)[0], flags=re.IGNORECASE)
    stem = " ".join(stem.replace("_", " ").split())
    disputa = _DISPUTA_RE.match(stem)
    if disputa:
        g = disputa.groupdict()
        casa = re.sub(r"^DISPUTA DE PENALTIS\s+", "", g["casa"], flags=re.IGNORECASE)
        jogo = f"{casa} {g['gols_casa']} × {g['gols_fora']} {g['fora']}"
        detalhe = f"Pênaltis: {g['pen_casa']} × {g['pen_fora']}"
        sufixo = stem[disputa.end():].strip()
        return {"jogo": jogo, "detalhe_video": detalhe + (f" · {sufixo}" if sufixo else "")}
    achado = _JOGO_RE.match(stem)
    if not achado:
        return {"jogo": stem or arquivo, "detalhe_video": ""}
    g = achado.groupdict()
    if g["casa"].split()[-1].isdigit() or g["fora"].split()[0].isdigit():
        return {"jogo": stem or arquivo, "detalhe_video": ""}
    jogo = f"{g['casa']} {g['gols_casa']} × {g['gols_fora']} {g['fora']}"
    return {"jogo": jogo, "detalhe_video": stem[achado.end():].strip()}


def _item(r: dict) -> dict:
    """Versao enxuta de um rotulo, para a lista da esquerda."""
    a = r["atual"]
    return {
        "uid":          r["uid"],
        "video_file":   a.get("video_file", ""),
        **_jogo_do_arquivo(a.get("video_file", "")),
        "penalty_id":   a.get("penalty_id", ""),
        "competicao":   r["competicao"],
        "competicao_label": r["competicao_label"],
        "region":       a.get("region", ""),
        "region_label": a.get("region_label", ""),
        "camera_type":  a.get("camera_type", ""),
        "is_goal":      R.is_goal(a),
        "chute_time_s": round(R.num(a, "chute_time_s"), 2),
        "status":       r["status"],
        "alterado":     bool(r["alteracoes"]),
        "revisado_em":  r["revisado_em"],
    }


_fps_video: dict[str, float] = {}


def _fps_do_video(caminho: Optional[str]) -> float:
    """FPS lido do proprio video.

    O fps do rotulo vem de uma divisao entre tempos arredondados a 4 casas
    (frames / segundos). Em cobrancas curtas o erro relativo e grande e, ao
    multiplicar por um frame na casa dos milhares, vira frame inteiro de
    desvio na hora de buscar no <video>. A taxa do container nao tem esse
    problema. Devolve 0.0 quando nao da para ler.
    """
    if not caminho:
        return 0.0
    if caminho not in _fps_video:
        fps = 0.0
        try:
            import cv2
            cap = cv2.VideoCapture(caminho)
            if cap.isOpened():
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            cap.release()
        except Exception:
            fps = 0.0
        _fps_video[caminho] = fps if 5.0 < fps < 240.0 else 0.0
    return _fps_video[caminho]


def _detalhe(r: dict, base: R.Base) -> dict:
    a, o = r["atual"], r["original"]
    video_path = indice().get(a.get("video_file", ""))
    fps = _fps_do_video(video_path) or R.fps_da_linha(a)
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
            "source_folder": a.get("source_folder", ""),
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
        "clip_existe":      os.path.isfile(R.caminho_local(paths(), a.get("clip_path", ""))),
        "png_inicio":       os.path.isfile(R.caminho_local(paths(), a.get("frame_inicio_path", ""))),
        "png_chute":        os.path.isfile(R.caminho_local(paths(), a.get("frame_chute_path", ""))),
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
    # inicio/final mudaram -> clip e frames exportados ficam defasados; regrava
    if any(m.startswith(("inicio_frame ", "chute_frame ")) for m in mudancas):
        mudancas += _regerar_midia(d["uid"])
    return jsonify({"ok": True, "mudancas": mudancas})


# ---------------------------------------------------------------------------
# Regeracao do clip e dos frames exportados a partir do frame corrigido
# ---------------------------------------------------------------------------
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")


def _dentro_da_saida(caminho: str) -> bool:
    caminho = os.path.abspath(caminho or "")
    return bool(caminho) and caminho.startswith(paths().output_base + os.sep)


def _cortar_clip(video: str, ini: int, fim: int, fps: float, destino: str) -> bool:
    """Clip [ini, fim] inclusivo, cortado pelo MEIO dos frames.

    -ss em (ini-0.5)/fps mantem o frame ini e derruba o anterior; dai em
    diante -frames:v conta exatamente fim-ini+1 frames. Cortar por tempo
    (-t) deixava o clip ora 1 frame curto, ora 1 frame longo.
    """
    tmp = destino + ".tmp.mp4"
    ok = False
    if FFMPEG_BIN:
        cmd = [FFMPEG_BIN, "-y", "-loglevel", "error",
               "-ss", f"{max(ini - 0.5, 0) / fps:.4f}", "-i", video,
               "-frames:v", str(fim - ini + 1), "-an",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ok = r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
        except (OSError, subprocess.SubprocessError):
            ok = False
    if not ok:                                   # sem ffmpeg: OpenCV, frame a frame
        cv2 = _cv2()
        cap = cv2.VideoCapture(video)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        wr = None
        for fourcc_tag in ("avc1", "H264", "mp4v"):
            wr = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*fourcc_tag), fps, (w, h))
            if wr.isOpened():
                break
        if not wr or not wr.isOpened():
            cap.release()
            return False
        cap.set(cv2.CAP_PROP_POS_FRAMES, ini)
        n = ini
        while n <= fim:
            lido, img = cap.read()
            if not lido:
                break
            wr.write(img)
            n += 1
        cap.release()
        wr.release()
        ok = n > ini and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
    if ok:
        os.replace(tmp, destino)
    elif os.path.isfile(tmp):
        os.remove(tmp)
    return ok


def _gravar_frame(video: str, n: int, destino: str) -> bool:
    cv2 = _cv2()
    with _lock:
        cap = cv2.VideoCapture(video)
        cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ok, img = cap.read()
        cap.release()
    if not ok:
        return False
    tmp = destino + ".tmp.jpg"
    if not cv2.imwrite(tmp, img, [cv2.IMWRITE_JPEG_QUALITY, 92]):
        return False
    os.replace(tmp, destino)
    return True


def _regerar_midia(uid: str) -> list[str]:
    """Regrava clip e frames _inicio/_chute do rotulo com os frames atuais.

    Escreve nos caminhos que o proprio rotulo aponta (clips/ e frames/ da
    saida), nunca no video original nem no labels.csv. Devolve o que fez,
    para entrar na mesma lista de mudancas do save.
    """
    try:
        a = R.carregar(paths()).get(uid)["atual"]
    except R.RevisaoErro:
        return ["midia nao regerada: rotulo nao encontrado"]
    video = indice().get(a.get("video_file", ""))
    if not video or not os.path.isfile(video):
        return ["midia nao regerada: video original fora do indice"]
    ini, fim = int(R.num(a, "inicio_frame")), int(R.num(a, "chute_frame"))
    fps = _fps_do_video(video) or R.fps_da_linha(a)

    feito, falhou = [], []
    for rotulo, campo, acao in (
        ("clip",         "clip_path",         lambda d: _cortar_clip(video, ini, fim, fps, d)),
        ("frame inicio", "frame_inicio_path", lambda d: _gravar_frame(video, ini, d)),
        ("frame chute",  "frame_chute_path",  lambda d: _gravar_frame(video, fim, d)),
    ):
        destino = R.caminho_local(paths(), a.get(campo, ""))
        if not _dentro_da_saida(destino):
            continue                             # rotulo sem midia exportada
        try:
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            (feito if acao(destino) else falhou).append(rotulo)
        except Exception:
            falhou.append(rotulo)
    out = []
    if feito:
        out.append("regravado: " + ", ".join(feito))
    if falhou:
        out.append("FALHOU ao regravar: " + ", ".join(falhou))
    return out


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
# Midia: video original (com Range), frame exato, clip e PNGs
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


def _arquivo_do_registro(uid: str, campo: str) -> str:
    """Caminho gravado no rotulo, restrito a pasta de saida por seguranca."""
    base = R.carregar(paths())
    try:
        reg = base.get(uid)
    except R.RevisaoErro:
        abort(404)
    # o CSV pode ter vindo de outra maquina (E:\\...): traduz para a pasta daqui
    caminho = R.caminho_local(paths(), reg["atual"].get(campo, ""))
    if not os.path.isfile(caminho):
        abort(404)
    return caminho


def _garantir_h264(caminho: str) -> str:
    """Garante que o clipe MP4 esteja codificado em H.264 (avc1) para tocar no Safari/Chrome no macOS."""
    if not caminho or not os.path.isfile(caminho):
        return caminho
    try:
        cv2 = _cv2()
        with _lock:
            cap = cv2.VideoCapture(caminho)
            if not cap.isOpened():
                return caminho
            fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
            fourcc_str = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)]).lower()
            if fourcc_str in ("avc1", "h264", "x264"):
                cap.release()
                return caminho

            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            frames = []
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                frames.append(frame)
            cap.release()

        if not frames:
            return caminho

        tmp = caminho + ".avc1.tmp.mp4"
        wr = cv2.VideoWriter(tmp, cv2.VideoWriter_fourcc(*"avc1"), fps, (w, h))
        if not wr.isOpened():
            return caminho
        for f in frames:
            wr.write(f)
        wr.release()

        if os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, caminho)
    except Exception:
        if "tmp" in locals() and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return caminho


@app.get("/midia/clip")
def midia_clip():
    caminho = _arquivo_do_registro(request.args.get("uid", ""), "clip_path")
    caminho = _garantir_h264(caminho)
    return _servir_com_range(caminho)


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
  .item .jogo{font-size:13px;font-weight:650;line-height:1.3;margin-top:5px;
              overflow-wrap:anywhere}
  .item .vid{margin-top:3px}

  /* ---- palco ---- */
  .jogo-cab{border:1px solid var(--borda);border-left:4px solid var(--azul);
            background:var(--painel);border-radius:8px;padding:12px 14px;margin-bottom:10px}
  .jogo-cab h1{font-size:21px;line-height:1.2;margin:0 0 7px;overflow-wrap:anywhere}
  .jogo-meta{display:flex;flex-wrap:wrap;gap:5px;align-items:center}
  .jogo-arquivo{font-size:11px;color:var(--txt2);margin-top:7px;overflow-wrap:anywhere}
  /* ---- passo a passo ---- */
  .passo-cab{background:var(--painel);border:1px solid var(--borda);border-radius:9px;
             padding:10px 14px;margin-bottom:8px}
  .passos{display:flex;gap:6px;margin-bottom:9px}
  .passos button{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;
                 font-size:11px;padding:6px 4px;color:var(--txt2)}
  .passos button .n{display:inline-flex;width:18px;height:18px;border-radius:50%;align-items:center;
                    justify-content:center;font-weight:700;background:var(--painel2);border:1px solid var(--borda)}
  .passos button.feita{color:var(--verde)}
  .passos button.feita .n{background:var(--verde);color:#11111b;border-color:var(--verde)}
  .passos button.on{color:var(--txt);border-color:var(--azul);background:#1e2a3a}
  .passos button.on .n{background:var(--azul);color:#11111b;border-color:var(--azul)}
  .passos button:disabled{opacity:.4;cursor:not-allowed}
  .passo-titulo{font-size:16px;font-weight:700;margin:2px 0 3px}
  .passo-titulo small{font-size:11px;color:var(--txt2);font-weight:400;margin-left:8px}
  .passo-instr{font-size:13px;color:var(--txt2);line-height:1.55}
  .passo-acao{margin:10px 0 14px}
  .btn-passo{width:100%;padding:14px;font-size:15px;font-weight:700;border-radius:9px;
             background:#1e3a2a;border-color:var(--verde);color:var(--verde)}
  .btn-passo:disabled{opacity:.45;cursor:not-allowed}
  .passo-dica{margin-top:7px;font-size:12px;color:var(--amarelo);text-align:center}
  .passo-voltar{margin-top:6px;font-size:11px;color:var(--txt2);text-align:center}
  .passo-voltar a{color:var(--txt2);cursor:pointer;text-decoration:underline}
  .p5{display:grid;grid-template-columns:1fr 1fr;gap:14px;background:var(--painel);
      border:1px solid var(--borda);border-radius:9px;padding:12px;margin-top:8px}
  .p5 .grade button,.p5 .fora button{padding:13px 4px;font-size:12px}
  .p5 .dupla button{padding:18px;font-size:15px;font-weight:700}
  #palco{position:relative;background:#000;border:1px solid var(--borda);border-radius:8px;
         overflow:hidden;display:flex;align-items:center;justify-content:center;
         min-height:300px;max-height:72vh}

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
  #palco video,#palco img{max-width:100%;max-height:72vh;display:block}
  .barra{display:flex;flex-wrap:wrap;gap:5px;align-items:center;margin:8px 0}
  .barra button{font-size:12px;padding:5px 9px}
  .frame-pos{font-weight:700;color:var(--txt);min-width:160px;text-align:center}
  .frame-mark{border-color:var(--azul);color:var(--azul)}
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
  :focus-visible{outline:2px solid var(--azul);outline-offset:2px}
  @media(max-width:1100px){#app{grid-template-columns:260px minmax(0,1fr) 310px}}
  @media(max-width:800px){body{height:auto;overflow:auto}#app{height:auto;display:block}
    .col{overflow:visible;border-left:0!important;border-bottom:1px solid var(--borda)}
    #palco{max-height:none}}
  @media(max-width:560px){.marcadores-grid{grid-template-columns:1fr}}
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
    <div class="passo-cab" id="passo-cab"></div>
    <div id="palco"><div style="color:#585b70;padding:60px">selecione um rótulo à esquerda</div></div>
    <div class="barra" id="controles"></div>
    <div id="passo-extra"></div>
    <div class="passo-acao" id="passo-acao"></div>
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
let itens = [], atual = null, modoAtual = 'frame', frameAtual = 0, limite = 300;
// Sequência obrigatória: 1 início → 2 final → 3 clip → 4 vídeo com régua →
// 5 região e gol. etapa = até onde o revisor chegou neste rótulo; alvo = qual
// frame a aba de frame está ajustando; feito = provas de que cada passo foi
// feito de verdade (andou frame a frame, viu o clip até o fim, usou a régua,
// clicou região e gol). Nada disso é gravado: zera ao abrir outro rótulo.
let etapa = 1, alvo = 'inicio', conferindo = false, manterEtapa = false;
let feito = {clip:false, regua:false, regiao:false, gol:false};
let statusGlobal = {}, filtroRegiao = '', sel = {region:'', gol:false};
let frames = {inicio:0, chute:0, fps:30};

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
      <div class="jogo">${esc(i.jogo)}</div>
      <div class="vid">${esc(i.competicao_label)} · pênalti ${esc(i.penalty_id)} · ${esc(i.video_file)}</div>
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
  frames = {inicio:r.inicio_frame, chute:r.chute_frame, fps:r.fps};
  if(!manterEtapa){ etapa = 1; alvo = 'inicio'; conferindo = false; modoAtual = 'frame'; frameAtual = r.inicio_frame;
    feito = {clip:false, regua:false, regiao:false, gol:false}; }
  else if(modoAtual === 'frame') frameAtual = frames[alvo];
  manterEtapa = false;
  sel = {region: r.region, gol: r.is_goal};
  reguaAoTrocarLance();
  const i = idx();
  $('#cab').innerHTML = `
    <div class="jogo-cab">
      <h1>${esc(r.jogo)}</h1>
      ${r.detalhe_video?`<div style="color:var(--txt2);font-size:12px;margin-bottom:7px">${esc(r.detalhe_video)}</div>`:''}
      <div class="jogo-meta"><span class="chip">${esc(r.competicao_label)}</span>
        <span class="chip">Pênalti ${esc(r.penalty_id)}</span>
        <span class="chip">Chute em ${esc(r.chute_time_s)}s do vídeo</span></div>
      <div class="jogo-arquivo" title="Nome completo do arquivo">${esc(r.video_file)}</div>
      ${r.source_folder?`<div class="jogo-arquivo">Pasta: ${esc(r.source_folder)}</div>`:''}
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:5px">
      <b style="font-size:15px">${esc(r.region_label || r.region)}</b>
      <span class="chip">${selo(r.status, true)}${r.revisado_em
        ? ' <span style="color:var(--txt2)">em ' + esc(r.revisado_em.replace('T',' ')) + '</span>' : ''}</span>
      <span style="margin-left:auto;display:flex;gap:4px;align-items:center">
        <span class="chip" ${i<0?'style="color:var(--amarelo)"':''}>${
          i<0 ? 'fora do filtro · '+itens.length : (i+1)+'/'+itens.length}</span>
        <button onclick="vizinho(-1)" title="Rótulo anterior (PgUp)" ${i===0||!itens.length?'disabled':''}>&larr; rótulo</button>
        <button onclick="vizinho(1)" title="Próximo rótulo (PgDn)" ${(i>=0&&i>=itens.length-1)||!itens.length?'disabled':''}>rótulo &rarr;</button>
      </span>
    </div>
    ${!atual.video_disponivel?'<div class="aviso">vídeo original não encontrado no índice — use o clip ou os PNGs</div>':''}`;
  renderLista(); render(); renderEditor(); renderPasso();
}

function modo(m){ modoAtual = m; render(); }

const ETAPAS = ['inicio','chute','clip','video','aprovar'];
const NOME_ETAPA = {1:'confirmar o frame de início', 2:'confirmar o frame final', 3:'conferir o clip',
                    4:'ver o vídeo com a régua', 5:'confirmar região e gol'};
function etapaVisivel(){
  return modoAtual==='frame' ? (alvo==='inicio'?1:2) : modoAtual==='clip' ? 3
       : modoAtual==='video' ? (conferindo?5:4) : 0;
}
// o que falta para o passo visível poder ser confirmado
function prontoEtapa(vis){
  if(vis===1||vis===2) return {pronto:true, dica:'',
    rotulo:vis===1 ? 'Confirmo: este é o frame de início' : 'Confirmo: este é o frame final (chute)'};
  if(vis===3){ const ok=feito.clip||!atual.clip_existe;
    return {pronto:ok, rotulo:'O clip está correto → ver o vídeo', dica:ok?'':'assista o clip até o fim'}; }
  if(vis===4) return {pronto:feito.regua, rotulo:'Vi onde a bola foi → confirmar região e gol',
                      dica:feito.regua?'':'aperte E e clique nas 4 traves para colocar a grade do gol sobre o vídeo'};
  const ok=feito.regiao&&feito.gol;
  return {pronto:ok, rotulo:'Aprovar e ir ao próximo rótulo',
          dica:ok?'':'clique na região onde a bola foi e em SIM/NÃO, logo abaixo'};
}
const PASSOS = [{t:'Frame de início', curto:'Início'}, {t:'Frame final (chute)', curto:'Final'},
                {t:'Clip da cobrança', curto:'Clip'}, {t:'Vídeo com a régua', curto:'Vídeo'},
                {t:'Região e gol', curto:'Região'}];
function instrucao(vis){
  if(vis===1) return 'Este é o <b>primeiro frame da cobrança</b> — o cobrador parado, antes da corrida? Se não for, ande com <kbd>←</kbd> <kbd>→</kbd> (Shift = 10) até ele. Quando estiver certo, clique em Confirmo.';
  if(vis===2) return 'Este é o <b>frame do chute</b> — a bola saindo do pé? Se não for, ande com <kbd>←</kbd> <kbd>→</kbd> até ele. Quando estiver certo, clique em Confirmo.';
  if(vis===3) return atual.clip_existe
    ? 'Este clip vai do início ao final que você confirmou. <b>Assista até o fim</b>: mostra a cobrança inteira, do cobrador parado até o chute?'
    : 'O clip deste rótulo não existe no disco. Confirme para seguir ao vídeo.';
  if(vis===4) return 'Veja <b>onde a bola foi</b>: o vídeo começa no início da cobrança e para sozinho 30 s depois. Aperte <kbd>E</kbd> e clique nas <b>4 traves</b> para colocar a grade do gol sobre a imagem.';
  return 'Com a grade sobre o gol, confirme <b>onde a bola foi</b> e <b>se foi gol</b> — mesmo que já esteja certo, clique nos dois.';
}
function painelRegiao(){
  const lab = c => (atual.regioes.find(x=>x.code===c)||{label:c}).label;
  const bt = c => `<button class="gol ${sel.region===c?'on':''}" data-reg="${c}" onclick="setReg('${c}')">${lab(c).replace('Gol - ','')}</button>`;
  return `<div class="p5">
    <div><h2>Onde a bola foi?</h2>
      <div class="grade">${['gol_topo_esquerdo','gol_topo_centro','gol_topo_direito','gol_meio_esquerdo','gol_meio_centro',
        'gol_meio_direito','gol_baixo_esquerdo','gol_baixo_centro','gol_baixo_direito'].map(bt).join('')}</div>
      <div class="fora">${FORA.map(c=>`<button data-reg="${c}" class="${sel.region===c?'on':''}"
        onclick="setReg('${c}')">${lab(c).replace('Fora - ','Fora ')}</button>`).join('')}</div>
    </div>
    <div><h2>Foi gol?</h2>
      <div class="dupla">
        <button class="${sel.gol?'on-sim':''}" onclick="setGol(true)">SIM</button>
        <button class="${!sel.gol?'on-nao':''}" onclick="setGol(false)">NÃO</button>
      </div>
      <div class="passo-instr" style="margin-top:8px">gravado: <b>${esc(atual.reg.region_label||atual.reg.region)}</b> · ${atual.reg.is_goal?'gol':'não foi gol'}</div>
    </div></div>`;
}
// cabeçalho do passo, painel extra (região/gol no 5) e o botão único de confirmar.
// Roda a cada ação do revisor: é barato e mantém tudo coerente com `feito`.
function renderPasso(){
  if(!atual) return;
  const vis = etapaVisivel() || etapa;
  const p = prontoEtapa(vis);
  $('#passo-cab').innerHTML = `
    <div class="passos">${PASSOS.map((x,i)=>{ const n=i+1;
      return `<button onclick="irEtapa(${n})" class="${n===vis?'on':''} ${n<etapa?'feita':''}" ${n>etapa?'disabled':''}
        title="${n>etapa?'conclua o passo '+etapa+' primeiro':x.t}"><span class="n">${n<etapa?'✓':n}</span>${x.curto}</button>`; }).join('')}</div>
    <div class="passo-titulo">Passo ${vis} de 5 · ${PASSOS[vis-1].t}${vis<=2
      ? `<small>gravado: frame ${vis===1?atual.reg.inicio_frame:atual.reg.chute_frame}</small>` : ''}</div>
    <div class="passo-instr">${instrucao(vis)}</div>`;
  $('#passo-extra').innerHTML = vis===5 ? painelRegiao() : '';
  const voltar = vis===3
    ? `<div class="passo-voltar">Não está certo? <a onclick="irEtapa(1)">voltar ao início</a> · <a onclick="irEtapa(2)">voltar ao final</a></div>`
    : vis>=4 ? `<div class="passo-voltar"><a onclick="irEtapa(3)">rever o clip</a> · <a onclick="irEtapa(2)">voltar aos frames</a></div>` : '';
  $('#passo-acao').innerHTML = `
    <button class="btn-passo" id="btn-confirma" onclick="avancar()" ${p.pronto?'':'disabled'}>${p.rotulo} <kbd>Enter</kbd></button>
    ${p.dica?`<div class="passo-dica">${p.dica}</div>`:''}${voltar}`;
}
// vai para uma etapa já liberada (voltar é sempre permitido; pular, não)
async function irEtapa(n){
  if(!atual) return;
  if(n>etapa){ toast(`Primeiro conclua o passo ${etapa}: ${NOME_ETAPA[etapa]} (Enter avança)`, 'erro'); return; }
  conferindo = n===5;
  if(n===1){ alvo='inicio'; frameAtual=frames.inicio; modo('frame'); }
  else if(n===2){ alvo='chute'; frameAtual=frames.chute; modo('frame'); }
  else if(n===3){
    // frames corrigidos: grava (e regera o clip) antes de mostrá-lo
    if(haPendencias()){ manterEtapa = true; modoAtual = 'clip';
      if(!await salvar()){ manterEtapa = false; etapa = 2; irEtapa(2); } }
    else modo('clip');
  }
  else if(modoAtual!=='video') modo('video');
  else renderPasso();
  if(n===5){ const acao = $('#passo-acao'); if(acao) acao.scrollIntoView({block:'end'}); }  // grade, SIM/NÃO e Aprovar à vista
}
// Enter: confirma a etapa visível e abre a seguinte
async function avancar(){
  if(!atual) return;
  const vis = etapaVisivel() || etapa;
  const p = prontoEtapa(vis);
  if(!p.pronto){ toast(p.dica, 'erro'); return; }
  if(vis<=2){
    if(!atual.video_disponivel){ toast('Vídeo original indisponível para conferir frames', 'erro'); return; }
    const qual = vis===1 ? 'inicio' : 'chute';
    const mudou = frames[qual] !== frameAtual;
    alterarFrame(qual, frameAtual, false);
    // frame alterado: clip, vídeo e região precisam ser conferidos de novo
    if(mudou){ feito.clip = feito.regua = feito.regiao = feito.gol = false; }
    etapa = mudou ? vis+1 : Math.max(etapa, vis+1);
    toast(qual==='inicio' ? `Início confirmado no frame ${frameAtual}. Agora o frame final.`
                          : `Final confirmado no frame ${frameAtual}. Confira o clip.`);
    await irEtapa(vis+1);
  } else if(vis===3){ etapa = Math.max(etapa, 4); await irEtapa(4); }
  else if(vis===4){ etapa = 5; await irEtapa(5); }
  else aprovar();
}
function haPendencias(){
  const r = atual.reg;
  return frames.inicio!==r.inicio_frame || frames.chute!==r.chute_frame ||
    sel.region!==r.region || sel.gol!==r.is_goal || $('#ed-camera').value!==r.camera_type ||
    $('#ed-obs').value!==r.observations;
}
function framesValidos(){
  return Number.isInteger(frames.inicio) && frames.inicio>=0 && Number.isInteger(frames.chute) &&
    frames.chute>frames.inicio && frames.fps>0;
}

function render(){
  if(!atual) return;
  const r = atual.reg, nome = encodeURIComponent(r.video_file), uid = encodeURIComponent(r.uid);

  if(modoAtual === 'frame'){
    $('#palco').innerHTML = atual.video_disponivel
      ? `<img id="mid" src="/midia/frame?nome=${nome}&n=${frameAtual}" alt="Frame ${frameAtual} do jogo"
          onerror="toast('Este frame não foi encontrado no vídeo.', 'erro')">`
      : '<div style="color:var(--txt2);padding:60px">vídeo original indisponível para conferir frames</div>';
    $('#controles').innerHTML = `
      <button onclick="passo(-10)" aria-label="Voltar 10 frames">−10</button>
      <button onclick="passo(-1)" aria-label="Voltar 1 frame">−1</button>
      <span class="chip frame-pos">frame <b id="fnum">${frameAtual}</b> · <span id="ftime">${tempoFrame(frameAtual)}</span> s</span>
      <button onclick="passo(1)" aria-label="Avançar 1 frame">+1</button>
      <button onclick="passo(10)" aria-label="Avançar 10 frames">+10</button>
      <span class="chip">ir ao frame <input id="ir-frame" type="number" min="0" style="width:84px"
        value="${frameAtual}" onchange="irFrame(this.value)"></span>
      <button onclick="irFrame(atual.reg[alvo==='inicio'?'inicio_frame':'chute_frame'])">voltar ao gravado</button>`;
  } else if(modoAtual === 'clip'){
    $('#palco').innerHTML = atual.clip_existe
      ? `<video id="mid" src="/midia/clip?uid=${uid}&v=${encodeURIComponent(r.revisado_em||'')}" controls autoplay loop muted playsinline></video>`
      : '<div style="color:var(--vermelho);padding:60px">clip não encontrado no disco</div>';
    $('#controles').innerHTML = velocidades() + ` <button onclick="marcarClipVisto()" style="margin-left:12px;opacity:0.85">marcar como conferido</button>`;
    // prova de que o clip foi visto: chegou ao fim, tocou ou interagiu
    const vc = $('#mid');
    if(vc && vc.tagName==='VIDEO'){
      let ult = 0;
      const visto = ()=>{ if(!feito.clip){ feito.clip = true; renderPasso(); } };
      window.marcarClipVisto = visto;
      vc.addEventListener('ended', visto);
      vc.addEventListener('click', visto);
      vc.addEventListener('play', ()=>{ setTimeout(visto, 1500); });
      vc.addEventListener('timeupdate', ()=>{
        if(vc.duration && (vc.currentTime+0.3 >= vc.duration || (vc.currentTime < ult && ult+0.6 >= vc.duration))) visto();
        ult = vc.currentTime; });
      vc.addEventListener('error', (e)=>{
        console.warn('Erro ao carregar clipe no navegador:', e);
        visto();
        toast('Não foi possível decodificar este clipe no navegador. Você pode prosseguir para o vídeo original.', 'aviso');
      });
      // Fallback para Safari/macOS caso o clipe seja muito curto (< 0.5s) ou autoplay seja bloqueado
      setTimeout(()=>{ if(modoAtual==='clip') visto(); }, 2500);
    }
  } else {
    if(!atual.video_disponivel){
      $('#palco').innerHTML = '<div style="color:#585b70;padding:60px">sem vídeo original</div>';
      $('#controles').innerHTML = '';
    } else {
      $('#palco').innerHTML = `<video id="mid" src="/midia/video?nome=${nome}" controls preload="metadata" autoplay></video>`;
      // janela de 30 s a partir do início da cobrança: começa nele e pausa
      // (uma vez) ao chegar no fim dela — dá para ver onde a bola foi
      const v0 = $('#mid'), t0 = tempoDoFrame(frames.inicio), tFim = t0 + 30;
      let avisou = false;
      v0.addEventListener('loadedmetadata', ()=>{ v0.currentTime = t0; }, {once:true});
      v0.addEventListener('timeupdate', ()=>{
        if(!avisou && v0.currentTime >= tFim){ avisou = true; v0.pause(); toast('Fim dos 30 s a partir do início da cobrança.'); } });
      $('#controles').innerHTML = `
        <button onclick="irFrameNoVideo(frames.inicio)">Início da cobrança</button>
        <button onclick="irFrameNoVideo(frames.chute)">Momento do chute</button>
        <button onclick="pular(-1)">-1s</button><button onclick="pular(1)">+1s</button>
        ${velocidades()}
        <button class="frame-mark" onclick="reguaAlternar()">Régua das traves <kbd>E</kbd></button>`;
    }
  }
  renderIrmaos();
  renderPasso();
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
function tempoFrame(n){ return (n / (frames.fps || 30)).toFixed(2); }
// Buscar no <video> pelo MEIO do frame: chute_time_s = N/fps cai rente a
// fronteira e o navegador mostra N-1 em ~40% dos rotulos (fps 29,97 arredondado
// a 4 casas no CSV). (N+0.5)/fps cai sempre dentro do frame N.
function tempoDoFrame(n){ return Math.max((Number(n) + 0.5) / (frames.fps || 30), 0); }
function irFrameNoVideo(n){ irPara(tempoDoFrame(n)); }
// frame que o <video> esta mostrando: N ocupa [N/fps, (N+1)/fps), logo floor;
// o epsilon cobre o erro de ponto flutuante quando currentTime cai exato em N/fps.
function frameNoVideo(v){ return Math.max(Math.floor(v.currentTime * (frames.fps||30) + 1e-4), 0); }
function irFrame(n){
  if(!atual.video_disponivel){ toast('Vídeo original indisponível para conferir frames', 'erro'); return; }
  if(String(n).trim()==='' || !Number.isInteger(Number(n)) || Number(n)<0){
    toast('Informe um número de frame válido.', 'erro'); return;
  }
  frameAtual = Number(n);
  if(modoAtual!=='frame') return modo('frame');
  $('#mid').src = `/midia/frame?nome=${encodeURIComponent(atual.reg.video_file)}&n=${frameAtual}`;
  $('#mid').alt = `Frame ${frameAtual} do jogo`;
  $('#fnum').textContent = frameAtual; $('#ftime').textContent = tempoFrame(frameAtual);
  const inp = $('#ir-frame'); if(inp) inp.value = frameAtual; }
function passo(d){ irFrame(Math.max(frameAtual + d, 0)); }
function alterarFrame(qual, valor, ver=true){
  const n = Number(valor);
  if(!Number.isInteger(n) || n<0) return;
  frames[qual] = n;
  if(ver) irFrame(n);
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

// marcar as traves pede imagem parada: pausa o vídeo ao entrar na calibração
// e a cada clique, e NÃO retoma depois do 4º ponto — quem decide é o revisor
function reguaPausar(){ const m=reguaMidia(); if(m && m.tagName==='VIDEO' && !m.paused) m.pause(); }
function reguaAlternar(){          // tecla E
  if(!atual) return;
  if(regua.modo==='off'){
    const p=reguaLer();
    if(p){ regua.pontos=p; regua.modo='on'; }
    else { regua.pontos=[]; regua.modo='calibrando'; }
  }else{                            // já visível: E recomeça a marcação
    regua.pontos=[]; regua.modo='calibrando';
  }
  if(regua.modo==='calibrando') reguaPausar();
  if(regua.modo==='on') reguaUsada();
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
function reguaUsada(){             // prova do passo 4: régua posta sobre o vídeo deste lance
  if(modoAtual==='video' && !feito.regua){ feito.regua = true; renderPasso();
    toast('Régua sobre o lance. Veja onde a bola foi e confirme.'); }
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
      reguaPausar();                 // segue parado enquanto os pontos são postos
      const r=svg.getBoundingClientRect();
      regua.pontos.push({x:(ev.clientX-r.left)/r.width,y:(ev.clientY-r.top)/r.height});
      if(regua.pontos.length===4){ regua.modo='on'; reguaGravar(); reguaUsada(); }
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

// ---------- rótulos próximos no mesmo vídeo ----------
function renderIrmaos(){
  const irs = atual.irmaos;
  if(!irs.length){ $('#irmaos').innerHTML = ''; return; }
  const div = new Set(irs.map(i=>i.region).concat(atual.reg.region)).size > 1;
  $('#irmaos').innerHTML = `
    <h2>${irs.length} marcação(ões) próxima(s) neste vídeo
        ${div?'<span style="color:var(--vermelho)"> — regiões diferentes</span>':''}</h2>
    <div style="font-size:11px;color:var(--txt2)">Podem ser outros ângulos desta cobrança; confira o tempo antes de comparar.</div>
    <table class="ang"><tr><th>região</th><th>câmera</th><th>gol</th><th>chute</th><th>status</th></tr>
    ${irs.map(i=>`<tr data-uid="${esc(i.uid)}">
      <td style="color:var(--laranja)">${esc(i.region)}</td><td>${esc(i.camera_type)}</td>
      <td style="color:${i.is_goal?'var(--verde)':'var(--vermelho)'}">${i.is_goal?'SIM':'não'}</td>
      <td>${i.chute_time_s}s</td><td>${selo(i.status, true)}</td></tr>`).join('')}</table>`;
}

// ---------- editor ----------
function renderEditor(){
  const r = atual.reg, o = atual.original;
  const mudou = [];
  if(o.region !== r.region) mudou.push(`região: ${o.region} → ${r.region}`);
  if(o.is_goal !== r.is_goal) mudou.push(`gol: ${o.is_goal} → ${r.is_goal}`);
  if(o.camera_type !== r.camera_type) mudou.push(`câmera: ${o.camera_type} → ${r.camera_type}`);
  if(o.inicio_frame !== r.inicio_frame) mudou.push(`início: ${o.inicio_frame} → ${r.inicio_frame}`);
  if(o.chute_frame !== r.chute_frame) mudou.push(`final: ${o.chute_frame} → ${r.chute_frame}`);

  $('#editor').innerHTML = `
    ${mudou.length?`<div class="diff"><b>alterado em relação ao original</b><br>${mudou.map(esc).join('<br>')}</div>`:''}
    ${r.status==='descartado'?`<div class="aviso">descartado: ${esc(r.motivo)}<br>
       <button onclick="reverter()" style="margin-top:6px">restaurar</button></div>`:''}

    <h2>Câmera</h2>
    <select id="ed-camera" style="width:100%">
      ${atual.cameras.map(c=>`<option ${c===r.camera_type?'selected':''}>${esc(c)}</option>`).join('')}
    </select>

    <h2>Observação (vai para o CSV)</h2>
    <textarea id="ed-obs" rows="2" style="width:100%">${esc(r.observations)}</textarea>
    <h2>Nota da revisão (só no backup)</h2>
    <textarea id="ed-nota" rows="2" style="width:100%" placeholder="o que você viu no vídeo...">${esc(r.nota)}</textarea>

    <div class="acoes">
      <button class="b-desc" onclick="descartar()">Descartar rótulo</button>
      <button onclick="reverter()" ${mudou.length||r.status!=='pendente'?'':'disabled'}>Voltar ao original</button>
      <button onclick="desfazer()" ${statusGlobal.pode_desfazer?'':'disabled'}>Desfazer última ação <kbd>Z</kbd></button>
      <button onclick="exportar()">Exportar CSVs revisados</button>
    </div>

    <div class="rodape">
      Região, gol e frames são confirmados no passo a passo do meio; câmera e observação são gravados junto, no passo 5.<br><br>
      <kbd>Enter</kbd> confirma o passo · <kbd>←</kbd><kbd>→</kbd> frame (<kbd>Shift</kbd> = 10) ·
      <kbd>PgUp</kbd><kbd>PgDn</kbd> rótulo anterior/próximo · <kbd>A</kbd> aprovar (passo 5)<br>
      <kbd>7</kbd><kbd>8</kbd><kbd>9</kbd> / <kbd>4</kbd><kbd>5</kbd><kbd>6</kbd> /
      <kbd>1</kbd><kbd>2</kbd><kbd>3</kbd> grade do gol (teclado numérico) ·
      <kbd>J</kbd> <kbd>I</kbd> <kbd>L</kbd> fora esquerda / cima / direita<br>
      <kbd>E</kbd> régua das traves (4 cliques) · <kbd>T</kbd> só os traços · <kbd>R</kbd> recolhe ·
      <kbd>Z</kbd> desfazer<br><br>
      Grava em <b>_revisao_manual/revisao.csv</b>.<br>Os labels.csv originais nunca são escritos.
    </div>`;
}
function setReg(c){
  sel.region = c;
  if(etapa>=4) feito.regiao = true;      // prova do passo 5
  if(c.startsWith('fora')) setGol(false); else renderPasso();
  reguaDesenhar();                       // a célula marcada segue a escolha
}
function setGol(v){
  sel.gol = v;
  if(etapa>=4) feito.gol = true;
  renderPasso();                         // redesenha a grade e SIM/NÃO
}

// ---------- ações ----------
async function salvar(){
  try{
    if(!framesValidos()) throw new Error('O frame final deve vir depois do início.');
    const r0 = atual.reg, framesMudaram = frames.inicio!==r0.inicio_frame || frames.chute!==r0.chute_frame;
    const r = await api('/api/editar', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid, region: sel.region, camera: $('#ed-camera').value,
        gol: sel.gol, inicio_frame: frames.inicio, chute_frame: frames.chute,
        // o fps do vídeo só vai junto quando um frame mudou: os tempos são
        // recalculados com a taxa certa, sem mexer em quem só corrigiu região
        fps: framesMudaram ? frames.fps : null,
        observations: $('#ed-obs').value, nota: $('#ed-nota').value})});
    toast('salvo: ' + r.mudancas.join(' · '), 'bom');
    manterEtapa = true;
    const uid = atual.reg.uid; await carregar(true); await abrir(uid);
    return true;
  }catch(e){ toast(e.message, 'erro'); return false; }
}
async function aprovar(){
  try{
    if(etapa<5) throw new Error(`Siga a sequência: falta ${NOME_ETAPA[etapa]} (passo ${etapa}).`);
    const p5 = prontoEtapa(5); if(!p5.pronto) throw new Error(p5.dica);
    if(!framesValidos()) throw new Error('O frame final deve vir depois do início.');
    // região/gol corrigidos no passo 5: grava antes de aprovar
    if(haPendencias()){ manterEtapa = true; if(!await salvar()) return; }
    await api('/api/aprovar', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({uid: atual.reg.uid, nota: $('#ed-nota').value})});
    toast((atual.reg.alteracoes||[]).length ? 'conferido, com correções' : 'conferido, sem alteração', 'bom');
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
  // as setas sao o ajuste de frame: e o trabalho principal da tela.
  // trocar de rotulo foi para PageUp/PageDown.
  if(k==='arrowleft'){ e.preventDefault(); if(modoAtual!=='frame') modo('frame');
                       else passo(e.shiftKey?-10:-1); }
  else if(k==='arrowright'){ e.preventDefault(); if(modoAtual!=='frame') modo('frame');
                             else passo(e.shiftKey?10:1); }
  else if(k==='pageup'){ e.preventDefault(); vizinho(-1); }
  else if(k==='pagedown'){ e.preventDefault(); vizinho(1); }
  else if(k==='enter'){ e.preventDefault(); avancar(); }
  else if(k==='a') aprovar();
  else if(k==='z') desfazer();
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
