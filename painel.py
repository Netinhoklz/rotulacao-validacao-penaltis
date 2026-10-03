"""
painel.py
=========
Painel do dataset de penaltis (Flask). SOMENTE LEITURA.

Uma pagina para enxergar a base inteira de uma vez: onde as bolas foram, quanto
vira gol, como isso muda por temporada e por competicao, e quanto do trabalho de
revisao e de identificacao de jogadores ja esta feito.

    python painel.py                 # http://127.0.0.1:5009

Nao escreve nada. Le:
    labels.csv de cada competicao          (ja com as correcoes de _revisao_manual/)
    _dataset_jogadores/penaltis_jogadores.csv
    elencos do projeto de scraping         (via elencos.py)

Rotulo x penalti
----------------
O labels.csv tem uma linha por ROTULO (angulo). O mesmo penalti costuma ser
rotulado 2-3 vezes: ao vivo e nos replays. Contar rotulo infla os numeros, entao
todas as distribuicoes aqui sao por PENALTI - os angulos sao agrupados pela mesma
regra do validador (mesmo video, chutes a menos de 20s um do outro) e cada
penalti entra uma vez so, com a regiao que a maioria dos angulos aponta.

Descartados na revisao ficam de fora; correcoes ja entram aplicadas.
"""

from __future__ import annotations

import json
import os
import re
import threading
import webbrowser
from collections import Counter, defaultdict
from typing import Any, Optional

from flask import Flask, Response, jsonify, request

import elencos as E
import navbar
import jogadores as J
import revisao as R
import validador as V

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_ctx: dict[str, Any] = {}

ORDEM_REGIOES = [
    "gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito",
    "gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito",
    "gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito",
    "fora_esquerda", "fora_cima", "fora_direita",
]


# ---------------------------------------------------------------------------
# Leitura e agrupamento
# ---------------------------------------------------------------------------
def _indice() -> dict[str, str]:
    """nome do video -> caminho. So le indices ja gravados pelas outras ferramentas."""
    if "indice" in _ctx:
        return _ctx["indice"]
    idx: dict[str, str] = {}
    for caminho in (R.Paths(_ctx["output_base"]).indice_vids,
                    J.PathsJog(_ctx["output_base"]).indice):
        if os.path.isfile(caminho):
            try:
                with open(caminho, "r", encoding="utf-8") as f:
                    idx.update(json.load(f))
            except (OSError, ValueError):
                continue
    if not idx and os.path.isdir(_ctx["source_dir"]):
        for raiz, _, arquivos in os.walk(_ctx["source_dir"]):
            for nome in arquivos:
                if R.eh_video(nome):
                    idx.setdefault(nome, os.path.join(raiz, nome))
    _ctx["indice"] = idx
    return idx


def _temporada(video_path: str, video_file: str) -> str:
    m = J.RX_TEMPORADA_PASTA.search(video_path or "")
    if m:
        return m.group(1)
    m = J.RX_TEMPORADA_NOME.search(J._sem_acento(video_file).upper())
    return m.group(1) if m else "sem ano"


def penaltis() -> tuple[list[dict], dict]:
    """
    Os penaltis reais da base, com os angulos ja agrupados.

    Devolve (lista, meta). Cada item tem regiao, gol, temporada, competicao,
    cameras e quantos angulos o sustentam.
    """
    base = R.carregar(R.Paths(_ctx["output_base"]))
    vivos = [r for r in base.registros if r["status"] != "descartado"]
    idx = _indice()

    por_comp: dict[str, list[dict]] = defaultdict(list)
    for r in vivos:
        por_comp[r["competicao"]].append(r["atual"])

    saida: list[dict] = []
    for comp, linhas in por_comp.items():
        for lance_id, rows in V.agrupar_lances(linhas, 20.0).items():
            video = rows[0].get("video_file", "")
            caminho = idx.get(video, "")
            regioes = [r.get("region", "") for r in rows if r.get("region")]
            gols = [V.is_goal(r) for r in rows]
            partida = J.partida_de(video, caminho)
            saida.append({
                "lance_id": f"{comp}|{lance_id}",
                # a regiao do penalti e a que a maioria dos angulos aponta;
                # divergencia entre angulos e assunto do revisor, nao do painel
                "region": Counter(regioes).most_common(1)[0][0] if regioes else "",
                "is_goal": sum(gols) > len(gols) / 2,
                "divergente": len(set(regioes)) > 1,
                "competicao": comp,
                "competicao_label": next((f["label"] for f in base.fontes
                                          if f["slug"] == comp), comp),
                "temporada": _temporada(caminho, video),
                "video_file": video,
                "mandante": partida["mandante"],
                "visitante": partida["visitante"],
                "n_angulos": len(rows),
                "cameras": sorted({r.get("camera_type", "") for r in rows} - {""}),
                "t_chute": min(V.num(r, "chute_time_s") for r in rows),
                "dur_s": round(sum(V.duracao_s(r) for r in rows) / len(rows), 2),
            })

    meta = {
        "n_rotulos": len(vivos),
        "n_descartados": sum(1 for r in base.registros if r["status"] == "descartado"),
        "revisao": R.resumo(base.registros)["status"],
        "competicoes": {f["slug"]: f["label"] for f in base.fontes},
    }
    saida.sort(key=lambda p: (p["temporada"], p["video_file"], p["t_chute"]))
    return saida, meta


