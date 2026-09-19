"""
revisao.py
==========
Camada de logica da REVISAO COMPLETA da base rotulada.

Diferenca para o validador.py
-----------------------------
O `validador.py` monta uma FILA com os pontos de atencao (divergencias
detectadas automaticamente). Aqui o alvo e outro: percorrer TODOS os rotulos
da base contra o video original, com filtro por tipo de chute (fora_cima,
fora_direita, fora_esquerda, cada canto do gol...), conferindo e corrigindo o
que estiver errado.

O dado original NUNCA e escrito
-------------------------------
Todo o estado vive em <output_base>/_revisao_manual/:

    _revisao_manual/
        MANIFEST.json          - sha256 + contagem de cada labels.csv de origem
        original/              - copias somente-leitura dos CSVs originais
        revisao.csv            - a camada de correcoes (so o que voce tocou)
        auditoria.jsonl        - log append-only (permite desfazer)
        indice_videos.json     - nome do arquivo -> caminho do video
        export/                - CSVs revisados, gerados sob demanda
        historico/             - validacoes zeradas, guardadas com data

`_guard()` levanta excecao em qualquer escrita fora dessa pasta - inclusive
uma tentativa de escrever nos labels.csv originais.

Como a correcao e guardada
--------------------------
`revisao.csv` e uma CAMADA (overlay): cada linha e a versao corrigida de um
rotulo, com as mesmas colunas do CSV original mais o estado da revisao
(_status, _nota, _alteracoes...). Rotulo que voce nao tocou nao aparece la.
A base efetiva = original + overlay aplicado por cima.

    pendente   - ainda nao revisado
    ok         - conferido no video, rotulo correto
    corrigido  - conferido e alterado
    descartado - marcado para sair do dataset (o motivo fica registrado)

Uso pela linha de comando
-------------------------
    python revisao.py status                     # panorama da base
    python revisao.py listar --regiao fora_cima  # o que ver
    python revisao.py exportar                   # CSVs revisados em export/
    python revisao.py caminhos                   # onde esta o dado validado
    python revisao.py zerar --sim                # recomeca do zero (arquiva a atual)
    python revisao.py web                        # interface (revisor_web.py)

A interface de tela fica em revisor_web.py; toda regra de negocio esta aqui.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import stat
import sys
from collections import defaultdict
from datetime import datetime
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

REVISAO_SUBDIR = "_revisao_manual"

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

STATUS = ("pendente", "ok", "corrigido", "descartado")
FPS_ESPERADO = 30.0

# Espelho de exporter.CSV_HEADER. Repetido aqui de proposito: importar o
# exporter arrastaria cv2/numpy so para ler uma lista de nomes de coluna.
CAMPOS_CANON = [
    "video_file", "penalty_id",
    "inicio_frame", "chute_frame",
    "inicio_time_s", "chute_time_s",
    "camera_type", "region", "region_label",
    "is_goal",
    "observations", "timestamp_rotulagem",
    "clip_path", "frame_inicio_path", "frame_chute_path",
    "competition", "competition_label", "source_folder",
]

# Colunas de controle da revisao, gravadas junto no overlay.
META = ["_uid", "_status", "_alteracoes", "_nota", "_motivo", "_revisado_em"]

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv")


class RevisaoErro(Exception):
    """
    Erro de uso previsto (regiao invalida, uid inexistente, edicao incoerente).

    O CLI transforma em mensagem; o servidor web, em HTTP 400. A regra fica
    escrita uma vez so.
    """


# ---------------------------------------------------------------------------
# Caminhos e o cinto de seguranca
# ---------------------------------------------------------------------------
class Paths:
    """Todos os caminhos da revisao, derivados do output_base."""

    def __init__(self, output_base: str):
        self.output_base   = os.path.abspath(output_base)
        self.dir           = os.path.join(self.output_base, REVISAO_SUBDIR)
        self.dir_original  = os.path.join(self.dir, "original")
        self.dir_export    = os.path.join(self.dir, "export")
        self.dir_historico = os.path.join(self.dir, "historico")
        self.manifest      = os.path.join(self.dir, "MANIFEST.json")
        self.overlay       = os.path.join(self.dir, "revisao.csv")
        self.auditoria     = os.path.join(self.dir, "auditoria.jsonl")
        self.indice_vids   = os.path.join(self.dir, "indice_videos.json")

    def criar_dirs(self) -> None:
        for d in (self.dir, self.dir_original, self.dir_export, self.dir_historico):
            os.makedirs(d, exist_ok=True)


def _guard(paths: Paths, destino: str) -> str:
    """
    Ultima barreira antes de qualquer escrita.

    Levanta se o destino for um labels.csv de origem ou qualquer caminho fora
    de _revisao_manual/. Toda funcao que escreve passa por aqui - assim um erro
    de digitacao em um caminho vira excecao, nao perda de dado.
    """
    alvo = os.path.abspath(destino)
    originais = {os.path.abspath(c) for _, c in comps.existing_csvs(paths.output_base)}
    if alvo in originais:
        raise PermissionError(
            f"BLOQUEADO: tentativa de escrever no labels.csv ORIGINAL ({alvo}). "
            f"A revisao so escreve em {REVISAO_SUBDIR}/; use `exportar` para "
            f"gerar os CSVs corrigidos em export/."
        )
    if not alvo.startswith(os.path.abspath(paths.dir) + os.sep):
        raise PermissionError(f"BLOQUEADO: escrita fora de {REVISAO_SUBDIR}/: {alvo}")
    return alvo


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def ler_csv(path: str) -> tuple[list[str], list[dict]]:
    if not os.path.isfile(path):
        return [], []
    with open(path, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def escrever_csv(paths: Paths, destino: str, campos: list[str],
                 linhas: Iterable[dict]) -> None:
    """Escrita atomica: grava .tmp no mesmo disco e so entao troca."""
    alvo = _guard(paths, destino)
    tmp = alvo + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore", restval="")
        w.writeheader()
        for linha in linhas:
            w.writerow(linha)
    os.replace(tmp, alvo)


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


def _reescrever_auditoria(paths: Paths, log: list[dict]) -> None:
    alvo = _guard(paths, paths.auditoria)
    tmp = alvo + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in log:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, alvo)


# ---------------------------------------------------------------------------
# Leitura dos rotulos
# ---------------------------------------------------------------------------
def num(linha: dict, campo: str, default: float = 0.0) -> float:
    try:
        return float(linha.get(campo, "") or default)
    except (TypeError, ValueError):
        return default


def is_goal(linha: dict) -> bool:
    return str(linha.get("is_goal", "")).strip().lower() in ("true", "1", "sim", "yes")


def duracao_s(linha: dict) -> float:
    return num(linha, "chute_time_s") - num(linha, "inicio_time_s")


def duracao_frames(linha: dict) -> int:
    return int(num(linha, "chute_frame") - num(linha, "inicio_frame"))


def fps_da_linha(linha: dict) -> float:
    """fps deduzido do proprio rotulo (frames / segundos), com piso seguro."""
    d = duracao_s(linha)
    fps = duracao_frames(linha) / d if d > 0 else 0.0
    return fps if 5.0 < fps < 240.0 else FPS_ESPERADO


def grupo_regiao(region: str) -> str:
    if region.startswith("fora"):
        return "fora"
    if region.startswith("gol"):
        return "gol"
    return "outro"


def fontes(paths: Paths) -> list[dict]:
    """Um dict por labels.csv de origem encontrado sob o output_base."""
    achadas = []
    for slug, csv_path in comps.existing_csvs(paths.output_base):
        header, linhas = ler_csv(csv_path)
        comp = comps.BY_SLUG.get(slug)
        achadas.append({
            "slug":   slug,
            "label":  comp.label if comp else slug,
            "csv":    csv_path,
            "header": header,
            "linhas": linhas,
        })
    return achadas


def campos_uniao(fs: list[dict]) -> list[str]:
    """Colunas de todos os CSVs, na ordem canonica primeiro."""
    vistos = list(CAMPOS_CANON)
    for f in fs:
        for c in f["header"]:
            if c not in vistos:
                vistos.append(c)
    return vistos


def uid_de(slug: str, linha: dict, usados: set[str]) -> str:
    """
    Chave estavel de um rotulo: competicao|video|penalty_id.

    A base atual nao tem duplicatas, mas se aparecer uma o sufixo #N evita que
    duas linhas diferentes compartilhem a mesma correcao.
    """
    base = f"{slug}|{linha.get('video_file','')}|{linha.get('penalty_id','')}"
    uid, n = base, 1
    while uid in usados:
        n += 1
        uid = f"{base}#{n}"
    usados.add(uid)
    return uid


def ler_overlay(paths: Paths) -> dict[str, dict]:
    _, linhas = ler_csv(paths.overlay)
    return {l["_uid"]: l for l in linhas if l.get("_uid")}


def gravar_overlay(paths: Paths, overlay: dict[str, dict], campos: list[str]) -> None:
    paths.criar_dirs()
    escrever_csv(paths, paths.overlay, META + campos,
                 [overlay[k] for k in sorted(overlay)])


class Base:
    """A base efetiva: originais + camada de correcoes ja aplicada."""

    def __init__(self, registros: list[dict], fs: list[dict], campos: list[str],
                 overlay: dict[str, dict]):
        self.registros = registros
        self.fontes = fs
        self.campos = campos
        self.overlay = overlay
        self.por_uid = {r["uid"]: r for r in registros}

    def get(self, uid: str) -> dict:
        r = self.por_uid.get(uid)
        if r is None:
            raise RevisaoErro(f"rotulo '{uid}' nao encontrado na base.")
        return r


def carregar(paths: Paths) -> Base:
    """Le os CSVs originais, aplica o overlay e devolve a base efetiva."""
    fs = fontes(paths)
    if not fs:
        raise RevisaoErro(f"nenhum labels.csv encontrado em {paths.output_base}")
    campos = campos_uniao(fs)
    overlay = ler_overlay(paths)

    registros: list[dict] = []
    usados: set[str] = set()
    for f in fs:
        for i, original in enumerate(f["linhas"]):
            uid = uid_de(f["slug"], original, usados)
            ov = overlay.get(uid)
            atual = dict(original)
            if ov:
                for c in atual:                     # o overlay manda em tudo
                    if c in ov:
                        atual[c] = ov[c]
            registros.append({
                "uid":          uid,
                "competicao":   f["slug"],
                "competicao_label": f["label"],
                "csv":          f["csv"],
                "n_csv":        i + 2,              # linha no arquivo (1 = cabecalho)
                "original":     original,
                "atual":        atual,
                "status":       (ov or {}).get("_status", "pendente"),
                "nota":         (ov or {}).get("_nota", ""),
                "motivo":       (ov or {}).get("_motivo", ""),
                "alteracoes":   [a for a in (ov or {}).get("_alteracoes", "").split(" | ") if a],
                "revisado_em":  (ov or {}).get("_revisado_em", ""),
            })

    registros.sort(key=lambda r: (r["atual"].get("video_file", ""),
                                  num(r["atual"], "chute_time_s")))
    return Base(registros, fs, campos, overlay)


# ---------------------------------------------------------------------------
# Backup do original
# ---------------------------------------------------------------------------
def garantir_backup(paths: Paths) -> dict:
    """
    Copia cada labels.csv de origem para original/ e registra o sha256.

    Idempotente e versionado: se o CSV de origem mudar depois (rotulagem
    continuou), a proxima chamada guarda um snapshot novo em vez de sobrepor o
    antigo. Nenhum snapshot e apagado.
    """
    paths.criar_dirs()
    man = {"criado_em": datetime.now().isoformat(timespec="seconds"),
           "output_base": paths.output_base, "fontes": {}}
    if os.path.isfile(paths.manifest):
        with open(paths.manifest, "r", encoding="utf-8") as f:
            man = json.load(f)
    man.setdefault("fontes", {})

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    novos = []
    for f in fontes(paths):
        digest = sha256(f["csv"])
        info = man["fontes"].setdefault(f["csv"], {"slug": f["slug"], "snapshots": []})
        if any(s["sha256"] == digest for s in info["snapshots"]):
            info.update(sha256=digest, n_linhas=len(f["linhas"]))
            continue
        destino = os.path.join(paths.dir_original, f"{f['slug']}_labels_{ts}.csv")
        _guard(paths, destino)
        shutil.copy2(f["csv"], destino)
        somente_leitura(destino)
        info["snapshots"].append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "arquivo": os.path.basename(destino),
            "sha256": digest,
            "n_linhas": len(f["linhas"]),
        })
        info.update(sha256=digest, n_linhas=len(f["linhas"]), header=f["header"])
        novos.append((f["slug"], os.path.basename(destino), len(f["linhas"])))

    man["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
    _guard(paths, paths.manifest)
    with open(paths.manifest, "w", encoding="utf-8") as fh:
        json.dump(man, fh, ensure_ascii=False, indent=2)

    if novos:
        auditar(paths, "backup", snapshots=[{"slug": s, "arquivo": a, "n_linhas": n}
                                            for s, a, n in novos])
    return man


def conferir_originais(paths: Paths) -> list[dict]:
    """Estado de cada CSV de origem em relacao ao ultimo backup."""
    man = {}
    if os.path.isfile(paths.manifest):
        with open(paths.manifest, "r", encoding="utf-8") as f:
            man = json.load(f).get("fontes", {})
    saida = []
    for f in fontes(paths):
        info = man.get(f["csv"], {})
        digest = sha256(f["csv"])
        saida.append({
            "slug": f["slug"], "label": f["label"], "csv": f["csv"],
            "n_linhas": len(f["linhas"]),
            "sha256": digest,
            "intacto": digest == info.get("sha256"),
            "snapshots": len(info.get("snapshots", [])),
        })
    return saida


# ---------------------------------------------------------------------------
# Filtros e resumo
# ---------------------------------------------------------------------------
def filtrar(registros: list[dict], *, regiao: str = "", grupo: str = "",
            status: str = "", competicao: str = "", camera: str = "",
            gol: str = "", busca: str = "") -> list[dict]:
    """
    Recorta a base. Campos vazios nao filtram.

    `regiao` aceita varios codigos separados por virgula
    (ex.: "fora_cima,fora_direita,fora_esquerda").
    """
    regioes = {r.strip() for r in regiao.split(",") if r.strip()}
    busca = busca.strip().lower()
    sel = []
    for r in registros:
        a = r["atual"]
        reg = a.get("region", "")
        if regioes and reg not in regioes:
            continue
        if grupo and grupo_regiao(reg) != grupo:
            continue
        if status and status != "todos" and r["status"] != status:
            continue
        if competicao and r["competicao"] != competicao:
            continue
        if camera and a.get("camera_type", "") != camera:
            continue
        if gol in ("sim", "nao") and is_goal(a) != (gol == "sim"):
            continue
        if busca and busca not in a.get("video_file", "").lower():
            continue
        sel.append(r)
    return sel


def resumo(registros: list[dict]) -> dict:
    """Contagens por regiao, status, competicao e camera."""
    def conta(chave) -> dict[str, int]:
        d: dict[str, int] = defaultdict(int)
        for r in registros:
            d[chave(r)] += 1
        return dict(sorted(d.items()))

    return {
        "total":      len(registros),
        "regiao":     conta(lambda r: r["atual"].get("region", "(vazio)")),
        "grupo":      conta(lambda r: grupo_regiao(r["atual"].get("region", ""))),
        "status":     conta(lambda r: r["status"]),
        "competicao": conta(lambda r: r["competicao"]),
        "camera":     conta(lambda r: r["atual"].get("camera_type", "(vazio)")),
        "gol":        conta(lambda r: "sim" if is_goal(r["atual"]) else "nao"),
    }


# ---------------------------------------------------------------------------
# Mutacoes - tudo grava so no overlay
# ---------------------------------------------------------------------------
def _salvar_estado(paths: Paths, base: Base, reg: dict, atual: dict,
                   status: str, alteracoes: list[str], nota: str = "",
                   motivo: str = "", acao: str = "editar") -> None:
    """Grava a entrada do overlay e registra no log de auditoria."""
    antes = base.overlay.get(reg["uid"])
    entrada = {c: atual.get(c, "") for c in base.campos}
    entrada.update({
        "_uid":        reg["uid"],
        "_status":     status,
        "_alteracoes": " | ".join(alteracoes),
        "_nota":       nota,
        "_motivo":     motivo,
        "_revisado_em": datetime.now().isoformat(timespec="seconds"),
    })
    base.overlay[reg["uid"]] = entrada
    gravar_overlay(paths, base.overlay, base.campos)
    auditar(paths, acao, uid=reg["uid"], video_file=reg["atual"].get("video_file", ""),
            alteracoes=alteracoes, antes=antes, depois=entrada)


def editar(paths: Paths, uid: str, *, region: Optional[str] = None,
           camera: Optional[str] = None, gol: Optional[bool] = None,
           inicio_frame: Optional[int] = None, chute_frame: Optional[int] = None,
           fps: Optional[float] = None, observations: Optional[str] = None,
           nota: str = "") -> list[str]:
    """
    Aplica uma correcao no overlay. Devolve a lista do que mudou.

    So os campos passados sao considerados; os demais ficam como estao.
    """
    base = carregar(paths)
    reg = base.get(uid)
    if reg["status"] == "descartado":
        raise RevisaoErro("este rotulo esta descartado. Use `reverter` antes de editar.")

    l = dict(reg["atual"])
    fps = float(fps) if fps else fps_da_linha(l)
    mudancas: list[str] = []

    if region:
        if region not in VALID_REGIONS:
            raise RevisaoErro(f"regiao invalida: '{region}'")
        if region != l.get("region", ""):
            mudancas.append(f"region {l.get('region','')} -> {region}")
        l["region"] = region
        l["region_label"] = REGION_LABELS[region]        # mantem os dois em sincronia
        if region.startswith("fora") and is_goal(l) and gol is None:
            l["is_goal"] = "False"                       # 'fora' nao pode ser gol
            mudancas.append("is_goal True -> False (implicado por 'fora_*')")

    if camera:
        if camera not in VALID_CAMERAS:
            raise RevisaoErro(f"camera invalida: '{camera}'")
        if camera != l.get("camera_type", ""):
            mudancas.append(f"camera {l.get('camera_type','')} -> {camera}")
        l["camera_type"] = camera

    if gol is not None:
        if l.get("region", "").startswith("fora") and gol:
            raise RevisaoErro("incoerente: regiao 'fora_*' nao pode ter is_goal=True. "
                              "Ajuste a regiao junto.")
        if gol != is_goal(l):
            mudancas.append(f"is_goal {l.get('is_goal','')} -> {gol}")
        l["is_goal"] = str(gol)

    for campo_f, campo_t, valor in (("inicio_frame", "inicio_time_s", inicio_frame),
                                    ("chute_frame",  "chute_time_s",  chute_frame)):
        if valor is None:
            continue
        valor = int(valor)
        if valor < 0:
            raise RevisaoErro(f"{campo_f} nao pode ser negativo.")
        if str(valor) != str(l.get(campo_f, "")):
            mudancas.append(f"{campo_f} {l.get(campo_f,'')} -> {valor}")
            l[campo_f] = str(valor)
            l[campo_t] = f"{valor / fps:.4f}"             # tempo derivado do frame

    if observations is not None and observations != l.get("observations", ""):
        mudancas.append("observations alterada")
        l["observations"] = observations

    if not mudancas:
        raise RevisaoErro("nada a alterar: os valores enviados sao iguais aos atuais.")
    if duracao_s(l) <= 0:
        raise RevisaoErro(f"bloqueado: a edicao deixaria o chute antes do inicio "
                          f"(duracao {duracao_s(l):.3f}s). Nada foi gravado.")

    _salvar_estado(paths, base, reg, l, "corrigido", mudancas, nota, acao="editar")
    return mudancas


def aprovar(paths: Paths, uid: str, nota: str = "") -> dict:
    """Marca o rotulo como conferido no video, sem alteracao."""
    base = carregar(paths)
    reg = base.get(uid)
    _salvar_estado(paths, base, reg, reg["atual"], "ok",
                   reg["alteracoes"], nota, acao="aprovar")
    return reg


def descartar(paths: Paths, uid: str, motivo: str) -> dict:
    """
    Marca o rotulo para sair do dataset. Nada e apagado.

    O motivo e obrigatorio: um descarte sem justificativa vira duvida daqui a
    tres meses. A linha continua no CSV original e no overlay, so nao entra no
    export revisado (vai para descartados.csv).
    """
    if not motivo.strip():
        raise RevisaoErro("o motivo e obrigatorio para descartar um rotulo.")
    base = carregar(paths)
    reg = base.get(uid)
    _salvar_estado(paths, base, reg, reg["atual"], "descartado",
                   reg["alteracoes"], reg["nota"], motivo.strip(), acao="descartar")
    return reg


def reverter(paths: Paths, uid: str) -> dict:
    """Remove a correcao: o rotulo volta ao original e ao status pendente."""
    base = carregar(paths)
    reg = base.get(uid)
    antes = base.overlay.get(uid)
    if antes is None:
        raise RevisaoErro("este rotulo esta como no original - nada a reverter.")
    del base.overlay[uid]
    gravar_overlay(paths, base.overlay, base.campos)
    auditar(paths, "reverter", uid=uid, video_file=reg["atual"].get("video_file", ""),
            alteracoes=[], antes=antes, depois=None)
    return reg


def desfazer(paths: Paths) -> Optional[dict]:
    """Desfaz a ultima acao ainda nao desfeita (edicao, aprovacao, descarte...)."""
    log = ler_auditoria(paths)
    alvo_i = next((i for i in range(len(log) - 1, -1, -1)
                   if not log[i].get("desfeito")
                   and log[i]["acao"] in ("editar", "aprovar", "descartar", "reverter")),
                  None)
    if alvo_i is None:
        return None

    reg = log[alvo_i]
    base = carregar(paths)
    if reg.get("antes") is None:
        base.overlay.pop(reg["uid"], None)
    else:
        base.overlay[reg["uid"]] = reg["antes"]
    gravar_overlay(paths, base.overlay, base.campos)

    log[alvo_i]["desfeito"] = True
    _reescrever_auditoria(paths, log)
    auditar(paths, "desfazer", uid=reg["uid"], acao_desfeita=reg["acao"])
    return reg


# ---------------------------------------------------------------------------
# Recomecar do zero
# ---------------------------------------------------------------------------
def zerar(paths: Paths, motivo: str = "") -> dict:
    """
    Recomeca a validacao do zero, sem apagar o que ja foi feito.

    A camada de correcoes e o log vao para historico/ com a data no nome e
    ficam somente leitura; no lugar deles nasce um revisao.csv vazio. O log
    novo comeca justamente com o registro do zeramento, apontando para os
    arquivos guardados - entao a conta de "quem conferiu o que, e quando"
    nunca some, ela so recomeca.

    A auditoria antiga sai junto de proposito: `desfazer` restaura o estado
    anterior de uma linha, e um log velho apontando para linhas que nao
    existem mais ressuscitaria correcoes ja zeradas.
    """
    paths.criar_dirs()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    antes = {}
    try:
        antes = resumo(carregar(paths).registros)["status"]
    except RevisaoErro:
        pass
    _, linhas_ov = ler_csv(paths.overlay)

    arquivados = []
    for origem, nome in ((paths.overlay, "revisao_" + ts + ".csv"),
                         (paths.auditoria, "auditoria_" + ts + ".jsonl")):
        if not os.path.isfile(origem):
            continue
        destino = os.path.join(paths.dir_historico, nome)
        _guard(paths, destino)
        shutil.move(origem, destino)
        somente_leitura(destino)
        arquivados.append(destino)

    gravar_overlay(paths, {}, campos_uniao(fontes(paths)))
    auditar(paths, "zerar", motivo=motivo, linhas_zeradas=len(linhas_ov),
            antes=antes, arquivados=[os.path.basename(a) for a in arquivados])

    return {"linhas_zeradas": len(linhas_ov), "antes": antes,
            "arquivados": arquivados, "historico": paths.dir_historico}


# ---------------------------------------------------------------------------
# Export - os CSVs corrigidos, fora do dado original
# ---------------------------------------------------------------------------
def exportar(paths: Paths) -> dict:
    """
    Gera em export/ os CSVs ja com as correcoes aplicadas.

    Um arquivo por competicao (mesmo cabecalho do original, drop-in), mais o
    consolidado e a lista de descartados. Os labels.csv originais nao sao
    tocados: a promocao, se um dia for feita, e uma copia manual consciente.
    """
    paths.criar_dirs()
    base = carregar(paths)
    por_comp: dict[str, list[dict]] = defaultdict(list)
    for r in base.registros:
        por_comp[r["competicao"]].append(r)

    gerados, descartados, consolidado = [], [], []
    for f in base.fontes:
        regs = por_comp.get(f["slug"], [])
        mantidos = [r for r in regs if r["status"] != "descartado"]
        fora = [r for r in regs if r["status"] == "descartado"]
        destino = os.path.join(paths.dir_export, f"labels_revisado_{f['slug']}.csv")
        escrever_csv(paths, destino, f["header"], [r["atual"] for r in mantidos])
        gerados.append({"slug": f["slug"], "arquivo": destino,
                        "linhas": len(mantidos), "descartadas": len(fora),
                        "corrigidas": sum(1 for r in mantidos if r["status"] == "corrigido"),
                        "conferidas": sum(1 for r in mantidos if r["status"] in ("ok", "corrigido"))})
        for r in mantidos:
            # o consolidado leva o selo da revisao junto: fora da interface,
            # e ele que diz o que ja foi conferido no video e quando.
            consolidado.append({**r["atual"], "competition": r["atual"].get("competition") or f["slug"],
                                "_status": r["status"], "_uid": r["uid"],
                                "_revisado": "sim" if r["status"] != "pendente" else "nao",
                                "_revisado_em": r["revisado_em"], "_nota": r["nota"],
                                "_alteracoes": " | ".join(r["alteracoes"])})
        for r in fora:
            descartados.append({**r["atual"], "_uid": r["uid"], "_motivo": r["motivo"],
                                "_revisado_em": r["revisado_em"]})

    escrever_csv(paths, os.path.join(paths.dir_export, "labels_revisado_completo.csv"),
                 base.campos + ["_uid", "_revisado", "_status", "_revisado_em",
                                "_alteracoes", "_nota"], consolidado)
    escrever_csv(paths, os.path.join(paths.dir_export, "descartados.csv"),
                 base.campos + ["_uid", "_motivo", "_revisado_em"], descartados)

    res = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "pasta": paths.dir_export,
        "arquivos": gerados,
        "total_linhas": len(consolidado),
        "total_descartados": len(descartados),
        "resumo": resumo(base.registros),
    }
    _guard(paths, os.path.join(paths.dir_export, "RESUMO.json"))
    with open(os.path.join(paths.dir_export, "RESUMO.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    auditar(paths, "exportar", total=len(consolidado), descartados=len(descartados))
    return res


# ---------------------------------------------------------------------------
# Indice de videos
# ---------------------------------------------------------------------------
def indexar_videos(paths: Paths, source_dir: str, recriar: bool = False) -> dict[str, str]:
    """Mapa nome_do_arquivo -> caminho completo, cacheado em disco."""
    if os.path.isfile(paths.indice_vids) and not recriar:
        with open(paths.indice_vids, "r", encoding="utf-8") as f:
            indice = json.load(f)
        # O indice envelhece: se os videos sao movidos de pasta, ele continua
        # apontando para o caminho velho e as telas devolvem 404 em silencio -
        # o video simplesmente "some" sem explicacao. Refaz sozinho quando o
        # estrago passa de um punhado de arquivos.
        sumidos = [c for c in indice.values() if not os.path.isfile(c)]
        if indice and len(sumidos) > max(2, 0.02 * len(indice)):
            print(f"indice de videos desatualizado ({len(sumidos)} de {len(indice)} "
                  f"nao estao mais no caminho gravado) - refazendo")
        else:
            if sumidos:
                print(f"aviso: {len(sumidos)} video(s) do indice sumiram do disco")
            return indice

    print(f"indexando videos em {source_dir} ...")
    indice: dict[str, str] = {}
    for raiz, _, arquivos in os.walk(source_dir):
        for nome in arquivos:
            if nome.lower().endswith(VIDEO_EXT):
                indice.setdefault(nome, os.path.join(raiz, nome))
    paths.criar_dirs()
    _guard(paths, paths.indice_vids)
    with open(paths.indice_vids, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=1)
    print(f"  {len(indice)} videos indexados")
    return indice


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _tabela(titulo: str, contagem: dict[str, int], total: int) -> None:
    print(f"\n  {titulo}")
    for k, v in sorted(contagem.items(), key=lambda kv: -kv[1]):
        barra = "#" * int(30 * v / max(total, 1))
        print(f"    {k:<22}{v:>5}  {barra}")


def cmd_status(paths: Paths) -> int:
    garantir_backup(paths)
    base = carregar(paths)
    r = resumo(base.registros)
    print(f"\nBase de revisao: {paths.output_base}")
    print(f"Backup/estado  : {paths.dir}")
    print(f"\n  {r['total']} rotulos em {len(base.fontes)} competicao(oes)")
    for f in conferir_originais(paths):
        selo = "intacto" if f["intacto"] else "MUDOU desde o backup"
        print(f"    {f['label']:<20}{f['n_linhas']:>5} linhas  "
              f"({f['snapshots']} snapshot(s), {selo})")
    _tabela("por regiao", r["regiao"], r["total"])
    _tabela("por status", r["status"], r["total"])
    revisados = r["total"] - r["status"].get("pendente", 0)
    print(f"\n  progresso: {revisados}/{r['total']} revisados "
          f"({100 * revisados / max(r['total'], 1):.1f}%)")
    print("\n  `python revisao.py web` abre a interface de revisao.\n")
    return 0


def cmd_listar(paths: Paths, limite: int = 40, **filtros: str) -> int:
    base = carregar(paths)
    sel = filtrar(base.registros, **filtros)
    print(f"\n{len(sel)} rotulo(s) de {len(base.registros)}\n")
    print(f"  {'STATUS':<11}{'REGIAO':<20}{'GOL':<5}{'CHUTE':>9}  VIDEO")
    print("  " + "-" * 86)
    for r in sel[:limite]:
        a = r["atual"]
        print(f"  {r['status']:<11}{a.get('region',''):<20}"
              f"{'SIM' if is_goal(a) else 'nao':<5}"
              f"{num(a,'chute_time_s'):>8.1f}s  {a.get('video_file','')[:44]}")
    if len(sel) > limite:
        print(f"  ... e mais {len(sel) - limite} (use --limite)")
    print()
    return 0


def caminho_validado(paths: Paths) -> str:
    """O CSV com tudo: rotulos corrigidos + o rastro da revisao."""
    return os.path.join(paths.dir_export, "labels_revisado_completo.csv")


def export_em_dia(paths: Paths) -> Optional[bool]:
    """
    True se o export ja inclui a ultima decisao gravada.

    None quando ainda nao houve export. Compara a data de modificacao: o
    export e um retrato do revisao.csv, e retrato velho engana quem for usar.
    """
    resumo_json = os.path.join(paths.dir_export, "RESUMO.json")
    if not os.path.isfile(resumo_json) or not os.path.isfile(paths.overlay):
        return None
    return os.path.getmtime(resumo_json) >= os.path.getmtime(paths.overlay)


def cmd_caminhos(paths: Paths, so_export: bool = False) -> int:
    """Onde esta cada coisa. `--so-export` imprime so o caminho, para colar."""
    principal = caminho_validado(paths)
    if so_export:
        print(principal)
        return 0

    def linha(rotulo, caminho):
        existe = os.path.isfile(caminho) or os.path.isdir(caminho)
        n = ""
        if caminho.endswith(".csv") and existe:
            n = f"  ({len(ler_csv(caminho)[1])} linhas)"
        print(f"  {rotulo:<22}{caminho}{n}{'' if existe else '   (nao existe ainda)'}")

    em_dia = export_em_dia(paths)
    print()
    print("DADO VALIDADO (use este):")
    linha("completo", principal)
    for slug, _ in ((f["slug"], f["csv"]) for f in fontes(paths)):
        linha(slug, os.path.join(paths.dir_export, f"labels_revisado_{slug}.csv"))
    linha("descartados", os.path.join(paths.dir_export, "descartados.csv"))
    if em_dia is False:
        print("\n  ATENCAO: o export esta mais velho que a ultima decisao.")
        print("           rode `python revisao.py exportar` antes de usar.")
    elif em_dia is None:
        print("\n  ainda nao exportado: rode `python revisao.py exportar`")
    else:
        print("\n  em dia com a ultima decisao gravada.")

    print()
    print("ESTADO DA REVISAO (a fonte viva):")
    linha("camada", paths.overlay)
    linha("auditoria", paths.auditoria)
    linha("historico", paths.dir_historico)
    print()
    print("ORIGINAL (nunca escrito):")
    for f in fontes(paths):
        linha(f["slug"], f["csv"])
    linha("backup", paths.dir_original)
    print()
    return 0


def cmd_zerar(paths: Paths, confirmado: bool, motivo: str = "") -> int:
    try:
        base = carregar(paths)
        feitos = resumo(base.registros)["status"]
    except RevisaoErro as e:
        print("ERRO: " + str(e))
        return 1
    revisados = sum(v for k, v in feitos.items() if k != "pendente")

    if not confirmado:
        print()
        print("Isto RECOMECA a validacao do zero.")
        print("  " + str(revisados) + " rotulo(s) ja revisados: " + str(feitos))
        print("  a camada atual e o log vao para historico/ (nada e apagado)")
        print("  os labels.csv originais nao sao tocados")
        print()
        print("Confirme com:  python revisao.py zerar --sim")
        return 1

    res = zerar(paths, motivo)
    print()
    print(str(res["linhas_zeradas"]) + " linha(s) de validacao zeradas.")
    for a in res["arquivados"]:
        print("  guardado em historico/" + os.path.basename(a))
    print()
    print("A validacao recomeca com todos os rotulos pendentes.")
    return 0


def cmd_exportar(paths: Paths) -> int:
    res = exportar(paths)
    print(f"\nCSVs revisados gerados em {res['pasta']}:")
    for a in res["arquivos"]:
        print(f"  {os.path.basename(a['arquivo']):<40}{a['linhas']:>5} linhas  "
              f"({a['corrigidas']} corrigidas, {a['descartadas']} descartadas)")
    print(f"\n  consolidado: labels_revisado_completo.csv ({res['total_linhas']} linhas)")
    print(f"  descartados: descartados.csv ({res['total_descartados']} linhas)")
    print("\n  os labels.csv originais continuam intactos.\n")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Revisao completa da base rotulada")
    ap.add_argument("--output-base", default=DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="panorama da base e do progresso")
    sub.add_parser("backup", help="atualiza a copia de seguranca dos originais")
    sub.add_parser("exportar", help="gera os CSVs revisados em export/")
    sub.add_parser("desfazer", help="desfaz a ultima acao")
    p_c = sub.add_parser("caminhos", help="onde esta o dado validado")
    p_c.add_argument("--so-export", action="store_true",
                     help="imprime so o caminho do CSV validado")
    p_z = sub.add_parser("zerar", help="recomeca a validacao do zero (arquiva a atual)")
    p_z.add_argument("--sim", action="store_true", help="confirma o zeramento")
    p_z.add_argument("--motivo", default="", help="fica registrado no log novo")

    p_l = sub.add_parser("listar", help="lista rotulos com filtro")
    p_l.add_argument("--regiao", default="", help="ex.: fora_cima,fora_direita")
    p_l.add_argument("--grupo", default="", choices=["", "gol", "fora"])
    p_l.add_argument("--status", default="", choices=["", *STATUS, "todos"])
    p_l.add_argument("--competicao", default="")
    p_l.add_argument("--camera", default="")
    p_l.add_argument("--gol", default="", choices=["", "sim", "nao"])
    p_l.add_argument("--busca", default="")
    p_l.add_argument("--limite", type=int, default=40)

    p_w = sub.add_parser("web", help="abre a interface de revisao no navegador")
    p_w.add_argument("--porta", type=int, default=5007)
    p_w.add_argument("--host", default="127.0.0.1")
    p_w.add_argument("--sem-navegador", action="store_true")

    a = ap.parse_args(argv)
    paths = Paths(a.output_base)

    if a.cmd == "status":
        return cmd_status(paths)
    if a.cmd == "backup":
        man = garantir_backup(paths)
        print(f"backup atualizado em {paths.dir_original}")
        for csv_path, info in man["fontes"].items():
            print(f"  {os.path.basename(os.path.dirname(csv_path)) or 'raiz':<18}"
                  f"{info.get('n_linhas', '?'):>5} linhas, "
                  f"{len(info.get('snapshots', []))} snapshot(s)")
        return 0
    if a.cmd == "exportar":
        return cmd_exportar(paths)
    if a.cmd == "caminhos":
        return cmd_caminhos(paths, a.so_export)
    if a.cmd == "zerar":
        return cmd_zerar(paths, a.sim, a.motivo)
    if a.cmd == "desfazer":
        reg = desfazer(paths)
        if reg is None:
            print("nada para desfazer.")
            return 1
        print(f"desfeito: {reg['acao']} em {reg['uid']}")
        return 0
    if a.cmd == "listar":
        return cmd_listar(paths, limite=a.limite, regiao=a.regiao, grupo=a.grupo,
                          status=a.status, competicao=a.competicao, camera=a.camera,
                          gol=a.gol, busca=a.busca)
    if a.cmd == "web":
        import revisor_web
        return revisor_web.servir(paths, a.source_dir, a.host, a.porta,
                                  not a.sem_navegador)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
