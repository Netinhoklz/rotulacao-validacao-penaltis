# =============================================================================
#  CONFIGURAÇÃO — ajuste estas variáveis para a sua máquina
# =============================================================================

# Pasta com os vídeos de origem (.mp4 .mkv .avi .mov .wmv). Subpastas também
# são lidas. A competição de cada vídeo sai do NOME das pastas do caminho:
#   ...copa_do_brasil...                         -> Copa do Brasil
#   ...brasileirao...  ou  videos_penaltis_2023  -> Brasileirão
#   qualquer outro nome                          -> "outros"
PASTA_VIDEOS = r"E:\base_videos_penaltis"

# Onde os rótulos (labels.csv), clips e frames são gravados. Criada se faltar.
PASTA_SAIDA = r"E:\penaltis_rotulados"

# Cache da rotulação: uma cópia leve de cada vídeo, para o frame a frame ser
# rápido. Qualquer pasta com espaço em disco; criada se faltar.
PASTA_CACHE = r"E:\pasta_ref_mais_rapida"

# Tela que abre no navegador: "rotulador", "validacao", "painel" ou "jogadores".
ABRIR_EM = "rotulador"

# =============================================================================
#
# run.py — o único lançador do projeto
# ------------------------------------
#     python run.py                  # sobe tudo e abre a tela de ABRIR_EM
#     python run.py validacao        # abre direto em outra tela
#
# Antes da primeira vez:  pip install -r requirements.txt
#
# As pastas vêm das variáveis acima. Só um --videos / --saida / --cache
# digitado na linha de comando passa por cima delas, e ao subir o run.py
# mostra de onde cada pasta veio (com a linha do arquivo).
#
# As telas sobem juntas, cada uma na sua porta, e a barra do topo troca entre
# elas:
#     rotulacao   http://127.0.0.1:5000   CRIAR rótulos novos (React + FastAPI)
#     validacao   http://127.0.0.1:5007   conferir a região do chute no vídeo
#     jogadores   http://127.0.0.1:5006   cobrador e goleiro pelo número da camisa
#     painel      http://127.0.0.1:5009   visão geral do dataset (somente leitura)
#
# Porta ocupada por outro programa? A tela anda para a próxima livre e a barra
# acompanha. Numa instalação nova, sem nenhum rótulo, só a rotulação sobe;
# validação e painel aparecem a partir do primeiro pênalti salvo (rode de novo).
#
# Nenhuma tela escreve no labels.csv pelas costas: a rotulação é a única que
# acrescenta pênaltis (é o ofício dela); a validação grava a camada de
# correções em <saida>/_revisao_manual/; o painel não grava nada.
# =============================================================================

import argparse
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

BASE_DIR = Path(__file__).parent
FRONTEND_DIR = BASE_DIR / "frontend"
LINHA = "=" * 62
EXT_VIDEO = (".mp4", ".mkv", ".avi", ".mov", ".wmv")   # as mesmas do api.py

# slug -> (rotulo, porta padrao, variavel de ambiente que a barra le)
TELAS = {
    "rotulador": ("Rotulação (criar rótulos)", 5000, "PORTA_ROTULADOR"),
    "validacao": ("Validação (região do chute)", 5007, "PORTA_REVISOR"),
    "jogadores": ("Jogadores (cobrador × goleiro)", 5006, "PORTA_JOGADORES"),
    "painel":    ("Painel do dataset", 5009, "PORTA_PAINEL"),
}


# ---------------------------------------------------------------------------
# [1/4] dependencias
# ---------------------------------------------------------------------------
def checar_dependencias(com_rotulador: bool) -> bool:
    print(LINHA)
    print("  [1/4] Conferindo dependencias...")
    print(LINHA)
    modulos = [("flask", "flask"), ("cv2", "opencv-python")]
    if com_rotulador:
        modulos += [("fastapi", "fastapi"), ("uvicorn", "uvicorn")]
    faltando = []
    for modulo, pacote in modulos:
        if importlib.util.find_spec(modulo) is None:
            faltando.append(pacote)
            print(f"  FALTA   {modulo:<8} -> pip install {pacote}")
        else:
            print(f"  ok      {modulo}")
    if faltando:
        print(f"\n  Instale tudo de uma vez com:\n"
              f"      {sys.executable} -m pip install -r requirements.txt\n")
        return False
    print()
    return True


# ---------------------------------------------------------------------------
# [2/4] pastas
# ---------------------------------------------------------------------------
def linha_da_variavel(nome: str) -> int:
    """Linha do run.py onde a variavel de configuracao e definida."""
    try:
        with open(__file__, encoding="utf-8") as f:
            for i, texto in enumerate(f, 1):
                if texto.startswith(nome + " ="):
                    return i
    except OSError:
        pass
    return 0