def progresso_jogadores() -> dict:
    """Quanto da identificacao de cobrador/goleiro ja esta resolvido."""
    p = J.PathsJog(_ctx["output_base"])
    if not os.path.isfile(p.csv):
        return {"disponivel": False}
    _, linhas = V.ler_csv(p.csv)
    ident: dict[str, int] = defaultdict(int)
    for r in linhas:
        ident[E.veredito_rapido(r)["veredito"]] += 1
    return {
        "disponivel": True,
        "total": len(linhas),
        "status": dict(Counter(r.get("status_rotulo", "") for r in linhas)),
        "identificacao": dict(ident),
        "com_cobrador": sum(1 for r in linhas if r.get("nome_cobrador", "").strip()),
        "com_goleiro": sum(1 for r in linhas if r.get("nome_goleiro", "").strip()),
    }


# ---------------------------------------------------------------------------
# Agregacoes
# ---------------------------------------------------------------------------
def _conta(itens: list[dict], chave) -> dict[str, int]:
    c: dict[str, int] = defaultdict(int)
    for i in itens:
        c[chave(i)] += 1
    return dict(c)


def agregar(itens: list[dict]) -> dict:
    n = len(itens)
    gols = sum(1 for i in itens if i["is_goal"])

    por_regiao = []
    for reg in ORDEM_REGIOES:
        do_reg = [i for i in itens if i["region"] == reg]
        por_regiao.append({
            "region": reg,
            "label": R.REGION_LABELS.get(reg, reg),
            "n": len(do_reg),
            "gols": sum(1 for i in do_reg if i["is_goal"]),
            "pct": round(100 * len(do_reg) / n, 1) if n else 0.0,
            "conversao": round(100 * sum(1 for i in do_reg if i["is_goal"]) / len(do_reg), 1)
                         if do_reg else None,
        })

    por_temporada = []
    for t in sorted({i["temporada"] for i in itens}):
        do_t = [i for i in itens if i["temporada"] == t]
        por_temporada.append({
            "temporada": t, "n": len(do_t),
            "gols": sum(1 for i in do_t if i["is_goal"]),
            "conversao": round(100 * sum(1 for i in do_t if i["is_goal"]) / len(do_t), 1),
        })

    cameras: dict[str, int] = defaultdict(int)
    for i in itens:                       # camera e do angulo, nao do penalti
        for c in i["cameras"]:
            cameras[c] += 1

    # Um penalti tem no maximo 3-4 angulos (ao vivo + replays). Grupo com 5+ e
    # quase sempre DISPUTA DE PENALTIS: os chutes vem a poucos segundos um do
    # outro e a regra dos 20s junta todos num lance so. Nao da para separar sem
    # ver o video, entao o painel conta o caso em vez de esconder.
    suspeitos = [i for i in itens if i["n_angulos"] >= 5]
    faixas: dict[str, int] = defaultdict(int)
    for i in itens:
        faixas["5+" if i["n_angulos"] >= 5 else str(i["n_angulos"])] += 1

    return {
        "n_penaltis": n,
        "n_videos": len({i["video_file"] for i in itens}),
        "n_gols": gols,
        "conversao": round(100 * gols / n, 1) if n else 0.0,
        "n_divergentes": sum(1 for i in itens if i["divergente"]),
        "n_suspeitos": len(suspeitos),
        "rotulos_suspeitos": sum(i["n_angulos"] for i in suspeitos),
        "dur_media": round(sum(i["dur_s"] for i in itens) / n, 2) if n else 0.0,
        "por_regiao": por_regiao,
        "por_temporada": por_temporada,
        "por_camera": [{"camera": k, "n": v}
                       for k, v in sorted(cameras.items(), key=lambda kv: -kv[1])],
        "por_angulos": [{"angulos": k, "n": faixas[k]}
                        for k in ("1", "2", "3", "4", "5+") if faixas.get(k)],
        "por_competicao": [{"competicao": k, "n": v}
                           for k, v in sorted(_conta(itens, lambda i: i["competicao_label"]).items())],
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.get("/api/painel")
def api_painel():
    itens, meta = penaltis()
    comp = request.args.get("competicao", "")
    temp = request.args.get("temporada", "")
    sel = [i for i in itens
           if (not comp or i["competicao"] == comp)
           and (not temp or i["temporada"] == temp)]

    return jsonify({
        "agregado": agregar(sel),
        "filtrados": len(sel),
        "meta": {
            **meta,
            "n_penaltis_total": len(itens),
            "temporadas": sorted({i["temporada"] for i in itens}),
            "jogadores": progresso_jogadores(),
        },
    })


@app.get("/")
def index():
    return Response(PAGINA.replace("<!--NAV-->", navbar.barra("painel")),
                    mimetype="text/html")


# ---------------------------------------------------------------------------
# Pagina
# ---------------------------------------------------------------------------
PAGINA = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Painel do dataset de pênaltis</title>
<style>
  /* Superficies e tinta do painel; as cores de dado vem dos tokens --serie-*,
     validados contra a superficie #181825 (categorical, modo escuro). */
  :root{
    --plano:#11111b; --superficie:#181825; --superficie2:#1e1e2e; --borda:#313244;
    --tinta:#ffffff; --tinta2:#c3c2b7; --tinta3:#898781;
    --grade:#2a2a3c; --eixo:#3a3a4d;
    --serie-1:#3987e5;   /* categorico 1 - azul  */
    --serie-2:#d95926;   /* categorico 2 - laranja */
    --serie-3:#199e70;   /* categorico 3 - verde */
    --bom:#0ca30c; --atencao:#fab219; --grave:#ec835a; --critico:#d03b3b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--plano);color:var(--tinta2);
       font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
  header{padding:18px 22px 0;max-width:1280px;margin:0 auto}
  h1{font-size:19px;margin:0 0 3px;color:var(--tinta);letter-spacing:.2px}
  .sub{font-size:12px;color:var(--tinta3);margin-bottom:14px}
  main{max-width:1280px;margin:0 auto;padding:0 22px 40px}

  /* uma unica linha de filtros, acima de tudo que ela recorta */
  .filtros{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
           padding:10px 12px;background:var(--superficie);border:1px solid var(--borda);
           border-radius:10px;margin-bottom:14px;position:sticky;top:0;z-index:5}
  .filtros label{font-size:11px;color:var(--tinta3)}
  select{font:inherit;font-size:12px;color:var(--tinta2);background:var(--superficie2);
         border:1px solid var(--borda);border-radius:7px;padding:5px 8px}
  .recorte{margin-left:auto;font-size:12px;color:var(--tinta3)}

  .grade{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}
  .card{grid-column:span 12;background:var(--superficie);border:1px solid var(--borda);
        border-radius:10px;padding:14px 16px}
  .c4{grid-column:span 4}.c6{grid-column:span 6}.c8{grid-column:span 8}
  @media(max-width:960px){.c4,.c6,.c8{grid-column:span 12}}
  .card h2{font-size:13px;margin:0;color:var(--tinta);font-weight:600}
  .card .desc{font-size:11px;color:var(--tinta3);margin:2px 0 12px}
  .topo{display:flex;align-items:flex-start;gap:10px}
  .btab{margin-left:auto;font:inherit;font-size:11px;color:var(--tinta3);cursor:pointer;
        background:none;border:1px solid var(--borda);border-radius:6px;padding:2px 8px}
  .btab:hover{color:var(--tinta);border-color:var(--tinta3)}

  /* numeros grandes: figuras proporcionais, sem tabular-nums */
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:12px}
  .tile{background:var(--superficie);border:1px solid var(--borda);border-radius:10px;
        padding:12px 14px}
  .tile .v{font-size:30px;line-height:1.05;color:var(--tinta);font-weight:600}
  .tile .k{font-size:11px;color:var(--tinta3);margin-top:3px}
  .tile .n{font-size:11px;color:var(--tinta3)}

  svg{display:block;width:100%;overflow:visible}
  .rotulo{font-size:11px;fill:var(--tinta2)}
  .rotulo.mini{font-size:10px;fill:var(--tinta3)}
  .valor{font-size:11px;fill:var(--tinta2)}
  .eixo{stroke:var(--eixo);stroke-width:1}
  .gradelinha{stroke:var(--grade);stroke-width:1}
  .marca{cursor:default}
  .marca:hover{filter:brightness(1.18)}
  .celula:hover{stroke:var(--tinta);stroke-width:2}

  .legenda{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--tinta2);
           margin-top:10px}
  .legenda i{display:inline-block;width:11px;height:11px;border-radius:3px;
             margin-right:5px;vertical-align:-1px}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px;
        font-variant-numeric:tabular-nums}
  th{text-align:left;color:var(--tinta3);font-weight:600;font-size:11px;padding:3px 6px}
  td{padding:3px 6px;border-top:1px solid var(--borda)}
  td.n{text-align:right}
  .escondido{display:none}
  .barra-prog{height:12px;border-radius:99px;overflow:hidden;display:flex;gap:2px;
              background:var(--superficie2);margin-top:8px}
  .barra-prog span{display:block;height:100%}
  #dica{position:fixed;pointer-events:none;background:var(--superficie2);
        border:1px solid var(--borda);border-radius:8px;padding:7px 10px;font-size:12px;
        color:var(--tinta2);opacity:0;transition:opacity .1s;z-index:20;
        box-shadow:0 6px 22px rgba(0,0,0,.45);max-width:260px}
  #dica b{color:var(--tinta);font-size:15px}
  #dica .k{color:var(--tinta3);font-size:11px}
  .aviso{font-size:11px;color:var(--tinta3);margin-top:10px}
  a{color:var(--serie-1)}
