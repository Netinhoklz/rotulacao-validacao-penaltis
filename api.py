"""
api.py
======
Backend FastAPI para o Rotulador de Penaltis.

Endpoints:
  GET  /videos                      - lista videos da pasta de origem (com competicao)
  GET  /video/frame/{idx}           - retorna frame como JPEG (do cache pre-extraido)
  GET  /video/info                  - metadados do video carregado
  POST /video/load                  - carrega um video
  POST /export                      - exporta penalti rotulado
  GET  /config                      - retorna configuracao atual + layout de saida
  POST /config                      - atualiza configuracao
  GET  /progress                    - progresso total e por competicao
  GET  /preextract/status           - status da pre-extracao de frames
  POST /preextract/start            - inicia pre-extracao manualmente

Segmentacao por competicao
--------------------------
Cada competicao tem suas proprias pastas e seu proprio labels.csv; a
competicao vem da pasta de origem do video (ver competitions.py):

    <output_base>/
        clips/ frames/ labels/ labels.csv      -> Brasileirao (raiz, legado)
        copa_do_brasil/
            clips/ frames/ labels/ labels.csv  -> Copa do Brasil
        outros/
            clips/ frames/ labels/ labels.csv  -> pasta nao reconhecida

Conferir a base a qualquer momento: python dataset_tools.py check
"""

from __future__ import annotations

import os
import io
import shutil
import sys
import subprocess
import threading
import datetime
import csv
import json
import re
import cv2
import numpy as np
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import competitions as comps

# ---------------------------------------------------------------------------
# Config padrao
# ---------------------------------------------------------------------------
SOURCE_DIR  = os.environ.get("SOURCE_DIR",  r"E:\base_videos_penaltis")
OUTPUT_BASE = os.environ.get("OUTPUT_BASE", r"E:\penaltis_rotulados")
FRAMES_CACHE_DIR = os.environ.get("FRAMES_CACHE_DIR", r"E:\pasta_ref_mais_rapida")
SUPPORTED_EXT = (".mp4", ".avi", ".mov", ".mkv", ".wmv")

# ---------------------------------------------------------------------------
# Config de codificacao (economia de espaco). Tudo ajustavel por env.
# ---------------------------------------------------------------------------
# Binario do ffmpeg (usado para gerar proxies e cortar clips com H.264).
FFMPEG_BIN  = os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg")
# Proxy de navegacao: 1 video H.264 reduzido por fonte (substitui milhares de JPEGs).
PROXY_MAX_W = int(os.environ.get("PROXY_MAX_W", "960"))   # largura maxima do proxy
PROXY_CRF   = int(os.environ.get("PROXY_CRF", "30"))      # qualidade (maior = menor arquivo)
PROXY_GOP   = int(os.environ.get("PROXY_GOP", "15"))      # GOP curto = seek rapido
# Clip final do penalti: re-encode H.264 em vez de mp4v (5-10x menor).
CLIP_CRF    = int(os.environ.get("CLIP_CRF", "23"))
# Qualidade dos frames PNG->JPEG exportados.
FRAME_JPEG_Q = int(os.environ.get("FRAME_JPEG_Q", "92"))

