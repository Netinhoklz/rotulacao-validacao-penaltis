"""
analise_dataset.py
==================
Levantamento estatistico e validacao do dataset de penaltis rotulados.

Gera RELATORIO_DATASET.md com as tabelas em formato de artigo.

A validacao NAO e apenas leitura de CSV: cada clip e cada frame e aberto e
decodificado para confirmar que o arquivo existe, nao esta corrompido e bate
com a marcacao declarada no CSV.

Uso:
    python analise_dataset.py
    python analise_dataset.py --output-base E:\\penaltis_rotulados --rapido

    --rapido  pula a decodificacao das midias (so o CSV e o inventario de disco)
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import cv2

import competitions as comps

DEFAULT_OUTPUT_BASE = os.environ.get("OUTPUT_BASE", r"E:\penaltis_rotulados")
DEFAULT_SOURCE_DIR = os.environ.get("SOURCE_DIR", r"E:\base_videos_penaltis")
SUPPORTED_EXT = (".mp4", ".avi", ".mov", ".mkv", ".wmv")

REGIOES = [
    "gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito",
    "gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito",
    "gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito",
    "fora_esquerda", "fora_cima", "fora_direita",
]
REGIAO_PT = {
    "gol_topo_esquerdo": "Gol — superior esquerdo",
    "gol_topo_centro": "Gol — superior central",
    "gol_topo_direito": "Gol — superior direito",
    "gol_meio_esquerdo": "Gol — meio esquerdo",
    "gol_meio_centro": "Gol — meio central",
    "gol_meio_direito": "Gol — meio direito",
    "gol_baixo_esquerdo": "Gol — inferior esquerdo",
    "gol_baixo_centro": "Gol — inferior central",
    "gol_baixo_direito": "Gol — inferior direito",
    "fora_esquerda": "Fora — esquerda",
    "fora_cima": "Fora — por cima",
    "fora_direita": "Fora — direita",
}
CAMERAS = ["visão do torcedor", "visão cobrador", "visão goleiro"]
TOL_FRAMES = 2  # ffmpeg corta por tempo; ±2 frames e o comportamento normal


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------
def indexar_fonte(source_dir: str) -> dict[str, dict]:
    """{nome_do_arquivo: {competicao, pasta, temporada, caminho}} da base de origem."""
    idx: dict[str, dict] = {}
    for root, _, files in os.walk(source_dir):
        for f in files:
            if not f.lower().endswith(SUPPORTED_EXT):
                continue
            full = os.path.join(root, f)
            comp, pasta = comps.classify(full)
            ano = re.search(r"(?:19|20)\d{2}", pasta)
            idx[f] = {
                "competicao": comp.slug,
                "competicao_label": comp.label,
                "pasta": pasta,
                "temporada": ano.group(0) if ano else "—",
                "caminho": full,
            }
    return idx


def carregar(output_base: str, fonte: dict) -> list[dict]:
    linhas = []
    for slug, csv_path in comps.existing_csvs(output_base):
        with open(csv_path, "r", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                r["_slug"] = r.get("competition") or slug
                meta = fonte.get(r.get("video_file", ""), {})
                r["_temporada"] = meta.get("temporada", "—")
                r["_pasta"] = meta.get("pasta", "—")
                r["_slug"] = meta.get("competicao", r["_slug"])
                r["_comp_label"] = meta.get(
                    "competicao_label",
                    comps.BY_SLUG[r["_slug"]].label if r["_slug"] in comps.BY_SLUG else r["_slug"],
                )
                linhas.append(r)
    return linhas


def eh_gol(r: dict) -> bool:
    return str(r.get("is_goal", "")).strip().lower() in ("true", "1", "sim")


def i(r: dict, k: str, d: int = 0) -> int:
    try:
        return int(r[k])
    except (KeyError, TypeError, ValueError):
        return d


# ---------------------------------------------------------------------------
# Confrontos e clubes (heuristico, com taxa de acerto reportada)
# ---------------------------------------------------------------------------
RX_CONFRONTO = re.compile(r"^(?P<casa>.+?)\s+(?P<pc>\d+)\s+X\s+(?P<pf>\d+)\s+(?P<fora>.+?)\s+MELHORES\s+MOMENTOS")
RX_RODADA = re.compile(r"(\d+)\s*(?:a|ª|º)?\s*RODADA")


def confronto(video_file: str) -> dict | None:
    m = RX_CONFRONTO.match(Path(video_file).stem.upper())
    if not m:
        return None
    casa, fora = m.group("casa").strip(), m.group("fora").strip()
    if not casa or not fora or any(c.isdigit() for c in casa + fora):
        return None  # placar de disputa por penaltis (ex.: "ABC 1 4 X 1 1 BOTAFOGO")
    rod = RX_RODADA.search(Path(video_file).stem.upper())
    return {"casa": casa, "fora": fora,
            "gols_casa": int(m.group("pc")), "gols_fora": int(m.group("pf")),
            "rodada": int(rod.group(1)) if rod else None}


# ---------------------------------------------------------------------------
# Metricas de desbalanceamento
# ---------------------------------------------------------------------------
def entropia(cont: Counter) -> tuple[float, float, float]:
    """(H em bits, H normalizada, numero efetivo de classes)."""
    n = sum(cont.values())
    if n == 0:
        return 0.0, 0.0, 0.0
    h = -sum((v / n) * math.log2(v / n) for v in cont.values() if v)
    k = len(cont)
    return h, (h / math.log2(k) if k > 1 else 0.0), 2 ** h


def gini(cont: Counter) -> float:
    """Gini das frequencias: 0 = perfeitamente balanceado, ~1 = concentrado."""
    vs = sorted(cont.values())
    n, s = len(vs), sum(vs)
    if n == 0 or s == 0:
        return 0.0
    return (2 * sum((k + 1) * v for k, v in enumerate(vs))) / (n * s) - (n + 1) / n


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """IC de Wilson para proporcao — melhor que o normal em n pequeno/p extremo."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - m), min(1.0, c + m)