</style>
</head>
<body>
<!--NAV-->
<header>
  <h1>Painel do dataset de pênaltis</h1>
  <div class="sub" id="sub">carregando...</div>
</header>
<main>
  <div class="filtros">
    <label for="f-comp">competição</label>
    <select id="f-comp"><option value="">todas</option></select>
    <label for="f-temp">temporada</label>
    <select id="f-temp"><option value="">todas</option></select>
    <span class="recorte" id="recorte"></span>
  </div>

  <div class="tiles" id="tiles"></div>

  <div class="grade" style="margin-top:12px">
    <section class="card c8">
      <div class="topo">
        <div><h2>Para onde as bolas foram</h2>
          <div class="desc">pênaltis por região, do ponto de vista de quem assiste atrás do
            batedor · as faixas externas são os chutes para fora · a cor acompanha a
            contagem, que também está escrita em cada célula</div></div>
        <button class="btab" onclick="tab('t-grade')">tabela</button>
      </div>
      <div id="c-grade"></div>
      <table id="t-grade" class="escondido"></table>
    </section>

    <section class="card c4">
      <div class="topo">
        <div><h2>Conversão por região</h2>
          <div class="desc">% dos chutes daquela região que viraram gol</div></div>
        <button class="btab" onclick="tab('t-conv')">tabela</button>
      </div>
      <div id="c-conv"></div>
      <table id="t-conv" class="escondido"></table>
    </section>

    <section class="card c6">
      <div class="topo">
        <div><h2>Pênaltis por temporada</h2>
          <div class="desc">quantos pênaltis a base tem em cada ano</div></div>
        <button class="btab" onclick="tab('t-temp')">tabela</button>
      </div>
      <div id="c-temp"></div>
      <table id="t-temp" class="escondido"></table>
    </section>

    <section class="card c6">
      <div class="topo">
        <div><h2>Conversão por temporada</h2>
          <div class="desc">% de gol em cada ano · escala própria, nunca no mesmo eixo do total</div></div>
        <button class="btab" onclick="tab('t-conv2')">tabela</button>
      </div>
      <div id="c-conv2"></div>
      <table id="t-conv2" class="escondido"></table>
    </section>

    <section class="card c6">
      <div class="topo">
        <div><h2>Ângulos por pênalti</h2>
          <div class="desc">quantas câmeras diferentes rotularam o mesmo chute</div></div>
        <button class="btab" onclick="tab('t-ang')">tabela</button>
      </div>
      <div id="c-ang"></div>
      <div class="aviso" id="a-ang"></div>
      <table id="t-ang" class="escondido"></table>
    </section>

    <section class="card c6">
      <div class="topo">
        <div><h2>Câmeras</h2>
          <div class="desc">em quantos pênaltis cada ponto de vista aparece</div></div>
        <button class="btab" onclick="tab('t-cam')">tabela</button>
      </div>
      <div id="c-cam"></div>
      <table id="t-cam" class="escondido"></table>
    </section>

    <section class="card">
      <div class="topo">
        <div><h2>Progresso do trabalho</h2>
          <div class="desc">estes números são sempre da base inteira — não seguem os filtros acima</div></div>
      </div>
      <div id="c-prog"></div>
    </section>
  </div>
