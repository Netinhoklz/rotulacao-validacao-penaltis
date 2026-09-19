"""
gerar_figuras.py
================
Figuras do artigo, geradas a partir dos dados reais da base rotulada.

Convencoes de publicacao adotadas
---------------------------------
* Largura fixa de coluna: LARG_1COL (88 mm) e LARG_2COL (182 mm). As figuras
  sao geradas no tamanho final de impressao — nada e reescalado depois, o que
  garante que o corpo do texto da figura case com o do artigo.
* Tipografia serifada em 7 pt, igual ao corpo de IEEE/Elsevier.
* Sem titulo embutido: o titulo de uma figura de artigo e a \\caption. Os
  paineis recebem apenas as letras (a), (b), (c).
* Sem texto explicativo dentro da arte — as legendas prontas para LaTeX estao
  em figuras/README.md.
* Tracos finos (0,5–0,7 pt), sem sombra, sem cantos arredondados, sem grade
  pesada. PDF vetorial e o entregavel; o PNG a 600 dpi serve para conferencia.
* A baliza e desenhada na proporcao regulamentar de 7,32 x 2,44 m (3:1) —
  desenha-la quadrada e um erro que um leitor da area percebe de imediato.

Saida: figuras/*.pdf (vetorial) + figuras/*.png (600 dpi)

Uso:
    python gerar_figuras.py
"""

from __future__ import annotations

import csv
import os
import re
import statistics
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import FancyArrowPatch, Polygon, Rectangle

import competitions as comps

OUTPUT_BASE = os.environ.get("OUTPUT_BASE", r"E:\penaltis_rotulados")
SOURCE_DIR = os.environ.get("SOURCE_DIR", r"E:\base_videos_penaltis")
DEST = Path("figuras")

# --------------------------------------------------------------------------
# Parametros de estilo — trocar FAMILIA aqui alinha tudo ao seu template
# --------------------------------------------------------------------------
LARG_1COL = 3.46      # 88 mm
LARG_15COL = 5.20     # 132 mm
LARG_2COL = 7.16      # 182 mm
FS = 7.0              # corpo
FS_P = 6.0            # secundario
FS_MIN = 5.2          # rotulos densos
FAMILIA = ["Times New Roman", "Nimbus Roman", "DejaVu Serif"]
MONO = ["Consolas", "DejaVu Sans Mono"]

TINTA = "#000000"
TINTA2 = "#3c3c3c"
FRACO = "#6e6e6e"
REGUA = "#9a9a9a"
GRADE = "#dcdcdc"

# Paleta impressa: sobria, distinguivel em escala de cinza pela ordem de
# luminancia (azul escuro < laranja < verde claro).
AZUL = "#1f4e79"
LARANJA = "#c25e1d"
VERDE = "#3d7d5a"
VERMELHO = "#a32020"

RAMPA_AZUL = ["#f2f6fb", "#dce8f4", "#c2d6ea", "#a3c0dd", "#7fa6cd",
              "#5c8bbc", "#3f72a8", "#2a5c92", "#1f4e79", "#163a5c"]
RAMPA_LARANJA = ["#fdf4ee", "#fae3d4", "#f5cbb0", "#eeae87", "#e39061",
                 "#d47643", "#c25e1d", "#a54e17", "#853d12", "#632c0d"]
CM_AZUL = LinearSegmentedColormap.from_list("az", RAMPA_AZUL)
CM_LARANJA = LinearSegmentedColormap.from_list("lj", RAMPA_LARANJA)

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": FAMILIA,
    "font.size": FS,
    "axes.titlesize": FS,
    "axes.labelsize": FS,
    "xtick.labelsize": FS_P,
    "ytick.labelsize": FS_P,
    "legend.fontsize": FS_P,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "savefig.facecolor": "white",
    "text.color": TINTA,
    "axes.labelcolor": TINTA,
    "axes.edgecolor": REGUA,
    "axes.linewidth": 0.6,
    "xtick.color": TINTA2, "ytick.color": TINTA2,
    "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    "xtick.major.size": 2.4, "ytick.major.size": 2.4,
    "xtick.direction": "out", "ytick.direction": "out",
    "axes.spines.top": False, "axes.spines.right": False,
    "lines.linewidth": 0.9,
    "patch.linewidth": 0.6,
    "legend.frameon": False,
    "legend.handlelength": 1.3,
    "legend.handletextpad": 0.5,
    "legend.columnspacing": 1.4,
    "pdf.fonttype": 42, "ps.fonttype": 42,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.015,
})