# ---------------------------------------------------------------------------
# Sistema de pre-extracao de frames
# ---------------------------------------------------------------------------
class FramePreExtractor:
    """Gera um proxy H.264 compacto por video para navegacao rapida.

    Estrategia anterior: gravava 1 JPEG por frame de cada video. Isso descarta
    a compressao temporal do codec e produz ~5x o tamanho do video original
    (ex.: um video de 70 MB virava ~390 MB de imagens soltas), enchendo o disco.

    Estrategia atual: para cada video gera UM unico proxy.mp4 re-encodado,
    reduzido para <=PROXY_MAX_W de largura, sem audio e com GOP curto (seek
    quase instantaneo). Ocupa ~15-20x menos espaco mantendo a navegacao rapida.
    O corte final e os frames de exportacao continuam vindo do video ORIGINAL
    em resolucao cheia, entao a qualidade do dataset nao muda.
    """

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None  # ffmpeg em andamento
        # Status
        self._status = "idle"  # idle | running | done
        self._current_video = ""
        self._current_video_progress = 0
        self._current_video_total = 0
        self._videos_done = 0
        self._videos_total = 0
        self._videos_skipped = 0

    def get_cache_dir_for_video(self, video_path: str) -> str:
        stem = Path(video_path).stem
        return os.path.join(self.cache_dir, stem)

    def get_cached_proxy_path(self, video_path: str) -> str:
        return os.path.join(self.get_cache_dir_for_video(video_path), "proxy.mp4")

    def is_video_cached(self, video_path: str) -> bool:
        meta = self.get_cached_metadata(video_path)
        if not meta or not meta.get("complete"):
            return False
        return os.path.isfile(self.get_cached_proxy_path(video_path))

    def get_cached_metadata(self, video_path: str) -> Optional[dict]:
        cache_dir = self.get_cache_dir_for_video(video_path)
        meta_path = os.path.join(cache_dir, "metadata.json")
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def extract_video(self, video_path: str) -> bool:
        """Gera o proxy.mp4 compacto do video. Retorna True se concluido."""
        if not FFMPEG_BIN:
            # Sem ffmpeg nao ha proxy; a UI cai no fallback de seek no video original.
            return False

        cache_dir = self.get_cache_dir_for_video(video_path)
        os.makedirs(cache_dir, exist_ok=True)

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        with self.lock:
            self._current_video = Path(video_path).name
            self._current_video_progress = 0
            self._current_video_total = total_frames

        proxy_path = self.get_cached_proxy_path(video_path)
        tmp_path = proxy_path + ".tmp.mp4"

        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-i", video_path,
            "-an",
            "-vf", f"scale='min({PROXY_MAX_W},iw)':-2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(PROXY_CRF),
            "-g", str(PROXY_GOP), "-fps_mode", "passthrough",
            "-progress", "pipe:1", "-nostats",
            tmp_path,
        ]

        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
            with self.lock:
                self._proc = proc
            for line in proc.stdout:
                if self._stop_event.is_set():
                    proc.terminate()
                    break
                line = line.strip()
                if line.startswith("frame="):
                    try:
                        n = int(line.split("=", 1)[1])
                        with self.lock:
                            self._current_video_progress = n
                    except ValueError:
                        pass
            proc.wait()
        except Exception:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return False
        finally:
            with self.lock:
                self._proc = None

        ok = (
            not self._stop_event.is_set()
            and proc is not None and proc.returncode == 0
            and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0
        )
        if not ok:
            try:
                if os.path.isfile(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            return False

        try:
            os.replace(tmp_path, proxy_path)
        except Exception:
            return False

        meta = {
            "original_path": video_path,
            "total_frames": total_frames,
            "fps": fps,
            "width": width,
            "height": height,
            "proxy_path": proxy_path,
            "complete": True,
        }
        meta_path = os.path.join(cache_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        return True

    def start_batch(self, video_paths: list[str]):
        """Inicia pre-extracao em batch numa thread de fundo."""
        if self._thread and self._thread.is_alive():
            return  # Ja esta rodando

        self._stop_event.clear()
        with self.lock:
            self._status = "running"
            self._videos_done = 0
            self._videos_skipped = 0
            self._videos_total = len(video_paths)

        def _worker():
            for vpath in video_paths:
                if self._stop_event.is_set():
                    break

                if self.is_video_cached(vpath):
                    with self.lock:
                        self._videos_skipped += 1
                        self._videos_done += 1
                    continue

                self.extract_video(vpath)
                with self.lock:
                    self._videos_done += 1

            with self.lock:
                self._status = "done"
                self._current_video = ""

        self._thread = threading.Thread(target=_worker, daemon=True, name="frame-preextractor")
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        with self.lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def get_status(self) -> dict:
        with self.lock:
            return {
                "status": self._status,
                "current_video": self._current_video,
                "current_video_progress": self._current_video_progress,
                "current_video_total": self._current_video_total,
                "videos_done": self._videos_done,
                "videos_total": self._videos_total,
                "videos_skipped": self._videos_skipped,
            }


_extractor = FramePreExtractor(FRAMES_CACHE_DIR)

# ---------------------------------------------------------------------------
# Estado global do player (single-user, desktop)
# ---------------------------------------------------------------------------
class PlayerState:
    def __init__(self):
        self.cap: Optional[cv2.VideoCapture] = None
        self.video_path: str = ""
        self.total_frames: int = 0
        self.fps: float = 25.0
        self.width: int = 640
        self.height: int = 480
        self.lock = threading.Lock()
        self._using_cache: bool = False

    def load(self, path: str):
        """Carrega video. Se ha proxy pre-extraido, navega por ele (seek rapido)."""
        with self.lock:
            if self.cap:
                self.cap.release()
                self.cap = None

            # Caminho rapido: navega pelo proxy compacto, se existir.
            meta = _extractor.get_cached_metadata(path)
            proxy_path = _extractor.get_cached_proxy_path(path)
            if meta and meta.get("complete") and os.path.isfile(proxy_path):
                cap = cv2.VideoCapture(proxy_path)
                if cap.isOpened():
                    self.cap          = cap             # handle aponta para o proxy
                    self.video_path   = path            # id logico continua sendo a fonte
                    self.total_frames = meta["total_frames"]
                    self.fps          = meta["fps"]
                    self.width        = meta["width"]   # dims da fonte (para exibicao/ordenacao)
                    self.height       = meta["height"]
                    self._using_cache = True
                    return

            # Fallback: abre o video original e faz seek direto nele.
            self._using_cache = False
            self.cap = cv2.VideoCapture(path)
            if not self.cap.isOpened():
                self.cap = None
                raise RuntimeError(f"Nao foi possivel abrir: {path}")
            self.video_path   = path
            self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.fps          = self.cap.get(cv2.CAP_PROP_FPS) or 25.0
            self.width        = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.height       = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    def get_frame_jpeg(self, idx: int) -> bytes:
        with self.lock:
            if self.cap is None:
                raise RuntimeError("Nenhum video carregado.")

            # Faz seek no handle atual (proxy compacto ou, no fallback, a fonte).
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = self.cap.read()
            if not ret:
                raise RuntimeError(f"Frame {idx} indisponivel.")
            # O proxy ja vem reduzido; so redimensiona no caminho de fallback (fonte grande).
            h, w = frame.shape[:2]
            if w > PROXY_MAX_W:
                scale = PROXY_MAX_W / w
                frame = cv2.resize(frame, (PROXY_MAX_W, int(h * scale)),
                                   interpolation=cv2.INTER_AREA)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            return buf.tobytes()

    def get_raw_frame(self, idx: int) -> Optional[np.ndarray]:
        with self.lock:
            if self.cap is None:
                return None
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = self.cap.read()
            return frame if ret else None


_player = PlayerState()
_config = {"source_dir": SOURCE_DIR, "output_base": OUTPUT_BASE}

# ---------------------------------------------------------------------------
# Helpers de exportacao (espelho do exporter.py original)
# ---------------------------------------------------------------------------
REGION_LABELS = {
    "gol_topo_esquerdo":  "Gol - Topo Esquerdo",
    "gol_topo_centro":    "Gol - Topo Centro",
    "gol_topo_direito":   "Gol - Topo Direito",
    "gol_meio_esquerdo":  "Gol - Meio Esquerdo",
    "gol_meio_centro":    "Gol - Meio Centro",
    "gol_meio_direito":   "Gol - Meio Direito",
    "gol_baixo_esquerdo": "Gol - Baixo Esquerdo",
    "gol_baixo_centro":   "Gol - Baixo Centro",
    "gol_baixo_direito":  "Gol - Baixo Direito",
    "fora_esquerda":      "Fora - Esquerda",
    "fora_cima":          "Fora - Cima",
    "fora_direita":       "Fora - Direita",
}

VALID_REGIONS = set(REGION_LABELS)

VALID_CAMERAS = {"visão do torcedor", "visão cobrador", "visão goleiro"}

CSV_HEADER = [
    "video_file", "penalty_id",
    "inicio_frame", "chute_frame",
    "inicio_time_s", "chute_time_s",
    "camera_type", "region", "region_label",
    "is_goal",
    "observations", "timestamp_rotulagem",
    "clip_path", "frame_inicio_path", "frame_chute_path",
    # segmentacao por competicao
    "competition", "competition_label", "source_folder",
]

# Serializa a exportacao: dois saves simultaneos poderiam escolher o mesmo
# penalty_id ou intercalar linhas no CSV.
_export_lock = threading.Lock()


def _save_frame_file(video_path: str, frame_idx: int, out_path: str) -> bool:
    """Salva um frame da fonte em resolucao cheia. JPEG q92 (10x menor que PNG)."""
    cap = cv2.VideoCapture(video_path)
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if ret:
            cv2.imwrite(out_path, frame, [cv2.IMWRITE_JPEG_QUALITY, FRAME_JPEG_Q])
    finally:
        cap.release()
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 0


# Codec do clip quando NAO ha ffmpeg. "mp4v" (MPEG-4 parte 2) e o que o OpenCV
# grava em qualquer lugar, mas Chrome e Safari NAO tocam: o clip existe e a tela
# fica preta. No macOS o OpenCV grava H.264 ("avc1") pelo AVFoundation, que o
# navegador toca; no Windows o "avc1" exige a DLL do OpenH264 e, sem ela,
# isOpened() mente (True) e sai arquivo vazio - por isso o teste e por sistema.
CLIP_FOURCC_SEM_FFMPEG = "avc1" if sys.platform == "darwin" else "mp4v"


def _save_clip_cv2(video_path: str, start: int, end: int, out_path: str, fps: float):
    """Fallback: extrai o clip com OpenCV (usado se ffmpeg indisponivel)."""
    cap = cv2.VideoCapture(video_path)
    writer = None
    try:
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = cv2.VideoWriter_fourcc(*CLIP_FOURCC_SEM_FFMPEG)
        writer = cv2.VideoWriter(out_path, fourcc, fps, (w, h))
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        for _ in range(end - start + 1):
            ret, frame = cap.read()
            if not ret:
                break
            writer.write(frame)
    finally:
        cap.release()
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass


def _save_clip(video_path: str, start: int, end: int, out_path: str, fps: float):
    """Extrai o clip [start, end] da fonte em resolucao cheia.

    Usa ffmpeg com H.264 (CRF), que aproveita a compressao temporal e gera
    arquivos 5-10x menores que o mp4v do OpenCV. Cai para OpenCV se o ffmpeg
    nao estiver disponivel ou falhar.
    """
    fps = fps or 25.0
    if FFMPEG_BIN:
        start_t = max(0, start) / fps
        dur = (end - start + 1) / fps
        cmd = [
            FFMPEG_BIN, "-y", "-loglevel", "error",
            "-i", video_path,
            "-ss", f"{start_t:.4f}", "-t", f"{dur:.4f}",  # seek de saida = corte exato
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(CLIP_CRF),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            out_path,
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
                return True
        except Exception:
            pass
    _save_clip_cv2(video_path, start, end, out_path, fps)
    return os.path.isfile(out_path) and os.path.getsize(out_path) > 0


def _append_csv(record: dict, csv_path: str):
    """Acrescenta uma linha ao CSV da competicao.

    Alinha-se ao cabecalho ja gravado no arquivo em vez de assumir CSV_HEADER:
    o CSV legado da raiz tem 15 colunas e o novo tem 18, e escrever 18 valores
    sob um cabecalho de 15 desalinharia todas as linhas seguintes.
    """
    header = CSV_HEADER
    is_new = not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0

    if not is_new:
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            existing = next(csv.reader(f), None)
        if existing:
            header = existing
        # Um append apos uma escrita interrompida (arquivo sem quebra de linha
        # final) grudaria duas linhas em uma so.
        with open(csv_path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            needs_newline = f.read(1) not in (b"\n", b"\r")
        if needs_newline:
            with open(csv_path, "a", newline="", encoding="utf-8") as f:
                f.write("\r\n")

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header,
                                extrasaction="ignore", restval="")
        if is_new:
            writer.writeheader()
        writer.writerow(record)


# ---------------------------------------------------------------------------
# Coleta lista de videos + helper de resolucao (com cache)
# ---------------------------------------------------------------------------
_resolution_cache: dict[str, int] = {}


def _get_video_resolution(path: str) -> int:
    if path in _resolution_cache:
        return _resolution_cache[path]
    # Tenta ler da metadata pre-extraida primeiro
    meta = _extractor.get_cached_metadata(path)
    if meta:
        res = meta.get("width", 0) * meta.get("height", 0)
        _resolution_cache[path] = res
        return res
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        _resolution_cache[path] = 0
        return 0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    res = w * h
    _resolution_cache[path] = res
    return res


# Restos do yt-dlp na pasta de origem. "X.f137.mp4" e o stream de video que ele
# baixa antes de juntar com o audio; "X.temp.mp4" e essa juncao pela metade.
# Nenhum dos dois e um jogo novo: contar como video pendente inflava a fila
# (e o .temp nem abre).
_RX_FORMATO_YTDLP = re.compile(r"\.f\d+$")


def _stem_jogo(stem: str) -> str:
    """Nome do jogo, sem o sufixo de formato do yt-dlp (".f137")."""
    return _RX_FORMATO_YTDLP.sub("", stem)


def _collect_all_videos(directory: str) -> list[dict]:
    """
    Coleta os videos da pasta de origem: um item por JOGO.

    `stem` e o nome do jogo (sem ".fNNN") - e o que casa com os labels.csv.
    Se o mesmo jogo existe como "X.f137.mp4" e "X.mp4", fica so o "X.mp4",
    que e o arquivo final; o ".fNNN" so aparece quando e a unica copia.
    """
    videos = []
    if not os.path.isdir(directory):
        return videos
    for root, dirs, files in os.walk(directory):
        dirs.sort()
        for fname in sorted(files):
            if not fname.lower().endswith(SUPPORTED_EXT):
                continue
            if fname.startswith("._"):
                continue                          # metadados AppleDouble do macOS, nao video
            if Path(fname).stem.lower().endswith(".temp"):
                continue                          # juncao incompleta do yt-dlp
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, directory)
            comp, source_folder = comps.classify(full_path)
            videos.append({
                "name": rel_path.replace("\\", "/"),
                "path": full_path,
                "stem": _stem_jogo(Path(fname).stem),
                "competition": comp.slug,
                "competition_label": comp.label,
                "source_folder": source_folder,
                "_parcial": bool(_RX_FORMATO_YTDLP.search(Path(fname).stem)),
            })

    # mesmo jogo na mesma pasta duas vezes: fica o arquivo final
    finais = {(os.path.dirname(v["path"]), v["stem"]) for v in videos if not v["_parcial"]}
    videos = [v for v in videos
              if not (v["_parcial"] and (os.path.dirname(v["path"]), v["stem"]) in finais)]
    for v in videos:
        del v["_parcial"]
    return videos


# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="Rotulador de Penaltis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Schemas Pydantic
# ---------------------------------------------------------------------------
class LoadVideoRequest(BaseModel):
    path: str


class ConfigUpdate(BaseModel):
    source_dir: Optional[str] = None
    output_base: Optional[str] = None


class ExportRequest(BaseModel):
    video_path: str
    penalty_id: int
    inicio_frame: int
    chute_frame: int
    camera_type: str
    region: str
    is_goal: bool
    observations: str = ""


# ---------------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------------
@app.get("/config")
def get_config():
    return {**_config, "layout": comps.layout(_config["output_base"])}


@app.post("/config")
def update_config(body: ConfigUpdate):
    if body.source_dir is not None:
        _config["source_dir"] = body.source_dir
    if body.output_base is not None:
        _config["output_base"] = body.output_base
    return {**_config, "layout": comps.layout(_config["output_base"])}


def _get_labeled_keys(output_base: str) -> set:
    """
    Videos ja rotulados, como pares (slug_da_competicao, stem).

    Le TODOS os labels.csv sob output_base (raiz + subpastas de competicao),
    entao a marcacao de "ja rotulado" nunca regride quando um dataset ganha
    uma competicao nova. A chave inclui a competicao de proposito: dois
    videos homonimos em competicoes diferentes sao registros distintos.
    """
    keys: set = set()
    for slug, csv_path in comps.existing_csvs(output_base):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    video_file = row.get("video_file", "")
                    if video_file:
                        # A competicao gravada na linha manda; o local do
                        # arquivo e o fallback para o CSV legado (sem coluna).
                        keys.add(((row.get("competition") or slug),
                                  _stem_jogo(Path(video_file).stem)))
        except Exception:
            continue
    return keys


@app.get("/videos")
def list_videos():
    directory = _config["source_dir"]
    output_base = _config["output_base"]
    if not os.path.isdir(directory):
        return {"videos": [], "directory": directory, "error": "Pasta nao encontrada."}

    labeled_keys = _get_labeled_keys(output_base)
    all_videos = _collect_all_videos(directory)
    all_videos.sort(key=lambda v: _get_video_resolution(v["path"]))

    videos = []
    for v in all_videos:
        cached = _extractor.is_video_cached(v["path"])
        videos.append({
            "name": v["name"],
            "path": v["path"],
            "labeled": (v["competition"], v["stem"]) in labeled_keys,
            "cached": cached,
            "competition": v["competition"],
            "competition_label": v["competition_label"],
        })

    return {"videos": videos, "directory": directory}


@app.get("/progress")
def get_progress():
    """Resumo de progresso, no total e segmentado por competicao."""
    directory = _config["source_dir"]
    output_base = _config["output_base"]

    all_videos = _collect_all_videos(directory)
    all_videos.sort(key=lambda v: _get_video_resolution(v["path"]))

    labeled_keys = _get_labeled_keys(output_base)
    is_labeled = {
        v["path"]: (v["competition"], v["stem"]) in labeled_keys for v in all_videos
    }

    total = len(all_videos)
    labeled_count = sum(1 for v in all_videos if is_labeled[v["path"]])

    # Uma entrada por competicao presente na pasta de origem, na ordem canonica.
    by_competition = []
    seen = {v["competition"] for v in all_videos}
    ordered = [c.slug for c in comps.ALL if c.slug in seen]
    ordered += sorted(seen - set(ordered))
    for slug in ordered:
        group = [v for v in all_videos if v["competition"] == slug]
        done = sum(1 for v in group if is_labeled[v["path"]])
        comp = comps.BY_SLUG.get(slug)
        out = comps.paths(output_base, comp) if comp else {"dir": "", "csv": ""}
        by_competition.append({
            "competition": slug,
            "competition_label": comp.label if comp else slug,
            "total": len(group),
            "labeled": done,
            "unlabeled": len(group) - done,
            "output_dir": out["dir"],
            "csv_path": out["csv"],
        })

    return {
        "total": total,
        "labeled": labeled_count,
        "unlabeled": total - labeled_count,
        "by_competition": by_competition,
        "videos": [
            {
                "name": v["name"],
                "path": v["path"],
                "labeled": is_labeled[v["path"]],
                "competition": v["competition"],
                "competition_label": v["competition_label"],
            }
            for v in all_videos
        ],
    }


@app.post("/video/load")
def load_video(body: LoadVideoRequest):
    try:
        _player.load(body.path)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "path": _player.video_path,
        "total_frames": _player.total_frames,
        "fps": _player.fps,
        "width": _player.width,
        "height": _player.height,
    }


@app.get("/video/info")
def video_info():
    if not _player.video_path:
        return {"loaded": False}
    return {
        "loaded": True,
        "path": _player.video_path,
        "total_frames": _player.total_frames,
        "fps": _player.fps,
        "width": _player.width,
        "height": _player.height,
    }


@app.get("/video/frame/{idx}")
def get_frame(idx: int):
    try:
        jpeg = _player.get_frame_jpeg(idx)
    except RuntimeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return StreamingResponse(
        io.BytesIO(jpeg),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _used_penalty_ids(out: dict, stem: str) -> set:
    """Ids de penalti ja gravados para um video, vistos no disco e no CSV."""
    used = set()
    prefix = f"{stem}_penalti_"

    try:
        for fname in os.listdir(out["labels"]):
            if fname.startswith(prefix) and fname.endswith(".json"):
                num = fname[len(prefix):-len(".json")]
                if num.isdigit():
                    used.add(int(num))
    except OSError:
        pass

    try:
        with open(out["csv"], "r", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if Path(row.get("video_file", "")).stem == stem:
                    try:
                        used.add(int(row.get("penalty_id", "")))
                    except (TypeError, ValueError):
                        continue
    except (OSError, csv.Error):
        pass

    return used


@app.post("/export")
def export_penalty(body: ExportRequest):
    output_base = _config["output_base"]

    # --- Validacao antes de tocar no disco ------------------------------
    if body.inicio_frame < 0:
        raise HTTPException(status_code=400, detail="inicio_frame nao pode ser negativo.")
    if body.inicio_frame >= body.chute_frame:
        raise HTTPException(status_code=400,
                            detail="inicio_frame deve ser menor que chute_frame.")
    if body.region not in VALID_REGIONS:
        raise HTTPException(status_code=400,
                            detail=f"Regiao invalida: {body.region!r}.")
    if body.camera_type not in VALID_CAMERAS:
        raise HTTPException(status_code=400,
                            detail=f"Tipo de camera invalido: {body.camera_type!r}.")
    if not os.path.isfile(body.video_path):
        raise HTTPException(status_code=400,
                            detail=f"Video de origem nao encontrado: {body.video_path}")

    # --- Segmentacao por competicao -------------------------------------
    comp, source_folder = comps.classify(body.video_path)
    out = comps.paths(output_base, comp)
    comps.ensure_dirs(out)

    stem = Path(body.video_path).stem
    fps = _player.fps if _player.video_path == body.video_path else 25.0
    fps = fps or 25.0

    with _export_lock:
        # Nunca sobrescreve: o contador do frontend reinicia em 1 a cada
        # video selecionado, entao voltar a um video ja rotulado colidiria
        # com os penaltis anteriores e apagaria os clips.
        used = _used_penalty_ids(out, stem)
        penalty_id = body.penalty_id
        if penalty_id in used or penalty_id < 1:
            penalty_id = max(used, default=0) + 1

        tag = f"{stem}_penalti_{penalty_id:03d}"
        clip_path         = os.path.join(out["clips"],  f"{tag}.mp4")
        frame_inicio_path = os.path.join(out["frames"], f"{tag}_inicio.jpg")
        frame_chute_path  = os.path.join(out["frames"], f"{tag}_chute.jpg")
        label_path        = os.path.join(out["labels"], f"{tag}.json")

        written = [clip_path, frame_inicio_path, frame_chute_path, label_path]

        try:
            # Frames e clip vem sempre do video original, em resolucao cheia.
            ok_inicio = _save_frame_file(body.video_path, body.inicio_frame, frame_inicio_path)
            ok_chute  = _save_frame_file(body.video_path, body.chute_frame,  frame_chute_path)
            ok_clip   = _save_clip(body.video_path, body.inicio_frame,
                                   body.chute_frame, clip_path, fps)

            # So registra no CSV depois que as tres midias existem: uma linha
            # apontando para arquivo inexistente corrompe o dataset em silencio.
            if not (ok_inicio and ok_chute and ok_clip):
                faltando = [
                    nome for nome, ok in (
                        ("frame de inicio", ok_inicio),
                        ("frame do chute",  ok_chute),
                        ("clip",            ok_clip),
                    ) if not ok
                ]
                raise RuntimeError(
                    "falha ao extrair " + ", ".join(faltando)
                    + " (frames fora do intervalo do video ou codec sem suporte)"
                )

            record = {
                "video_file":          Path(body.video_path).name,
                "penalty_id":          penalty_id,
                "inicio_frame":        body.inicio_frame,
                "chute_frame":         body.chute_frame,
                "inicio_time_s":       round(body.inicio_frame / fps, 4),
                "chute_time_s":        round(body.chute_frame  / fps, 4),
                "camera_type":         body.camera_type,
                "region":              body.region,
                "region_label":        REGION_LABELS.get(body.region, body.region),
                "is_goal":             body.is_goal,
                "observations":        body.observations,
                "timestamp_rotulagem": datetime.datetime.now().isoformat(timespec="seconds"),
                "clip_path":           clip_path,
                "frame_inicio_path":   frame_inicio_path,
                "frame_chute_path":    frame_chute_path,
                "competition":         comp.slug,
                "competition_label":   comp.label,
                "source_folder":       source_folder,
            }

            with open(label_path, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)

            _append_csv(record, out["csv"])

        except Exception as e:
            # Exportacao e tudo-ou-nada: remove o que ficou pela metade para
            # o disco nao acumular orfaos sem linha no CSV.
            for path in written:
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            raise HTTPException(status_code=500,
                                detail=f"Falha ao exportar o penalti: {e}")

    response = {
        **record,
        "output_dir": out["dir"],
        "csv_path":   out["csv"],
    }
    if penalty_id != body.penalty_id:
        response["penalty_id_ajustado"] = True
        response["aviso"] = (
            f"Ja existiam penaltis gravados para este video; "
            f"salvo como #{penalty_id} para nao sobrescrever."
        )
    if comp is comps.DESCONHECIDA:
        response["aviso_competicao"] = (
            f"Competicao nao identificada pela pasta {source_folder!r}; "
            f"salvo em '{out['dir']}'."
        )
    return response


# ---------------------------------------------------------------------------
# Endpoints de pre-extracao
# ---------------------------------------------------------------------------
@app.get("/preextract/status")
def preextract_status():
    return _extractor.get_status()


@app.post("/preextract/start")
def preextract_start():
    """Inicia pre-extracao de todos os videos nao rotulados."""
    directory = _config["source_dir"]
    output_base = _config["output_base"]
    labeled_keys = _get_labeled_keys(output_base)
    all_videos = _collect_all_videos(directory)
    # Ordena por resolucao (menores primeiro = mais rapidos)
    all_videos.sort(key=lambda v: _get_video_resolution(v["path"]))
    # Filtra apenas nao rotulados
    unlabeled_paths = [v["path"] for v in all_videos
                       if (v["competition"], v["stem"]) not in labeled_keys]
    _extractor.start_batch(unlabeled_paths)
    return {"started": True, "videos_to_process": len(unlabeled_paths)}


# ---------------------------------------------------------------------------
# Startup: inicia pre-extracao automaticamente
# ---------------------------------------------------------------------------
@app.on_event("startup")
def on_startup():
    os.makedirs(FRAMES_CACHE_DIR, exist_ok=True)
    # Inicia pre-extracao automaticamente em background
    directory = _config["source_dir"]
    output_base = _config["output_base"]
    labeled_keys = _get_labeled_keys(output_base)
    all_videos = _collect_all_videos(directory)
    all_videos.sort(key=lambda v: _get_video_resolution(v["path"]))
    unlabeled_paths = [v["path"] for v in all_videos
                       if (v["competition"], v["stem"]) not in labeled_keys]
    if unlabeled_paths:
        _extractor.start_batch(unlabeled_paths)


# ---------------------------------------------------------------------------
# Serve frontend React (deve ficar por ultimo para nao conflitar com /api)
# ---------------------------------------------------------------------------
_DIST = Path(__file__).parent / "frontend" / "dist"

if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(str(_DIST / "favicon.svg"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str):
        """
        Serve o SPA com a barra de navegacao das outras telas injetada.

        A injecao acontece na resposta, nao no arquivo: o build do React
        continua intacto em dist/ e um `npm run build` novo nao apaga nada.
        Se o navbar.py nao estiver disponivel, cai no arquivo puro.
        """
        html = (_DIST / "index.html").read_text(encoding="utf-8")
        try:
            import navbar
            # #root ocupa 100% da altura; sem descontar a barra o rodape
            # do rotulador ficaria cortado embaixo dela
            ajuste = ("<style>#root{height:calc(100% - var(--nav-h,0px))}</style>")
            html = html.replace("<body>", "<body>" + navbar.barra("rotulador") + ajuste, 1)
        except Exception:
            pass
        return HTMLResponse(html)