</main>
<div id="dica"></div>

<script>
const $=s=>document.querySelector(s);
const NS='http://www.w3.org/2000/svg';
let D=null;

// ---------- utilitários ----------
const fmt=n=>Number(n).toLocaleString('pt-BR');
function el(tag,attrs,pai){
  const e=document.createElementNS(NS,tag);
  for(const k in attrs) e.setAttribute(k,attrs[k]);
  if(pai) pai.appendChild(e);
  return e;
}
function texto(pai,x,y,txt,cls,extra){
  const t=el('text',{x,y,class:cls||'rotulo',...(extra||{})},pai);
  t.textContent=txt;                       // nunca innerHTML: rótulo é dado
  return t;
}
// tooltip: valor em destaque, rótulo secundário
function dica(alvo,titulo,valor,detalhe){
  alvo.addEventListener('pointermove',e=>{
    const d=$('#dica');
    d.innerHTML='';
    const b=document.createElement('b'); b.textContent=valor;
    const k=document.createElement('div'); k.className='k'; k.textContent=titulo;
    d.appendChild(b); d.appendChild(k);
    if(detalhe){const x=document.createElement('div');x.className='k';x.textContent=detalhe;d.appendChild(x);}
    d.style.opacity=1;
    d.style.left=Math.min(e.clientX+14,innerWidth-270)+'px';
    d.style.top=(e.clientY+16)+'px';
  });
  alvo.addEventListener('pointerleave',()=>$('#dica').style.opacity=0);
  alvo.setAttribute('tabindex','0');
  alvo.addEventListener('focus',()=>{});   // foco de teclado mostra o mesmo pela tabela
}
function tab(id){
  const t=$('#'+id); t.classList.toggle('escondido');
}
function tabela(id,colunas,linhas){
  const t=$('#'+id); t.innerHTML='';
  const tr=document.createElement('tr');
  colunas.forEach(c=>{const th=document.createElement('th');th.textContent=c;tr.appendChild(th);});
  t.appendChild(tr);
  linhas.forEach(l=>{
    const tr=document.createElement('tr');
    l.forEach((v,i)=>{const td=document.createElement('td');
      if(i)td.className='n'; td.textContent=v; tr.appendChild(td);});
    t.appendChild(tr);
  });
}

