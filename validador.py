"""
validador.py
============
Revisao assistida dos pontos de atencao do dataset rotulado.

Serve para conferir, contra o VIDEO ORIGINAL, os registros que a analise
estatistica marcou como suspeitos - e corrigi-los quando for o caso.

SEGURANCA (a regra que manda em todo o resto)
---------------------------------------------
Este script NUNCA escreve no labels.csv original. Toda a edicao acontece em
uma copia de trabalho, dentro de uma pasta propria:

    <output_base>/_validacao/
        original/labels_original_<ts>.csv   copia imutavel (somente leitura)
        original/MANIFEST.json              sha256 + contagem do original
        labels_validado.csv                 <- copia de trabalho, editavel
        revisao.csv                         fila de pontos de atencao
        descartes.csv                       linhas removidas (nada e apagado)
        auditoria.jsonl                     log append-only de cada alteracao
        frames_revisao/                     contact sheets gerados pelo `ver`

O prefixo "_" na pasta e proposital: competitions.existing_csvs() ignora
pastas que comecam com "_", entao a copia de trabalho nao entra na contagem
de "ja rotulado" nem e confundida com uma competicao nova.

O original so e tocado pelo comando `promover`, que exige confirmacao digitada
e faz backup datado antes de qualquer coisa.

Uso tipico
----------
    python validador.py init             # cria backup e monta a fila
    python validador.py status           # progresso da revisao
    python validador.py listar           # pontos de atencao pendentes
    python validador.py ver R001         # abre o video no lance + contact sheet
    python validador.py editar R001 --linha 2 --region gol_baixo_direito
    python validador.py ok R001 --nota "conferido, rotulo correto"
    python validador.py diff             # o que mudou vs o original
    python validador.py promover         # (opcional) aplica sobre o original
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import competitions as comps

try:                                    # o console do Windows costuma ser cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------
DEFAULT_OUTPUT_BASE = os.environ.get("OUTPUT_BASE", r"E:\penaltis_rotulados")
DEFAULT_SOURCE_DIR  = os.environ.get("SOURCE_DIR",  r"E:\base_videos_penaltis")

VALIDACAO_SUBDIR = "_validacao"

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

# Um "lance" agrupa rotulos vizinhos no tempo: o mesmo penalti costuma ser
# rotulado 2-3 vezes (ao vivo + replays de outro angulo). Medido na base:
# gap mediano entre angulos do mesmo lance = 6,3s (p95 = 20s); entre lances
# diferentes = 58,6s. 20s separa os dois grupos com folga.
LANCE_GAP_S = 20.0
LANCE_GAP_AMBIGUO_S = 60.0   # entre 20s e 60s a separacao nao e obvia

# Janela inicio->chute considerada normal (IQR da base: mediana 1,17s).
DUR_MIN_S, DUR_MAX_S = 0.30, 2.53
DUR_MIN_FRAMES = 4
FPS_ESPERADO = 30.0
FPS_TOLERANCIA = 1.5

PRIORIDADES = ("CRITICA", "ALTA", "MEDIA", "BAIXA")


class ValidacaoErro(Exception):
    """
    Erro de uso previsto (regiao invalida, linha inexistente, edicao incoerente).

    As funcoes `aplicar_*` levantam isto em vez de imprimir e sair: o CLI
    converte em mensagem de erro, o servidor web converte em HTTP 400. A regra
    de negocio fica escrita uma vez so.
    """


# ---------------------------------------------------------------------------
# Utilitarios de arquivo - o cinto de seguranca
# ---------------------------------------------------------------------------
class Paths:
    """Todos os caminhos da validacao, derivados do output_base."""

    def __init__(self, output_base: str):
        self.output_base = os.path.abspath(output_base)
        self.csv_original = os.path.join(self.output_base, "labels.csv")
        self.dir          = os.path.join(self.output_base, VALIDACAO_SUBDIR)
        self.dir_original = os.path.join(self.dir, "original")
        self.dir_frames   = os.path.join(self.dir, "frames_revisao")
        self.manifest     = os.path.join(self.dir_original, "MANIFEST.json")
        self.trabalho     = os.path.join(self.dir, "labels_validado.csv")
        self.revisao      = os.path.join(self.dir, "revisao.csv")
        self.descartes    = os.path.join(self.dir, "descartes.csv")
        self.auditoria    = os.path.join(self.dir, "auditoria.jsonl")
        self.indice_vids  = os.path.join(self.dir, "indice_videos.json")

    def criar_dirs(self) -> None:
        for d in (self.dir, self.dir_original, self.dir_frames):
            os.makedirs(d, exist_ok=True)


def _guard(paths: Paths, destino: str) -> str:
    """
    Ultima barreira antes de qualquer escrita.

    Levanta se o destino for o labels.csv original ou qualquer coisa fora da
    pasta _validacao. Toda funcao que escreve passa por aqui - assim um erro
    de digitacao em um caminho vira excecao, nao perda de dado.
    """
    alvo = os.path.abspath(destino)
    if alvo == os.path.abspath(paths.csv_original):
        raise PermissionError(
            "BLOQUEADO: tentativa de escrever no labels.csv ORIGINAL. "
            "A validacao so escreve em _validacao/. Use `promover` se a "
            "intencao for mesmo aplicar as correcoes no original."
        )
    if not alvo.startswith(os.path.abspath(paths.dir) + os.sep):
        raise PermissionError(f"BLOQUEADO: escrita fora de _validacao/: {alvo}")
    return alvo


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def escrever_csv(paths: Paths, destino: str, fieldnames: list[str],
                 linhas: Iterable[dict], *, guard: bool = True) -> None:
    """Escrita atomica: grava .tmp no mesmo disco e so entao troca."""
    alvo = _guard(paths, destino) if guard else os.path.abspath(destino)
    tmp = alvo + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for linha in linhas:
            w.writerow(linha)
    os.replace(tmp, alvo)


def ler_csv(path: str) -> tuple[list[str], list[dict]]:
    if not os.path.isfile(path):
        return [], []
    with open(path, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def somente_leitura(path: str) -> None:
    try:
        os.chmod(path, stat.S_IREAD)
    except OSError:
        pass


def auditar(paths: Paths, acao: str, **campos: Any) -> None:
    """Log append-only. E o que torna `desfazer` possivel."""
    _guard(paths, paths.auditoria)
    registro = {"ts": datetime.now().isoformat(timespec="seconds"),
                "acao": acao, "desfeito": False, **campos}
    with open(paths.auditoria, "a", encoding="utf-8") as f:
        f.write(json.dumps(registro, ensure_ascii=False) + "\n")


def ler_auditoria(paths: Paths) -> list[dict]:
    if not os.path.isfile(paths.auditoria):
        return []
    with open(paths.auditoria, "r", encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
# Normalizacao e acesso as linhas
# ---------------------------------------------------------------------------
_TRACOS = dict.fromkeys(map(ord, "–—−"), "-")


def normalizar_traco(texto: str) -> str:
    return " ".join(str(texto).translate(_TRACOS).split())


def row_id(linha: dict) -> str:
    """Chave estavel de uma linha do CSV (verificado: 0 duplicatas na base)."""
    return f"{linha.get('video_file','')}|{linha.get('penalty_id','')}"


def is_goal(linha: dict) -> bool:
    return str(linha.get("is_goal", "")).strip().lower() in ("true", "1", "sim", "yes")


def num(linha: dict, campo: str, default: float = 0.0) -> float:
    try:
        return float(linha.get(campo, "") or default)
    except (TypeError, ValueError):
        return default


def duracao_s(linha: dict) -> float:
    return num(linha, "chute_time_s") - num(linha, "inicio_time_s")


def duracao_frames(linha: dict) -> int:
    return int(num(linha, "chute_frame") - num(linha, "inicio_frame"))


def fps_estimado(linha: dict) -> Optional[float]:
    d = duracao_s(linha)
    return duracao_frames(linha) / d if d > 0 else None


def agrupar_lances(linhas: list[dict], gap: float = LANCE_GAP_S) -> dict[str, list[dict]]:
    """
    Agrupa por proximidade temporal dentro do mesmo video, IGNORANDO a regiao.

    Ignorar a regiao e o ponto: e justamente quando dois angulos do mesmo
    lance recebem regioes diferentes que existe um erro de rotulagem para
    achar. Agrupar por regiao esconderia esses casos.
    """
    por_video: dict[str, list[dict]] = defaultdict(list)
    for l in linhas:
        por_video[l.get("video_file", "")].append(l)

    lances: dict[str, list[dict]] = {}
    for video, rows in por_video.items():
        rows.sort(key=lambda r: num(r, "chute_time_s"))
        idx, anterior = 0, None
        for r in rows:
            t = num(r, "chute_time_s")
            if anterior is None or (t - anterior) > gap:
                idx += 1
            anterior = t
            lances.setdefault(f"{video}#L{idx}", []).append(r)
    return lances


# ---------------------------------------------------------------------------
# Deteccao dos pontos de atencao
# ---------------------------------------------------------------------------
def detectar(linhas: list[dict]) -> list[dict]:
    """
    Varre a base e devolve a fila de revisao, mais grave primeiro.

    Cada item aponta para uma ou mais linhas do CSV (campo `linhas`, com os
    row_id separados por ';') e traz o instante do video onde conferir.
    """
    achados: list[dict] = []
    lances = agrupar_lances(linhas)

    def add(prioridade: str, tipo: str, descricao: str,
            rows: list[dict], lance_id: str = "") -> None:
        rows = sorted(rows, key=lambda r: num(r, "chute_time_s"))
        achados.append({
            "prioridade": prioridade,
            "tipo": tipo,
            "descricao": descricao,
            "video_file": rows[0].get("video_file", ""),
            "lance_id": lance_id,
            "n_linhas": len(rows),
            "linhas": ";".join(row_id(r) for r in rows),
            "t_inicio_s": f"{num(rows[0], 'inicio_time_s'):.2f}",
            "t_chute_s": ";".join(f"{num(r, 'chute_time_s'):.2f}" for r in rows),
        })

    # ---- checagens no nivel do LANCE (varios angulos do mesmo penalti) ----
    for lance_id, rows in lances.items():
        regioes = {r.get("region", "") for r in rows}
        gols = {is_goal(r) for r in rows}
        cameras = [r.get("camera_type", "") for r in rows]

        if len(regioes) > 1:
            # o mesmo chute recebeu regioes diferentes em angulos diferentes;
            # no maximo um deles esta certo
            fora = any(reg.startswith("fora") for reg in regioes)
            dentro = any(reg.startswith("gol") for reg in regioes)
            if fora and dentro:
                add("CRITICA", "gol_vs_fora",
                    f"mesmo lance rotulado como GOL e como FORA: {sorted(regioes)}",
                    rows, lance_id)
            else:
                add("CRITICA", "regiao_divergente",
                    f"mesmo lance com regioes diferentes: {sorted(regioes)}",
                    rows, lance_id)

        if len(gols) > 1:
            add("CRITICA", "is_goal_divergente",
                "mesmo lance com is_goal True e False ao mesmo tempo",
                rows, lance_id)

        if len(cameras) != len(set(cameras)):
            repetidas = sorted({c for c in cameras if cameras.count(c) > 1})
            add("ALTA", "camera_repetida",
                f"camera repetida no mesmo lance ({', '.join(repetidas)}): "
                f"ou o replay veio de angulo parecido, ou sao 2 penaltis distintos",
                rows, lance_id)

        if len(rows) > 3:
            add("ALTA", "muitos_angulos",
                f"{len(rows)} rotulos agrupados no mesmo lance - conferir se "
                f"nao sao penaltis diferentes proximos no tempo",
                rows, lance_id)

    # ---- agrupamento ambiguo: gap na zona cinzenta entre 20s e 60s ----
    por_video: dict[str, list[dict]] = defaultdict(list)
    for l in linhas:
        por_video[l.get("video_file", "")].append(l)
    for video, rows in por_video.items():
        rows.sort(key=lambda r: num(r, "chute_time_s"))
        for a, b in zip(rows, rows[1:]):
            gap = num(b, "chute_time_s") - num(a, "chute_time_s")
            if LANCE_GAP_S < gap <= LANCE_GAP_AMBIGUO_S and a.get("region") == b.get("region"):
                add("MEDIA", "agrupamento_ambiguo",
                    f"gap de {gap:.1f}s com a mesma regiao: replay tardio "
                    f"(1 penalti) ou 2 penaltis iguais?", [a, b])

    # ---- checagens no nivel da LINHA ----
    for l in linhas:
        rid = row_id(l)
        reg = l.get("region", "")
        rotulo_bruto = l.get("region_label", "")
        dur, durf = duracao_s(l), duracao_frames(l)
        fps = fps_estimado(l)

        if reg not in VALID_REGIONS:
            add("CRITICA", "regiao_invalida", f"region fora do vocabulario: '{reg}'", [l])
        if l.get("camera_type", "") not in VALID_CAMERAS:
            add("ALTA", "camera_invalida",
                f"camera_type fora do vocabulario: '{l.get('camera_type','')}'", [l])
        if reg.startswith("fora") and is_goal(l):
            add("CRITICA", "fora_marcado_gol",
                "region 'fora_*' com is_goal=True - contradicao logica", [l])

        if dur <= 0:
            add("CRITICA", "janela_invalida",
                f"chute nao vem depois do inicio (duracao {dur:.3f}s)", [l])
        elif durf < DUR_MIN_FRAMES:
            add("ALTA", "janela_curta",
                f"janela de {durf} frame(s) ({dur:.3f}s) - curta demais para "
                f"conter a corrida e o chute", [l])
        elif dur < DUR_MIN_S:
            add("MEDIA", "janela_curta",
                f"janela de {dur:.2f}s abaixo do minimo esperado ({DUR_MIN_S}s)", [l])
        elif dur > DUR_MAX_S:
            add("MEDIA", "janela_longa",
                f"janela de {dur:.2f}s ({durf} frames) - outlier, provavel "
                f"inicio marcado cedo demais", [l])

        if fps is not None and abs(fps - FPS_ESPERADO) > FPS_TOLERANCIA:
            add("MEDIA", "fps_atipico",
                f"fps estimado {fps:.1f} (esperado ~{FPS_ESPERADO:.0f}) - amostrar "
                f"frames por tempo, nao por indice, neste video", [l])

        if rotulo_bruto != normalizar_traco(rotulo_bruto):
            add("BAIXA", "traco_travessao",
                "region_label gravado com travessao '–' em vez de hifen '-' "
                "(corrigivel em lote: `python validador.py corrigir-tracos`)", [l])
        elif reg in VALID_REGIONS and rotulo_bruto != REGION_LABELS[reg]:
            add("ALTA", "rotulo_incoerente",
                f"region_label '{rotulo_bruto}' nao corresponde a region '{reg}' "
                f"(esperado '{REGION_LABELS[reg]}')", [l])

        for campo, nome in (("clip_path", "clip"),
                            ("frame_inicio_path", "frame de inicio"),
                            ("frame_chute_path", "frame de chute")):
            caminho = l.get(campo, "")
            if caminho and not os.path.isfile(caminho):
                add("ALTA", "artefato_ausente", f"{nome} nao existe no disco: {caminho}", [l])

    ordem = {p: i for i, p in enumerate(PRIORIDADES)}
    achados.sort(key=lambda a: (ordem.get(a["prioridade"], 9), a["tipo"],
                                a["video_file"], a["t_inicio_s"]))
    for i, a in enumerate(achados, 1):
        a["id_revisao"] = f"R{i:03d}"
        a["status"] = "pendente"
        a["decisao"] = ""
        a["revisado_em"] = ""
        a["nota"] = ""
    return achados


REVISAO_HEADER = ["id_revisao", "prioridade", "tipo", "status", "descricao",
                  "video_file", "lance_id", "n_linhas", "linhas",
                  "t_inicio_s", "t_chute_s", "decisao", "revisado_em", "nota"]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------
def cmd_init(paths: Paths, force: bool = False) -> int:
    if not os.path.isfile(paths.csv_original):
        print(f"ERRO: nao encontrei {paths.csv_original}")
        return 1

    if os.path.isfile(paths.trabalho) and not force:
        print(f"Ja existe uma validacao em andamento em {paths.dir}")
        print("  - `status` para ver o progresso")
        print("  - `init --force` para RECOMECAR (a copia de trabalho atual e")
        print("    preservada com sufixo .substituido-<ts>, nada e perdido)")
        return 1

    paths.criar_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    if os.path.isfile(paths.trabalho) and force:
        antigo = f"{paths.trabalho}.substituido-{ts}"
        shutil.move(paths.trabalho, _guard(paths, antigo))
        print(f"copia de trabalho anterior preservada em {os.path.basename(antigo)}")

    # 1) snapshot imutavel do original
    snap = os.path.join(paths.dir_original, f"labels_original_{ts}.csv")
    _guard(paths, snap)
    shutil.copy2(paths.csv_original, snap)
    somente_leitura(snap)

    digest = sha256(paths.csv_original)
    header, linhas = ler_csv(paths.csv_original)
    manifesto = {
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "csv_original": paths.csv_original,
        "sha256_original": digest,
        "n_linhas": len(linhas),
        "snapshot": snap,
        "header": header,
    }
    _guard(paths, paths.manifest)
    with open(paths.manifest, "w", encoding="utf-8") as f:
        json.dump(manifesto, f, ensure_ascii=False, indent=2)

    # 2) copia de trabalho (mesmo header do original: drop-in replacement)
    escrever_csv(paths, paths.trabalho, header, linhas)

    # 3) fila de revisao
    achados = detectar(linhas)
    escrever_csv(paths, paths.revisao, REVISAO_HEADER, achados)
    escrever_csv(paths, paths.descartes, header + ["_motivo", "_descartado_em"], [])

    auditar(paths, "init", sha256_original=digest, n_linhas=len(linhas),
            n_pontos_atencao=len(achados))

    print(f"\nValidacao iniciada em {paths.dir}\n")
    print(f"  original      : {len(linhas)} linhas, sha256 {digest[:16]}...")
    print(f"  snapshot      : {os.path.basename(snap)} (somente leitura)")
    print(f"  copia trabalho: labels_validado.csv")
    print(f"  pontos atencao: {len(achados)}\n")
    _resumo_fila(achados)
    print("\nProximo passo: `python validador.py listar --prioridade CRITICA`")
    return 0


def _resumo_fila(achados: list[dict]) -> None:
    por_prio: dict[str, int] = defaultdict(int)
    por_tipo: dict[tuple[str, str], int] = defaultdict(int)
    for a in achados:
        por_prio[a["prioridade"]] += 1
        por_tipo[(a["prioridade"], a["tipo"])] += 1

    print(f"  {'PRIORIDADE':<12}{'TIPO':<24}{'N':>5}")
    print("  " + "-" * 41)
    for prio in PRIORIDADES:
        if not por_prio.get(prio):
            continue
        for (p, tipo), n in sorted(por_tipo.items(), key=lambda kv: -kv[1]):
            if p == prio:
                print(f"  {prio:<12}{tipo:<24}{n:>5}")
        print(f"  {'':<12}{'subtotal':<24}{por_prio[prio]:>5}")
    print("  " + "-" * 41)
    print(f"  {'TOTAL':<36}{len(achados):>5}")


# ---------------------------------------------------------------------------
# Carga do estado
# ---------------------------------------------------------------------------
def carregar(paths: Paths) -> tuple[list[str], list[dict], list[dict]]:
    if not os.path.isfile(paths.trabalho):
        raise SystemExit("Validacao nao iniciada. Rode `python validador.py init`.")
    header, linhas = ler_csv(paths.trabalho)
    _, revisoes = ler_csv(paths.revisao)
    return header, linhas, revisoes


def verificar_original(paths: Paths) -> None:
    """Avisa se o labels.csv original mudou desde o init."""
    if not os.path.isfile(paths.manifest):
        return
    with open(paths.manifest, "r", encoding="utf-8") as f:
        man = json.load(f)
    if os.path.isfile(paths.csv_original) and sha256(paths.csv_original) != man["sha256_original"]:
        print("AVISO: o labels.csv original mudou depois do init desta validacao.")
        print("       A copia de trabalho esta baseada na versao antiga.")
        print("       Considere rodar `init --force` para recomecar do estado atual.\n")


def achar_revisao(revisoes: list[dict], ident: str) -> dict:
    ident = ident.strip()
    for r in revisoes:
        if r["id_revisao"].lower() == ident.lower():
            return r
    raise ValidacaoErro(f"Ponto de atencao '{ident}' nao encontrado.")


def linhas_do_ponto(ponto: dict, linhas: list[dict]) -> list[dict]:
    ids = [i for i in ponto["linhas"].split(";") if i]
    por_id = {row_id(l): l for l in linhas}
    return [por_id[i] for i in ids if i in por_id]


# ---------------------------------------------------------------------------
# listar / status
# ---------------------------------------------------------------------------
def cmd_listar(paths: Paths, prioridade: str = "", tipo: str = "",
               status: str = "pendente", limite: int = 40, video: str = "") -> int:
    _, _, revisoes = carregar(paths)
    sel = [r for r in revisoes
           if (not prioridade or r["prioridade"] == prioridade.upper())
           and (not tipo or tipo.lower() in r["tipo"].lower())
           and (not status or status == "todos" or r["status"] == status)
           and (not video or video.lower() in r["video_file"].lower())]

    if not sel:
        print("Nenhum ponto de atencao com esses filtros.")
        return 0

    print(f"\n{len(sel)} ponto(s) de atencao"
          f"{' (mostrando ' + str(limite) + ')' if len(sel) > limite else ''}:\n")
    print(f"{'ID':<6}{'PRIOR.':<10}{'TIPO':<22}{'ST':<4}{'VIDEO':<44}{'t(s)':>9}")
    print("-" * 97)
    for r in sel[:limite]:
        st = {"pendente": "-", "ok": "OK", "editado": "ED", "descartado": "DS"}.get(r["status"], "?")
        vid = r["video_file"]
        vid = vid[:41] + "..." if len(vid) > 44 else vid
        print(f"{r['id_revisao']:<6}{r['prioridade']:<10}{r['tipo']:<22}{st:<4}"
              f"{vid:<44}{r['t_chute_s'].split(';')[0]:>9}")
    print("-" * 97)
    print("`ver <ID>` para inspecionar um caso contra o video original.")
    return 0


def cmd_status(paths: Paths) -> int:
    verificar_original(paths)
    header, linhas, revisoes = carregar(paths)
    _, descartes = ler_csv(paths.descartes)

    por_status: dict[str, int] = defaultdict(int)
    for r in revisoes:
        por_status[r["status"]] += 1
    pend = por_status.get("pendente", 0)

    print(f"\nPasta de validacao : {paths.dir}")
    print(f"Copia de trabalho  : {len(linhas)} linhas")
    print(f"Descartadas        : {len(descartes)}")
    print(f"Alteracoes no log  : {sum(1 for a in ler_auditoria(paths) if a['acao'] in ('editar','descartar','corrigir-tracos'))}")
    print(f"\nRevisao: {len(revisoes) - pend}/{len(revisoes)} tratados "
          f"({(len(revisoes)-pend)/max(len(revisoes),1)*100:.0f}%)")
    for st in ("pendente", "ok", "editado", "descartado"):
        if por_status.get(st):
            print(f"  {st:<12}{por_status[st]:>5}")

    pendentes = [r for r in revisoes if r["status"] == "pendente"]
    if pendentes:
        print("\nPendentes por prioridade:")
        _resumo_fila(pendentes)
    else:
        print("\nFila zerada. `diff` mostra o que mudou; `promover` aplica no original.")
    return 0


# ---------------------------------------------------------------------------
# ver - inspecao contra o video original
# ---------------------------------------------------------------------------
def indexar_videos(paths: Paths, source_dir: str, recriar: bool = False) -> dict[str, str]:
    """Mapa nome_do_arquivo -> caminho completo, cacheado em disco."""
    if os.path.isfile(paths.indice_vids) and not recriar:
        with open(paths.indice_vids, "r", encoding="utf-8") as f:
            return json.load(f)

    print(f"indexando videos em {source_dir} ...")
    indice: dict[str, str] = {}
    for raiz, _, arquivos in os.walk(source_dir):
        for nome in arquivos:
            if nome.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".webm")):
                indice.setdefault(nome, os.path.join(raiz, nome))
    paths.criar_dirs()
    _guard(paths, paths.indice_vids)
    with open(paths.indice_vids, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=1)
    print(f"  {len(indice)} videos indexados")
    return indice


def _achar_player() -> Optional[tuple[str, str]]:
    """(executavel, template_de_argumento_de_tempo) do primeiro player achado."""
    candidatos = [
        (r"C:\Program Files\VideoLAN\VLC\vlc.exe", "--start-time={t:.0f}"),
        (r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe", "--start-time={t:.0f}"),
        (shutil.which("vlc"), "--start-time={t:.0f}"),
        (shutil.which("mpv"), "--start={t:.2f}"),
    ]
    for exe, arg in candidatos:
        if exe and os.path.isfile(exe):
            return exe, arg
    return None


def contact_sheet(video_path: str, linha: dict, destino: str,
                  n_antes: int = 4, n_depois: int = 4, passo: int = 3) -> Optional[str]:
    """
    Grade de frames ao redor do chute, para conferir a regiao sem abrir player.

    E o modo mais confiavel de validar: o frame do chute sozinho costuma nao
    mostrar onde a bola entrou.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  (opencv/numpy nao instalados - contact sheet indisponivel)")
        return None

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"  nao consegui abrir o video: {video_path}")
        return None

    chute = int(num(linha, "chute_frame"))
    alvo = [chute + d * passo for d in range(-n_antes, n_depois + 1)]
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 10**9
    alvo = [f for f in alvo if 0 <= f < total]

    imgs, rotulos = [], []
    for f in alvo:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f)
        ok, img = cap.read()
        if not ok:
            continue
        h, w = img.shape[:2]
        escala = 360 / max(h, 1)
        img = cv2.resize(img, (int(w * escala), 360))
        cor = (0, 0, 255) if f == chute else (200, 200, 200)
        cv2.rectangle(img, (0, 0), (img.shape[1] - 1, 359), cor, 3 if f == chute else 1)
        texto = f"{f}{' <-CHUTE' if f == chute else ''}"
        cv2.putText(img, texto, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(img, texto, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, cor, 2)
        imgs.append(img)
        rotulos.append(f)
    cap.release()

    if not imgs:
        return None

    cols = 3
    larg = max(i.shape[1] for i in imgs)
    imgs = [cv2.copyMakeBorder(i, 0, 0, 0, larg - i.shape[1],
                               cv2.BORDER_CONSTANT, value=(20, 20, 20)) for i in imgs]
    while len(imgs) % cols:
        imgs.append(np.full_like(imgs[0], 20))
    grade = np.vstack([np.hstack(imgs[i:i + cols]) for i in range(0, len(imgs), cols)])
    cv2.imwrite(destino, grade)
    return destino


def cmd_ver(paths: Paths, ident: str, source_dir: str,
            player: bool = True, frames: bool = True) -> int:
    header, linhas, revisoes = carregar(paths)
    ponto = achar_revisao(revisoes, ident)
    alvo = linhas_do_ponto(ponto, linhas)

    print("\n" + "=" * 92)
    print(f"{ponto['id_revisao']}  [{ponto['prioridade']}]  {ponto['tipo']}   status={ponto['status']}")
    print("=" * 92)
    print(f"{ponto['descricao']}\n")
    print(f"video: {ponto['video_file']}")

    if not alvo:
        print("\n(as linhas deste ponto ja foram descartadas da copia de trabalho)")
        return 0

    print(f"\n{'#':<3}{'pen':>4}{'inicio_s':>10}{'chute_s':>10}{'dur_s':>7}"
          f"{'frames':>8}  {'camera':<20}{'region':<20}{'gol':<5}")
    print("-" * 92)
    for i, l in enumerate(alvo, 1):
        print(f"{i:<3}{l.get('penalty_id',''):>4}{num(l,'inicio_time_s'):>10.2f}"
              f"{num(l,'chute_time_s'):>10.2f}{duracao_s(l):>7.2f}"
              f"{duracao_frames(l):>8}  {l.get('camera_type',''):<20}"
              f"{l.get('region',''):<20}{'SIM' if is_goal(l) else 'nao':<5}")
    print("-" * 92)
    print("O '#' e o que se passa em --linha nos comandos `editar` / `descartar`.")

    indice = indexar_videos(paths, source_dir)
    video_path = indice.get(ponto["video_file"])
    if not video_path:
        print(f"\nAVISO: video original nao encontrado em {source_dir}")
        print("       (confira --source-dir; os clips recortados seguem abaixo)")
    else:
        print(f"\noriginal: {video_path}")

    if frames and video_path:
        paths.criar_dirs()
        for i, l in enumerate(alvo, 1):
            destino = os.path.join(paths.dir_frames,
                                   f"{ponto['id_revisao']}_linha{i}_frame{int(num(l,'chute_frame'))}.png")
            _guard(paths, destino)
            if contact_sheet(video_path, l, destino):
                print(f"  contact sheet linha {i}: {destino}")

    print("\nclips ja recortados (abrem direto):")
    for i, l in enumerate(alvo, 1):
        cp = l.get("clip_path", "")
        marca = "" if os.path.isfile(cp) else "   [AUSENTE]"
        print(f"  linha {i}: {cp}{marca}")

    if player and video_path:
        p = _achar_player()
        t = max(num(alvo[0], "inicio_time_s") - 3, 0)
        if p:
            exe, arg = p
            subprocess.Popen([exe, arg.format(t=t), video_path])
            print(f"\nabrindo o player em {t:.0f}s ...")
        else:
            print(f"\n(VLC/mpv nao encontrados - abra manualmente em {t:.0f}s)")
            try:
                os.startfile(video_path)  # type: ignore[attr-defined]
            except Exception:
                pass

    print(f"\nDepois de conferir:")
    print(f"  python validador.py ok {ponto['id_revisao']} --nota \"...\"")
    print(f"  python validador.py editar {ponto['id_revisao']} --linha 1 --region gol_baixo_direito")
    print(f"  python validador.py descartar {ponto['id_revisao']} --linha 2 --motivo \"replay duplicado\"")
    return 0


# ---------------------------------------------------------------------------
# editar / ok / descartar
# ---------------------------------------------------------------------------
def _marcar(paths: Paths, revisoes: list[dict], ponto: dict,
            status: str, decisao: str, nota: str) -> None:
    ponto["status"] = status
    ponto["decisao"] = decisao
    ponto["revisado_em"] = datetime.now().isoformat(timespec="seconds")
    if nota:
        ponto["nota"] = nota
    escrever_csv(paths, paths.revisao, REVISAO_HEADER, revisoes)


def aplicar_edicao(paths: Paths, ident: str, linha_n: int, region: str = "",
                   camera: str = "", gol: Optional[bool] = None,
                   inicio_frame: Optional[int] = None, chute_frame: Optional[int] = None,
                   fps: float = FPS_ESPERADO, nota: str = "") -> list[str]:
    """Nucleo da edicao, sem imprimir nada. Usado pelo CLI e pelo servidor web."""
    header, linhas, revisoes = carregar(paths)
    ponto = achar_revisao(revisoes, ident)
    alvo = linhas_do_ponto(ponto, linhas)

    if not (1 <= linha_n <= len(alvo)):
        raise ValidacaoErro(f"linha {linha_n} invalida: este ponto tem {len(alvo)} linha(s).")
    l = alvo[linha_n - 1]
    antes = dict(l)
    mudancas: list[str] = []

    if region:
        if region not in VALID_REGIONS:
            raise ValidacaoErro(f"region invalida: '{region}'")
        if region != l.get("region", ""):
            mudancas.append(f"region {l.get('region','')} -> {region}")
        l["region"] = region
        l["region_label"] = REGION_LABELS[region]          # mantem os dois em sincronia
        if region.startswith("fora") and is_goal(l) and gol is None:
            l["is_goal"] = "False"                          # 'fora' nao pode ser gol
            mudancas.append("is_goal True -> False (implicado por 'fora_*')")

    if camera:
        if camera not in VALID_CAMERAS:
            raise ValidacaoErro(f"camera invalida: '{camera}'")
        if camera != l.get("camera_type", ""):
            mudancas.append(f"camera {l.get('camera_type','')} -> {camera}")
        l["camera_type"] = camera

    if gol is not None:
        if l.get("region", "").startswith("fora") and gol:
            raise ValidacaoErro("incoerente: region 'fora_*' nao pode ter is_goal=True. "
                                "Ajuste a regiao junto.")
        if gol != is_goal(l):
            mudancas.append(f"is_goal {l.get('is_goal','')} -> {gol}")
        l["is_goal"] = str(gol)

    for campo_f, campo_t, valor in (("inicio_frame", "inicio_time_s", inicio_frame),
                                    ("chute_frame", "chute_time_s", chute_frame)):
        if valor is not None and str(valor) != str(l.get(campo_f, "")):
            mudancas.append(f"{campo_f} {l.get(campo_f,'')} -> {valor}")
            l[campo_f] = str(valor)
            l[campo_t] = f"{valor / fps:.4f}"               # tempo derivado do frame

    if not mudancas:
        raise ValidacaoErro("Nada a alterar: os valores enviados sao iguais aos atuais.")

    if duracao_s(l) <= 0:
        raise ValidacaoErro(f"bloqueado: a edicao deixaria o chute antes do inicio "
                            f"(duracao {duracao_s(l):.3f}s). Nada foi gravado.")

    escrever_csv(paths, paths.trabalho, header, linhas)
    auditar(paths, "editar", id_revisao=ponto["id_revisao"], row_id=row_id(l),
            linha_n=linha_n, mudancas=mudancas, antes=antes, depois=dict(l))
    _marcar(paths, revisoes, ponto, "editado", "; ".join(mudancas), nota)
    return mudancas


def marcar_ok(paths: Paths, ident: str, nota: str = "") -> str:
    _, _, revisoes = carregar(paths)
    ponto = achar_revisao(revisoes, ident)
    _marcar(paths, revisoes, ponto, "ok", "conferido, sem alteracao", nota)
    auditar(paths, "ok", id_revisao=ponto["id_revisao"], nota=nota)
    return ponto["id_revisao"]


def cmd_editar(paths: Paths, ident: str, linha_n: int, region: str = "",
               camera: str = "", gol: Optional[bool] = None,
               inicio_frame: Optional[int] = None, chute_frame: Optional[int] = None,
               fps: float = FPS_ESPERADO, nota: str = "") -> int:
    try:
        mudancas = aplicar_edicao(paths, ident, linha_n, region, camera, gol,
                                  inicio_frame, chute_frame, fps, nota)
    except ValidacaoErro as e:
        msg = str(e)
        if "region invalida" in msg:
            msg += f"\nvalidas: {', '.join(sorted(VALID_REGIONS))}"
        elif "camera invalida" in msg:
            msg += f"\nvalidas: {', '.join(sorted(VALID_CAMERAS))}"
        raise SystemExit(msg)

    print(f"\n{ident} linha {linha_n} atualizada na COPIA DE TRABALHO:")
    for m in mudancas:
        print(f"  - {m}")
    print(f"\noriginal intacto: {paths.csv_original}")
    print("`desfazer` reverte esta edicao.")
    return 0


def cmd_ok(paths: Paths, ident: str, nota: str = "") -> int:
    try:
        rid = marcar_ok(paths, ident, nota)
    except ValidacaoErro as e:
        raise SystemExit(str(e))
    print(f"{rid} marcado como conferido (sem alteracao).")
    return 0


def aplicar_descarte(paths: Paths, ident: str, linha_n: int, motivo: str) -> str:
    if not motivo:
        raise ValidacaoErro("o motivo e obrigatorio para descartar uma linha.")
    header, linhas, revisoes = carregar(paths)
    ponto = achar_revisao(revisoes, ident)
    alvo = linhas_do_ponto(ponto, linhas)
    if not (1 <= linha_n <= len(alvo)):
        raise ValidacaoErro(f"linha {linha_n} invalida: este ponto tem {len(alvo)} linha(s).")

    l = alvo[linha_n - 1]
    rid = row_id(l)
    restantes = [x for x in linhas if row_id(x) != rid]

    dh, descartes = ler_csv(paths.descartes)
    dh = dh or (header + ["_motivo", "_descartado_em"])
    descartes.append({**l, "_motivo": motivo,
                      "_descartado_em": datetime.now().isoformat(timespec="seconds")})

    escrever_csv(paths, paths.trabalho, header, restantes)
    escrever_csv(paths, paths.descartes, dh, descartes)
    auditar(paths, "descartar", id_revisao=ponto["id_revisao"], row_id=rid,
            motivo=motivo, antes=dict(l))
    _marcar(paths, revisoes, ponto, "descartado", f"linha {linha_n} removida: {motivo}", "")
    return rid


def cmd_descartar(paths: Paths, ident: str, linha_n: int, motivo: str) -> int:
    try:
        rid = aplicar_descarte(paths, ident, linha_n, motivo)
    except ValidacaoErro as e:
        raise SystemExit(str(e))
    print(f"linha {linha_n} ({rid}) removida da copia de trabalho.")
    print("guardada em descartes.csv - nada foi apagado, `desfazer` restaura.")
    return 0


def aplicar_corrigir_tracos(paths: Paths) -> int:
    """Troca travessao por hifen no region_label - 118 linhas de abril."""
    header, linhas, revisoes = carregar(paths)
    n = 0
    for l in linhas:
        bruto = l.get("region_label", "")
        norm = normalizar_traco(bruto)
        if bruto != norm:
            l["region_label"] = norm
            n += 1
    if not n:
        return 0

    escrever_csv(paths, paths.trabalho, header, linhas)
    auditar(paths, "corrigir-tracos", n_linhas=n)
    for r in revisoes:
        if r["tipo"] == "traco_travessao" and r["status"] == "pendente":
            r["status"] = "editado"
            r["decisao"] = "travessao -> hifen (lote)"
            r["revisado_em"] = datetime.now().isoformat(timespec="seconds")
    escrever_csv(paths, paths.revisao, REVISAO_HEADER, revisoes)
    return n


def cmd_corrigir_tracos(paths: Paths) -> int:
    n = aplicar_corrigir_tracos(paths)
    if not n:
        print("Nenhum region_label com travessao - nada a fazer.")
        return 0
    _, linhas, _ = carregar(paths)
    print(f"{n} region_label normalizados (travessao -> hifen) na copia de trabalho.")
    print("classes distintas depois disso:",
          len({normalizar_traco(l.get('region_label', '')) for l in linhas}))
    return 0


# ---------------------------------------------------------------------------
# desfazer / diff / promover
# ---------------------------------------------------------------------------
def aplicar_desfazer(paths: Paths) -> Optional[dict]:
    """Reverte a ultima edicao/descarte. Devolve o registro desfeito, ou None."""
    header, linhas, revisoes = carregar(paths)
    log = ler_auditoria(paths)
    alvo_idx = next((i for i in range(len(log) - 1, -1, -1)
                     if not log[i].get("desfeito")
                     and log[i]["acao"] in ("editar", "descartar")), None)
    if alvo_idx is None:
        return None

    reg = log[alvo_idx]
    if reg["acao"] == "editar":
        por_id = {row_id(l): l for l in linhas}
        atual = por_id.get(reg["row_id"])
        if atual is None:
            raise ValidacaoErro("a linha desta edicao nao esta mais na copia de trabalho.")
        atual.clear()
        atual.update(reg["antes"])
    else:                                     # descartar
        linhas.append(reg["antes"])
        linhas.sort(key=lambda r: (r.get("video_file", ""), num(r, "chute_time_s")))
        dh, descartes = ler_csv(paths.descartes)
        descartes = [d for d in descartes if row_id(d) != reg["row_id"]]
        escrever_csv(paths, paths.descartes, dh, descartes)

    escrever_csv(paths, paths.trabalho, header, linhas)
    log[alvo_idx]["desfeito"] = True
    _guard(paths, paths.auditoria)
    with open(paths.auditoria, "w", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    for r in revisoes:
        if r["id_revisao"] == reg.get("id_revisao"):
            r["status"], r["decisao"], r["revisado_em"] = "pendente", "", ""
    escrever_csv(paths, paths.revisao, REVISAO_HEADER, revisoes)

    auditar(paths, "desfazer", alvo=reg["acao"], id_revisao=reg.get("id_revisao", ""))
    return reg


def cmd_desfazer(paths: Paths) -> int:
    try:
        reg = aplicar_desfazer(paths)
    except ValidacaoErro as e:
        raise SystemExit(str(e))
    if reg is None:
        print("Nada para desfazer.")
        return 0
    print(f"desfeito: {reg['acao']} em {reg.get('row_id','')} ({reg['ts']})")
    return 0


def cmd_diff(paths: Paths) -> int:
    if not os.path.isfile(paths.manifest):
        raise SystemExit("Validacao nao iniciada.")
    with open(paths.manifest, "r", encoding="utf-8") as f:
        man = json.load(f)
    _, orig = ler_csv(man["snapshot"])
    header, atual = ler_csv(paths.trabalho)

    o = {row_id(l): l for l in orig}
    a = {row_id(l): l for l in atual}
    removidas = sorted(set(o) - set(a))
    campos = [c for c in header if not c.startswith("_")]

    alteradas = []
    for rid in sorted(set(o) & set(a)):
        difs = [(c, o[rid].get(c, ""), a[rid].get(c, ""))
                for c in campos if o[rid].get(c, "") != a[rid].get(c, "")]
        if difs:
            alteradas.append((rid, difs))

    print(f"\nsnapshot original : {len(orig)} linhas")
    print(f"copia de trabalho : {len(atual)} linhas")
    print(f"alteradas         : {len(alteradas)}")
    print(f"removidas         : {len(removidas)}")
    print(f"novas             : {len(set(a) - set(o))}\n")

    for rid, difs in alteradas[:60]:
        print(f"~ {rid}")
        for campo, antes, depois in difs:
            print(f"    {campo}: '{antes}' -> '{depois}'")
    if len(alteradas) > 60:
        print(f"  ... e mais {len(alteradas)-60} linhas alteradas")
    for rid in removidas[:30]:
        print(f"- {rid}")
    return 0


def cmd_promover(paths: Paths, sim: bool = False) -> int:
    """Aplica a copia de trabalho sobre o original. Passo explicito e reversivel."""
    header, linhas, revisoes = carregar(paths)
    pendentes = sum(1 for r in revisoes if r["status"] == "pendente")

    print("\nEsta operacao SUBSTITUI o labels.csv original pela copia de trabalho.")
    cmd_diff(paths)
    if pendentes:
        print(f"\nATENCAO: ainda ha {pendentes} ponto(s) de atencao pendentes.")

    if not sim:
        try:
            resp = input('\nDigite exatamente  PROMOVER  para confirmar (ou Enter para abortar): ')
        except EOFError:
            resp = ""
        if resp.strip() != "PROMOVER":
            print("abortado - nada foi alterado.")
            return 1

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(paths.dir_original, f"labels_antes_de_promover_{ts}.csv")
    _guard(paths, backup)
    shutil.copy2(paths.csv_original, backup)
    somente_leitura(backup)

    tmp = paths.csv_original + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for l in linhas:
            w.writerow(l)
    os.replace(tmp, paths.csv_original)

    auditar(paths, "promover", backup=backup, n_linhas=len(linhas))
    print(f"\noriginal atualizado: {paths.csv_original} ({len(linhas)} linhas)")
    print(f"backup do estado anterior: {backup}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Revisao assistida dos pontos de atencao do dataset rotulado. "
                    "Nunca escreve no labels.csv original.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Uso tipico")[-1])
    p.add_argument("--output-base", default=DEFAULT_OUTPUT_BASE,
                   help=f"base rotulada (padrao: {DEFAULT_OUTPUT_BASE})")
    p.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR,
                   help=f"pasta dos videos originais (padrao: {DEFAULT_SOURCE_DIR})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="cria backup e monta a fila de revisao")
    s.add_argument("--force", action="store_true", help="recomeca a validacao do zero")

    s = sub.add_parser("listar", help="lista os pontos de atencao")
    s.add_argument("--prioridade", default="", choices=["", *PRIORIDADES])
    s.add_argument("--tipo", default="")
    s.add_argument("--status", default="pendente",
                   help="pendente|ok|editado|descartado|todos (padrao: pendente)")
    s.add_argument("--video", default="", help="filtra por trecho do nome do video")
    s.add_argument("--limite", type=int, default=40)

    s = sub.add_parser("ver", help="inspeciona um caso contra o video original")
    s.add_argument("id_revisao")
    s.add_argument("--sem-player", action="store_true", help="nao abre o video")
    s.add_argument("--sem-frames", action="store_true", help="nao gera contact sheet")

    s = sub.add_parser("editar", help="corrige uma linha na copia de trabalho")
    s.add_argument("id_revisao")
    s.add_argument("--linha", type=int, default=1, help="qual linha do ponto (ver `ver`)")
    s.add_argument("--region", default="", help=f"uma de: {', '.join(sorted(VALID_REGIONS))}")
    s.add_argument("--camera", default="", help=f"uma de: {', '.join(sorted(VALID_CAMERAS))}")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--gol", dest="gol", action="store_true", default=None)
    g.add_argument("--nao-gol", dest="gol", action="store_false")
    s.add_argument("--inicio-frame", type=int)
    s.add_argument("--chute-frame", type=int)
    s.add_argument("--fps", type=float, default=FPS_ESPERADO,
                   help="fps usado para recalcular os tempos (padrao: 30)")
    s.add_argument("--nota", default="")

    s = sub.add_parser("ok", help="marca conferido, sem alteracao")
    s.add_argument("id_revisao")
    s.add_argument("--nota", default="")

    s = sub.add_parser("descartar", help="remove uma linha (vai para descartes.csv)")
    s.add_argument("id_revisao")
    s.add_argument("--linha", type=int, default=1)
    s.add_argument("--motivo", required=True)

    sub.add_parser("corrigir-tracos", help="normaliza travessao -> hifen em lote")
    sub.add_parser("desfazer", help="reverte a ultima edicao ou descarte")
    sub.add_parser("status", help="progresso da revisao")
    sub.add_parser("diff", help="diferencas entre a copia de trabalho e o original")
    sub.add_parser("reindexar", help="refaz o indice de videos originais")

    s = sub.add_parser("promover", help="aplica as correcoes no labels.csv original")
    s.add_argument("--sim", action="store_true", help="pula a confirmacao digitada")

    s = sub.add_parser("web", help="abre a interface de revisao no navegador (Flask)")
    s.add_argument("--porta", type=int, default=5005)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--sem-navegador", action="store_true")

    a = p.parse_args(argv)
    paths = Paths(a.output_base)

    if a.cmd == "web":
        try:
            import validador_web
        except ImportError as e:
            print(f"ERRO ao carregar a interface web: {e}")
            print("Instale as dependencias: pip install flask opencv-python")
            return 1
        return validador_web.servir(paths, a.source_dir, host=a.host, porta=a.porta,
                                    abrir_navegador=not a.sem_navegador)

    if a.cmd == "init":
        return cmd_init(paths, a.force)
    if a.cmd == "listar":
        return cmd_listar(paths, a.prioridade, a.tipo, a.status, a.limite, a.video)
    if a.cmd == "ver":
        return cmd_ver(paths, a.id_revisao, a.source_dir,
                       player=not a.sem_player, frames=not a.sem_frames)
    if a.cmd == "editar":
        return cmd_editar(paths, a.id_revisao, a.linha, a.region, a.camera, a.gol,
                          a.inicio_frame, a.chute_frame, a.fps, a.nota)
    if a.cmd == "ok":
        return cmd_ok(paths, a.id_revisao, a.nota)
    if a.cmd == "descartar":
        return cmd_descartar(paths, a.id_revisao, a.linha, a.motivo)
    if a.cmd == "corrigir-tracos":
        return cmd_corrigir_tracos(paths)
    if a.cmd == "desfazer":
        return cmd_desfazer(paths)
    if a.cmd == "status":
        return cmd_status(paths)
    if a.cmd == "diff":
        return cmd_diff(paths)
    if a.cmd == "reindexar":
        indexar_videos(paths, a.source_dir, recriar=True)
        return 0
    if a.cmd == "promover":
        return cmd_promover(paths, a.sim)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValidacaoErro as e:
        print(f"ERRO: {e}")
        sys.exit(1)