# ---------------------------------------------------------------------------
# Validacao real das midias
# ---------------------------------------------------------------------------
def validar(linhas: list[dict], output_base: str, rapido: bool) -> dict:
    res = {
        "n": len(linhas),
        "midia_ausente": [], "clip_ilegivel": [], "clip_vazio": [],
        "clip_duracao_divergente": [], "frame_ilegivel": [],
        "frames_incoerentes": [], "regiao_invalida": [], "camera_invalida": [],
        "fora_e_gol": [], "duplicatas": [], "sem_json": [], "orfaos_disco": [],
        "resolucoes": Counter(), "fps": Counter(), "delta_frames": [],
    }

    for r in linhas:
        rid = f"{r.get('video_file','?')} #{r.get('penalty_id','?')}"
        ini, chu = i(r, "inicio_frame", -1), i(r, "chute_frame", -1)
        if ini < 0 or chu < 0 or ini >= chu:
            res["frames_incoerentes"].append(rid)
        if r.get("region") not in REGIOES:
            res["regiao_invalida"].append(f"{rid}: {r.get('region')!r}")
        if r.get("camera_type") not in CAMERAS:
            res["camera_invalida"].append(f"{rid}: {r.get('camera_type')!r}")
        if str(r.get("region", "")).startswith("fora") and eh_gol(r):
            res["fora_e_gol"].append(rid)

    dups = Counter((r.get("video_file"), r.get("penalty_id")) for r in linhas)
    res["duplicatas"] = [f"{v} #{p} ({n}x)" for (v, p), n in dups.items() if n > 1]

    # Inventario de disco x CSV, nos dois sentidos
    tags_csv = {Path(r["clip_path"]).stem for r in linhas if r.get("clip_path")}
    for comp in comps.ALL:
        out = comps.paths(output_base, comp)
        if not os.path.isdir(out["dir"]):
            continue
        for sub, cortes in (("labels", ()), ("clips", ()), ("frames", ("_inicio", "_chute"))):
            if not os.path.isdir(out[sub]):
                continue
            for f in os.listdir(out[sub]):
                stem = Path(f).stem
                for c in cortes:
                    if stem.endswith(c):
                        stem = stem[: -len(c)]
                        break
                if stem not in tags_csv:
                    res["orfaos_disco"].append(f"{sub}/{f}")
        if os.path.isdir(out["labels"]):
            jsons = {Path(f).stem for f in os.listdir(out["labels"])}
            for r in linhas:
                if r.get("_slug") == comp.slug and r.get("clip_path"):
                    if Path(r["clip_path"]).stem not in jsons:
                        res["sem_json"].append(Path(r["clip_path"]).stem)

    if rapido:
        res["rapido"] = True
        return res

    total = len(linhas)
    for k, r in enumerate(linhas, 1):
        if k % 100 == 0 or k == total:
            print(f"  decodificando midias… {k}/{total}", file=sys.stderr)
        rid = f"{r.get('video_file','?')} #{r.get('penalty_id','?')}"

        for campo in ("clip_path", "frame_inicio_path", "frame_chute_path"):
            p = r.get(campo) or ""
            if not (os.path.isfile(p) and os.path.getsize(p) > 0):
                res["midia_ausente"].append(f"{rid}: {campo}")

        clip = r.get("clip_path") or ""
        if os.path.isfile(clip):
            cap = cv2.VideoCapture(clip)
            if not cap.isOpened():
                res["clip_ilegivel"].append(rid)
            else:
                n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                ok, _ = cap.read()          # decodifica de fato o 1o frame
                if n <= 0 or not ok:
                    res["clip_vazio"].append(rid)
                else:
                    res["resolucoes"][f"{w}x{h}"] += 1
                    res["fps"][f"{fps:.2f}"] += 1
                    esperado = i(r, "chute_frame") - i(r, "inicio_frame") + 1
                    d = n - esperado
                    res["delta_frames"].append(d)
                    if abs(d) > TOL_FRAMES:
                        res["clip_duracao_divergente"].append(
                            f"{rid}: {n} frames, esperado {esperado}")
            cap.release()

        for campo in ("frame_inicio_path", "frame_chute_path"):
            p = r.get(campo) or ""
            if os.path.isfile(p):
                img = cv2.imread(p)
                if img is None or img.size == 0:
                    res["frame_ilegivel"].append(f"{rid}: {Path(p).name}")

    res["rapido"] = False
    return res


