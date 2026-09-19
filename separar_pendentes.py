r"""
separar_pendentes.py
====================
Junta numa pasta separada todos os videos que ainda faltam rotular.

    python separar_pendentes.py                           # copia para <ao lado da saida>\videos_para_rotular
    python separar_pendentes.py --destino "D:\fila"       # outro destino
    python separar_pendentes.py --modo link               # sem gastar disco (hard link)

"Pendente" e a mesma regra da tela de rotulacao: o video nao tem nenhuma linha
em nenhum labels.csv (restos do yt-dlp - .temp, .f137 duplicado - nao contam).

Organizacao:

    <destino>/
        brasileirao/<ano>/<video>.mp4
        copa_do_brasil/<ano>/<video>.mp4
        _lista_para_rotular.csv     um video por linha: origem, cobrancas, lote

Os nomes das pastas sao os que o rotulador entende: apontar PASTA_VIDEOS do
run.py para o destino rotula cada video na competicao certa, e o rotulo casa
com o video original (a chave e competicao + nome do arquivo).

Modos
-----
copia (padrao)  arquivo independente, pronto para levar a outro disco ou
                pessoa. Confere o espaco livre ANTES de comecar e para se nao
                couber - disco cheio no meio derrubaria o cache da rotulacao.
                Se o destino tiver hard links de uma rodada anterior, eles sao
                trocados por copias.
link            segundo nome para o mesmo arquivo (os.link): nao ocupa espaco,
                e instantaneo; o original fica intacto. Para quando nao cabe.

Cada copia e gravada como <video>.copiando e so vira <video>.mp4 no fim, entao
uma interrupcao nunca deixa video pela metade com o nome certo. Rodar de novo
retoma de onde parou e nunca sobrescreve um arquivo que nao seja da origem.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import shutil
import sys
import time

import api
import competitions as comps

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# As pastas vem das variaveis do topo do run.py - o unico lugar de ajuste.
import run  # noqa: E402  (so definicoes; o servidor so sobe em run.main())

ORIGEM = run.PASTA_VIDEOS
SAIDA = run.PASTA_SAIDA
# ao lado da pasta de saida: E:\penaltis_rotulados -> E:\videos_para_rotular
DESTINO = os.path.join(os.path.dirname(os.path.abspath(run.PASTA_SAIDA)), "videos_para_rotular")
CATALOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "web-screaping-penaltis", "catalogo_videos_proveniencia.csv")
MARGEM_DISCO = 2e9          # nunca deixar o disco com menos que isso livre
PROVISORIO = ".copiando"


def ano_do_caminho(caminho: str, origem: str) -> str:
    """Ano da temporada pela pasta (videos_penaltis_2023 -> 2023)."""
    relativo = os.path.dirname(caminho)[len(origem):]
    m = re.search(r"(?:19|20)\d{2}", relativo)
    return m.group(0) if m else "sem_ano"


def ler_catalogo() -> dict:
    """arquivo (sem extensao) -> linha do catalogo do projeto de scraping, se houver."""
    if not os.path.isfile(CATALOGO):
        return {}
    with open(CATALOGO, encoding="utf-8-sig", newline="") as f:
        return {os.path.splitext(r["arquivo"])[0].strip(): r for r in csv.DictReader(f)}


def amostra_igual(a: str, b: str, bloco: int = 1 << 20) -> bool:
    """Tamanho igual e primeiro/ultimo MB iguais - conferencia rapida da copia."""
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        if fa.read(bloco) != fb.read(bloco):
            return False
        fim = max(os.path.getsize(a) - bloco, 0)
        fa.seek(fim)
        fb.seek(fim)
        return fa.read(bloco) == fb.read(bloco)


def copiar(origem: str, alvo: str) -> None:
    """Copia para <alvo>.copiando e so no fim troca pelo nome certo."""
    tmp = alvo + PROVISORIO
    shutil.copy2(origem, tmp)
    os.replace(tmp, alvo)          # tambem substitui um hard link antigo


def main() -> int:
    ap = argparse.ArgumentParser(description="Separa os videos que faltam rotular.")
    ap.add_argument("--origem", default=ORIGEM)
    ap.add_argument("--saida", default=SAIDA, help="pasta dos labels.csv")
    ap.add_argument("--destino", default=DESTINO)
    ap.add_argument("--modo", choices=["copia", "link"], default="copia")
    a = ap.parse_args()

    videos = api._collect_all_videos(a.origem)
    rotulados = api._get_labeled_keys(a.saida)
    pendentes = sorted((v for v in videos if (v["competition"], v["stem"]) not in rotulados),
                       key=lambda v: (v["competition"], v["path"]))
    catalogo = ler_catalogo()
    origem_abs = os.path.abspath(a.origem)
    os.makedirs(a.destino, exist_ok=True)

    # copia interrompida numa rodada anterior: o provisorio nunca e aproveitado
    for resto in glob.glob(os.path.join(a.destino, "**", "*" + PROVISORIO), recursive=True):
        os.remove(resto)

    print(f"\n{len(videos)} videos na origem · {len(videos) - len(pendentes)} rotulados · "
          f"{len(pendentes)} pendentes · modo: {a.modo}")

    # o que falta fazer, para conferir o espaco antes de escrever qualquer coisa
    plano = []
    for v in pendentes:
        ano = ano_do_caminho(v["path"], origem_abs)
        alvo = os.path.join(a.destino, v["competition"], ano, os.path.basename(v["path"]))
        plano.append((v, ano, alvo))

    def precisa_copia(alvo, origem):
        if not os.path.exists(alvo):
            return True
        return os.path.samefile(alvo, origem)      # hard link a trocar por copia

    if a.modo == "copia":
        bytes_faltam = sum(os.path.getsize(v["path"]) for v, _, alvo in plano
                           if precisa_copia(alvo, v["path"]))
        livre = shutil.disk_usage(a.destino).free
        print(f"  a copiar: {bytes_faltam / 1e9:.1f} GB · livre: {livre / 1e9:.1f} GB")
        if bytes_faltam > livre - MARGEM_DISCO:
            print(f"\n  NAO CABE: faltariam {(bytes_faltam - livre + MARGEM_DISCO) / 1e9:.1f} GB "
                  f"(margem de {MARGEM_DISCO / 1e9:.0f} GB). Nada foi escrito.")
            print("  Libere espaco ou use --modo link (nao gasta disco).")
            return 1

    feitos = trocados = ja = 0
    problemas: list[str] = []
    linhas: list[dict] = []
    t0 = time.time()
    for i, (v, ano, alvo) in enumerate(plano, 1):
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        try:
            if os.path.exists(alvo):
                mesmo = os.path.samefile(alvo, v["path"])
                if mesmo and a.modo == "copia":
                    copiar(v["path"], alvo)            # hard link -> copia de verdade
                    trocados += 1
                elif mesmo or amostra_igual(alvo, v["path"]):
                    ja += 1                            # ja esta como pedido
                else:
                    problemas.append(f"ja existe OUTRO arquivo com esse nome: {alvo}")
                    continue
            elif a.modo == "copia":
                copiar(v["path"], alvo)
                feitos += 1
            else:
                os.link(v["path"], alvo)
                feitos += 1
        except OSError as e:
            problemas.append(f"{os.path.basename(alvo)}: {e}")
            continue

        if i % 20 == 0 or i == len(plano):
            print(f"  {i}/{len(plano)} ({time.time() - t0:.0f}s)")

        c = catalogo.get(os.path.splitext(os.path.basename(v["path"]))[0].strip(), {})
        linhas.append({
            "competicao": v["competition"],
            "ano": ano,
            "arquivo": os.path.basename(v["path"]),
            "cobrancas_conhecidas": c.get("n_cobrancas_conhecidas", ""),
            "lote": c.get("lote", ""),
            "caminho_novo": alvo,
            "caminho_original": v["path"],
            "tamanho_mb": round(os.path.getsize(v["path"]) / 1e6, 1),
        })

    lista = os.path.join(a.destino, "_lista_para_rotular.csv")
    with open(lista, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0]) if linhas else ["arquivo"])
        w.writeheader()
        w.writerows(linhas)

    # prova: competicao certa e, no modo copia, arquivo independente e identico
    ruins = []
    for l in linhas:
        novo, orig = l["caminho_novo"], l["caminho_original"]
        if comps.classify(novo)[0].slug != l["competicao"]:
            ruins.append(("competicao", novo))
        elif a.modo == "copia" and (os.path.samefile(novo, orig) or not amostra_igual(novo, orig)):
            ruins.append(("copia", novo))
        elif a.modo == "link" and not os.path.samefile(novo, orig):
            ruins.append(("link", novo))

    print(f"\n  novos           : {feitos}")
    if a.modo == "copia":
        print(f"  hard link -> copia: {trocados}")
    print(f"  ja estavam      : {ja}")
    print(f"  problemas       : {len(problemas)}")
    for p in problemas[:10]:
        print("     " + p)
    print(f"  conferencia     : {len(linhas) - len(ruins)}/{len(linhas)} ok"
          + ("  (arquivo independente, tamanho e amostras iguais ao original)"
             if a.modo == "copia" else "  (apontam para o original)"))
    for tipo, caminho in ruins[:10]:
        print(f"     falhou ({tipo}): {caminho}")
    print(f"\n  destino: {a.destino}")
    print(f"  lista  : {lista}")
    return 1 if problemas or ruins else 0


if __name__ == "__main__":
    raise SystemExit(main())