// ---------- escala sequencial (uma cor só, clara = mais) ----------
// Passos do azul, do mais próximo da superfície ao mais claro. Sequencial nunca
// é arco-íris: uma matiz só, e o número está escrito na célula de qualquer forma.
const RAMPA=['#1b2438','#184f95','#256abf','#3987e5','#6da7ec','#9ec5f4'];
function mistura(a,b,t){
  const p=h=>[1,3,5].map(i=>parseInt(h.slice(i,i+2),16));
  const [r1,g1,b1]=p(a),[r2,g2,b2]=p(b);
  const c=(x,y)=>Math.round(x+(y-x)*t).toString(16).padStart(2,'0');
  return '#'+c(r1,r2)+c(g1,g2)+c(b1,b2);
}
function cor(v,max){
  if(!max||!v) return RAMPA[0];
  const t=Math.sqrt(v/max)*(RAMPA.length-1);   // raiz: a cauda longa não some
  const i=Math.min(Math.floor(t),RAMPA.length-2);
  return mistura(RAMPA[i],RAMPA[i+1],t-i);
}
const tintaSobre=(v,max)=>(max&&Math.sqrt(v/max)>0.62)?'#0b0b0b':'#cdd6f4';

// ---------- grade do gol ----------
function grade(reg){
  const box=$('#c-grade'); box.innerHTML='';
  const mapa={}; reg.forEach(r=>mapa[r.region]=r);
  const max=Math.max(...reg.map(r=>r.n),1);
  const svg=el('svg',{viewBox:'0 0 460 300','aria-label':'mapa da grade do gol'},box);

  const G={x:110,y:78,w:250,h:170};          // gol
  const cel=(c,l)=>({x:G.x+c*(G.w/3), y:G.y+l*(G.h/3), w:G.w/3, h:G.h/3});
  const fora={
    fora_cima:{x:12,y:14,w:436,h:46},   // folga para o rótulo GOL não encostar
    fora_esquerda:{x:12,y:78,w:86,h:170},
    fora_direita:{x:372,y:78,w:76,h:170},
  };
  const desenhar=(r,g)=>{
    if(!r) return;
    // 2px de folga na cor da superfície separam as células: sem contorno
    const c=el('rect',{x:g.x+1,y:g.y+1,width:g.w-2,height:g.h-2,rx:4,
      fill:cor(r.n,max),class:'celula'},svg);
    const cx=g.x+g.w/2, cy=g.y+g.h/2;
    // fill vai no style, não como atributo: a classe CSS venceria o atributo de
    // apresentação e a tinta clara sumiria em cima das células claras
    const ink=tintaSobre(r.n,max);
    texto(svg,cx,cy+2,fmt(r.n),'valor',
      {'text-anchor':'middle','font-size':'18','font-weight':'700',style:`fill:${ink}`});
    texto(svg,cx,cy+18,r.pct+'%','rotulo mini',
      {'text-anchor':'middle',style:`fill:${ink};opacity:.8`});
    dica(c,r.label,fmt(r.n)+' pênaltis',
      `${r.pct}% da base · ${r.conversao===null?'sem dados':r.conversao+'% viram gol'}`);
  };
  for(let l=0;l<3;l++)for(let c=0;c<3;c++)
    desenhar(mapa[['gol_topo_','gol_meio_','gol_baixo_'][l]+
      ['esquerdo','centro','direito'][c]],cel(c,l));
  for(const k in fora) desenhar(mapa[k],fora[k]);

  // trave: o retângulo do gol, hairline recessivo
  el('rect',{x:G.x,y:G.y,width:G.w,height:G.h,fill:'none',stroke:'#3a3a4d',
             'stroke-width':1.5,rx:2},svg);
  texto(svg,G.x+G.w/2,G.y-8,'GOL','rotulo mini',{'text-anchor':'middle'});

  // legenda da escala (sequencial exige legenda de escala), à esquerda;
  // a nota de leitura vai à direita, na mesma linha, sem colidir
  const lx=12,ly=264,lw=140;
  const grad=el('defs',{},svg);
  const lg=el('linearGradient',{id:'esc'},grad);
  RAMPA.forEach((c,i)=>el('stop',{offset:(100*i/(RAMPA.length-1))+'%','stop-color':c},lg));
  el('rect',{x:lx,y:ly,width:lw,height:9,rx:4,fill:'url(#esc)'},svg);
  texto(svg,lx,ly+21,'0 pênaltis','rotulo mini');
  texto(svg,lx+lw,ly+21,fmt(max),'rotulo mini',{'text-anchor':'end'});
  texto(svg,448,ly+8,'% = fatia da base','rotulo mini',{'text-anchor':'end'});

  tabela('t-grade',['região','pênaltis','% da base','gols','% gol'],
    reg.map(r=>[r.label,fmt(r.n),r.pct+'%',fmt(r.gols),r.conversao===null?'—':r.conversao+'%']));
}

