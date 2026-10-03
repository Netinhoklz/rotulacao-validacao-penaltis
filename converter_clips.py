# -*- coding: utf-8 -*-
"""
converter_clips.py - deixa todos os clips do dataset em H.264, que o navegador toca.

    python converter_clips.py              # so conta: quantos clips nao estao em H.264
    python converter_clips.py --aplicar    # reconverte esses clips

Por que existe: os clips cortados sem o ffmpeg saem do OpenCV em MPEG-4 parte 2
("mp4v"). O Edge no Windows toca esse codec com o decodificador do sistema, mas
Chrome e Safari (e portanto o Mac) nao - a aba de clip da validacao fica preta
com o arquivo la. Este script acha esses clips e os recorta DE NOVO a partir do
video original, por numero de frame (os frames de inicio e final do rotulo, ja
com as correcoes da validacao), em H.264. Se o video original nao estiver nesta
maquina, transcodifica o clip existente.

Precisa do ffmpeg no PATH (macOS: brew install ffmpeg). Grava em .tmp e so
troca o arquivo no fim; o labels.csv nao e tocado.

As pastas vem do run.py (PASTA_VIDEOS, PASTA_SAIDA) ou de --videos / --saida.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import revisao as R

FFMPEG = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe") or (FFMPEG and os.path.join(os.path.dirname(FFMPEG), "ffprobe"))


def pastas_do_run_py() -> tuple[str, str]:
    """PASTA_VIDEOS e PASTA_SAIDA do run.py, sem executar o lancador."""
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")
    spec = importlib.util.spec_from_file_location("run_cfg", caminho)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                       # so define funcoes e constantes
    return mod.PASTA_VIDEOS, mod.PASTA_SAIDA


def codec_de(caminho: str) -> str:
    if not os.path.isfile(caminho):
        return "ausente"
    r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name", "-of", "csv=p=0", caminho],
                       capture_output=True, text=True)
    return r.stdout.strip() or "ilegivel"


def fps_de(caminho: str) -> float:
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", caminho],
                           capture_output=True, text=True)
        num, den = r.stdout.strip().split("/")
        fps = float(num) / float(den)
        return fps if 5.0 < fps < 240.0 else 0.0
    except (OSError, ValueError, ZeroDivisionError):
        return 0.0


def recortar(video: str, ini: int, fim: int, fps: float, destino: str) -> bool:
    """Mesmo corte do revisor: -ss no meio do frame anterior, -frames:v exato."""
    tmp = destino + ".tmp.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error",
           "-ss", f"{max(ini - 0.5, 0) / fps:.4f}", "-i", video,
           "-frames:v", str(fim - ini + 1), "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
    return _rodar(cmd, tmp, destino)


def transcodificar(origem: str, destino: str) -> bool:
    """Sem o video original por perto: converte o clip que existe."""
    tmp = destino + ".tmp.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-i", origem, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
    return _rodar(cmd, tmp, destino)


def _rodar(cmd: list[str], tmp: str, destino: str) -> bool:
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0
    except OSError:
        ok = False
    if ok:
        os.replace(tmp, destino)
    elif os.path.isfile(tmp):
        os.remove(tmp)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description="Reconverte para H.264 os clips que o navegador nao toca.")
    ap.add_argument("--videos", help="pasta dos videos originais (padrao: PASTA_VIDEOS do run.py)")
    ap.add_argument("--saida", help="pasta de saida com os labels.csv (padrao: PASTA_SAIDA do run.py)")
    ap.add_argument("--aplicar", action="store_true", help="converte de verdade (sem isto, so conta)")
    # 2 e o bastante: o libx264 ja usa varios nucleos, e cada ffmpeg decodificando
    # 1080p pesa; com 8 em paralelo o Windows ficou sem memoria de paginacao (1455)
    ap.add_argument("--paralelo", type=int, default=2, help="quantos ffmpeg ao mesmo tempo (padrao 2)")
    a = ap.parse_args()

    if not FFMPEG or not FFPROBE:
        print("ERRO: ffmpeg/ffprobe nao encontrados no PATH.")
        print("      macOS: brew install ffmpeg   Windows: winget install ffmpeg")
        return 2

    videos_cfg, saida_cfg = pastas_do_run_py()
    videos = os.path.abspath(os.path.expanduser(a.videos or videos_cfg))
    saida = os.path.abspath(os.path.expanduser(a.saida or saida_cfg))
    if not os.path.isdir(saida):
        print(f"ERRO: pasta de saida nao existe: {saida}")
        return 2

    paths = R.Paths(saida)
    base = R.carregar(paths)
    indice = R.indexar_videos(paths, videos) if os.path.isdir(videos) else {}
    if not indice:
        print(f"aviso: pasta de videos nao encontrada ({videos}); so da para transcodificar os clips")

    print(f"rotulos: {len(base.registros)} - conferindo o codec de cada clip...")
    alvos = []
    for reg in base.registros:
        atual = reg["atual"]
        clip = R.caminho_local(paths, atual.get("clip_path", ""))
        alvos.append((reg, clip))
    with ThreadPoolExecutor(8) as ex:
        codecs = list(ex.map(lambda t: codec_de(t[1]), alvos))

    resumo: dict[str, int] = {}
    for c in codecs:
        resumo[c] = resumo.get(c, 0) + 1
    for c, n in sorted(resumo.items(), key=lambda kv: -kv[1]):
        marca = "  <- o navegador NAO toca" if c not in ("h264", "ausente", "ilegivel") else ""
        print(f"  {c:<10} {n:>5}{marca}")

    fila = [(reg, clip) for (reg, clip), c in zip(alvos, codecs)
            if c not in ("h264", "ausente", "ilegivel")]
    if not fila:
        print("\nTodos os clips existentes ja estao em H.264. Nada a fazer.")
        return 0
    if not a.aplicar:
        print(f"\n{len(fila)} clip(s) para reconverter. Rode de novo com --aplicar.")
        return 1

    print(f"\nreconvertendo {len(fila)} clip(s) com {a.paralelo} ffmpeg em paralelo...")
    t0 = time.time()
    feitos = {"recortado": 0, "transcodificado": 0, "falhou": 0}
    _fps: dict[str, float] = {}

    erros: list[str] = []

    def converter(item):
        reg, clip = item
        try:
            atual = reg["atual"]
            video = indice.get(atual.get("video_file", ""))
            if video and os.path.isfile(video):
                fps = _fps.get(video) or fps_de(video) or R.fps_da_linha(atual)
                _fps[video] = fps
                ini, fim = int(R.num(atual, "inicio_frame")), int(R.num(atual, "chute_frame"))
                if fim > ini and recortar(video, ini, fim, fps, clip):
                    return "recortado", clip
            return ("transcodificado" if transcodificar(clip, clip) else "falhou"), clip
        except Exception as e:                       # um clip com problema nao derruba o lote
            erros.append(f"{os.path.basename(clip)}: {e}")
            return "falhou", clip

    with ThreadPoolExecutor(a.paralelo) as ex:
        for i, (como, clip) in enumerate(ex.map(converter, fila), 1):
            feitos[como] += 1
            if i % 25 == 0 or i == len(fila):
                print(f"  {i}/{len(fila)}  ({time.time() - t0:.0f}s)")
    print(f"\nrecortados do original: {feitos['recortado']}  |  transcodificados: "
          f"{feitos['transcodificado']}  |  falharam: {feitos['falhou']}")
    for e in erros[:10]:
        print("  falha:", e)
    if feitos["falhou"]:
        print("Rode de novo: so os que faltam sao refeitos. Se o erro for de memoria, use --paralelo 1.")
    return 1 if feitos["falhou"] else 0


if __name__ == "__main__":
    sys.exit(main())
