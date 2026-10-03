"""
exporter.py
===========
Responsável por salvar os dados de cada pênalti rotulado.

A saída é segmentada por competição (ver competitions.py). Cada competição
tem seu próprio conjunto de pastas e seu próprio labels.csv:

    <output_base>/<competição>/
        clips/
            <stem>_penalti_<N>.mp4          – trecho início→chute
        frames/
            <stem>_penalti_<N>_inicio.png   – frame de início
            <stem>_penalti_<N>_chute.png    – frame do chute
        labels/
            <stem>_penalti_<N>.json         – label completo
        labels.csv                           – consolidado da competição

Onde <stem> = nome do arquivo de vídeo sem extensão. O Brasileirão fica na
raiz do <output_base> (layout legado); a Copa do Brasil em copa_do_brasil/.

A competição sai da pasta de origem do vídeo, então o chamador não precisa
informá-la — basta passar o video_path.
"""

import os
import csv
import json
import datetime
import cv2
import numpy as np

import competitions as comps


# Regiões de gol válidas (espelho do labeler.py)
VALID_REGIONS = {
    "gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito",
    "gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito",
    "gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito",
    "fora_esquerda", "fora_cima", "fora_direita",
}

# Nomes legíveis para exibição nos metadados
REGION_LABELS = {
    "gol_topo_esquerdo":  "Gol – Topo Esquerdo",
    "gol_topo_centro":    "Gol – Topo Centro",
    "gol_topo_direito":   "Gol – Topo Direito",
    "gol_meio_esquerdo":  "Gol – Meio Esquerdo",
    "gol_meio_centro":    "Gol – Meio Centro",
    "gol_meio_direito":   "Gol – Meio Direito",
    "gol_baixo_esquerdo": "Gol – Baixo Esquerdo",
    "gol_baixo_centro":   "Gol – Baixo Centro",
    "gol_baixo_direito":  "Gol – Baixo Direito",
    "fora_esquerda":      "Fora – Esquerda",
    "fora_cima":          "Fora – Cima",
    "fora_direita":       "Fora – Direita",
}

CSV_HEADER = [
    "video_file", "penalty_id",
    "inicio_frame", "chute_frame",
    "inicio_time_s", "chute_time_s",
    "camera_type", "region", "region_label",
    "is_goal",
    "observations", "timestamp_rotulagem",
    "clip_path", "frame_inicio_path", "frame_chute_path",
    # segmentação por competição
    "competition", "competition_label", "source_folder",
]