// ---------- barras horizontais (uma série, uma cor) ----------
function barras(alvo,dados,{rotulo,valor,sufixo='',cor:c='var(--serie-1)',max=null,
                            unidade='',larguraRotulo=104}){
  const box=$(alvo); box.innerHTML='';
  const alt=26, pad=6;
  const h=dados.length*alt+22;
  const svg=el('svg',{viewBox:`0 0 460 ${h}`},box);
  const x0=larguraRotulo, x1=406;
  const m=max!==null?max:Math.max(...dados.map(valor),1);
  dados.forEach((d,i)=>{
    const y=i*alt+pad, v=valor(d);
    const w=Math.max(m?(x1-x0)*v/m:0,0);
    texto(svg,x0-8,y+13,rotulo(d),'rotulo',{'text-anchor':'end'});
    // trilho discreto, para a barra curta ainda ter contexto
    el('rect',{x:x0,y:y+3,width:x1-x0,height:14,rx:4,fill:'#1e1e2e'},svg);
    const b=el('rect',{x:x0,y:y+3,width:w,height:14,rx:4,fill:c,class:'marca'},svg);
    if(!w)b.setAttribute('width',0);
    const rot=fmt(v)+sufixo;
    // valor na ponta; se não couber fora, some para o tooltip/tabela
    if(x0+w+8+rot.length*6.4<456)
      texto(svg,x0+w+8,y+14,rot,'valor');
    dica(b,rotulo(d),rot+(unidade?' '+unidade:''),d.detalhe||'');
  });
  el('line',{x1:x0,y1:pad,x2:x0,y2:h-16,class:'eixo'},svg);
}

