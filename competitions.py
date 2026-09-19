"""
competitions.py
===============
Fonte unica de verdade para a segmentacao dos rotulos por competicao.

Usado por api.py (app web) e exporter.py (app desktop) para que os dois
gravem exatamente no mesmo lugar, sem divergencia.

Layout de saida (atual)
-----------------------
    <output_base>/
        clips/ frames/ labels/ labels.csv        -> Brasileirao (raiz, legado)
        copa_do_brasil/
            clips/ frames/ labels/ labels.csv    -> Copa do Brasil
        outros/
            clips/ frames/ labels/ labels.csv    -> nao identificado

O Brasileirao fica na raiz porque a base ja tinha centenas de rotulos la.
Para migra-lo para uma subpasta propria depois, basta definir a variavel de
ambiente BRASILEIRAO_SUBDIR=brasileirao e mover os arquivos - nenhuma outra
parte do codigo precisa mudar.

Deteccao
--------
A competicao vem da PASTA de origem do video, nao do nome do arquivo: as
pastas de origem sao estaveis, os nomes dos arquivos nao. A busca vai da
pasta mais profunda para a mais rasa, entao funciona tanto com
source_dir = E:\\base_videos_penaltis quanto com source_dir apontando
direto para dentro de uma das pastas de competicao.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import NamedTuple


class Competition(NamedTuple):
    slug: str      # identificador estavel (nome da subpasta, coluna do CSV)
    label: str     # nome legivel para a interface
    subdir: str    # subpasta sob output_base; "" = raiz do output_base


# "" mantem o Brasileirao na raiz (layout legado, sem migracao de arquivos).
BRASILEIRAO_SUBDIR = os.environ.get("BRASILEIRAO_SUBDIR", "").strip().strip("/\\")

BRASILEIRAO    = Competition("brasileirao",    "Brasileirão",    BRASILEIRAO_SUBDIR)
COPA_DO_BRASIL = Competition("copa_do_brasil", "Copa do Brasil", "copa_do_brasil")
DESCONHECIDA   = Competition("outros",         "Não identificado", "outros")

ALL: tuple[Competition, ...] = (BRASILEIRAO, COPA_DO_BRASIL, DESCONHECIDA)
BY_SLUG: dict[str, Competition] = {c.slug: c for c in ALL}

# Regras FORTES: o nome da competicao escrito na pasta. A primeira que casar
# vence; Copa vem antes de proposito, porque a pasta da Copa contem "brasil" e
# a regra do Brasileirao poderia roubar o match.
_RULES: tuple[tuple[re.Pattern[str], Competition], ...] = (
    (re.compile(r"copa[\W_]*do[\W_]*brasil"),                 COPA_DO_BRASIL),
    (re.compile(r"brasileirao|serie[\W_]*a(?![a-z])"),        BRASILEIRAO),
)

# Regra FRACA: pastas do tipo "videos_penaltis_2023", que so dizem o ANO.
# (Exige o ano: a pasta-base "base_videos_penaltis" nao pode casar por acidente.)
# So vale quando nenhuma pasta do caminho traz o nome de uma competicao - senao
# a subpasta de ano dentro de "video_penaltis_copa_do_brasil" venceria a Copa
# e os videos dela seriam rotulados e GRAVADOS como Brasileirao.
_RULES_ANO: tuple[tuple[re.Pattern[str], Competition], ...] = (
    (re.compile(r"videos?[\W_]*penaltis[\W_]*(?:19|20)\d{2}"), BRASILEIRAO),
)


def _norm(text: str) -> str:
    """Minusculas e sem acento, para o match nao depender de grafia."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def classify(video_path: str) -> tuple[Competition, str]:
    """
    Descobre a competicao de um video a partir das pastas do caminho.

    Retorna (competicao, pasta_que_decidiu). Quando nada casa, devolve
    DESCONHECIDA e a pasta imediatamente acima do arquivo - assim o registro
    fica rastreavel em vez de ser jogado silenciosamente no Brasileirao.
    """
    try:
        parts = Path(video_path).expanduser().resolve().parts
    except (OSError, ValueError):
        parts = Path(video_path).parts

    folders = [p for p in parts[:-1] if p not in ("/", "\\")]

    # Duas passadas, da pasta mais profunda para a mais rasa. Nome de
    # competicao em QUALQUER nivel vence uma pasta que so diz o ano, mesmo que
    # a de ano seja mais funda: `copa_do_brasil/videos_penaltis_2022/x.mp4` e
    # Copa, nao Brasileirao.
    for regras in (_RULES, _RULES_ANO):
        for folder in reversed(folders):
            norm = _norm(folder)
            for rx, comp in regras:
                if rx.search(norm):
                    return comp, folder

    return DESCONHECIDA, (folders[-1] if folders else "")


def of(video_path: str) -> Competition:
    """Atalho quando so a competicao interessa."""
    return classify(video_path)[0]


def paths(output_base: str, comp: Competition) -> dict[str, str]:
    """Resolve todos os caminhos de saida de uma competicao."""
    root = os.path.join(output_base, comp.subdir) if comp.subdir else output_base
    return {
        "slug":   comp.slug,
        "label":  comp.label,
        "dir":    root,
        "clips":  os.path.join(root, "clips"),
        "frames": os.path.join(root, "frames"),
        "labels": os.path.join(root, "labels"),
        "csv":    os.path.join(root, "labels.csv"),
    }


def ensure_dirs(out: dict[str, str]) -> None:
    """Cria clips/frames/labels da competicao (idempotente)."""
    for key in ("clips", "frames", "labels"):
        os.makedirs(out[key], exist_ok=True)


def layout(output_base: str) -> list[dict[str, str]]:
    """Layout completo de saida, para exibir na interface / conferencia."""
    return [paths(output_base, c) for c in ALL]


def existing_csvs(output_base: str) -> list[tuple[str, str]]:
    """
    Todos os labels.csv existentes sob output_base, como (slug, caminho).

    Inclui subpastas desconhecidas (slug = nome da pasta) para que nenhum
    rotulo ja gravado passe despercebido na deteccao de "ja rotulado".
    """
    found: list[tuple[str, str]] = []
    if not os.path.isdir(output_base):
        return found

    # CSV da raiz: pertence a competicao configurada para morar na raiz.
    root_slug = next((c.slug for c in ALL if not c.subdir), BRASILEIRAO.slug)
    root_csv = os.path.join(output_base, "labels.csv")
    if os.path.isfile(root_csv):
        found.append((root_slug, root_csv))

    try:
        entries = sorted(os.scandir(output_base), key=lambda e: e.name)
    except OSError:
        return found

    for entry in entries:
        # Pastas com "_" na frente sao auxiliares (backups, exportacoes) e nao
        # competicoes - um labels.csv dentro delas nao pode entrar na contagem.
        if (not entry.is_dir() or entry.name.startswith("_")
                or entry.name in ("clips", "frames", "labels")):
            continue
        csv_path = os.path.join(entry.path, "labels.csv")
        if os.path.isfile(csv_path):
            slug = BY_SLUG[entry.name].slug if entry.name in BY_SLUG else entry.name
            found.append((slug, csv_path))

    return found