def nb(v, casas=1):
    """Numero pt-BR: virgula decimal, ponto de milhar."""
    return f"{v:,.{casas}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _lum(c):
    r, g, b = to_rgb(c)
    f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def tinta_sobre(cor):
    return TINTA if _lum(cor) > 0.42 else "#ffffff"


def painel(ax, letra, x=-0.02, y=1.045):
    ax.text(x, y, f"({letra})", transform=ax.transAxes, fontsize=FS,
            fontweight="bold", va="bottom", ha="left")


def salvar(fig, nome):
    DEST.mkdir(exist_ok=True)
    fig.savefig(DEST / f"{nome}.pdf")
    fig.savefig(DEST / f"{nome}.png", dpi=600)
    plt.close(fig)
    print(f"  {nome}")


# --------------------------------------------------------------------------
# Dados
# --------------------------------------------------------------------------
REGIOES = ["gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito",
           "gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito",
           "gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito",
           "fora_esquerda", "fora_cima", "fora_direita"]
GRELHA = [["gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito"],
          ["gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito"],
          ["gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito"]]
CAMERAS = ["visão do torcedor", "visão cobrador", "visão goleiro"]

# Geometria regulamentar da meta (m)
GW, GH = 7.32, 2.44
CW, CH = GW / 3, GH / 3