// ---------- progresso ----------
function progresso(meta){
  const box=$('#c-prog'); box.innerHTML='';
  const j=meta.jogadores||{};
  const cartao=(titulo,partes,total,rodape)=>{
    const d=document.createElement('div');
    d.style.cssText='flex:1 1 300px';
    const h=document.createElement('div');
    h.style.cssText='font-size:12px;color:var(--tinta)';
    h.textContent=titulo; d.appendChild(h);
    const b=document.createElement('div'); b.className='barra-prog';
    partes.forEach(p=>{
      if(!p.n)return;
      const s=document.createElement('span');
      s.style.width=(100*p.n/Math.max(total,1))+'%'; s.style.background=p.cor;
      s.title=`${p.rot}: ${p.n}`;
      b.appendChild(s);
    });
    d.appendChild(b);
    const l=document.createElement('div'); l.className='legenda';
    partes.forEach(p=>{
      const s=document.createElement('span');
      const i=document.createElement('i'); i.style.background=p.cor;
      s.appendChild(i);
      s.appendChild(document.createTextNode(`${p.rot} ${fmt(p.n)}`));
      l.appendChild(s);
    });
    d.appendChild(l);
    if(rodape){const r=document.createElement('div');r.className='aviso';r.textContent=rodape;d.appendChild(r);}
    return d;
  };
  const linha=document.createElement('div');
  linha.style.cssText='display:flex;gap:22px;flex-wrap:wrap';

  const rev=meta.revisao||{};
  linha.appendChild(cartao('Revisão dos rótulos (região do chute)',[
    {rot:'conferido',n:rev.ok||0,cor:'var(--bom)'},
    {rot:'corrigido',n:rev.corrigido||0,cor:'var(--atencao)'},
    {rot:'descartado',n:rev.descartado||0,cor:'var(--critico)'},
    {rot:'a conferir',n:rev.pendente||0,cor:'#313244'},
  ],meta.n_rotulos+(rev.descartado||0),'rótulos · a tela é a Validação, no topo'));

  if(j.disponivel){
    const id=j.identificacao||{};
    linha.appendChild(cartao('Identificação do cobrador (elencos)',[
      {rot:'resolvido pelo elenco',n:id.resolvido||0,cor:'var(--bom)'},
      {rot:'dúvida: ler o número',n:id.duvida||0,cor:'var(--atencao)'},
      {rot:'sem dados',n:id.sem_dados||0,cor:'#313244'},
    ],j.total,`${fmt(j.com_cobrador)} de ${fmt(j.total)} pênaltis já com cobrador gravado`));
    const st=j.status||{};
    linha.appendChild(cartao('Rotulagem cobrador × goleiro',[
      {rot:'completo',n:st.completo||0,cor:'var(--bom)'},
      {rot:'parcial',n:st.parcial||0,cor:'var(--atencao)'},
      {rot:'pendente',n:st.pendente||0,cor:'#313244'},
    ],j.total,'pênaltis · a tela é a Jogadores, no topo'));
  }
  box.appendChild(linha);
}

