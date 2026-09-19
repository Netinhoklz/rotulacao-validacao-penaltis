"""
elencos.py
==========
Ponte com o projeto de web scraping: NUMERO DA CAMISA -> JOGADOR.

O rotulador de jogadores pedia o nome do cobrador e do goleiro digitados a
mao, aprendendo os elencos aos poucos. Aqui a fonte passa a ser a real: os
elencos do Transfermarkt ja raspados em
<web-screaping-penaltis>/elencos_transfermarkt.json, mais o dataset de
penaltis ja casado com os videos.

Assim quem valida so precisa LER O NUMERO na camisa - o nome sai daqui.

As duas fontes
--------------
1. `dataset_penaltis_rotulagem.csv` - uma linha por penalti cobrado, ja ligada
   ao arquivo .mp4 pelo par (pasta_videos, nome_arquivo_video). Traz o
   cobrador, o numero dele, o clube, o pe e o nome do goleiro adversario.
   Como o arquivo identifica o JOGO e nao o penalti, um video com 2 cobradores
   diferentes tem 2 linhas e so o video diz qual e qual - e exatamente a
   duvida que a interface precisa destacar.

2. `elencos_transfermarkt.json` - elenco completo de cada clube em cada
   temporada. E o que responde "quem e o camisa 9 do Corinthians em 2023?"
   quando o cobrador nao esta na lista de candidatos.

As regras de casamento (temporada a partir da data, clube por tokens, camisa
que trocou de dono no meio do ano) NAO sao reimplementadas aqui: sao
importadas do `resolver_camisa.py` do proprio projeto de scraping, para nao
existirem duas versoes da mesma regra.

Veredito
--------
`identificar()` devolve um destes:

    resolvido  - o video tem UM cobrador conhecido; o nome sai sozinho
    duvida     - o video tem 2+ cobradores; a interface mostra os candidatos
                 e quem valida escolhe pelo numero da camisa
    sem_dados  - o jogo nao esta no dataset do scraping; cai no elenco puro

Nada aqui escreve em disco - e so leitura.

Uso direto:
    python elencos.py cobertura       # quanto da base o scraping resolve
    python elencos.py numero 10 --clube "Sociedade Esportiva Palmeiras" --temporada 2023
"""

from __future__ import annotations

import csv
import os
import sys
import unicodedata
from collections import defaultdict
from functools import lru_cache
from typing import Any, Optional

# Onde mora o projeto de scraping. Ajustavel por ambiente porque nada garante
# que os dois projetos fiquem lado a lado para sempre.
PROJETO_SCRAPING = os.environ.get(
    "WEB_SCRAPING_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web-screaping-penaltis"),
)
CSV_ROTULAGEM = "dataset_penaltis_rotulagem.csv"
JSON_ELENCOS = "elencos_transfermarkt.json"

VEREDITOS = ("resolvido", "duvida", "sem_dados")


# ---------------------------------------------------------------------------
# Carga preguicosa do projeto de scraping
# ---------------------------------------------------------------------------
_cache: dict[str, Any] = {}


def raiz() -> str:
    return os.path.abspath(PROJETO_SCRAPING)


@lru_cache(maxsize=1)
def _resolver():
    """
    Importa o resolver_camisa.py do projeto de scraping.

    Importar em vez de copiar: as regras de temporada/clube foram ajustadas la
    contra a base inteira (inclusive o Brasileirao 2020, que terminou em
    fevereiro de 2021). Duas copias divergiriam na primeira correcao.
    """
    base = raiz()
    if not os.path.isfile(os.path.join(base, "resolver_camisa.py")):
        return None
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        import resolver_camisa
        return resolver_camisa
    except ImportError:
        return None


def disponivel() -> bool:
    return _resolver() is not None and bool(_dataset())