def resolver_pastas(videos: Optional[str], saida: Optional[str],
                    cache: Optional[str]) -> Dict[str, Tuple[str, str]]:
    """
    Cada pasta com a sua origem.

    A variavel no topo do run.py manda. So um valor digitado na linha de
    comando passa por cima - de proposito NAO se le variavel de ambiente
    (SOURCE_DIR etc.): ela seria uma sobreposicao invisivel, e quem editou a
    variavel no codigo ficaria sem entender por que a pasta nao mudou.
    """
    def escolher(cli, nome, valor):
        if cli:
            return os.path.abspath(cli), "linha de comando"
        return os.path.abspath(valor), f"variavel {nome} (run.py, linha {linha_da_variavel(nome)})"

    return {
        "videos": escolher(videos, "PASTA_VIDEOS", PASTA_VIDEOS),
        "saida":  escolher(saida, "PASTA_SAIDA", PASTA_SAIDA),
        "cache":  escolher(cache, "PASTA_CACHE", PASTA_CACHE),
    }


def contar_videos(pasta: str) -> int:
    """
    Quantos jogos a fila da rotulacao vai mostrar - a mesma regra do api.py.

    Restos do yt-dlp nao contam: ".temp.mp4" (juncao pela metade), ".f251.webm"
    (faixa de audio) e ".f137.mp4" quando o ".mp4" final do mesmo jogo existe.
    """
    jogos: Set[Tuple[str, str]] = set()
    for raiz, _, arquivos in os.walk(pasta):
        finais = {a.rsplit(".", 1)[0] for a in arquivos
                  if a.lower().endswith(EXT_VIDEO) and not re.search(r"\.(temp|f\d+)\.", a.lower())}
        for a in arquivos:
            if not a.lower().endswith(EXT_VIDEO) or ".temp." in a.lower():
                continue
            stem = re.sub(r"\.f\d+$", "", a.rsplit(".", 1)[0])
            if stem != a.rsplit(".", 1)[0] and stem in finais:
                continue                  # stream parcial com o arquivo final ao lado
            jogos.add((raiz, stem))
    return len(jogos)


def conferir_pastas(pastas: Dict[str, Tuple[str, str]]) -> bool:
    """
    Valida as pastas e as repassa para todas as telas.

    O repasse e pelo ambiente do processo: a rotulacao roda em processo
    proprio (uvicorn) e le SOURCE_DIR, OUTPUT_BASE e FRAMES_CACHE_DIR ao
    iniciar - sem isto ela ignorava a pasta escolhida e seguia no E:.
    """
    print(LINHA)
    print("  [2/4] Pastas...")
    print(LINHA)
    videos, origem_v = pastas["videos"]
    if not os.path.isdir(videos):
        print(f"  ERRO: a pasta de videos nao existe:\n      {videos}")
        print(f"      definida em: {origem_v}\n")
        print(f"  Corrija PASTA_VIDEOS na linha {linha_da_variavel('PASTA_VIDEOS')} do run.py.\n")
        return False
    n = contar_videos(videos)
    print(f"  videos : {videos}")
    print(f"           {n} video(s) na fila da rotulacao")
    print(f"           definida em: {origem_v}")
    if n == 0:
        print("           AVISO: nenhum video (.mp4 .mkv .avi .mov .wmv) nessa pasta")

    for chave, nome in (("saida", "PASTA_SAIDA"), ("cache", "PASTA_CACHE")):
        pasta, origem = pastas[chave]
        try:
            os.makedirs(pasta, exist_ok=True)
        except OSError as e:
            print(f"\n  ERRO: nao deu para criar a pasta de {chave}:\n      {pasta}\n      {e}")
            print(f"  Corrija {nome} na linha {linha_da_variavel(nome)} do run.py.\n")
            return False
        print(f"  {chave:<7}: {pasta}")
        print(f"           definida em: {origem}")

    os.environ["SOURCE_DIR"] = videos
    os.environ["OUTPUT_BASE"] = pastas["saida"][0]
    os.environ["FRAMES_CACHE_DIR"] = pastas["cache"][0]
    print()
    return True


