"""
jogadores.py
============
Dataset de backup com cobrador e goleiro de cada penalti do Brasileirao.

Uma linha = UM PENALTI REAL (nao um rotulo). O labels.csv tem 807 linhas, mas
muitas sao o mesmo chute visto de outro angulo (ao vivo + replays): 461
penaltis de verdade. Aqui cada penalti aparece uma vez so, com os row_id de
todos os seus angulos guardados em `rotulos_ids` para o join de volta.

SEGURANCA
---------
Base nova, em pasta propria, sem tocar em nada do que ja existe:

    <output_base>/_dataset_jogadores/
        penaltis_jogadores.csv          <- o dataset
        elenco.json                     numero -> nome, por temporada e clube
        origem/labels_origem_<ts>.csv   snapshot do labels.csv (somente leitura)
        origem/MANIFEST.json            sha256 da origem
        auditoria.jsonl                 log append-only

O prefixo "_" mantem a pasta fora de competitions.existing_csvs(), entao ela
nao vira uma competicao fantasma nem entra na contagem de "ja rotulado".
`construir` e idempotente: rodar de novo reaproveita a origem e PRESERVA todo
rotulo manual ja feito (casado por lance_id).

IDENTIFICACAO PELO NUMERO DA CAMISA
-----------------------------------
Quem bateu e quem defendeu nao precisam ser digitados a mao: o `elencos.py`
liga esta base aos elencos do Transfermarkt ja raspados no projeto de web
scraping. Medido na base atual (461 penaltis):

    357  o video tem UM cobrador conhecido  -> nome e camisa saem sozinhos
    101  o video tem 2 cobradores           -> DUVIDA: escolher pelo numero
      3  jogo fora do dataset do scraping   -> a mao

So a duvida chega na tela em destaque; o resto `sugerir --aplicar` preenche.
Jogador que usou 2 numeros na temporada (73 casos) tem o NOME resolvido e a
camisa em aberto - e o video que diz qual ele veste. A coluna `fonte_cobrador`
guarda de onde veio cada identificacao.

Uso
---
    python jogadores.py construir        # monta/atualiza o esqueleto
    python jogadores.py status           # progresso da rotulagem
    python jogadores.py sugerir          # o que os elencos ja resolvem
    python jogadores.py sugerir --aplicar  # preenche os sem ambiguidade
    python jogadores.py exportar         # CSV final, so os penaltis completos
    python jogadores.py web              # interface de rotulagem (Flask)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import validador as V           # reaproveita agrupamento de lances, guardas e helpers

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


SUBDIR = "_dataset_jogadores"
COMPETICAO = "brasileirao"

# O mesmo limiar do validador: gap mediano entre angulos do mesmo penalti e
# 6,3s (p95 = 20s); entre penaltis diferentes, 58,6s.
LANCE_GAP_S = 20.0

# ---------------------------------------------------------------------------
# Vocabularios do que e rotulado a mao
# ---------------------------------------------------------------------------
PES = ["destro", "canhoto", "nd"]

# Lado para onde o goleiro foi. MESMO referencial da coluna `region` do
# labels.csv (ponto de vista de quem assiste atras do batedor), para dar
# para cruzar "lado do chute" com "lado do goleiro" sem inverter nada.
LADOS_GOLEIRO = ["esquerda", "centro", "direita", "nd"]

DESFECHOS = ["gol", "defesa", "trave", "fora", "gol_rebote", "defesa_rebote_gol", "nd"]

PERIODOS = ["1T", "2T", "nd"]

STATUS = ["pendente", "parcial", "completo"]

# Campos preenchidos a mao (o resto vem do rotulado ou do nome do arquivo)
CAMPOS_MANUAIS = [
    "time_cobrador", "camisa_cobrador", "nome_cobrador", "pe_cobrador",
    "camisa_goleiro", "nome_goleiro",
    "lado_goleiro", "desfecho", "periodo", "minuto",
    "placar_mandante_momento", "placar_visitante_momento",
    "observacao",
]

CSV_HEADER = [
    # --- identificacao ---
    "penalti_uid", "lance_id", "rotulos_ids", "n_angulos",
    # --- partida (derivado do nome do arquivo e da pasta de origem) ---
    "competicao", "temporada", "rodada", "mandante", "visitante",
    "placar_final_mandante", "placar_final_visitante",
    "video_file", "video_path",
    # --- cobranca (derivado do labels.csv) ---
    "inicio_frame", "chute_frame", "inicio_time_s", "chute_time_s",
    "region", "region_label", "lado_chute", "altura_chute", "is_goal",
    "cameras", "clip_principal", "desfecho_derivado",
    # --- rotulado a mao ---
    *CAMPOS_MANUAIS,
    # --- derivado do que foi rotulado a mao ---
    "time_goleiro", "cobrador_mandante", "goleiro_acertou_lado",
    # --- procedencia da identificacao (ver elencos.py) ---
    # 'scraping' = veio do dataset do Transfermarkt sem ambiguidade
    # 'numero'   = quem validou leu o numero na camisa e escolheu
    # 'manual'   = digitado a mao, sem apoio do elenco
    "fonte_cobrador", "fonte_goleiro",
    # --- controle ---
    "status_rotulo", "rotulado_em",
]

FONTES = ["scraping", "numero", "manual", ""]


class ValidacaoErro(Exception):
    """Erro de uso previsto. O CLI vira mensagem; a web vira HTTP 400."""


# ---------------------------------------------------------------------------
# Caminhos e guarda de escrita
# ---------------------------------------------------------------------------
class PathsJog:
    def __init__(self, output_base: str):
        self.output_base = os.path.abspath(output_base)
        self.csv_origem = os.path.join(self.output_base, "labels.csv")
        self.dir        = os.path.join(self.output_base, SUBDIR)
        self.dir_origem = os.path.join(self.dir, "origem")
        self.manifest   = os.path.join(self.dir_origem, "MANIFEST.json")
        self.csv        = os.path.join(self.dir, "penaltis_jogadores.csv")
        self.elenco     = os.path.join(self.dir, "elenco.json")
        self.auditoria  = os.path.join(self.dir, "auditoria.jsonl")
        self.export     = os.path.join(self.dir, "penaltis_jogadores_completos.csv")
        self.indice     = os.path.join(self.dir, "indice_videos.json")

    def criar_dirs(self) -> None:
        for d in (self.dir, self.dir_origem):
            os.makedirs(d, exist_ok=True)


def _guard(p: PathsJog, destino: str) -> str:
    """Barreira de escrita: so dentro de _dataset_jogadores/, nunca no labels.csv."""
    alvo = os.path.abspath(destino)
    if alvo == os.path.abspath(p.csv_origem):
        raise PermissionError("BLOQUEADO: tentativa de escrever no labels.csv de origem.")
    if not alvo.startswith(os.path.abspath(p.dir) + os.sep):
        raise PermissionError(f"BLOQUEADO: escrita fora de {SUBDIR}/: {alvo}")
    return alvo


def escrever_csv(p: PathsJog, destino: str, header: list[str], linhas: Iterable[dict]) -> None:
    alvo = _guard(p, destino)
    tmp = alvo + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        # restval: colunas novas (ex.: fonte_cobrador) em linhas gravadas por uma
        # versao anterior saem vazias em vez de estourar na escrita.
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore", restval="")
        w.writeheader()
        for l in linhas:
            w.writerow(l)
    os.replace(tmp, alvo)


def escrever_json(p: PathsJog, destino: str, dados: Any) -> None:
    alvo = _guard(p, destino)
    tmp = alvo + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)
    os.replace(tmp, alvo)


def auditar(p: PathsJog, acao: str, **campos: Any) -> None:
    _guard(p, p.auditoria)
    with open(p.auditoria, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "acao": acao, **campos}, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Extracao dos metadados da partida
# ---------------------------------------------------------------------------
# "ATHLETICO PR 0 X 1 ATLETICO MG MELHORES MOMENTOS 25 RO.mp4"
#  ^mandante   ^gm  ^gv ^visitante  ^ancora
# A ancora e o que separa o nome do visitante do resto: sem ela, "ATLETICO MG
# MELHORES MOMENTOS" viraria o nome do time. Cobre as tres grafias da base.
RX_PARTIDA = re.compile(
    r"^\s*(?P<mandante>.+?)\s+(?P<gm>\d+)\s*[xX]\s*(?P<gv>\d+)\s+(?P<visitante>.+?)"
    r"\s+(?:MELHORES|GOLS|MELHORS|\d{1,2}\s*RODADA|RODADA)")

RX_RODADA = re.compile(r"(\d{1,2})\s*(?:RO|RODADA)", re.I)
RX_TEMPORADA_PASTA = re.compile(r"videos?[\W_]*penaltis[\W_]*((?:19|20)\d{2})", re.I)
RX_TEMPORADA_NOME = re.compile(r"\b((?:19|20)\d{2})\b")


def _sem_acento(t: str) -> str:
    nfkd = unicodedata.normalize("NFKD", t)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def partida_de(video_file: str, video_path: str = "") -> dict:
    """Mandante, visitante, placar, rodada e temporada a partir do nome/pasta."""
    nome = _sem_acento(video_file).upper()
    m = RX_PARTIDA.match(nome)
    dados = {"mandante": "", "visitante": "",
             "placar_final_mandante": "", "placar_final_visitante": ""}
    if m:
        dados["mandante"] = m.group("mandante").strip()
        dados["visitante"] = m.group("visitante").strip()
        dados["placar_final_mandante"] = m.group("gm")
        dados["placar_final_visitante"] = m.group("gv")

    r = RX_RODADA.search(nome)
    dados["rodada"] = r.group(1) if r else ""

    # A pasta e mais confiavel que o nome: o nome as vezes traz o ano da
    # temporada, as vezes o ano do upload, as vezes nada.
    t = RX_TEMPORADA_PASTA.search(video_path or "")
    if not t:
        t = RX_TEMPORADA_NOME.search(nome)
    dados["temporada"] = t.group(1) if t else ""
    return dados


def decompor_region(region: str) -> tuple[str, str]:
    """(lado, altura) da region, para o cruzamento com o lado do goleiro."""
    lado = next((l for l in ("esquerdo", "direito", "centro", "esquerda", "direita", "cima")
                 if region.endswith("_" + l)), "")
    lado = {"esquerdo": "esquerda", "direito": "direita",
            "cima": "centro"}.get(lado, lado)
    altura = next((a for a in ("topo", "meio", "baixo")
                   if f"_{a}_" in region or region.endswith(f"_{a}")), "")
    return lado, altura


def desfecho_derivado(region: str, gol: bool) -> str:
    """
    O que da para afirmar sem ver o video.

    'no_alvo_parado' cobre defesa e trave ao mesmo tempo - separar os dois
    exige assistir, entao fica para o campo manual `desfecho`.
    """
    if gol:
        return "gol"
    if region.startswith("fora"):
        return "fora"
    return "no_alvo_parado"


# ---------------------------------------------------------------------------
# construir
# ---------------------------------------------------------------------------
def sha256(path: str) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def indice_videos(p: PathsJog, source_dir: str, recriar: bool = False) -> dict[str, str]:
    if os.path.isfile(p.indice) and not recriar:
        with open(p.indice, "r", encoding="utf-8") as f:
            idx = json.load(f)
        # mesma armadilha do revisao.py: video movido de pasta vira 404 mudo
        sumidos = [c for c in idx.values() if not os.path.isfile(c)]
        if not (idx and len(sumidos) > max(2, 0.02 * len(idx))):
            return idx
        print(f"indice de videos desatualizado ({len(sumidos)}/{len(idx)}) - refazendo")
    idx: dict[str, str] = {}
    for raiz, _, arquivos in os.walk(source_dir):
        for nome in arquivos:
            if nome.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".webm")):
                idx.setdefault(nome, os.path.join(raiz, nome))
    p.criar_dirs()
    escrever_json(p, p.indice, idx)
    return idx


def construir(p: PathsJog, source_dir: str, apenas_brasileirao: bool = True) -> dict:
    """
    Monta (ou atualiza) o esqueleto do dataset.

    Idempotente: rodar de novo reaproveita os rotulos manuais ja feitos,
    casados por lance_id. Nenhum trabalho e perdido ao reconstruir.
    """
    if not os.path.isfile(p.csv_origem):
        raise ValidacaoErro(f"nao encontrei {p.csv_origem}")

    p.criar_dirs()
    _, linhas = V.ler_csv(p.csv_origem)
    idx = indice_videos(p, source_dir)

    # ------- preserva o que ja foi rotulado a mao -------
    anteriores: dict[str, dict] = {}
    if os.path.isfile(p.csv):
        _, antigas = V.ler_csv(p.csv)
        anteriores = {r["lance_id"]: r for r in antigas}

    lances = V.agrupar_lances(linhas, LANCE_GAP_S)

    registros: list[dict] = []
    for lance_id, rows in sorted(lances.items()):
        rows = sorted(rows, key=lambda r: V.num(r, "chute_time_s"))
        primeiro = rows[0]
        video_file = primeiro.get("video_file", "")
        video_path = idx.get(video_file, "")
        meta = partida_de(video_file, video_path)

        # o rotulo "principal" e o do angulo mais aberto quando existe: e o que
        # mostra a cobranca inteira, e o melhor ponto de partida para rotular
        principal = next((r for r in rows if r.get("camera_type") == "visão do torcedor"),
                         primeiro)
        region = principal.get("region", "")
        gol = V.is_goal(principal)
        lado, altura = decompor_region(region)

        reg = {
            "penalti_uid": "",                       # atribuido depois, na ordem final
            "lance_id": lance_id,
            "rotulos_ids": ";".join(V.row_id(r) for r in rows),
            "n_angulos": len(rows),
            "competicao": COMPETICAO,
            **meta,
            "video_file": video_file,
            "video_path": video_path,
            "inicio_frame": int(V.num(principal, "inicio_frame")),
            "chute_frame": int(V.num(principal, "chute_frame")),
            "inicio_time_s": round(V.num(principal, "inicio_time_s"), 3),
            "chute_time_s": round(V.num(principal, "chute_time_s"), 3),
            "region": region,
            "region_label": V.normalizar_traco(principal.get("region_label", "")),
            "lado_chute": lado,
            "altura_chute": altura,
            "is_goal": str(gol),
            "cameras": ";".join(sorted({r.get("camera_type", "") for r in rows})),
            "clip_principal": principal.get("clip_path", ""),
            "desfecho_derivado": desfecho_derivado(region, gol),
        }

        antigo = anteriores.get(lance_id)
        for campo in CAMPOS_MANUAIS:
            reg[campo] = (antigo or {}).get(campo, "")
        reg["status_rotulo"] = (antigo or {}).get("status_rotulo", "pendente")
        reg["rotulado_em"] = (antigo or {}).get("rotulado_em", "")
        _derivar(reg)
        registros.append(reg)

    # ordem estavel e legivel: temporada, rodada, jogo, instante do chute
    registros.sort(key=lambda r: (r["temporada"], _int(r["rodada"]), r["video_file"],
                                  float(r["chute_time_s"])))
    for i, r in enumerate(registros, 1):
        r["penalti_uid"] = f"P{i:04d}"

    escrever_csv(p, p.csv, CSV_HEADER, registros)

    # snapshot da origem, para o dataset ser reproduzivel
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = os.path.join(p.dir_origem, f"labels_origem_{ts}.csv")
    _guard(p, snap)
    shutil.copy2(p.csv_origem, snap)
    try:
        os.chmod(snap, stat.S_IREAD)
    except OSError:
        pass
    escrever_json(p, p.manifest, {
        "criado_em": datetime.now().isoformat(timespec="seconds"),
        "csv_origem": p.csv_origem,
        "sha256_origem": sha256(p.csv_origem),
        "linhas_origem": len(linhas),
        "penaltis": len(registros),
        "snapshot": snap,
        "lance_gap_s": LANCE_GAP_S,
        "competicao": COMPETICAO,
    })

    reaproveitados = sum(1 for r in registros if r["status_rotulo"] != "pendente")
    auditar(p, "construir", penaltis=len(registros), rotulos=len(linhas),
            reaproveitados=reaproveitados)
    return {"penaltis": len(registros), "rotulos": len(linhas),
            "reaproveitados": reaproveitados, "snapshot": snap}


def _int(v: Any, default: int = 999) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _derivar(reg: dict) -> None:
    """Campos que saem de graca do que foi rotulado a mao."""
    tc = (reg.get("time_cobrador") or "").strip()
    if tc == "mandante":
        reg["time_goleiro"] = "visitante"
        reg["cobrador_mandante"] = "True"
    elif tc == "visitante":
        reg["time_goleiro"] = "mandante"
        reg["cobrador_mandante"] = "False"
    else:
        reg["time_goleiro"] = ""
        reg["cobrador_mandante"] = ""

    lg = (reg.get("lado_goleiro") or "").strip()
    lc = (reg.get("lado_chute") or "").strip()
    # so faz sentido comparar quando os dois lados sao conhecidos
    reg["goleiro_acertou_lado"] = str(lg == lc) if lg and lg != "nd" and lc else ""


def _completo(reg: dict) -> bool:
    """O minimo para o penalti servir a uma analise de cobrador x goleiro."""
    obrig = ["time_cobrador", "camisa_cobrador", "camisa_goleiro",
             "lado_goleiro", "desfecho"]
    return all(str(reg.get(c, "")).strip() for c in obrig)


# ---------------------------------------------------------------------------
# Leitura / escrita do dataset
# ---------------------------------------------------------------------------
def carregar(p: PathsJog) -> list[dict]:
    if not os.path.isfile(p.csv):
        raise ValidacaoErro("dataset nao construido. Rode: python jogadores.py construir")
    _, linhas = V.ler_csv(p.csv)
    return linhas


def clube_do(reg: dict, papel: str) -> str:
    """Nome do clube do cobrador ou do goleiro, ja resolvido."""
    lado = reg.get("time_cobrador" if papel == "cobrador" else "time_goleiro", "")
    return reg.get(lado, "") if lado in ("mandante", "visitante") else ""


def carregar_elenco(p: PathsJog) -> dict:
    if not os.path.isfile(p.elenco):
        return {}
    with open(p.elenco, "r", encoding="utf-8") as f:
        return json.load(f)


def _aprender_elenco(p: PathsJog, reg: dict) -> None:
    """
    Acumula numero -> nome por temporada e clube.

    E o que faz a rotulagem acelerar: na segunda vez que o mesmo camisa 10 do
    mesmo time aparece, o nome ja vem sugerido.
    """
    elenco = carregar_elenco(p)
    temp = reg.get("temporada") or "sem_temporada"
    for papel, campo_n, campo_nome in (("cobrador", "camisa_cobrador", "nome_cobrador"),
                                       ("goleiro", "camisa_goleiro", "nome_goleiro")):
        clube = clube_do(reg, papel)
        numero = str(reg.get(campo_n, "")).strip()
        nome = str(reg.get(campo_nome, "")).strip()
        if clube and numero and nome:
            elenco.setdefault(temp, {}).setdefault(clube, {})[numero] = nome
    escrever_json(p, p.elenco, elenco)


def salvar_rotulo(p: PathsJog, uid: str, dados: dict) -> dict:
    """Grava os campos manuais de um penalti. Valida os vocabularios."""
    linhas = carregar(p)
    reg = next((r for r in linhas if r["penalti_uid"] == uid), None)
    if reg is None:
        raise ValidacaoErro(f"penalti '{uid}' nao encontrado")

    checagens = {
        "time_cobrador": ["mandante", "visitante", ""],
        "pe_cobrador": PES + [""],
        "lado_goleiro": LADOS_GOLEIRO + [""],
        "desfecho": DESFECHOS + [""],
        "periodo": PERIODOS + [""],
    }
    antes = dict(reg)
    for campo in CAMPOS_MANUAIS:
        if campo not in dados:
            continue
        valor = str(dados[campo]).strip()
        if campo in checagens and valor not in checagens[campo]:
            raise ValidacaoErro(f"{campo} invalido: '{valor}' "
                                f"(use: {', '.join(x for x in checagens[campo] if x)})")
        if campo in ("camisa_cobrador", "camisa_goleiro") and valor:
            if not valor.isdigit() or not (1 <= int(valor) <= 99):
                raise ValidacaoErro(f"{campo} deve ser um numero de 1 a 99: '{valor}'")
        if campo == "minuto" and valor:
            if not valor.isdigit() or not (0 <= int(valor) <= 130):
                raise ValidacaoErro(f"minuto invalido: '{valor}'")
        reg[campo] = valor

    # de onde veio a identificacao (scraping / numero lido na camisa / mao)
    for campo in ("fonte_cobrador", "fonte_goleiro"):
        if campo in dados:
            valor = str(dados[campo]).strip()
            if valor not in FONTES:
                raise ValidacaoErro(f"{campo} invalido: '{valor}' "
                                    f"(use: {', '.join(x for x in FONTES if x)})")
            reg[campo] = valor

    # coerencia com o que ja estava rotulado: 'fora' nunca vira gol
    if reg.get("desfecho") == "gol" and reg.get("region", "").startswith("fora"):
        raise ValidacaoErro("desfecho 'gol' incompativel com regiao 'fora_*' do rotulado. "
                            "Se a regiao estiver errada, corrija antes no labels.csv.")
    if reg.get("desfecho") in ("defesa", "trave", "fora") and reg.get("is_goal") == "True":
        raise ValidacaoErro(f"desfecho '{reg['desfecho']}' incompativel com is_goal=True "
                            f"do rotulado.")

    _derivar(reg)
    reg["status_rotulo"] = "completo" if _completo(reg) else (
        "parcial" if any(str(reg.get(c, "")).strip() for c in CAMPOS_MANUAIS) else "pendente")
    reg["rotulado_em"] = datetime.now().isoformat(timespec="seconds")

    escrever_csv(p, p.csv, CSV_HEADER, linhas)
    _aprender_elenco(p, reg)
    mudou = [c for c in CAMPOS_MANUAIS if antes.get(c, "") != reg.get(c, "")]
    auditar(p, "rotular", penalti_uid=uid, campos=mudou, status=reg["status_rotulo"])
    return reg


# ---------------------------------------------------------------------------
# Identificacao automatica (elencos do projeto de scraping)
# ---------------------------------------------------------------------------
def sugestao(reg: dict) -> dict:
    """
    Quem bateu e quem defendeu, segundo os elencos do Transfermarkt.

    Veredito 'resolvido' = o video tem um cobrador so e o nome sai sozinho;
    'duvida' = 2+ cobradores no mesmo video e so o numero na camisa desempata.
    Ver elencos.py. Nunca grava nada - quem grava e `aplicar_sugestao`.
    """
    import elencos
    return elencos.identificar(reg)


def campos_da_sugestao(ident: dict) -> dict:
    """Converte a identificacao nos campos manuais do dataset."""
    campos: dict[str, str] = {}
    cob = ident.get("cobrador")
    if cob:
        campos.update({
            "time_cobrador":  cob.get("lado", ""),
            "camisa_cobrador": str(cob.get("numero", "")),
            "nome_cobrador":  cob.get("nome", ""),
            "pe_cobrador":    cob.get("pe", ""),
            "fonte_cobrador": "scraping",
        })
    gol = ident.get("goleiro")
    if gol:
        campos.update({
            "camisa_goleiro": str(gol.get("numero", "")),
            "nome_goleiro":   gol.get("nome", ""),
            "fonte_goleiro":  "scraping",
        })
    # campo vazio nao adianta: so atrapalha a checagem de "ja preenchido"
    return {k: v for k, v in campos.items() if str(v).strip()}


def aplicar_sugestao(p: PathsJog, uid: str, sobrescrever: bool = False) -> dict:
    """Grava a sugestao de UM penalti. Por padrao nao sobrescreve o que ja existe."""
    reg = next((r for r in carregar(p) if r["penalti_uid"] == uid), None)
    if reg is None:
        raise ValidacaoErro(f"penalti '{uid}' nao encontrado")
    ident = sugestao(reg)
    if ident["veredito"] != "resolvido":
        raise ValidacaoErro(
            "este penalti nao tem identificacao automatica "
            f"({ident['veredito']}): escolha o cobrador pelo numero da camisa.")
    campos = campos_da_sugestao(ident)
    if not sobrescrever:
        campos = {k: v for k, v in campos.items() if not str(reg.get(k, "")).strip()}
    if not campos:
        raise ValidacaoErro("nada a aplicar: os campos ja estao preenchidos.")
    return salvar_rotulo(p, uid, campos)


def aplicar_sugestoes(p: PathsJog, sobrescrever: bool = False) -> dict:
    """
    Preenche de uma vez todos os penaltis com identificacao sem ambiguidade.

    Escreve so cobrador/goleiro: lado do goleiro e desfecho continuam exigindo
    o video, entao o penalti fica 'parcial', nunca 'completo'. Quem tem duvida
    (2+ cobradores no video) e deixado de fora de proposito - e o que sobra
    para conferir na tela, pelo numero da camisa.
    """
    linhas = carregar(p)
    aplicados, pulados = [], defaultdict(int)
    for reg in linhas:
        ident = sugestao(reg)
        if ident["veredito"] != "resolvido":
            pulados[ident["veredito"]] += 1
            continue
        campos = campos_da_sugestao(ident)
        if not sobrescrever:
            campos = {k: v for k, v in campos.items() if not str(reg.get(k, "")).strip()}
        if not campos:
            pulados["ja_preenchido"] += 1
            continue
        salvar_rotulo(p, reg["penalti_uid"], campos)
        aplicados.append(reg["penalti_uid"])
    auditar(p, "aplicar_sugestoes", n=len(aplicados), pulados=dict(pulados))
    return {"aplicados": aplicados, "pulados": dict(pulados), "total": len(linhas)}


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
def cmd_sugerir(p: PathsJog, aplicar: bool = False, sobrescrever: bool = False) -> int:
    linhas = carregar(p)
    contagem: dict[str, int] = defaultdict(int)
    duvidas = []
    for reg in linhas:
        ident = sugestao(reg)
        contagem[ident["veredito"]] += 1
        if ident["veredito"] == "duvida":
            duvidas.append((reg, ident))

    print(f"\n{len(linhas)} penaltis:")
    for v in ("resolvido", "duvida", "sem_dados"):
        print(f"  {v:<12}{contagem[v]:>5}")

    if duvidas:
        print(f"\n{len(duvidas)} com DUVIDA - escolher pelo numero da camisa:")
        for reg, ident in duvidas[:12]:
            # camisa vazia = o jogador usou mais de um numero na temporada
            cands = " vs ".join(
                f"#{c['numero'] or '/'.join(c['numeros']) or '?'} {c['nome']}"
                for c in ident["candidatos"])
            print(f"  {reg['penalti_uid']}  {reg['mandante']} x {reg['visitante']}"
                  f" ({reg['temporada']}): {cands}")
        if len(duvidas) > 12:
            print(f"  ... e mais {len(duvidas) - 12}")

    if not aplicar:
        print("\n  `sugerir --aplicar` preenche os resolvidos (nao toca nos de duvida).")
        return 0

    res = aplicar_sugestoes(p, sobrescrever)
    print(f"\n  {len(res['aplicados'])} penaltis preenchidos automaticamente")
    print(f"  pulados: {res['pulados']}")
    print("  os de duvida continuam para conferir na tela: python jogadores.py web")
    return 0


def cmd_construir(p: PathsJog, source_dir: str) -> int:
    r = construir(p, source_dir)
    print(f"\nDataset de jogadores em {p.dir}\n")
    print(f"  penaltis reais       : {r['penaltis']}   (de {r['rotulos']} rotulos)")
    print(f"  rotulos manuais mantidos: {r['reaproveitados']}")
    print(f"  arquivo              : {p.csv}")
    print(f"  snapshot da origem   : {os.path.basename(r['snapshot'])} (somente leitura)")
    cmd_status(p)
    print("\nProximo passo: python jogadores.py web")
    return 0


def cmd_status(p: PathsJog) -> int:
    linhas = carregar(p)
    por_status: dict[str, int] = defaultdict(int)
    por_temp: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in linhas:
        por_status[r["status_rotulo"]] += 1
        t = por_temp[r["temporada"] or "?"]
        t[0] += 1
        t[1] += 1 if r["status_rotulo"] == "completo" else 0

    comp = por_status.get("completo", 0)
    print(f"\n{comp}/{len(linhas)} penaltis completos "
          f"({comp/max(len(linhas),1)*100:.1f}%)")
    for s in STATUS:
        if por_status.get(s):
            print(f"  {s:<10}{por_status[s]:>5}")

    print(f"\n{'TEMPORADA':<12}{'PENALTIS':>10}{'COMPLETOS':>11}")
    for t in sorted(por_temp):
        n, c = por_temp[t]
        print(f"{t:<12}{n:>10}{c:>11}")

    el = carregar_elenco(p)
    jogadores = sum(len(v) for temp in el.values() for v in temp.values())
    print(f"\nelenco aprendido: {jogadores} jogador(es) em "
          f"{sum(len(t) for t in el.values())} clube-temporada(s)")
    return 0


def cmd_exportar(p: PathsJog) -> int:
    linhas = carregar(p)
    completos = [r for r in linhas if r["status_rotulo"] == "completo"]
    if not completos:
        print("Nenhum penalti completo ainda - nada a exportar.")
        return 1
    for r in completos:                       # resolve os clubes no export
        r["clube_cobrador"] = clube_do(r, "cobrador")
        r["clube_goleiro"] = clube_do(r, "goleiro")
    header = CSV_HEADER + ["clube_cobrador", "clube_goleiro"]
    escrever_csv(p, p.export, header, completos)
    print(f"{len(completos)} penaltis completos exportados para\n  {p.export}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Dataset de backup com cobrador e goleiro dos penaltis do Brasileirao.")
    ap.add_argument("--output-base", default=V.DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=V.DEFAULT_SOURCE_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("construir", help="monta/atualiza o esqueleto do dataset")
    sub.add_parser("status", help="progresso da rotulagem")
    sub.add_parser("exportar", help="CSV final, so os penaltis completos")
    g = sub.add_parser("sugerir", help="identifica cobrador/goleiro pelos elencos")
    g.add_argument("--aplicar", action="store_true", help="grava os sem ambiguidade")
    g.add_argument("--sobrescrever", action="store_true",
                   help="tambem substitui o que ja estava preenchido")
    s = sub.add_parser("web", help="interface de rotulagem (Flask)")
    s.add_argument("--porta", type=int, default=5006)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--sem-navegador", action="store_true")

    a = ap.parse_args(argv)
    p = PathsJog(a.output_base)

    if a.cmd == "construir":
        return cmd_construir(p, a.source_dir)
    if a.cmd == "status":
        return cmd_status(p)
    if a.cmd == "exportar":
        return cmd_exportar(p)
    if a.cmd == "sugerir":
        return cmd_sugerir(p, a.aplicar, a.sobrescrever)
    if a.cmd == "web":
        try:
            import jogadores_web
        except ImportError as e:
            print(f"ERRO ao carregar a interface: {e}")
            print("Instale: pip install flask opencv-python")
            return 1
        return jogadores_web.servir(p, a.source_dir, a.host, a.porta, not a.sem_navegador)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValidacaoErro as e:
        print(f"ERRO: {e}")
        sys.exit(1)