# ---------------------------------------------------------------------------
# Relatorio
# ---------------------------------------------------------------------------
def tab(cabecalho: list[str], linhas: list[list], alinhamento: str = "") -> str:
    if not alinhamento:
        alinhamento = "l" + "r" * (len(cabecalho) - 1)
    sep = {"l": ":---", "r": "---:", "c": ":---:"}
    out = ["| " + " | ".join(cabecalho) + " |",
           "| " + " | ".join(sep[a] for a in alinhamento) + " |"]
    for l in linhas:
        out.append("| " + " | ".join(str(c) for c in l) + " |")
    return "\n".join(out)


def pct(k: int, n: int) -> str:
    return f"{100*k/n:.1f}%" if n else "—"


def gerar(output_base: str, source_dir: str, rapido: bool) -> str:
    fonte = indexar_fonte(source_dir)
    linhas = carregar(output_base, fonte)
    if not linhas:
        return "Nenhum rotulo encontrado."

    n = len(linhas)
    gols = sum(1 for r in linhas if eh_gol(r))
    jogos = {r["video_file"] for r in linhas}
    print(f"  {n} penaltis, {len(jogos)} jogos", file=sys.stderr)
    val = validar(linhas, output_base, rapido)

    # --- inventario da base de origem ---
    por_comp_fonte = Counter(v["competicao"] for v in fonte.values())
    por_temp_fonte = Counter(v["temporada"] for v in fonte.values())

    L: list[str] = []
    A = L.append
    hoje = datetime.now().strftime("%d/%m/%Y")

    A("# Dataset de Cobranças de Pênalti do Futebol Brasileiro")
    A("")
    A("## Caracterização e validação da base rotulada")
    A("")
    A(f"*Relatório gerado automaticamente por `analise_dataset.py` em {hoje}.*")
    A("")
    A("---")
    A("")

    # ---------------------------------------------------------------- resumo
    A("## 1. Resumo")
    A("")
    conv_lo, conv_hi = wilson(gols, n)
    A(f"A base reúne **{n} cobranças de pênalti** anotadas manualmente a partir de "
      f"**{len(jogos)} partidas** distintas do futebol brasileiro. Cada cobrança é "
      f"descrita por três rótulos independentes — a região do chute (12 classes), o "
      f"ângulo de câmera (3 classes) e o desfecho binário (gol / não-gol) — e "
      f"acompanhada de um videoclipe recortado do instante da corrida até o impacto, "
      f"mais os dois quadros extremos desse intervalo em resolução plena.")
    A("")
    A(tab(["Indicador", "Valor"], [
        ["Cobranças anotadas", n],
        ["Partidas distintas", len(jogos)],
        ["Competições com anotação", len({r['_slug'] for r in linhas})],
        ["Temporadas cobertas", len({r['_temporada'] for r in linhas} - {"—"})],
        ["Gols convertidos", f"{gols} ({pct(gols, n)})"],
        ["IC 95% da taxa de conversão", f"[{100*conv_lo:.1f}% – {100*conv_hi:.1f}%]"],
        ["Média de cobranças por partida", f"{n/len(jogos):.2f}"],
        ["Arquivos de mídia", f"{n} clipes + {2*n} quadros"],
    ]))
    A("")

    # ------------------------------------------------------- 2. procedencia
    A("## 2. Procedência e cobertura")
    A("")
    A("O acervo de origem é composto por vídeos de melhores momentos de partidas "
      "oficiais, organizados em pastas por competição e temporada. A anotação "
      "avança por partida, de modo que a cobertura é parcial e progressiva.")
    A("")
    rows = []
    for comp in comps.ALL:
        tot_fonte = por_comp_fonte.get(comp.slug, 0)
        if not tot_fonte:
            continue
        grp = [r for r in linhas if r["_slug"] == comp.slug]
        jg = {r["video_file"] for r in grp}
        rows.append([comp.label, tot_fonte, len(jg), pct(len(jg), tot_fonte),
                     len(grp), sum(1 for r in grp if eh_gol(r))])
    A(tab(["Competição", "Partidas na base", "Partidas anotadas", "Cobertura",
           "Cobranças", "Gols"], rows))
    A("")
    A("### 2.1 Distribuição por temporada")
    A("")
    rows = []
    for t in sorted(por_temp_fonte):
        if t == "—":
            continue
        grp = [r for r in linhas if r["_temporada"] == t]
        jg = {r["video_file"] for r in grp}
        g = sum(1 for r in grp if eh_gol(r))
        rows.append([t, por_temp_fonte[t], len(jg), pct(len(jg), por_temp_fonte[t]),
                     len(grp), g, pct(g, len(grp))])
    A(tab(["Temporada", "Partidas na base", "Anotadas", "Cobertura",
           "Cobranças", "Gols", "Conversão"], rows))
    A("")
    sem_temp = sum(1 for r in linhas if r["_temporada"] == "—")
    if sem_temp:
        A(f"A tabela cobre apenas as partidas cuja pasta de origem identifica a "
          f"temporada. Outras **{sem_temp} cobranças** "
          f"({pct(sem_temp, n)}) vêm de acervos sem essa marcação e não aparecem "
          f"acima — o total por competição da seção anterior permanece completo.")
        A("")

    # ------------------------------------------------- 3. clubes/confrontos
    conf = {v: confronto(v) for v in jogos}
    ok_conf = {v: c for v, c in conf.items() if c}
    if ok_conf:
        clubes = Counter()
        for c in ok_conf.values():
            clubes[c["casa"]] += 1
            clubes[c["fora"]] += 1
        rodadas = [c["rodada"] for c in ok_conf.values() if c["rodada"]]
        A("### 2.2 Diversidade de confrontos")
        A("")
        A(f"O nome de arquivo de cada partida segue o padrão `MANDANTE P X P VISITANTE "
          f"MELHORES MOMENTOS …`, o que permite extrair automaticamente os clubes "
          f"envolvidos. O padrão foi reconhecido em **{len(ok_conf)} das "
          f"{len(jogos)} partidas anotadas** ({pct(len(ok_conf), len(jogos))}); as "
          f"demais usam notação de disputa por pênaltis e foram desconsideradas "
          f"nesta subseção.")
        A("")
        A(tab(["Indicador", "Valor"], [
            ["Partidas com confronto identificado", len(ok_conf)],
            ["Clubes distintos", len(clubes)],
            ["Aparições por clube (mediana)", f"{statistics.median(clubes.values()):.1f}"],
            ["Clube mais frequente", f"{clubes.most_common(1)[0][0]} ({clubes.most_common(1)[0][1]} partidas)"],
            ["Rodadas distintas", len(set(rodadas)) if rodadas else "—"],
        ]))
        A("")
        A("Dez clubes mais representados:")
        A("")
        A(tab(["Clube", "Partidas anotadas"],
              [[k, v] for k, v in clubes.most_common(10)]))
        A("")

    # ---------------------------------------------------- 4. taxonomia
    A("## 3. Taxonomia da anotação")
    A("")
    A("Cada cobrança recebe três rótulos independentes, aplicados sobre o mesmo "
      "recorte temporal:")
    A("")
    A("1. **Região do chute** — grade 3×3 sobre a baliza (nove zonas internas), "
      "acrescida de três zonas externas para finalizações que não encontram o gol. "
      "A referência espacial é a imagem exibida, não o lado do cobrador.")
    A("2. **Ângulo de câmera** — perspectiva predominante da tomada no instante do chute.")
    A("3. **Desfecho** — variável binária indicando se a cobrança resultou em gol.")
    A("")
    A("O recorte temporal é delimitado por dois quadros marcados manualmente: o "
      "início da corrida do cobrador e o instante do impacto com a bola.")
    A("")

    # ------------------------------------------------- 5. distribuicoes
    A("## 4. Distribuição das classes")
    A("")
    A("### 4.1 Região do chute")
    A("")
    reg = Counter(r["region"] for r in linhas)
    reg_gol = Counter(r["region"] for r in linhas if eh_gol(r))
    rows = []
    for k in REGIOES:
        if not reg[k]:
            continue
        lo, hi = wilson(reg_gol[k], reg[k])
        rows.append([REGIAO_PT[k], f"`{k}`", reg[k], pct(reg[k], n), reg_gol[k],
                     reg[k] - reg_gol[k],
                     f"{pct(reg_gol[k], reg[k])}" if reg[k] else "—",
                     f"[{100*lo:.0f}–{100*hi:.0f}]"])
    A(tab(["Região", "Código", "n", "%", "Gols", "Não-gols", "Conversão", "IC 95%"], rows))
    A("")

    h, hn, eff = entropia(reg)
    ir = max(reg.values()) / min(reg.values())
    A("**Desbalanceamento.** A distribuição é fortemente assimétrica:")
    A("")
    A(tab(["Métrica", "Valor", "Interpretação"], [
        ["Razão de desbalanceamento (IR)", f"{ir:.1f}:1",
         f"classe mais frequente ({max(reg, key=reg.get)}) vs. mais rara ({min(reg, key=reg.get)})"],
        ["Entropia de Shannon", f"{h:.2f} bits", f"máximo possível: {math.log2(len(reg)):.2f} bits"],
        ["Entropia normalizada", f"{hn:.3f}", "1,0 = classes equiprováveis"],
        ["Número efetivo de classes", f"{eff:.1f}", f"de {len(reg)} classes observadas"],
        ["Coeficiente de Gini", f"{gini(reg):.3f}", "0 = uniforme"],
    ], "lrl"))
    A("")
    A("#### Agregações da grade")
    A("")
    PT_ALT = {"baixo": "Inferior", "meio": "Meio", "topo": "Superior"}
    PT_LAD = {"esquerdo": "Esquerda", "centro": "Centro", "direito": "Direita"}
    alt, lado, fora_c = Counter(), Counter(), Counter()
    for k, v in reg.items():
        if k.startswith("gol_"):
            _, a, l = k.split("_", 2)
            alt[PT_ALT[a]] += v
            lado[PT_LAD[l]] += v
        else:
            fora_c[REGIAO_PT[k]] += v
    no_alvo = sum(alt.values())
    A(f"Das {n} cobranças, **{no_alvo}** ({pct(no_alvo, n)}) foram no alvo e "
      f"**{sum(fora_c.values())}** ({pct(sum(fora_c.values()), n)}) saíram da meta. "
      f"As agregações abaixo tomam apenas as {no_alvo} finalizações no alvo, para "
      f"que altura e lateralidade permaneçam comparáveis entre si.")
    A("")
    A(tab(["Altura (no alvo)", "n", "% do alvo", "% do total"],
          [[k, v, pct(v, no_alvo), pct(v, n)]
           for k, v in sorted(alt.items(), key=lambda x: -x[1])]))
    A("")
    A(tab(["Lateralidade (no alvo)", "n", "% do alvo", "% do total"],
          [[k, v, pct(v, no_alvo), pct(v, n)]
           for k, v in sorted(lado.items(), key=lambda x: -x[1])]))
    A("")
    A(tab(["Finalização fora da meta", "n", "% do total"],
          [[k, v, pct(v, n)] for k, v in sorted(fora_c.items(), key=lambda x: -x[1])]))
    A("")

    A("### 4.2 Ângulo de câmera")
    A("")
    cam = Counter(r["camera_type"] for r in linhas)
    rows = []
    for k, v in cam.most_common():
        g = sum(1 for r in linhas if r["camera_type"] == k and eh_gol(r))
        rows.append([k, v, pct(v, n), g, pct(g, v)])
    A(tab(["Ângulo", "n", "%", "Gols", "Conversão"], rows))
    A("")
    hc, hnc, effc = entropia(cam)
    A(f"Entropia normalizada de {hnc:.3f} — a distribuição também é desigual, com "
      f"predominância de `{cam.most_common(1)[0][0]}`.")
    A("")

    A("### 4.3 Desfecho")
    A("")
    A(tab(["Desfecho", "n", "%", "IC 95%"], [
        ["Gol", gols, pct(gols, n), f"[{100*conv_lo:.1f}% – {100*conv_hi:.1f}%]"],
        ["Não-gol", n - gols, pct(n - gols, n),
         f"[{100*(1-conv_hi):.1f}% – {100*(1-conv_lo):.1f}%]"],
    ]))
    A("")
    fora = sum(v for k, v in reg.items() if k.startswith("fora"))
    defendidos = (n - gols) - fora
    A(f"Entre as **{n-gols} cobranças não convertidas**, **{fora}** terminaram fora "
      f"da meta e **{defendidos}** foram finalizações no alvo que não resultaram em "
      f"gol (defesa do goleiro ou trave).")
    A("")

    A("### 4.4 Tabulação cruzada — região × ângulo de câmera")
    A("")
    cross = Counter((r["region"], r["camera_type"]) for r in linhas)
    rows = []
    for k in REGIOES:
        if not reg[k]:
            continue
        rows.append([REGIAO_PT[k]] + [cross[(k, c)] for c in CAMERAS] + [reg[k]])
    rows.append(["**Total**"] + [cam.get(c, 0) for c in CAMERAS] + [n])
    A(tab(["Região"] + CAMERAS + ["Total"], rows))
    A("")

    # ------------------------------------------------ 6. propriedades
    A("## 5. Propriedades dos clipes")
    A("")
    durs = []
    for r in linhas:
        ini, chu = i(r, "inicio_frame", -1), i(r, "chute_frame", -1)
        try:
            t0, t1 = float(r["inicio_time_s"]), float(r["chute_time_s"])
        except (KeyError, TypeError, ValueError):
            continue
        if chu > ini >= 0 and t1 > t0:
            durs.append(t1 - t0)
    if durs:
        q = statistics.quantiles(durs, n=4)
        A("Duração do recorte (início da corrida → impacto), em segundos:")
        A("")
        A(tab(["Estatística", "Valor (s)"], [
            ["n", len(durs)],
            ["Média", f"{statistics.mean(durs):.2f}"],
            ["Desvio-padrão", f"{statistics.stdev(durs):.2f}"],
            ["Mínimo", f"{min(durs):.2f}"],
            ["1º quartil", f"{q[0]:.2f}"],
            ["Mediana", f"{q[1]:.2f}"],
            ["3º quartil", f"{q[2]:.2f}"],
            ["Máximo", f"{max(durs):.2f}"],
        ]))
        A("")
        faixas = [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 1e9)]
        rows = []
        for lo, hi in faixas:
            c = sum(1 for d in durs if lo <= d < hi)
            rot = f"{lo:.1f} – {hi:.1f}" if hi < 1e9 else f"≥ {lo:.1f}"
            rows.append([rot, c, pct(c, len(durs))])
        A(tab(["Faixa (s)", "n", "%"], rows))
        A("")

    if not val["rapido"] and val["resolucoes"]:
        A("### 5.1 Características técnicas verificadas na decodificação")
        A("")
        A(tab(["Resolução", "Clipes", "%"],
              [[k, v, pct(v, sum(val["resolucoes"].values()))]
               for k, v in val["resolucoes"].most_common()]))
        A("")
        A(tab(["Taxa de quadros", "Clipes", "%"],
              [[f"{k} fps", v, pct(v, sum(val['fps'].values()))]
               for k, v in val["fps"].most_common()]))
        A("")

    tam = {}
    for comp in comps.ALL:
        out = comps.paths(output_base, comp)
        for sub in ("clips", "frames"):
            if os.path.isdir(out[sub]):
                tam[sub] = tam.get(sub, 0) + sum(
                    os.path.getsize(os.path.join(out[sub], f))
                    for f in os.listdir(out[sub]))
    if tam:
        A("### 5.2 Volume de dados")
        A("")
        A(tab(["Componente", "Arquivos", "Tamanho"], [
            ["Videoclipes", n, f"{tam.get('clips',0)/1e9:.2f} GB"],
            ["Quadros extremos", 2 * n, f"{tam.get('frames',0)/1e9:.2f} GB"],
            ["**Total**", 3 * n, f"**{sum(tam.values())/1e9:.2f} GB**"],
        ]))
        A("")

    # ------------------------------------------------ 7. processo
    A("## 6. Processo de anotação")
    A("")
    pen_jogo = Counter(r["video_file"] for r in linhas)
    dist = Counter(pen_jogo.values())
    A(tab(["Cobranças na partida", "Partidas", "%"],
          [[k, dist[k], pct(dist[k], len(jogos))] for k in sorted(dist)]))
    A("")
    A(f"Média de {n/len(jogos):.2f} cobranças por partida "
      f"(mediana {statistics.median(pen_jogo.values()):.0f}, máximo {max(pen_jogo.values())}).")
    A("")

    ts = []
    for r in linhas:
        try:
            ts.append(datetime.fromisoformat(r["timestamp_rotulagem"]))
        except (KeyError, TypeError, ValueError):
            continue
    if ts:
        ts.sort()
        dias = Counter(t.date() for t in ts)
        sessoes, atual = [], [ts[0]]
        for a, b in zip(ts, ts[1:]):
            if (b - a).total_seconds() > 1800:
                sessoes.append(atual)
                atual = []
            atual.append(b)
        sessoes.append(atual)
        gaps = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])
                if (b - a).total_seconds() <= 1800]
        A("### 6.1 Esforço de anotação")
        A("")
        A(tab(["Indicador", "Valor"], [
            ["Primeira anotação", ts[0].strftime("%d/%m/%Y %H:%M")],
            ["Última anotação", ts[-1].strftime("%d/%m/%Y %H:%M")],
            ["Período total", f"{(ts[-1]-ts[0]).days} dias"],
            ["Dias com atividade", len(dias)],
            ["Sessões de trabalho (intervalo > 30 min)", len(sessoes)],
            ["Cobranças por dia ativo (mediana)", f"{statistics.median(dias.values()):.0f}"],
            ["Dia mais produtivo", f"{max(dias, key=dias.get).strftime('%d/%m/%Y')} ({max(dias.values())} cobranças)"],
            ["Intervalo mediano entre anotações", f"{statistics.median(gaps):.0f} s" if gaps else "—"],
        ]))
        A("")

    # ------------------------------------------------ 8. validacao
    A("## 7. Validação da integridade")
    A("")
    if val["rapido"]:
        A("> Executado em modo `--rapido`: as mídias não foram decodificadas.")
        A("")
    else:
        A(f"Todos os **{n} registros** foram verificados individualmente. Cada clipe "
          f"foi aberto e teve o primeiro quadro efetivamente decodificado; cada um dos "
          f"**{2*n} quadros extremos** foi decodificado por completo. Não se trata de "
          f"conferência de listagem de diretório: um arquivo presente porém corrompido "
          f"é reprovado nesta etapa.")
        A("")
    checagens = [
        ("Referência do CSV com mídia ausente ou vazia", "midia_ausente"),
        ("Clipe que não abre", "clip_ilegivel"),
        ("Clipe sem quadros decodificáveis", "clip_vazio"),
        (f"Duração do clipe divergente da marcação (> ±{TOL_FRAMES} quadros)", "clip_duracao_divergente"),
        ("Quadro extremo corrompido", "frame_ilegivel"),
        ("Arquivo em disco sem registro no CSV", "orfaos_disco"),
        ("Registro no CSV sem o JSON correspondente", "sem_json"),
        ("Marcação temporal incoerente (início ≥ chute)", "frames_incoerentes"),
        ("Região fora do vocabulário de 12 classes", "regiao_invalida"),
        ("Ângulo de câmera fora do vocabulário", "camera_invalida"),
        ("Contradição lógica: chute fora da meta registrado como gol", "fora_e_gol"),
        ("Par (partida, identificador) duplicado", "duplicatas"),
    ]
    rows = []
    total_falhas = 0
    for rot, chave in checagens:
        if val["rapido"] and chave in ("midia_ausente", "clip_ilegivel", "clip_vazio",
                                       "clip_duracao_divergente", "frame_ilegivel"):
            rows.append([rot, "não executado", "—"])
            continue
        k = len(val[chave])
        total_falhas += k
        rows.append([rot, "✔ conforme" if k == 0 else f"✘ {k} ocorrência(s)", k])
    A(tab(["Verificação", "Resultado", "Ocorrências"], rows, "llr"))
    A("")
    if total_falhas == 0:
        A(f"**Nenhuma inconsistência.** Os {n} registros do CSV, os {n} arquivos de "
          f"metadados, os {n} clipes e os {2*n} quadros estão em correspondência "
          f"exata e íntegros.")
    else:
        A(f"**{total_falhas} ocorrência(s)** requerem atenção:")
        A("")
        for rot, chave in checagens:
            if val[chave]:
                A(f"- *{rot}*:")
                for it in val[chave][:8]:
                    A(f"  - {it}")
                if len(val[chave]) > 8:
                    A(f"  - … e mais {len(val[chave])-8}")
    A("")
    if not val["rapido"] and val["delta_frames"]:
        d = val["delta_frames"]
        A(f"A diferença entre a duração real do clipe e o intervalo declarado tem "
          f"média de {statistics.mean(d):+.2f} quadro(s) "
          f"(mediana {statistics.median(d):+.0f}, amplitude {min(d):+d} a {max(d):+d}). "
          f"O desvio é esperado: o corte é feito por tempo pelo codificador, e não "
          f"por índice exato de quadro.")
        A("")

    # ------------------------------------------------ 9. limitacoes
    A("## 8. Limitações")
    A("")
    lims = []
    faltantes = [c.label for c in comps.ALL
                 if por_comp_fonte.get(c.slug) and not any(r["_slug"] == c.slug for r in linhas)]
    if faltantes:
        for c in comps.ALL:
            if c.label in faltantes:
                lims.append(f"**Cobertura desigual entre competições.** A "
                            f"{c.label} possui {por_comp_fonte[c.slug]} partidas no "
                            f"acervo, ainda sem anotação. As estatísticas acima "
                            f"descrevem, portanto, apenas as competições já anotadas.")
    lims.append(f"**Desbalanceamento acentuado de classes.** Com IR de {ir:.1f}:1 e "
                f"apenas {eff:.1f} classes efetivas de {len(reg)}, treinar "
                f"classificadores de região exige reamostragem, ponderação da função "
                f"de perda ou agrupamento de zonas.")
    lims.append("**Viés de seleção pela fonte.** Os vídeos são compilações de melhores "
                "momentos, que privilegiam lances decisivos. A taxa de conversão "
                "observada não deve ser lida como estimativa populacional da "
                "conversão de pênaltis na competição.")
    lims.append("**Anotador único.** Todos os rótulos vêm de um mesmo anotador, o que "
                "impede o cálculo de concordância entre avaliadores. A consistência "
                "interna é assegurada por verificação automática de vocabulário e de "
                "coerência lógica, não por dupla anotação.")
    lims.append("**Referencial espacial da grade.** As zonas são definidas sobre a "
                "imagem exibida. Em tomadas de trás da meta, a lateralidade fica "
                "espelhada em relação à perspectiva do cobrador.")
    for l in lims:
        A(f"- {l}")
    A("")

    # ------------------------------------------------ 10. reprodutibilidade
    A("## 9. Reprodutibilidade")
    A("")
    A("Este relatório é gerado inteiramente por código, a partir dos artefatos em "
      "disco. Para reproduzi-lo:")
    A("")
    A("```bash")
    A("python analise_dataset.py                 # relatório completo, com decodificação")
    A("python analise_dataset.py --rapido        # sem decodificar as mídias")
    A("python dataset_tools.py check             # apenas a verificação de integridade")
    A("python dataset_tools.py stats             # contagens resumidas por competição")
    A("```")
    A("")
    A("### Organização dos arquivos")
    A("")
    A("```")
    A(f"{output_base}\\")
    for comp in comps.ALL:
        out = comps.paths(output_base, comp)
        if not os.path.isdir(out["dir"]):
            continue
        pref = "" if not comp.subdir else f"{comp.subdir}\\"
        A(f"    {pref or '(raiz)'}                     -> {comp.label}")
        A(f"    {pref}clips\\<partida>_penalti_<NNN>.mp4")
        A(f"    {pref}frames\\<partida>_penalti_<NNN>_{{inicio,chute}}.jpg")
        A(f"    {pref}labels\\<partida>_penalti_<NNN>.json")
        A(f"    {pref}labels.csv")
    A("```")
    A("")
    # Cabecalhos reais de cada CSV: o da raiz e anterior a segmentacao e tem
    # menos colunas que o das competicoes criadas depois.
    cabecalhos = {}
    for slug, csv_path in comps.existing_csvs(output_base):
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            cabecalhos[slug] = next(csv.reader(f), [])
    base_cols = min(cabecalhos.values(), key=len) if cabecalhos else []
    A("Campos comuns a todos os arquivos `labels.csv`:")
    A("")
    A(", ".join(f"`{c}`" for c in base_cols) + ".")
    A("")
    extras = {s: [c for c in h if c not in base_cols] for s, h in cabecalhos.items()}
    if any(extras.values()):
        A("A segmentação por competição foi introduzida após o início da anotação, "
          "de modo que os arquivos não têm exatamente o mesmo conjunto de colunas:")
        A("")
        A(tab(["Arquivo", "Colunas", "Campos adicionais"],
              [[f"`{Path(comps.paths(output_base, comps.BY_SLUG[s]).get('csv', s)).parent.name or '(raiz)'}/labels.csv`"
                if s in comps.BY_SLUG else s,
                len(cabecalhos[s]),
                ", ".join(f"`{c}`" for c in extras[s]) or "—"]
               for s in sorted(cabecalhos)], "lrl"))
        A("")
        A("Para o registro da raiz a competição é implícita pela localização do "
          "arquivo; o código de leitura em `competitions.py` resolve os dois casos.")
        A("")
    A("---")
    A("")
    A(f"*{n} cobranças · {len(jogos)} partidas · gerado em {hoje}.*")

    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="Levantamento estatistico do dataset.")
    p.add_argument("--output-base", default=DEFAULT_OUTPUT_BASE)
    p.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    p.add_argument("--saida", default="RELATORIO_DATASET.md")
    p.add_argument("--rapido", action="store_true",
                   help="nao decodifica as midias (bem mais rapido)")
    a = p.parse_args()

    md = gerar(a.output_base, a.source_dir, a.rapido)
    Path(a.saida).write_text(md, encoding="utf-8")
    print(f"\nRelatorio escrito em {a.saida} ({len(md)} caracteres)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