# ---------------------------------------------------------------------------
# Portas: outro programa pode ja estar usando uma delas
# ---------------------------------------------------------------------------
def porta_livre(host: str, porta: int) -> bool:
    """True se da para escutar na porta agora (tenta o bind de verdade)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, porta))
            return True
        except OSError:
            return False


def resolver_portas(host: str, pedidas: Dict[str, int]) -> Tuple[Dict[str, int], List[str]]:
    """
    Garante uma porta livre e distinta para cada tela.

    Sem isto, uma porta ocupada por OUTRO programa (ex.: um rotulador de outro
    projeto na 5000) faz a tela falhar calada - e pior, a barra de navegacao
    continua apontando para a porta, levando para o app errado. Aqui a tela
    anda para a proxima porta livre e o link da barra acompanha.
    """
    finais: Dict[str, int] = {}
    avisos: List[str] = []
    usadas: Set[int] = set()
    for slug, porta in pedidas.items():
        p = porta
        while (p in usadas or not porta_livre(host, p)) and p < porta + 100:
            p += 1
        if p != porta:
            avisos.append(f"porta {porta} ocupada -> {TELAS[slug][0]} vai para {p}")
        finais[slug] = p
        usadas.add(p)
    return finais, avisos


def esperar_porta(host: str, porta: int, proc, segundos: float = 20.0) -> bool:
    """Espera o processo abrir a porta. False se ele morrer ou demorar demais."""
    fim = time.time() + segundos
    while time.time() < fim:
        if proc.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, porta)) == 0:
                return True
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------------------
# Rotulacao: build do React + API FastAPI num processo proprio
# ---------------------------------------------------------------------------
def garantir_build() -> bool:
    """Confere o build do frontend; constroi se faltar. False = a aba nao sobe."""
    if (FRONTEND_DIR / "dist" / "index.html").is_file():
        return True
    if not FRONTEND_DIR.is_dir():
        print("  rotulacao fora: pasta frontend/ nao encontrada")
        return False
    if not shutil.which("npm"):
        print("  rotulacao fora: npm nao esta no PATH (instale o Node.js)")
        return False
    print("  build do frontend React (primeira vez, demora um pouco)...")
    try:
        if not (FRONTEND_DIR / "node_modules").is_dir():
            subprocess.run(["npm", "install"], cwd=str(FRONTEND_DIR),
                           check=True, shell=True)
        subprocess.run(["npm", "run", "build"], cwd=str(FRONTEND_DIR),
                       check=True, shell=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print("  rotulacao fora: falha no build (" + str(e) + ")")
        return False
    return True


def subir_rotulacao(host: str, porta: int):
    """
    Sobe a API de rotulagem em processo separado.

    Ela e FastAPI/uvicorn, nao Flask: nao roda numa thread junto das outras.
    Herda o ambiente, entao ja nasce com as pastas repassadas em [2/4]. Sem
    --reload de proposito - o reload abre um processo filho que sobrevive ao
    Ctrl+C e fica segurando a porta.
    """
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", host,
         "--port", str(porta), "--log-level", "warning"],
        cwd=str(BASE_DIR))


# ---------------------------------------------------------------------------
# [3/4] backup e [4/4] servidores
# ---------------------------------------------------------------------------
def subir(abrir: str, output_base: str, source_dir: str, host: str,
          portas: Dict[str, int], abrir_navegador: bool,
          com_rotulador: bool = True) -> int:
    import revisao as R
    import jogadores as J
    import jogadores_web
    import painel
    import revisor_web

    portas, avisos = resolver_portas(host, portas)
    for aviso in avisos:
        print("  " + aviso)
    if avisos:
        print()

    # As portas vao para o ambiente antes de qualquer pagina ser servida: e
    # dali que a barra de navegacao monta os links entre as telas.
    for slug, (_, _, env) in TELAS.items():
        os.environ[env] = str(portas[slug])
    os.environ["HOST_APPS"] = host

    paths = R.Paths(output_base)
    # Instalacao nova: ainda nao existe nenhum labels.csv. Validacao e painel
    # leem rotulos - sem eles, so a rotulacao faz sentido.
    ha_rotulos = bool(R.fontes(paths))

    print(LINHA)
    print("  [3/4] Backup dos labels.csv originais...")
    print(LINHA)
    if ha_rotulos:
        R.garantir_backup(paths)
        for f in R.conferir_originais(paths):
            selo = "intacto" if f["intacto"] else "MUDOU desde o ultimo backup"
            print(f"  {f['label']:<20}{f['n_linhas']:>5} linhas  "
                  f"{f['snapshots']} snapshot(s)  {selo}")
        print(f"  copia de seguranca: {paths.dir_original}\n")
    else:
        print("  ainda nao ha rotulos nesta pasta de saida - nada a copiar\n")

    print(LINHA)
    print("  [4/4] Subindo as telas...")
    print(LINHA)

    apps = []
    no_ar = []
    if ha_rotulos:
        # contexto de cada app (indice de videos, rotulos de origem, caminhos)
        revisor_web.preparar(paths, source_dir)
        painel.preparar(output_base, source_dir)
        apps += [("validacao", revisor_web.app), ("painel", painel.app)]
        no_ar += ["revisor", "painel"]
        paths_jog = J.PathsJog(output_base)
        if os.path.isfile(paths_jog.csv):
            jogadores_web.preparar(paths_jog, source_dir)
            apps.append(("jogadores", jogadores_web.app))
            no_ar.append("jogadores")
    else:
        print("  validacao e painel ficam para depois do primeiro penalti salvo")
        print("  (rode o run.py de novo quando tiver rotulos)")

    # decidido ANTES de subir qualquer coisa: a barra so mostra aba de app que
    # esta de pe, e o processo da rotulacao precisa herdar essa lista pronta
    pode_rotular = com_rotulador and garantir_build()
    if pode_rotular:
        no_ar.append("rotulador")
    os.environ["APPS_NO_AR"] = ",".join(no_ar)

    if not apps and not pode_rotular:
        print("\n  Nada para subir: sem rotulos ainda e sem a rotulacao disponivel.")
        return 1

    def marca(slug):
        return "  <-- abre aqui" if slug == abrir else ""

    for slug, app in apps:
        # cada tela no seu proprio servidor; daemon para o Ctrl+C encerrar tudo
        threading.Thread(
            target=app.run,
            kwargs={"host": host, "port": portas[slug], "debug": False,
                    "threaded": True, "use_reloader": False},
            daemon=True,
        ).start()
        print(f"  {TELAS[slug][0]:<32}http://{host}:{portas[slug]}{marca(slug)}")

    proc_rot = subir_rotulacao(host, portas["rotulador"]) if pode_rotular else None
    rotulacao_ok = False
    if proc_rot:
        rotulacao_ok = esperar_porta(host, portas["rotulador"], proc_rot)
        if rotulacao_ok:
            print(f"  {TELAS['rotulador'][0]:<32}http://{host}:{portas['rotulador']}"
                  f"{marca('rotulador')}")
        else:
            print(f"  ROTULACAO NAO SUBIU na porta {portas['rotulador']} "
                  f"(codigo {proc_rot.poll()}) - veja o erro acima")

    if ha_rotulos:
        resumo = R.resumo(R.carregar(paths).registros)
        pend = resumo["status"].get("pendente", 0)
        print(f"\n  {resumo['total']} rotulos · {pend} ainda a conferir na validacao")
        print(f"  originais: nunca sao escritos pela validacao")

    # abre a tela pedida; se ela nao subiu, a primeira que estiver de pe
    no_ar_slugs = [s for s, _ in apps] + (["rotulador"] if rotulacao_ok else [])
    alvo = abrir if abrir in no_ar_slugs else (no_ar_slugs[0] if no_ar_slugs else "")
    if alvo:
        url = f"http://{host}:{portas[alvo]}/"
        print(f"\n  Abrindo {url}")
        if abrir_navegador:
            threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print("  Ctrl+C encerra tudo.\n")

    try:
        while True:                      # os servidores vivem nas threads
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  encerrando...")
    finally:
        if proc_rot and proc_rot.poll() is None:
            proc_rot.terminate()         # o filho nao pode ficar segurando a porta
            try:
                proc_rot.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc_rot.kill()
    print("  encerrado.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Sobe o servidor local das ferramentas de penaltis. "
                    "As pastas ficam nas variaveis do topo do run.py.")
    ap.add_argument("abrir", nargs="?", default=ABRIR_EM,
                    choices=["rotulador", "validacao", "jogadores", "painel"],
                    help=f"em qual tela abrir o navegador (padrao: ABRIR_EM = {ABRIR_EM})")
    ap.add_argument("--videos", dest="videos", default=None,
                    help=f"opcional: passa por cima de PASTA_VIDEOS ({PASTA_VIDEOS})")
    ap.add_argument("--saida", dest="saida", default=None,
                    help=f"opcional: passa por cima de PASTA_SAIDA ({PASTA_SAIDA})")
    ap.add_argument("--cache", dest="cache", default=None,
                    help=f"opcional: passa por cima de PASTA_CACHE ({PASTA_CACHE})")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--porta-rotulador", type=int, default=TELAS["rotulador"][1])
    ap.add_argument("--porta-validacao", type=int, default=TELAS["validacao"][1])
    ap.add_argument("--porta-jogadores", type=int, default=TELAS["jogadores"][1])
    ap.add_argument("--porta-painel", type=int, default=TELAS["painel"][1])
    ap.add_argument("--sem-rotulador", action="store_true",
                    help="nao sobe a aba de rotulacao (dispensa Node/npm)")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()

    if not checar_dependencias(not a.sem_rotulador):
        return 1
    pastas = resolver_pastas(a.videos, a.saida, a.cache)
    if not conferir_pastas(pastas):
        return 1
    return subir(a.abrir, pastas["saida"][0], pastas["videos"][0], a.host,
                 {"rotulador": a.porta_rotulador,
                  "validacao": a.porta_validacao,
                  "jogadores": a.porta_jogadores,
                  "painel": a.porta_painel},
                 not a.sem_navegador, not a.sem_rotulador)


if __name__ == "__main__":
    raise SystemExit(main())