def diagnostico() -> dict:
    """O que foi encontrado - para a interface avisar em vez de falhar calada."""
    base = raiz()
    R = _resolver()
    return {
        "pasta": base,
        "pasta_existe": os.path.isdir(base),
        "resolver_camisa": R is not None,
        "elencos_json": os.path.isfile(os.path.join(base, JSON_ELENCOS)),
        "dataset_csv": os.path.isfile(os.path.join(base, CSV_ROTULAGEM)),
        "jogos_indexados": len(_dataset()),
        "temporadas_elenco": sorted(R.carregar().keys()) if R else [],
    }


@lru_cache(maxsize=1)
def _dataset() -> dict[tuple[str, str], list[dict]]:
    """Penaltis do scraping indexados por (pasta_videos, nome_arquivo_video).

    A chave e o par, nunca so o nome: 'ATLETICO GO 1 X 1 CORINTHIANS ... 20
    RODA.mp4' existe em 2020 e em 2021.
    """
    caminho = os.path.join(raiz(), CSV_ROTULAGEM)
    if not os.path.isfile(caminho):
        return {}
    por_video: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with open(caminho, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            por_video[(r.get("pasta_videos", ""), r.get("nome_arquivo_video", ""))].append(r)
    return dict(por_video)


# ---------------------------------------------------------------------------
# Nomes de clube
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


def _tokens(nome: str) -> set[str]:
    R = _resolver()
    return R._tokens(nome) if R else set(_norm(nome).split())


def _parecenca(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def orientar(clube_cobrador: str, adversario: str,
             mandante: str, visitante: str) -> tuple[str, float]:
    """
    De que lado da partida esta o cobrador: 'mandante' ou 'visitante'.

    Compara as DUAS hipoteses de uma vez em vez de pontuar um clube isolado.
    Isolado, 'ATLETICO MG' pontua igual contra 'Clube Atletico Mineiro' e
    'Atletico Goianiense' (metade dos tokens em comum nos dois); somando o
    adversario, a hipotese certa ganha da errada com folga.
    """
    h_mandante = _parecenca(clube_cobrador, mandante) + _parecenca(adversario, visitante)
    h_visitante = _parecenca(clube_cobrador, visitante) + _parecenca(adversario, mandante)
    if h_mandante > h_visitante:
        return "mandante", h_mandante / 2
    if h_visitante > h_mandante:
        return "visitante", h_visitante / 2
    return "", 0.0


@lru_cache(maxsize=1)
def _aliases() -> dict[str, str]:
    """
    'ATLETICO MG' (nome do arquivo) -> 'Clube Atletico Mineiro' (Transfermarkt).

    Aprendido do proprio dataset: cada jogo casado ensina o par de nomes. Serve
    de rede de seguranca para os poucos jogos que nao estao no scraping, onde
    so temos o nome cru do arquivo para achar o elenco.
    """
    import re
    rx = re.compile(r"^\s*(?P<m>.+?)\s+\d+\s*[xX]\s*\d+\s+(?P<v>.+?)"
                    r"\s+(?:MELHORES|GOLS|MELHORS|\d{1,2}\s*RODADA|RODADA)")
    mapa: dict[str, str] = {}
    for (_, arquivo), linhas in _dataset().items():
        m = rx.match(_norm(arquivo))
        if not m:
            continue
        mand, visi = m.group("m").strip(), m.group("v").strip()
        for r in linhas:
            lado, conf = orientar(r.get("clube_penalti", ""), r.get("adversario", ""),
                                  mand, visi)
            if conf < 0.5:
                continue
            mapa.setdefault(mand if lado == "mandante" else visi, r["clube_penalti"])
            mapa.setdefault(visi if lado == "mandante" else mand, r["adversario"])
    return mapa


def clube_transfermarkt(nome_arquivo: str) -> str:
    """Nome do clube como o Transfermarkt escreve, quando conhecido."""
    return _aliases().get(_norm(nome_arquivo).strip(), nome_arquivo)


# ---------------------------------------------------------------------------
# Elenco e numero
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1024)
def elenco(clube: str, temporada) -> list[dict]:
    """Elenco do clube na temporada: [{numero, nome, posicao, pid}].

    Cacheado: casar o clube percorre os ~90 clubes da temporada comparando
    tokens, e a fila da interface pede o mesmo par clube/temporada dezenas de
    vezes seguidas.
    """
    R = _resolver()
    if not R or not temporada:
        return []
    try:
        _, reg = R.clube_na_temporada(clube_transfermarkt(clube), int(temporada))
    except (TypeError, ValueError):
        return []
    return reg["jogadores"] if reg else []


def elenco_por_numero(clube: str, temporada) -> dict[str, list[dict]]:
    """{numero: [jogadores]} - lista porque a camisa pode ter trocado de dono."""
    saida: dict[str, list[dict]] = defaultdict(list)
    for j in elenco(clube, temporada):
        num = str(j.get("numero", "")).strip()
        if num.isdigit():
            saida[num].append(j)
    return dict(saida)


def resolver_numero(numero, clube: str, temporada) -> dict:
    """
    Numero da camisa -> jogador(es) daquele clube naquela temporada.

    Repassa para o resolver_camisa do scraping (que ja trata camisa herdada no
    meio do ano e busca nas temporadas vizinhas) e normaliza a resposta.
    """
    R = _resolver()
    if not R:
        return {"ok": False, "erro": "projeto de scraping nao encontrado", "jogadores": []}
    res = R.resolver(numero, clube_transfermarkt(clube), temporada=_int(temporada))
    return {
        "ok": bool(res.get("ok")),
        "ambiguo": bool(res.get("ambiguo")),
        "jogadores": res.get("jogadores", []),
        "aproximados": res.get("aproximados", []),
        "clube_encontrado": res.get("clube_encontrado", ""),
        "temporada": res.get("temporada"),
        "erro": res.get("erro", ""),
    }


def goleiros(clube: str, temporada) -> list[dict]:
    """Goleiros do clube na temporada - quem pode estar defendendo."""
    return [j for j in elenco(clube, temporada)
            if "keeper" in str(j.get("posicao", "")).lower()
            and str(j.get("numero", "")).strip().isdigit()]


def _int(v, default=None):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _casa_nome(alvo: str, jogadores: list[dict]) -> Optional[dict]:
    """Acha um jogador pelo nome (o scraping da o nome do goleiro, nao o numero)."""
    a = _norm(alvo).strip()
    if not a:
        return None
    for j in jogadores:                       # nome igual
        if _norm(j.get("nome", "")).strip() == a:
            return j
    partes = {p for p in a.split() if len(p) > 2}
    melhor, best = None, 0.0
    for j in jogadores:                       # ultimo/primeiro nome em comum
        t = {p for p in _norm(j.get("nome", "")).split() if len(p) > 2}
        if not t or not partes:
            continue
        s = len(partes & t) / min(len(partes), len(t))
        if s > best:
            best, melhor = s, j
    return melhor if best >= 0.5 else None


# ---------------------------------------------------------------------------
# Identificacao de um penalti
# ---------------------------------------------------------------------------
def candidatos_do_video(pasta: str, arquivo: str) -> list[dict]:
    return _dataset().get((pasta, arquivo), [])


def veredito_rapido(reg: dict) -> dict:
    """
    So o veredito, sem tocar nos elencos.

    A lista da interface precisa marcar as duvidas em 461 penaltis de uma vez;
    resolver elenco e goleiro de todos a cada carregamento seria desperdicio.
    """
    linhas = candidatos_do_video(
        os.path.basename(os.path.dirname(reg.get("video_path", ""))),
        reg.get("video_file", ""))
    if not linhas:
        return {"veredito": "sem_dados", "n_candidatos": 0, "cobradores": []}
    nomes = sorted({(r.get("jogador_confirmado") or r.get("jogador") or "").strip()
                    for r in linhas} - {""})
    return {
        "veredito": "resolvido" if len(nomes) == 1 else ("duvida" if nomes else "sem_dados"),
        "n_candidatos": len(nomes),
        "cobradores": nomes,
    }


def identificar(reg: dict) -> dict:
    """
    Quem bateu e quem defendeu este penalti, ate onde os dados alcancam.

    `reg` e um registro do penaltis_jogadores.csv (jogadores.py). Devolve
    sempre a mesma forma, com `veredito` dizendo o quanto da para confiar:
    'resolvido' preenche sozinho, 'duvida' precisa do numero na camisa.
    """
    pasta = os.path.basename(os.path.dirname(reg.get("video_path", "")))
    arquivo = reg.get("video_file", "")
    mandante, visitante = reg.get("mandante", ""), reg.get("visitante", "")
    temporada = reg.get("temporada", "")

    saida = {
        "veredito": "sem_dados",
        "candidatos": [],
        "cobrador": None,
        "goleiro": None,
        "goleiros_possiveis": [],
        "clube_mandante": clube_transfermarkt(mandante),
        "clube_visitante": clube_transfermarkt(visitante),
        "temporada": temporada,
        "avisos": [],
        "disponivel": disponivel(),
    }
    if not saida["disponivel"]:
        saida["avisos"].append("projeto de scraping nao encontrado - so elenco manual")
        return saida

    linhas = candidatos_do_video(pasta, arquivo)
    if not linhas:
        saida["avisos"].append("este jogo nao esta no dataset do scraping")
        saida["goleiros_possiveis"] = _goleiros_dos_dois(saida, temporada)
        return saida

    # Um cobrador pode ter batido 2 penaltis no mesmo jogo: agrupa por jogador.
    por_jogador: dict[str, dict] = {}
    for r in linhas:
        nome = (r.get("jogador_confirmado") or r.get("jogador") or "").strip()
        if not nome:
            continue
        lado, conf = orientar(r.get("clube_penalti", ""), r.get("adversario", ""),
                              mandante, visitante)
        nums = numeros_da_camisa(r.get("numero_camisa", ""))
        c = por_jogador.setdefault(nome, {
            "nome": nome,
            # numero so quando ele identifica sozinho; com 2+ camisas na
            # temporada, quem confere le o numero no video e escolhe
            "numero": nums[0] if len(nums) == 1 else "",
            "numeros": nums,
            "camisa_ambigua": len(nums) > 1,
            "camisa_bruta": str(r.get("numero_camisa", "")).strip(),
            "clube": r.get("clube_penalti", ""),
            "lado": lado,
            "confianca_lado": round(conf, 2),
            "posicao": r.get("posicao", ""),
            "pe": _pe(r.get("pe_dominante", "")),
            "goleiro_adversario": (r.get("goleiro") or "").strip(),
            "resultados": [],
            "n_penaltis": 0,
        })
        c["n_penaltis"] += 1
        if r.get("resultado"):
            c["resultados"].append(r["resultado"])
        if not c["lado"]:
            saida["avisos"].append(f"nao deu para dizer de que lado joga {nome}")

    saida["candidatos"] = sorted(por_jogador.values(), key=lambda c: (-c["n_penaltis"], c["nome"]))

    if len(saida["candidatos"]) == 1:
        # um cobrador so no video: o NOME esta resolvido. A camisa ainda pode
        # estar em aberto se ele usou mais de um numero na temporada.
        saida["veredito"] = "resolvido"
        saida["cobrador"] = saida["candidatos"][0]
        if saida["cobrador"]["camisa_ambigua"]:
            saida["avisos"].append(
                f"{saida['cobrador']['nome']} usou as camisas "
                f"{', '.join(saida['cobrador']['numeros'])} nesta temporada - "
                f"confirme no video qual ele veste")
    elif len(saida["candidatos"]) > 1:
        saida["veredito"] = "duvida"
        nums = [c["numero"] for c in saida["candidatos"]]
        if len(set(nums)) != len(nums) or not all(nums):
            saida["avisos"].append("os candidatos nao tem um numero unico cada - "
                                   "confira tambem a posicao/aparencia, nao so o numero")

    # Goleiro: e sempre do time adversario ao do cobrador.
    saida["goleiros_possiveis"] = _goleiros_dos_dois(saida, temporada)
    ref = saida["cobrador"] or (saida["candidatos"][0] if saida["candidatos"] else None)
    if ref and ref["lado"]:
        lado_gol = "visitante" if ref["lado"] == "mandante" else "mandante"
        clube_gol = saida[f"clube_{lado_gol}"]
        possiveis = [g for g in saida["goleiros_possiveis"] if g["lado"] == lado_gol]
        achado = _casa_nome(ref["goleiro_adversario"], possiveis)
        if achado and saida["veredito"] == "resolvido":
            saida["goleiro"] = {**achado, "lado": lado_gol, "clube": clube_gol}
        elif ref["goleiro_adversario"] and saida["veredito"] == "resolvido":
            # o scraping sabe o nome, o elenco nao devolveu o numero
            saida["goleiro"] = {"nome": ref["goleiro_adversario"], "numero": "",
                                "lado": lado_gol, "clube": clube_gol}
            saida["avisos"].append(
                f"goleiro '{ref['goleiro_adversario']}' nao achado no elenco - numero em branco")
    return saida


def _goleiros_dos_dois(saida: dict, temporada) -> list[dict]:
    saida_gk = []
    for lado in ("mandante", "visitante"):
        clube = saida[f"clube_{lado}"]
        for g in goleiros(clube, temporada):
            saida_gk.append({**g, "lado": lado, "clube": clube})
    return saida_gk


def numeros_da_camisa(valor: str) -> list[str]:
    """
    '23/25' -> ['23', '25'].

    O dataset guarda todos os numeros que o jogador usou na temporada separados
    por barra - 105 das 509 linhas. Nesses casos o numero NAO identifica sozinho
    e e o video que diz qual camisa ele vestia naquele jogo; por isso a lista
    volta inteira em vez de um palpite.
    """
    saida = []
    for parte in str(valor).replace(",", "/").split("/"):
        parte = parte.strip()
        if parte.isdigit() and 1 <= int(parte) <= 99 and parte not in saida:
            saida.append(parte)
    return saida


def _pe(valor: str) -> str:
    """Pe dominante do Transfermarkt no vocabulario do rotulador."""
    v = str(valor).strip().lower()
    if v.startswith("right"):
        return "destro"
    if v.startswith("left"):
        return "canhoto"
    return ""


# ---------------------------------------------------------------------------
# CLI - conferencia
# ---------------------------------------------------------------------------
def cmd_cobertura(csv_penaltis: str) -> int:
    import json
    print(json.dumps(diagnostico(), ensure_ascii=False, indent=2))
    if not os.path.isfile(csv_penaltis):
        print(f"\nsem {csv_penaltis} - rode antes: python jogadores.py construir")
        return 1
    with open(csv_penaltis, "r", encoding="utf-8", newline="") as f:
        regs = list(csv.DictReader(f))

    cont = defaultdict(int)
    sem_lado = 0
    for r in regs:
        ident = identificar(r)
        cont[ident["veredito"]] += 1
        if ident["cobrador"] and not ident["cobrador"]["lado"]:
            sem_lado += 1
    print(f"\n{len(regs)} penaltis na base:")
    for v in VEREDITOS:
        print(f"  {v:<12}{cont[v]:>5}")
    print(f"\n  cobradores resolvidos sem saber o lado da partida: {sem_lado}")
    print("\n  'duvida' e o que a interface destaca: escolher pelo numero da camisa.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Ponte com os elencos do projeto de scraping")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_c = sub.add_parser("cobertura", help="quanto da base o scraping ja resolve")
    p_c.add_argument("--csv", default=os.environ.get(
        "CSV_PENALTIS", r"E:\penaltis_rotulados\_dataset_jogadores\penaltis_jogadores.csv"))
    p_n = sub.add_parser("numero", help="resolve um numero de camisa")
    p_n.add_argument("numero")
    p_n.add_argument("--clube", required=True)
    p_n.add_argument("--temporada", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "cobertura":
        return cmd_cobertura(a.csv)
    print(json.dumps(resolver_numero(a.numero, a.clube, a.temporada),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