class Exporter:
    def __init__(self, output_base: str):
        self.output_base = output_base

    # ------------------------------------------------------------------
    # Método principal
    # ------------------------------------------------------------------
    def export(
        self,
        video_path: str,
        penalty_id: int,
        inicio_frame: int,
        chute_frame: int,
        fps: float,
        label_data: dict,
    ) -> dict:
        """
        Executa a exportação completa de um pênalti rotulado.

        Parâmetros
        ----------
        video_path   : caminho completo do vídeo de origem
        penalty_id   : número sequencial do pênalti no vídeo atual
        inicio_frame : índice do frame de início da cobrança
        chute_frame  : índice do frame do momento do chute
        fps          : taxa de frames do vídeo
        label_data   : dict com {camera_type, region, observations}

        Retorna
        -------
        dict com os caminhos dos arquivos gerados
        """
        # 0. Descobrir a competição pela pasta de origem e preparar o destino
        comp, source_folder = comps.classify(video_path)
        out = comps.paths(self.output_base, comp)
        comps.ensure_dirs(out)

        stem = os.path.splitext(os.path.basename(video_path))[0]

        # Não sobrescreve penáltis já gravados para o mesmo vídeo.
        used = self._used_penalty_ids(out, stem)
        if penalty_id in used or penalty_id < 1:
            penalty_id = max(used, default=0) + 1

        tag = f"{stem}_penalti_{penalty_id:03d}"

        # Caminhos de saída (dentro da competição)
        clip_path          = os.path.join(out["clips"],  f"{tag}.mp4")
        frame_inicio_path  = os.path.join(out["frames"], f"{tag}_inicio.png")
        frame_chute_path   = os.path.join(out["frames"], f"{tag}_chute.png")
        label_path         = os.path.join(out["labels"], f"{tag}.json")

        # 1. Extrair frames individuais
        self._save_frame(video_path, inicio_frame, frame_inicio_path)
        self._save_frame(video_path, chute_frame,  frame_chute_path)

        # 2. Extrair clip (início → chute inclusive)
        self._save_clip(video_path, inicio_frame, chute_frame, clip_path, fps)

        # 3. Conferir que as três mídias existem antes de registrar no CSV
        faltando = [p for p in (frame_inicio_path, frame_chute_path, clip_path)
                    if not (os.path.isfile(p) and os.path.getsize(p) > 0)]
        if faltando:
            for p in (frame_inicio_path, frame_chute_path, clip_path):
                try:
                    if os.path.isfile(p):
                        os.remove(p)
                except OSError:
                    pass
            raise RuntimeError(
                "Falha ao extrair as mídias do pênalti: "
                + ", ".join(os.path.basename(p) for p in faltando)
            )

        # 4. Montar registro de metadados
        region = label_data.get("region", "")
        record = {
            "video_file":         os.path.basename(video_path),
            "penalty_id":         penalty_id,
            "inicio_frame":       inicio_frame,
            "chute_frame":        chute_frame,
            "inicio_time_s":      round(inicio_frame / fps, 4) if fps else None,
            "chute_time_s":       round(chute_frame  / fps, 4) if fps else None,
            "camera_type":        label_data.get("camera_type", ""),
            "region":             region,
            "region_label":       REGION_LABELS.get(region, region),
            "is_goal":            label_data.get("is_goal", None),
            "observations":       label_data.get("observations", ""),
            "timestamp_rotulagem": datetime.datetime.now().isoformat(timespec="seconds"),
            "clip_path":          clip_path,
            "frame_inicio_path":  frame_inicio_path,
            "frame_chute_path":   frame_chute_path,
            "competition":        comp.slug,
            "competition_label":  comp.label,
            "source_folder":      source_folder,
        }

        # 5. Salvar JSON individual
        self._save_json(record, label_path)

        # 6. Append no CSV da competição
        self._append_csv(record, out["csv"])

        return record

    @staticmethod
    def _used_penalty_ids(out: dict, stem: str) -> set:
        """Ids de pênalti já gravados para um vídeo, no disco e no CSV."""
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
                    if os.path.splitext(row.get("video_file", ""))[0] == stem:
                        try:
                            used.add(int(row.get("penalty_id", "")))
                        except (TypeError, ValueError):
                            continue
        except (OSError, csv.Error):
            pass
        return used

    # ------------------------------------------------------------------
    # Helpers privados
    # ------------------------------------------------------------------
    @staticmethod
    def _save_frame(video_path: str, frame_idx: int, out_path: str):
        """Captura um único frame e salva como PNG."""
        cap = cv2.VideoCapture(video_path)
        try:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if ret:
                cv2.imwrite(out_path, frame)
        finally:
            cap.release()

    @staticmethod
    def _save_clip(
        video_path: str,
        start_frame: int,
        end_frame: int,
        out_path: str,
        fps: float,
    ):
        """
        Extrai o trecho [start_frame, end_frame] (inclusivo) e salva como MP4.
        Usa codec mp4v; compatível com a maioria dos players.
        """
        cap = cv2.VideoCapture(video_path)
        try:
            width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

            writer = None
            for fourcc_tag in ("avc1", "H264", "mp4v"):
                fourcc = cv2.VideoWriter_fourcc(*fourcc_tag)
                writer = cv2.VideoWriter(out_path, fourcc, fps, (width, height))
                if writer.isOpened():
                    break

            cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
            for _ in range(end_frame - start_frame + 1):
                ret, frame = cap.read()
                if not ret:
                    break
                writer.write(frame)
        finally:
            cap.release()
            try:
                writer.release()
            except Exception:
                pass

    @staticmethod
    def _save_json(record: dict, out_path: str):
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _append_csv(record: dict, csv_path: str):
        """Adiciona uma linha ao CSV da competição, criando o cabeçalho se necessário.

        Alinha-se ao cabeçalho já gravado no arquivo em vez de assumir
        CSV_HEADER: o CSV legado da raiz tem 15 colunas e o novo tem 18, e
        escrever 18 valores sob um cabeçalho de 15 desalinharia as linhas.
        """
        header = CSV_HEADER
        is_new = not os.path.isfile(csv_path) or os.path.getsize(csv_path) == 0

        if not is_new:
            with open(csv_path, "r", encoding="utf-8", newline="") as f:
                existing = next(csv.reader(f), None)
            if existing:
                header = existing
            # Append após uma escrita interrompida (sem quebra de linha final)
            # grudaria duas linhas em uma só.
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