// ---------- carga ----------
async function carregar(){
  const q=new URLSearchParams({competicao:$('#f-comp').value,temporada:$('#f-temp').value});
  const r=await fetch('/api/painel?'+q);
  D=await r.json();
  const a=D.agregado, m=D.meta;

  if(!$('#f-comp').dataset.ok){
    for(const [slug,rot] of Object.entries(m.competicoes))
      $('#f-comp').insertAdjacentHTML('beforeend',`<option value="${slug}"></option>`);
    [...$('#f-comp').options].forEach((o,i)=>{ if(i)o.textContent=m.competicoes[o.value]; });
    m.temporadas.forEach(t=>{
      const o=document.createElement('option'); o.value=t; o.textContent=t;
      $('#f-temp').appendChild(o);
    });
    $('#f-comp').dataset.ok='1';
  }

  $('#sub').textContent=
    `${fmt(m.n_rotulos)} rótulos agrupados em ${fmt(m.n_penaltis_total)} pênaltis reais`+
    (m.n_descartados?` · ${m.n_descartados} descartado(s) na revisão`:'')+
    ' · as distribuições contam pênaltis, não rótulos';
  $('#recorte').textContent=`${fmt(a.n_penaltis)} pênaltis no recorte`;

  const tile=(v,k,n)=>`<div class="tile"><div class="v">${v}</div>
     <div class="k">${k}</div>${n?`<div class="n">${n}</div>`:''}</div>`;
  $('#tiles').innerHTML=
    tile(fmt(a.n_penaltis),'pênaltis',`em ${fmt(a.n_videos)} vídeos`)+
    tile(a.conversao+'%','viraram gol',`${fmt(a.n_gols)} de ${fmt(a.n_penaltis)}`)+
    tile(fmt(a.por_regiao.filter(r=>r.region.startsWith('fora')).reduce((s,r)=>s+r.n,0)),
         'foram para fora','sem chance de defesa')+
    tile(a.dur_media+'s','duração média','do início ao chute')+
    tile(fmt(a.n_divergentes),'com ângulos divergentes','regiões diferentes no mesmo lance')+
    tile(fmt(a.n_suspeitos),'possíveis disputas','grupos com 5+ ângulos');

  grade(a.por_regiao);

  const conv=a.por_regiao.filter(r=>r.conversao!==null)
                         .sort((x,y)=>y.conversao-x.conversao)
                         .map(r=>({...r,detalhe:`${r.gols} gols em ${r.n} chutes`}));
  barras('#c-conv',conv,{rotulo:r=>r.label.replace('Gol - ','').replace('Fora - ','fora '),
    valor:r=>r.conversao,sufixo:'%',max:100,unidade:'de gol',larguraRotulo:108});
  tabela('t-conv',['região','chutes','gols','% gol'],
    conv.map(r=>[r.label,fmt(r.n),fmt(r.gols),r.conversao+'%']));

  barras('#c-temp',a.por_temporada,{rotulo:t=>t.temporada,valor:t=>t.n,
    unidade:'pênaltis',larguraRotulo:64});
  tabela('t-temp',['temporada','pênaltis','gols'],
    a.por_temporada.map(t=>[t.temporada,fmt(t.n),fmt(t.gols)]));

  barras('#c-conv2',a.por_temporada.map(t=>({...t,
      detalhe:`${t.gols} gols em ${t.n} pênaltis`})),
    {rotulo:t=>t.temporada,valor:t=>t.conversao,sufixo:'%',max:100,
     cor:'var(--serie-3)',unidade:'de gol',larguraRotulo:64});
  tabela('t-conv2',['temporada','pênaltis','% gol'],
    a.por_temporada.map(t=>[t.temporada,fmt(t.n),t.conversao+'%']));

  barras('#c-ang',a.por_angulos,{rotulo:x=>x.angulos+(x.angulos==='1'?' ângulo':' ângulos'),
    valor:x=>x.n,unidade:'pênaltis',larguraRotulo:84});
  tabela('t-ang',['ângulos','pênaltis'],a.por_angulos.map(x=>[x.angulos,fmt(x.n)]));
  // um chute tem 3-4 ângulos no máximo: 5+ é disputa de pênaltis colada no tempo
  $('#a-ang').textContent = a.n_suspeitos
    ? `${a.n_suspeitos} grupo(s) com 5+ ângulos (${a.rotulos_suspeitos} rótulos) são quase `
      + `certamente disputas de pênaltis: os chutes vêm a poucos segundos um do outro e a `
      + `regra dos 20 s junta todos num lance só. Nesses casos o total de pênaltis está `
      + `subestimado — separá-los exige ver o vídeo.`
    : '';

  barras('#c-cam',a.por_camera,{rotulo:c=>c.camera,valor:c=>c.n,
    cor:'var(--serie-2)',unidade:'pênaltis',larguraRotulo:118});
  tabela('t-cam',['câmera','pênaltis'],a.por_camera.map(c=>[c.camera,fmt(c.n)]));

  progresso(m);
}
['f-comp','f-temp'].forEach(i=>$('#'+i).onchange=carregar);
carregar();
</script>
</body></html>
"""


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------
def preparar(output_base: str, source_dir: str) -> None:
    _ctx["output_base"] = output_base
    _ctx["source_dir"] = source_dir


def servir(output_base: str, source_dir: str, host: str = "127.0.0.1",
           porta: int = 5009, abrir_navegador: bool = True) -> int:
    preparar(output_base, source_dir)

    itens, meta = penaltis()
    url = f"http://{host}:{porta}"
    print(f"\n  Painel do dataset  ->  {url}")
    print(f"  {meta['n_rotulos']} rotulos -> {len(itens)} penaltis reais")
    print(f"  somente leitura: nada e gravado\n")

    if abrir_navegador:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=host, port=porta, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Painel do dataset de penaltis (somente leitura)")
    ap.add_argument("--output-base", default=R.DEFAULT_OUTPUT_BASE)
    ap.add_argument("--source-dir", default=R.DEFAULT_SOURCE_DIR)
    ap.add_argument("--porta", type=int, default=5009)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    raise SystemExit(servir(a.output_base, a.source_dir, a.host, a.porta,
                            not a.sem_navegador))
