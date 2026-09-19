"""
navbar.py
=========
A barra de navegacao comum das tres telas locais.

Existe para o aplicativo de VALIDACAO ficar em destaque e o resto ficar a um
clique de distancia, sem cada pagina inventar o proprio menu. Cada tela tem um
marcador `<!--NAV-->` logo depois do <body>; a rota `/` troca o marcador pelo
resultado de `barra()`.

As portas vem do ambiente (o run.py as define ao subir tudo junto), com os
padroes de quando cada app e aberto sozinho.
"""

from __future__ import annotations

import os

# slug -> (rotulo, subtitulo, porta padrao, variavel de ambiente)
APPS = (
    ("revisor",   "Validação",  "região do chute, no vídeo original", 5007, "PORTA_REVISOR"),
    ("jogadores", "Jogadores",  "cobrador e goleiro pela camisa",     5006, "PORTA_JOGADORES"),
    ("painel",    "Painel",     "visão geral do dataset",             5009, "PORTA_PAINEL"),
    ("rotulador", "Rotulação",  "criar rótulos novos (React)",        5000, "PORTA_ROTULADOR"),
)

CSS = """
<style>
 /* altura fixa e publicada: as telas de 100vh descontam --nav-h para o rodape
    nao ficar cortado embaixo da barra */
 :root{--nav-h:38px}
 .nav-topo{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;overflow-x:auto;
   height:var(--nav-h);box-sizing:border-box;
   padding:0 14px;background:#0d0d16;border-bottom:1px solid #313244;
   font:13px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif;position:relative;z-index:50}
 .nav-topo .marca{font-size:12px;color:#585b70;letter-spacing:.4px;
   text-transform:uppercase;margin-right:4px}
 .nav-topo a{display:flex;align-items:baseline;gap:6px;text-decoration:none;
   color:#9399b2;border:1px solid transparent;border-radius:7px;padding:4px 10px}
 .nav-topo a:hover{color:#cdd6f4;border-color:#313244}
 .nav-topo a small{font-size:10px;color:#585b70}
 /* o app de validacao e o destaque: pilula cheia, sempre o primeiro */
 .nav-topo a.destaque{color:#11111b;background:#a6e3a1;border-color:#a6e3a1;font-weight:700}
 .nav-topo a.destaque small{color:#11111b;opacity:.7}
 .nav-topo a.destaque:hover{color:#11111b;filter:brightness(1.06)}
 .nav-topo a.atual{color:#cdd6f4;border-color:#585b70;background:#181825}
 /* o destaque tambem pode ser a tela atual: repete fundo E tinta, senao a
    regra .atual (mesma especificidade, declarada depois) apaga o verde */
 .nav-topo a.destaque.atual{color:#11111b;background:#a6e3a1;border-color:#cdd6f4}
 .nav-topo .fim{margin-left:auto;font-size:11px;color:#585b70}
</style>
"""


def porta(slug: str) -> int:
    for s, _, _, padrao, env in APPS:
        if s == slug:
            try:
                return int(os.environ.get(env, padrao))
            except ValueError:
                return padrao
    return 0


def no_ar(slug: str) -> bool:
    """
    A aba so vira link se aquele app estiver de pe.

    O run.py publica APPS_NO_AR ao subir; sem essa variavel (app aberto
    sozinho) todas aparecem, porque nao da para saber quem esta rodando.
    """
    lista = os.environ.get("APPS_NO_AR", "")
    return True if not lista else slug in lista.split(",")


def barra(atual: str, host: str = "") -> str:
    """HTML da barra. `atual` e o slug da tela que esta sendo servida."""
    host = host or os.environ.get("HOST_APPS", "127.0.0.1")
    itens = []
    for slug, rotulo, sub, _, _ in APPS:
        if not no_ar(slug) and slug != atual:
            continue
        classes = " ".join(c for c in (
            "destaque" if slug == "revisor" else "",
            "atual" if slug == atual else "") if c)
        alvo = "" if slug == atual else f' href="http://{host}:{porta(slug)}/"'
        itens.append(f'<a class="{classes}"{alvo}>{rotulo}<small>{sub}</small></a>')
    return (CSS + '<nav class="nav-topo"><span class="marca">pênaltis</span>'
            + "".join(itens)
            + '<span class="fim">python run.py</span></nav>')
