"""
dataset_tools.py
================
Conferencia e estatisticas do dataset rotulado, por competicao.

Uso:
    python dataset_tools.py stats            # contagens por competicao/regiao/camera
    python dataset_tools.py check            # integridade: CSV x disco, duplicatas
    python dataset_tools.py stats --output-base E:\\penaltis_rotulados

`check` e somente leitura: aponta os problemas, nao altera nada.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import competitions as comps

DEFAULT_OUTPUT_BASE = os.environ.get("OUTPUT_BASE", r"E:\penaltis_rotulados")

REGION_ORDER = [
    "gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito",
    "gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito",
    "gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito",
    "fora_esquerda", "fora_cima", "fora_direita",
]
VALID_CAMERAS = {"visão do torcedor", "visão cobrador", "visão goleiro"}


def _is_goal(row: dict) -> bool:
    return str(row.get("is_goal", "")).strip().lower() in ("true", "1", "sim", "yes")


def _load(output_base: str) -> dict[str, list[dict]]:
    """{slug: [linhas]} de todos os labels.csv sob output_base."""
    data: dict[str, list[dict]] = {}
    for slug, csv_path in comps.existing_csvs(output_base):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError as e:
            print(f"  ! nao foi possivel ler {csv_path}: {e}")
            continue
        for r in rows:
            r["_csv"] = csv_path
            r["_slug"] = slug
        data.setdefault(slug, []).extend(rows)
    return data


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------
def cmd_stats(output_base: str) -> int:
    data = _load(output_base)
    if not data:
        print(f"Nenhum labels.csv encontrado em {output_base}")
        return 1

    total = sum(len(v) for v in data.values())
    print(f"\nBase: {output_base}")
    print(f"TOTAL GERAL: {total} penaltis\n")

    print(f"{'COMPETICAO':<20}{'penaltis':>10}{'videos':>9}{'gols':>7}{'%gol':>7}")
    print("-" * 53)
    for slug in sorted(data, key=lambda s: -len(data[s])):
        rows = data[slug]
        comp = comps.BY_SLUG.get(slug)
        name = comp.label if comp else slug
        vids = len({r.get("video_file", "") for r in rows})
        gols = sum(1 for r in rows if _is_goal(r))
        pct = 100 * gols / len(rows) if rows else 0
        print(f"{name:<20}{len(rows):>10}{vids:>9}{gols:>7}{pct:>6.1f}%")
    print("-" * 53)
    print(f"{'TOTAL':<20}{total:>10}")

    for slug in sorted(data, key=lambda s: -len(data[s])):
        rows = data[slug]
        comp = comps.BY_SLUG.get(slug)
        name = comp.label if comp else slug
        print(f"\n{'=' * 53}\n{name}  ({len(rows)} penaltis)\n{'=' * 53}")

        reg = Counter(r.get("region", "") for r in rows)
        gol_reg = Counter(r.get("region", "") for r in rows if _is_goal(r))
        print(f"  {'REGIAO':<22}{'total':>7}{'%':>7}{'gol':>6}{'nao':>6}")
        for k in REGION_ORDER + sorted(set(reg) - set(REGION_ORDER)):
            if not reg[k]:
                continue
            marca = "" if k in REGION_ORDER else "  <- INVALIDA"
            print(f"  {k:<22}{reg[k]:>7}{100*reg[k]/len(rows):>6.1f}%"
                  f"{gol_reg[k]:>6}{reg[k]-gol_reg[k]:>6}{marca}")

        cam = Counter(r.get("camera_type", "") for r in rows)
        print(f"\n  {'CAMERA':<22}{'total':>7}{'%':>7}")
        for k, v in cam.most_common():
            marca = "" if k in VALID_CAMERAS else "  <- INVALIDA"
            print(f"  {k:<22}{v:>7}{100*v/len(rows):>6.1f}%{marca}")

    return 0


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------
def cmd_check(output_base: str) -> int:
    data = _load(output_base)
    if not data:
        print(f"Nenhum labels.csv encontrado em {output_base}")
        return 1

    problemas = 0
    print(f"\nConferindo {output_base}\n")

    for slug in sorted(data):
        rows = data[slug]
        comp = comps.BY_SLUG.get(slug)
        name = comp.label if comp else slug
        out = comps.paths(output_base, comp) if comp else None
        print(f"{'=' * 60}\n{name}  ({len(rows)} linhas)\n{'=' * 60}")

        def falha(titulo: str, itens: list[str]) -> int:
            if not itens:
                return 0
            print(f"  [X] {titulo}: {len(itens)}")
            for i in itens[:10]:
                print(f"        {i}")
            if len(itens) > 10:
                print(f"        ... e mais {len(itens) - 10}")
            return 1

        # 1. Linha do CSV apontando para midia inexistente
        sem_midia = [
            f"{r.get('video_file','?')} #{r.get('penalty_id','?')} -> {Path(p).name}"
            for r in rows
            for p in (r.get("clip_path"), r.get("frame_inicio_path"), r.get("frame_chute_path"))
            if p and not os.path.isfile(p)
        ]
        problemas += falha("linhas do CSV com midia faltando", sem_midia)

        # 2. Disco e CSV tem que bater nos dois sentidos: arquivo sem linha
        #    (lixo de export interrompido) e linha sem JSON (label incompleto).
        tags_csv = {Path(r["clip_path"]).stem for r in rows if r.get("clip_path")}
        if out and os.path.isdir(out["dir"]):
            def _tags(sub: str, corte: tuple[str, ...] = ()) -> set[str]:
                d = out[sub]
                if not os.path.isdir(d):
                    return set()
                nomes = set()
                for f in os.listdir(d):
                    stem = Path(f).stem
                    for suf in corte:
                        if stem.endswith(suf):
                            stem = stem[: -len(suf)]
                            break
                    nomes.add(stem)
                return nomes

            for rotulo, sub, corte in (
                ("labels", "labels", ()),
                ("clips", "clips", ()),
                ("frames", "frames", ("_inicio", "_chute")),
            ):
                orfaos = sorted(_tags(sub, corte) - tags_csv)
                problemas += falha(f"{rotulo} no disco sem linha no CSV", orfaos)

            sem_json = sorted(tags_csv - _tags("labels"))
            problemas += falha("linhas do CSV sem o JSON do label", sem_json)

        # 3. Mesmo (video, penalty_id) gravado duas vezes
        vistos: dict[tuple, int] = Counter(
            (r.get("video_file", ""), r.get("penalty_id", "")) for r in rows
        )
        dups = [f"{v} #{p} ({n}x)" for (v, p), n in vistos.items() if n > 1]
        problemas += falha("penaltis duplicados (mesmo video + id)", dups)

        # 4. Valores fora do vocabulario
        invalidos = [
            f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: regiao={r.get('region')!r}"
            for r in rows if r.get("region") not in REGION_ORDER
        ]
        problemas += falha("regiao invalida", invalidos)

        cams = [
            f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: camera={r.get('camera_type')!r}"
            for r in rows if r.get("camera_type") not in VALID_CAMERAS
        ]
        problemas += falha("tipo de camera invalido", cams)

        # 5. Contradicao logica: chute fora do gol marcado como gol
        contra = [
            f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: "
            f"{r.get('region')} + is_goal={r.get('is_goal')}"
            for r in rows
            if str(r.get("region", "")).startswith("fora") and _is_goal(r)
        ]
        problemas += falha("chute 'fora' marcado como GOL", contra)

        # 6. Frames incoerentes
        frames = []
        for r in rows:
            try:
                ini, chu = int(r["inicio_frame"]), int(r["chute_frame"])
            except (KeyError, TypeError, ValueError):
                frames.append(f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: frames ilegiveis")
                continue
            if ini >= chu:
                frames.append(f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: inicio {ini} >= chute {chu}")
        problemas += falha("marcacao de frames incoerente", frames)

        # 7. Linha gravada na competicao errada
        fora_lugar = [
            f"{r.get('video_file','?')} #{r.get('penalty_id','?')}: coluna={r.get('competition')}"
            for r in rows
            if r.get("competition") and r["competition"] != slug
        ]
        problemas += falha("linha no CSV de outra competicao", fora_lugar)

        print("  [OK] nenhum problema\n" if problemas == 0 else "")

    # Cruzamento entre competicoes: mesmo video rotulado em duas delas
    por_video: dict[str, set] = defaultdict(set)
    for slug, rows in data.items():
        for r in rows:
            por_video[r.get("video_file", "")].add(slug)
    cruzados = {v: s for v, s in por_video.items() if len(s) > 1}
    if cruzados:
        print(f"[X] videos rotulados em mais de uma competicao: {len(cruzados)}")
        for v, s in list(cruzados.items())[:10]:
            print(f"      {v} -> {sorted(s)}")
        problemas += 1

    print(f"\n{'TUDO CERTO' if problemas == 0 else f'{problemas} tipo(s) de problema encontrados'}")
    return 0 if problemas == 0 else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Conferencia do dataset de penaltis.")
    p.add_argument("comando", choices=("stats", "check"))
    p.add_argument("--output-base", default=DEFAULT_OUTPUT_BASE,
                   help=f"pasta base dos rotulos (padrao: {DEFAULT_OUTPUT_BASE})")
    args = p.parse_args()

    if not os.path.isdir(args.output_base):
        print(f"Pasta nao encontrada: {args.output_base}")
        return 1

    return cmd_stats(args.output_base) if args.comando == "stats" else cmd_check(args.output_base)


if __name__ == "__main__":
    sys.exit(main())