def carregar():
    fonte = {}
    for root, _, files in os.walk(SOURCE_DIR):
        for f in files:
            if f.lower().endswith((".mp4", ".avi", ".mov", ".mkv", ".wmv")):
                comp, pasta = comps.classify(os.path.join(root, f))
                ano = re.search(r"(?:19|20)\d{2}", pasta)
                fonte[f] = {"comp": comp.slug,
                            "temporada": ano.group(0) if ano else None}
    linhas = []
    for slug, csv_path in comps.existing_csvs(OUTPUT_BASE):
        with open(csv_path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                m = fonte.get(r["video_file"], {})
                r["_comp"] = m.get("comp") or r.get("competition") or slug
                r["_temp"] = m.get("temporada")
                linhas.append(r)
    return linhas, fonte


def eh_gol(r):
    return str(r.get("is_goal", "")).strip().lower() in ("true", "1", "sim")


# --------------------------------------------------------------------------
# Baliza em proporcao regulamentar
# --------------------------------------------------------------------------
def desenhar_meta(ax, preencher=None, rotulo=None, rot_fora=None,
                  preencher_fora=None, rede=True, fs=FS_MIN, codigos=False):
    """Elevacao frontal da meta (7,32 x 2,44 m) com a grelha 3x3 e as zonas
    externas. `preencher` e `rotulo` sao dicts codigo -> cor / texto.
    Com `codigos`, imprime o identificador da classe sob o nome da zona."""
    preencher = preencher or {}
    rotulo = rotulo or {}
    preencher_fora = preencher_fora or {}
    rot_fora = rot_fora or {}

    mx, my = GW * 0.26, GH * 0.40        # extensao das faixas externas
    folga = GW * 0.085                   # respiro entre a meta e as faixas
    fora_geo = {
        "fora_esquerda": (-folga - mx, 0, mx, GH),
        "fora_direita": (GW + folga, 0, mx, GH),
        "fora_cima": (-folga - mx, GH + folga * 0.55, GW + 2 * (folga + mx), my),
    }
    for cod, (x, y, w, h) in fora_geo.items():
        c = preencher_fora.get(cod, "#ffffff")
        ax.add_patch(Rectangle((x, y), w, h, facecolor=c, edgecolor=REGUA,
                               lw=0.5, ls=(0, (2.5, 2)), zorder=1))
        if cod in rot_fora:
            tc = tinta_sobre(c) if cod in preencher_fora else TINTA2
            dy = 0.11 * GH if codigos else 0.0
            ax.text(x + w / 2, y + h / 2 + dy, rot_fora[cod], ha="center",
                    va="center", fontsize=fs, color=tc, linespacing=1.25,
                    zorder=4)
            if codigos:
                ax.text(x + w / 2, y + h / 2 - dy, cod, ha="center", va="center",
                        fontsize=fs - 0.9, color=FRACO, family="monospace",
                        zorder=4)

    if rede:   # malha da rede, bem tenue
        for i in np.arange(0, GW + 0.01, GW / 30):
            ax.plot([i, i], [0, GH], color="#e8e8e8", lw=0.28, zorder=1.5)
        for j in np.arange(0, GH + 0.01, GH / 10):
            ax.plot([0, GW], [j, j], color="#e8e8e8", lw=0.28, zorder=1.5)

    for li, fila in enumerate(GRELHA):
        for co, cod in enumerate(fila):
            x, y = co * CW, (2 - li) * CH
            c = preencher.get(cod)
            ax.add_patch(Rectangle((x, y), CW, CH, facecolor=c if c else "none",
                                   edgecolor=REGUA, lw=0.5, zorder=2))
            if cod in rotulo:
                tc = tinta_sobre(c) if c else TINTA
                dy = 0.15 * CH if codigos else 0.0
                ax.text(x + CW / 2, y + CH / 2 + dy, rotulo[cod], ha="center",
                        va="center", fontsize=fs, color=tc, linespacing=1.25,
                        zorder=4)
                if codigos:
                    ax.text(x + CW / 2, y + CH / 2 - dy, cod, ha="center",
                            va="center", fontsize=fs - 0.9, color=FRACO,
                            family="monospace", zorder=4)

    # Estrutura: travessao e postes, em traco cheio
    esp = 0.085
    ax.add_patch(Rectangle((-esp, GH), GW + 2 * esp, esp, facecolor=TINTA,
                           edgecolor="none", zorder=5))
    for px in (-esp, GW):
        ax.add_patch(Rectangle((px, 0), esp, GH, facecolor=TINTA,
                               edgecolor="none", zorder=5))
    ax.plot([-folga - mx, GW + folga + mx], [0, 0], color=TINTA, lw=0.8,
            zorder=5)

    ax.set_xlim(-folga - mx - 0.30, GW + folga + mx + 0.30)
    ax.set_ylim(-0.34, GH + folga * 0.55 + my + 0.10)
    ax.set_aspect("equal")
    ax.axis("off")


# ==========================================================================
# FIG 1 — taxonomia
# ==========================================================================
def fig01(linhas):
    fig, ax = plt.subplots(figsize=(LARG_15COL, LARG_15COL * 0.46))
    rot = {c: c.replace("gol_", "").replace("_", " ")
           for c in REGIOES if c.startswith("gol")}
    rf = {c: c.replace("fora_", "") for c in REGIOES if c.startswith("fora")}
    desenhar_meta(ax, rotulo=rot, rot_fora=rf, fs=FS_MIN, codigos=True)

    # Cotas regulamentares, no respiro entre a meta e as faixas externas
    xc = GW + GW * 0.085 / 2
    ax.annotate("", xy=(xc, 0), xytext=(xc, GH),
                arrowprops=dict(arrowstyle="<->", lw=0.5, color=TINTA2,
                                shrinkA=0, shrinkB=0))
    ax.text(xc + 0.06, GH / 2, "2,44 m", ha="left", va="center",
            fontsize=FS_MIN, color=TINTA2, rotation=90)
    ax.annotate("", xy=(0, -0.19), xytext=(GW, -0.19),
                arrowprops=dict(arrowstyle="<->", lw=0.5, color=TINTA2,
                                shrinkA=0, shrinkB=0))
    ax.text(GW / 2, -0.24, "7,32 m", ha="center", va="top", fontsize=FS_MIN,
            color=TINTA2)
    salvar(fig, "fig01_taxonomia_baliza")


# ==========================================================================
# FIG 3 — frequencia e conversao por zona
# ==========================================================================
def fig03(linhas):
    n = len(linhas)
    freq = Counter(r["region"] for r in linhas)
    gols = Counter(r["region"] for r in linhas if eh_gol(r))
    taxa = {c: (gols[c] / freq[c] if freq[c] else 0) for c in REGIOES}

    # A meta tem proporcao fixa; a altura da figura e derivada dela para nao
    # sobrar faixa morta entre o desenho e a barra de cor.
    mx, my, folga = GW * 0.26, GH * 0.40, GW * 0.085
    larg_dados = GW + 2 * (folga + mx) + 0.60
    alt_dados = GH + folga * 0.55 + my + 0.44
    asp = larg_dados / alt_dados

    marg_x, vao = 0.012, 0.030
    lp = (1 - 2 * marg_x - vao) / 2                     # largura do painel
    lp_pol = lp * LARG_2COL
    ap_pol = lp_pol / asp
    h_cb, h_rot = 0.075, 0.30                           # barra de cor + rotulos
    fig_h = ap_pol + h_cb + h_rot
    fig = plt.figure(figsize=(LARG_2COL, fig_h))

    def montar(k, valores, cmap, fmt, vmax, letra, rot_cb, ticks):
        x = marg_x + k * (lp + vao)
        ax = fig.add_axes([x, (h_cb + h_rot) / fig_h, lp, ap_pol / fig_h])
        pf, pl, ff, fl = {}, {}, {}, {}
        for cod in REGIOES:
            cor = cmap(0.06 + 0.86 * (valores[cod] / vmax if vmax else 0))
            (pf if cod.startswith("gol") else ff)[cod] = cor
            (pl if cod.startswith("gol") else fl)[cod] = fmt(cod)
        desenhar_meta(ax, preencher=pf, rotulo=pl, preencher_fora=ff,
                      rot_fora=fl, rede=False, fs=FS_MIN)
        ax.text(0.0, 1.0, f"({letra})", transform=ax.transAxes, fontsize=FS,
                fontweight="bold", va="top", ha="left")

        cax = fig.add_axes([x + lp * 0.24, h_rot / fig_h * 0.62,
                            lp * 0.52, h_cb / fig_h])
        cax.imshow(np.linspace(0, 1, 256).reshape(1, -1), aspect="auto",
                   cmap=cmap, extent=(0, 1, 0, 1))
        cax.set_yticks([])
        cax.set_xticks(ticks[0]); cax.set_xticklabels(ticks[1], fontsize=FS_MIN)
        cax.tick_params(length=1.6, pad=1.2)
        for sp in cax.spines.values():
            sp.set_visible(True); sp.set_linewidth(0.4); sp.set_edgecolor(REGUA)
        cax.set_xlabel(rot_cb, fontsize=FS_P, labelpad=1.8)

    vmax = max(freq.values())
    montar(0, freq, CM_AZUL, lambda c: f"{freq[c]}\n{nb(100*freq[c]/n)}%", vmax,
           "a", "cobranças por zona",
           ([0, 0.5, 1], ["0", str(vmax // 2), str(vmax)]))
    montar(1, taxa, CM_LARANJA,
           lambda c: f"{nb(100*taxa[c], 0)}%\nn={freq[c]}", 1.0, "b",
           "taxa de conversão", ([0, 0.5, 1], ["0%", "50%", "100%"]))
    salvar(fig, "fig03_zonas_frequencia_conversao")


# ==========================================================================
# FIG 5 — os tres pontos de vista
# ==========================================================================
def fig05(linhas):
    porvid = defaultdict(list)
    for r in linhas:
        porvid[r["video_file"]].append(r)
    escolha = None
    for vf, g in porvid.items():
        if {x["camera_type"] for x in g} == set(CAMERAS) and len(g) == 3:
            escolha = (vf, g)
            if vf.startswith("CORITIBA 0 X 1 CORINTHIANS"):
                break
    if not escolha:
        print("  [!] fig05 ignorada")
        return
    vf, grupo = escolha
    grupo.sort(key=lambda r: CAMERAS.index(r["camera_type"]))

    fig, axes = plt.subplots(1, 3, figsize=(LARG_2COL, LARG_2COL * 0.215))
    fig.subplots_adjust(wspace=0.035, left=0, right=1, top=0.90, bottom=0.10)
    sub = {"visão do torcedor": "lateral de arquibancada",
           "visão cobrador": "atrás do batedor",
           "visão goleiro": "atrás da meta"}
    for ax, r, letra in zip(axes, grupo, "abc"):
        ax.imshow(cv2.cvtColor(cv2.imread(r["frame_chute_path"]), cv2.COLOR_BGR2RGB))
        ax.set_xticks([]); ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(True); s.set_linewidth(0.5); s.set_edgecolor(REGUA)
        ax.set_title(f"({letra}) {sub[r['camera_type']]}", fontsize=FS_P,
                     color=TINTA, pad=2.4)
        ax.set_xlabel(r["camera_type"], fontsize=FS_MIN, color=TINTA2,
                      labelpad=2.0, family="monospace")
    salvar(fig, "fig05_pontos_de_vista")


# ==========================================================================
# FIG 6 — anatomia do clipe
# ==========================================================================
def fig06(linhas):
    alvo = next((r for r in linhas
                 if r["video_file"].startswith("CORITIBA 0 X 1 CORINTHIANS")
                 and r["camera_type"] == "visão cobrador"), None) or linhas[0]
    src = None
    for root, _, files in os.walk(SOURCE_DIR):
        if alvo["video_file"] in files:
            src = os.path.join(root, alvo["video_file"]); break
    if not src:
        print("  [!] fig06 ignorada"); return

    ini, chu = int(alvo["inicio_frame"]), int(alvo["chute_frame"])
    dentro = [ini, ini + (chu - ini) // 3, ini + 2 * (chu - ini) // 3, chu]
    fora = [chu + max(5, (chu - ini) // 5), chu + max(14, (chu - ini) // 2)]
    cap = cv2.VideoCapture(src)
    qs = {}
    for idx in dentro + fora:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, f = cap.read()
        if ok:
            qs[idx] = cv2.cvtColor(cv2.resize(f, (320, 180)), cv2.COLOR_BGR2RGB)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.release()

    fig = plt.figure(figsize=(LARG_2COL, LARG_2COL * 0.30))
    gs = fig.add_gridspec(2, 6, height_ratios=[1, 0.40], hspace=0.34,
                          wspace=0.035, left=0.005, right=0.995,
                          top=0.93, bottom=0.10)
    for k, idx in enumerate(dentro + fora):
        ax = fig.add_subplot(gs[0, k])
        img = qs.get(idx)
        if img is not None:
            ax.imshow(img if idx in dentro
                      else (img * 0.30 + 255 * 0.70).astype(np.uint8))
        ax.set_xticks([]); ax.set_yticks([])
        for s in ax.spines.values():
            s.set_visible(True)
            s.set_linewidth(0.8 if idx in dentro else 0.5)
            s.set_edgecolor(AZUL if idx in dentro else "#c9c9c9")
            if idx not in dentro:
                s.set_linestyle((0, (2, 1.6)))
        ax.set_xlabel(f"{nb((idx-ini)/fps, 2)} s", fontsize=FS_MIN,
                      color=TINTA2 if idx in dentro else FRACO, labelpad=1.6)
        if idx == dentro[0]:
            ax.set_title("início da corrida", fontsize=FS_MIN, pad=2.0)
        if idx == dentro[-1]:
            ax.set_title("último quadro (pré-contato)", fontsize=FS_MIN, pad=2.0)

    ax = fig.add_subplot(gs[1, :])
    ax.set_xlim(-0.015, 1.36); ax.set_ylim(0, 1); ax.axis("off")
    ax.add_patch(Rectangle((0, 0.52), 1.0, 0.30, facecolor=AZUL, edgecolor="none"))
    ax.add_patch(Rectangle((1.005, 0.52), 0.355, 0.30, facecolor="#f4f4f4",
                           edgecolor="#c9c9c9", lw=0.5, ls=(0, (2, 1.6))))
    ax.text(0.5, 0.67, "clipe publicado — estritamente pré-contato",
            ha="center", va="center", fontsize=FS_P, color="white")
    ax.text(1.182, 0.67, "contato · trajetória · desfecho",
            ha="center", va="center", fontsize=FS_P, color=TINTA2)
    for x, rot, ha in ((0.0, "marco 1", "left"), (1.0, "marco 2", "right")):
        ax.plot([x, x], [0.46, 0.88], color=TINTA, lw=0.9)
        ax.text(x + (0.008 if ha == "left" else -0.008), 0.40, rot, ha=ha,
                va="top", fontsize=FS_P)
    ax.text(1.182, 0.40, "não integra o dataset", ha="center", va="top",
            fontsize=FS_P, color=FRACO, style="italic")
    ax.annotate("", xy=(1.0, 0.95), xytext=(0.0, 0.95),
                arrowprops=dict(arrowstyle="<->", lw=0.5, color=FRACO,
                                shrinkA=0, shrinkB=0))
    ax.text(0.5, 0.97, f"{nb((chu-ini)/fps, 2)} s ({chu-ini+1} quadros)",
            ha="center", va="bottom", fontsize=FS_MIN, color=FRACO)
    salvar(fig, "fig06_anatomia_clipe")


# ==========================================================================
# FIG 7 — cobertura por temporada
# ==========================================================================
def fig07(linhas, fonte):
    temps = sorted({m["temporada"] for m in fonte.values()
                    if m["comp"] == "brasileirao" and m["temporada"]})
    base, anot, cobr = [], [], []
    for t in temps:
        vids = {f for f, m in fonte.items()
                if m["comp"] == "brasileirao" and m["temporada"] == t}
        g = [r for r in linhas if r["_temp"] == t]
        base.append(len(vids)); anot.append(len({r["video_file"] for r in g}))
        cobr.append(len(g))

    x = np.arange(len(temps)); w = 0.27
    fig, ax = plt.subplots(figsize=(LARG_1COL, LARG_1COL * 0.72))
    for k, (rot, v, c) in enumerate((("partidas na base", base, AZUL),
                                     ("partidas anotadas", anot, LARANJA),
                                     ("cobranças", cobr, VERDE))):
        ax.bar(x + (k - 1) * w, v, w * 0.92, label=rot, color=c,
               edgecolor="none")
    ax.set_xticks(x); ax.set_xticklabels(temps)
    ax.set_ylabel("contagem")
    ax.set_ylim(0, max(cobr) * 1.30)
    ax.yaxis.grid(True, color=GRADE, lw=0.5)
    ax.set_axisbelow(True)
    ax.spines["left"].set_color(REGUA); ax.spines["bottom"].set_color(REGUA)
    ax.legend(ncols=1, loc="upper left", fontsize=FS_MIN,
              bbox_to_anchor=(0.005, 1.02))
    i22 = temps.index("2022") if "2022" in temps else None
    if i22 is not None:
        ax.annotate("acervo escasso", xy=(i22 + w, cobr[i22] + 4),
                    xytext=(i22 + 0.20, max(cobr) * 0.72), fontsize=FS_MIN,
                    color=TINTA2, ha="center",
                    arrowprops=dict(arrowstyle="-", lw=0.5, color=FRACO))
    salvar(fig, "fig07_cobertura_temporada")


# ==========================================================================
# FIG 8 — duracao dos clipes
# ==========================================================================
def fig08(linhas):
    d = []
    for r in linhas:
        try:
            v = float(r["chute_time_s"]) - float(r["inicio_time_s"])
        except (KeyError, ValueError, TypeError):
            continue
        if v > 0:
            d.append(v)
    med, mea = statistics.median(d), statistics.mean(d)
    q1, _, q3 = statistics.quantiles(d, n=4)

    fig, ax = plt.subplots(figsize=(LARG_1COL, LARG_1COL * 0.72))
    ax.hist(np.clip(d, 0, 3.05), bins=np.arange(0, 3.15, 0.1), color=AZUL,
            edgecolor="white", linewidth=0.25)
    ax.axvspan(q1, q3, color=AZUL, alpha=0.10, lw=0)
    topo = ax.get_ylim()[1] * 1.22
    ax.set_ylim(0, topo)
    ax.axvline(med, color=LARANJA, lw=1.0)
    ax.annotate(f"mediana {nb(med, 2)} s", xy=(med, topo * 0.80),
                xytext=(med + 0.32, topo * 0.94), fontsize=FS_MIN,
                color=LARANJA, ha="left",
                arrowprops=dict(arrowstyle="-", lw=0.5, color=LARANJA))
    ax.text(q3 + 0.04, topo * 0.60, f"IQR\n{nb(q1,2)}–{nb(q3,2)} s",
            fontsize=FS_MIN, color=TINTA2, va="top", linespacing=1.25)
    ax.set_xlabel("duração início→impacto (s)")
    ax.set_ylabel("cobranças")
    ax.set_xlim(0, 3.10)
    ax.set_xticks(np.arange(0, 3.5, 0.5))
    ax.set_xticklabels([nb(v) for v in np.arange(0, 3.0, 0.5)] + ["3,0+"])
    ax.yaxis.grid(True, color=GRADE, lw=0.5); ax.set_axisbelow(True)
    ax.spines["left"].set_color(REGUA); ax.spines["bottom"].set_color(REGUA)
    salvar(fig, "fig08_duracao_clipes")


# ==========================================================================
# FIG 9 — decomposicao do titulo
# ==========================================================================
def fig09(linhas):
    """Decomposicao do titulo. Campos de um unico caractere nao comportam
    rotulo abaixo do bracket, entao o papel e codificado pela cor e resolvido
    numa legenda unica no rodape."""
    PAPEIS = [("clube", AZUL),
              ("placar do tempo normal", VERDE),
              ("placar da disputa (ruído)", VERMELHO),
              ("rodada / fase", LARANJA)]
    COR = {nome: cor for nome, cor in PAPEIS}

    fig, ax = plt.subplots(figsize=(LARG_2COL, LARG_2COL * 0.215))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    X0, CW_ = 0.040, 0.0098      # origem e passo do monoespacado

    def caso(y, letra, bruto, campos, ok, chave, nota=None):
        ax.text(0.0, y, f"({letra})", fontsize=FS, fontweight="bold",
                va="baseline")
        ax.text(X0, y, bruto, fontsize=FS, family="monospace", va="baseline")
        for a, b, papel in campos:
            xa, xb = X0 + a * CW_, X0 + b * CW_
            cor = COR[papel]
            ax.plot([xa, xb], [y - 0.075, y - 0.075], color=cor, lw=1.2,
                    solid_capstyle="butt")
            for xe in (xa, xb):
                ax.plot([xe, xe], [y - 0.075, y - 0.040], color=cor, lw=1.2)
        ax.plot([X0 + 0.004], [y - 0.163], marker="o" if ok else "X", ms=3.2,
                mew=0.9, color=VERDE if ok else VERMELHO, clip_on=False)
        ax.text(X0 + 0.020, y - 0.180,
                ("chave construída: " if ok else "chave incorreta: ") + chave,
                fontsize=FS_P, color=VERDE if ok else VERMELHO, va="baseline")
        if nota:
            ax.text(X0 + 0.020, y - 0.265, nota, fontsize=FS_MIN, color=TINTA2,
                    va="baseline")

    caso(0.95, "a", "GREMIO 2 X 1 FORTALEZA MELHORES MOMENTOS 24 RODADA",
         [(0, 6, "clube"), (7, 12, "placar do tempo normal"),
          (13, 22, "clube"), (43, 50, "rodada / fase")],
         True, "GREMIO \u00d7 FORTALEZA, rodada 24")

    caso(0.50, "b", "CAXIAS 2 (5) X (6) 2 BAHIA MELHORES MOMENTOS 2 FASE",
         [(0, 6, "clube"), (7, 8, "placar do tempo normal"),
          (9, 18, "placar da disputa (ruído)"),
          (19, 20, "placar do tempo normal"), (21, 26, "clube"),
          (44, 50, "rodada / fase")],
         False, "CAXIAS \u00d7 BAHIA, 2\u00aa fase",
         "o padrão (\\d+) X (\\d+) captura “5 X 6”, o placar da disputa, "
         "e o confronto não é encontrado na base de partidas")

    # Legenda unica: resolve o papel de cada bracket sem poluir a arte
    x = X0
    for nome, cor in PAPEIS:
        ax.plot([x, x + 0.016], [0.055, 0.055], color=cor, lw=1.6,
                solid_capstyle="butt")
        ax.text(x + 0.022, 0.055, nome, fontsize=FS_MIN, color=TINTA2,
                va="center")
        x += 0.036 + len(nome) * 0.0058
    salvar(fig, "fig09_decomposicao_titulo")


# ==========================================================================
# FIG 2 — funil de construcao
# ==========================================================================
ETAPAS = [("Transfermarkt", "partidas com\npênalti registrado", "partidas"),
          ("extração da\nplaylist", "candidato de vídeo\nlocalizado", "partidas"),
          ("casamento e\ndownload", "vídeo obtido\n(yt-dlp)", "partidas"),
          ("rotulação", "lances distintos\nanotados", "lances"),
          ("validação", "rótulos íntegros\n(um por ângulo)", "rótulos")]
FUNIL = {"Brasileirão": ([615, 482, 401, 460, 807], AZUL),
         "Copa do Brasil": ([888, 333, None, 163, 227], LARANJA)}


def fig02(linhas):
    fig, ax = plt.subplots(figsize=(LARG_2COL, LARG_2COL * 0.29))
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")

    n = len(ETAPAS); larg, vao = 0.166, 0.042
    x0 = (1 - (n * larg + (n - 1) * vao)) / 2
    yb, yt = 0.60, 0.94
    for k, (tit, sub, uni) in enumerate(ETAPAS):
        x = x0 + k * (larg + vao)
        ax.add_patch(Rectangle((x, yb), larg, yt - yb, facecolor="white",
                               edgecolor=REGUA, lw=0.6))
        ax.text(x + larg / 2, yt - 0.055, tit, ha="center", va="center",
                fontsize=FS, linespacing=1.2)
        ax.text(x + larg / 2, yb + 0.105, sub, ha="center", va="center",
                fontsize=FS_MIN, color=TINTA2, linespacing=1.25)
        ax.text(x + larg / 2, yb + 0.032, uni, ha="center", va="center",
                fontsize=FS_MIN, color=FRACO, style="italic")
        if k < n - 1:
            ax.add_patch(FancyArrowPatch((x + larg + 0.004, (yb + yt) / 2),
                                         (x + larg + vao - 0.004, (yb + yt) / 2),
                                         arrowstyle="-|>", mutation_scale=6,
                                         lw=0.6, color=FRACO))
    for j, (nome, (vals, cor)) in enumerate(FUNIL.items()):
        y = 0.40 - j * 0.185
        ax.text(x0 - 0.012, y, nome, fontsize=FS_P, color=cor, ha="right",
                va="center")
        ant = None
        for k, v in enumerate(vals):
            x = x0 + k * (larg + vao)
            if v is None:
                ax.text(x + larg / 2, y, "—", ha="center", va="center",
                        fontsize=FS, color=FRACO)
                ant = None
                continue
            ax.add_patch(Rectangle((x + larg / 2 - 0.040, y - 0.038), 0.080,
                                   0.076, facecolor=cor, edgecolor="none"))
            ax.text(x + larg / 2, y, f"{v:,}".replace(",", "."), ha="center",
                    va="center", fontsize=FS_P, color="white")
            if ant:
                mesma = ETAPAS[k - 1][2] == ETAPAS[k][2]
                txt = f"{nb(100*v/ant, 0)}%" if mesma else f"×{nb(v/ant, 2)}"
                ax.text(x - vao / 2, y, txt, ha="center", va="center",
                        fontsize=FS_MIN, color=TINTA2)
            ant = v
    ax.text(x0, 0.075,
            "retenção entre etapas de mesma unidade; fator de multiplicação nas "
            "transições partida → lance → rótulo",
            fontsize=FS_MIN, color=FRACO, style="italic", va="center")
    salvar(fig, "fig02_funil_construcao")


# ==========================================================================
# FIG 10 — identificacao do batedor
# ==========================================================================
def fig10(linhas):
    grupos = [("Brasileirão", AZUL, [("súmula completa", 99.4),
                                     ("súmula incompleta", 0.0)]),
              ("Copa do Brasil", LARANJA, [("sem disputa", 83.3),
                                           ("com disputa", 35.8)])]
    fig, ax = plt.subplots(figsize=(LARG_1COL, LARG_1COL * 0.62))
    ys, rot, vals, cors = [], [], [], []
    y = 0
    for nome, cor, itens in grupos:
        for r, v in itens:
            ys.append(y); rot.append(r); vals.append(v); cors.append(cor)
            y -= 1
        y -= 0.55
    ax.barh(ys, vals, 0.62, color=cors, edgecolor="none")
    for yi, v in zip(ys, vals):
        if v < 2:
            ax.plot([0.35, 0.35], [yi - 0.31, yi + 0.31], color=VERMELHO, lw=1.2)
            ax.text(1.8, yi, "0,0%  (nenhuma)", va="center",
                    fontsize=FS_MIN, color=VERMELHO)
        else:
            ax.text(v - 1.8, yi, f"{nb(v)}%", va="center", ha="right",
                    fontsize=FS_MIN, color="white")
    ax.set_yticks(ys); ax.set_yticklabels(rot, fontsize=FS_MIN)
    ax.set_xlim(0, 104); ax.set_xlabel("cobranças com batedor identificado (%)")
    ax.xaxis.grid(True, color=GRADE, lw=0.5); ax.set_axisbelow(True)
    ax.spines["left"].set_color(REGUA); ax.spines["bottom"].set_color(REGUA)
    for (nome, cor, itens), yy in zip(grupos, (ys[0], ys[2])):
        ax.text(-0.40, yy + 0.5, nome, transform=ax.get_yaxis_transform(),
                fontsize=FS_P, color=cor, ha="left", va="center",
                clip_on=False)
    salvar(fig, "fig10_identificacao_batedor")


def main():
    print("Carregando base…")
    linhas, fonte = carregar()
    print(f"  {len(linhas)} cobranças, "
          f"{len({r['video_file'] for r in linhas})} partidas\n")
    print("Figuras:")
    fig01(linhas); fig02(linhas); fig03(linhas); fig05(linhas)
    fig06(linhas); fig07(linhas, fonte); fig08(linhas); fig09(linhas)
    fig10(linhas)
    print("\nConcluído.")


if __name__ == "__main__":
    main()
