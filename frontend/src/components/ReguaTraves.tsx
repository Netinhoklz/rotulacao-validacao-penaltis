import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { useStore } from "../store/useStore";

/*
 * Régua das traves (E marca · T tira os nomes · R recolhe)
 *
 * Mesma ferramenta da tela de Validação, agora na rotulação: quatro cliques
 * nos cantos do gol e o quadro se divide nas 9 regiões por cima do frame, para
 * decidir onde a bola entrou antes de clicar na grade do painel.
 *
 * É só uma sobreposição de conferência. Não entra no CSV, no JSON nem no clip
 * exportado. Os 4 pontos ficam no localStorage do navegador, por vídeo, para a
 * régua continuar onde foi posta enquanto se anda pelos frames.
 */

type Ponto = { x: number; y: number };          // fração da imagem, 0..1
type Modo = "off" | "calibrando" | "on";
type Caixa = { left: number; top: number; width: number; height: number };

const PEDIDOS = [
  "trave ESQUERDA em CIMA",
  "trave ESQUERDA em BAIXO",
  "trave DIREITA em BAIXO",
  "trave DIREITA em CIMA",
];

// Mesmos códigos do LabelPanel: a célula da região escolhida acende.
const CELS = [
  ["gol_topo_esquerdo", "gol_topo_centro", "gol_topo_direito"],
  ["gol_meio_esquerdo", "gol_meio_centro", "gol_meio_direito"],
  ["gol_baixo_esquerdo", "gol_baixo_centro", "gol_baixo_direito"],
];
const CURTO: Record<string, string> = { esquerdo: "esq", direito: "dir", centro: "centro" };

const chave = (video: string) => `regua|${video}`;

function lerPontos(video: string): Ponto[] | null {
  try {
    const p = JSON.parse(localStorage.getItem(chave(video)) || "null");
    return Array.isArray(p) && p.length === 4 ? p : null;
  } catch {
    return null;
  }
}

function gravarPontos(video: string, pontos: Ponto[]) {
  try {
    localStorage.setItem(chave(video), JSON.stringify(pontos));
  } catch {
    /* navegador sem storage: a régua só não sobrevive ao recarregar */
  }
}

function lerNomes(): boolean {
  try {
    return localStorage.getItem("regua:nomes") !== "0";
  } catch {
    return true;
  }
}

const limitar = (v: number) => Math.min(Math.max(v, 0), 1);

// Bilinear nos cantos [TL, BL, BR, TR]: dividir cada aresta em três e ligar
// dá a divisão "igual" que o olho espera, já acompanhando a perspectiva.
function bilinear(p: Ponto[], u: number, v: number): Ponto {
  const [tl, bl, br, tr] = p;
  const cima = { x: tl.x + (tr.x - tl.x) * u, y: tl.y + (tr.y - tl.y) * u };
  const baixo = { x: bl.x + (br.x - bl.x) * u, y: bl.y + (br.y - bl.y) * u };
  return { x: cima.x + (baixo.x - cima.x) * v, y: cima.y + (baixo.y - cima.y) * v };
}

interface Props {
  imgRef: RefObject<HTMLImageElement | null>;
  canvasRef: RefObject<HTMLDivElement | null>;
}

export function ReguaTraves({ imgRef, canvasRef }: Props) {
  const { currentVideoPath, selectedRegion, setStatus } = useStore();
  const [modo, setModo] = useState<Modo>("off");
  const [pontos, setPontos] = useState<Ponto[]>([]);
  const [nomes, setNomes] = useState<boolean>(lerNomes);
  const [caixa, setCaixa] = useState<Caixa | null>(null);

  const svgRef = useRef<SVGSVGElement>(null);
  const arrastando = useRef(-1);
  // espelhos para os listeners de janela, que não são recriados a cada render
  const modoRef = useRef(modo);
  const pontosRef = useRef(pontos);
  useEffect(() => { modoRef.current = modo; }, [modo]);
  useEffect(() => { pontosRef.current = pontos; }, [pontos]);

  // ---- onde a imagem está dentro do palco (object-fit: contain) ----
  const medir = useCallback(() => {
    const img = imgRef.current;
    const palco = canvasRef.current;
    if (!img || !palco) return;
    const ri = img.getBoundingClientRect();
    const rp = palco.getBoundingClientRect();
    if (!ri.width || !ri.height) return;
    const nova = { left: ri.left - rp.left, top: ri.top - rp.top, width: ri.width, height: ri.height };
    // cada frame novo dispara "load"; sem esta comparação a régua
    // re-renderizaria a 25 quadros por segundo durante o play à toa
    setCaixa((antes) =>
      antes && antes.left === nova.left && antes.top === nova.top &&
      antes.width === nova.width && antes.height === nova.height ? antes : nova);
  }, [imgRef, canvasRef]);

  useLayoutEffect(() => {
    const img = imgRef.current;
    const palco = canvasRef.current;
    if (!img || !palco) return;
    const ro = new ResizeObserver(medir);
    ro.observe(img);
    ro.observe(palco);
    img.addEventListener("load", medir);
    medir();
    return () => {
      ro.disconnect();
      img.removeEventListener("load", medir);
    };
  }, [currentVideoPath, imgRef, canvasRef, medir]);

  // ---- outro vídeo = outro enquadramento ----
  useEffect(() => {
    if (modoRef.current === "off") return;
    const salvos = lerPontos(currentVideoPath);
    if (salvos) {
      setPontos(salvos);
      setModo("on");
    } else {
      setPontos([]);
      setModo("off");
    }
  }, [currentVideoPath]);

  // ---- teclas ----
  const pedir = useCallback((n: number) => {
    setStatus(`Régua: clique na ${PEDIDOS[n]} (${n + 1}/4) · R cancela`);
  }, [setStatus]);

  const alternar = useCallback(() => {
    if (!currentVideoPath) return;
    if (modoRef.current === "off") {
      const salvos = lerPontos(currentVideoPath);
      if (salvos) {
        setPontos(salvos);
        setModo("on");
        setStatus("Régua das traves ativa · E remarca · T tira os nomes · R recolhe");
        return;
      }
    }
    // desligada sem marcação salva, ou já visível: E (re)começa os 4 cliques
    setPontos([]);
    setModo("calibrando");
    pedir(0);
  }, [currentVideoPath, pedir, setStatus]);

  const recolher = useCallback(() => {
    if (modoRef.current === "off") return;
    arrastando.current = -1;
    setModo("off");
    setStatus("Régua recolhida · E mostra de novo");
  }, [setStatus]);

  const trocarNomes = useCallback(() => {
    if (modoRef.current !== "on") return;      // na marcação os pedidos precisam aparecer
    setNomes((n) => {
      const novo = !n;
      try { localStorage.setItem("regua:nomes", novo ? "1" : "0"); } catch { /* ok */ }
      return novo;
    });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if (k === "e") { e.preventDefault(); alternar(); }
      else if (k === "r") { e.preventDefault(); recolher(); }
      else if (k === "t") { e.preventDefault(); trocarNomes(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [alternar, recolher, trocarNomes]);

  // avisa na barra de status quando os nomes somem/voltam
  const primeiraVez = useRef(true);
  useEffect(() => {
    if (primeiraVez.current) { primeiraVez.current = false; return; }
    if (modoRef.current === "on") {
      setStatus(nomes ? "Régua: nomes das regiões à mostra"
                      : "Régua: só os traços · T traz os nomes de volta");
    }
  }, [nomes, setStatus]);

  // ---- arrastar alça: eventos na janela para o ponteiro poder sair do palco ----
  useEffect(() => {
    const mover = (e: PointerEvent) => {
      const i = arrastando.current;
      const svg = svgRef.current;
      if (i < 0 || !svg) return;
      const r = svg.getBoundingClientRect();
      const ponto = { x: limitar((e.clientX - r.left) / r.width), y: limitar((e.clientY - r.top) / r.height) };
      // o espelho é atualizado AQUI, não num efeito: o pointerup pode chegar
      // antes do React pintar, e aí gravaria a posição de um movimento atrás
      const novos = pontosRef.current.map((q, k) => (k === i ? ponto : q));
      pontosRef.current = novos;
      setPontos(novos);
    };
    const soltar = () => {
      if (arrastando.current < 0) return;
      arrastando.current = -1;
      gravarPontos(currentVideoPath, pontosRef.current);
    };
    window.addEventListener("pointermove", mover);
    window.addEventListener("pointerup", soltar);
    return () => {
      window.removeEventListener("pointermove", mover);
      window.removeEventListener("pointerup", soltar);
    };
  }, [currentVideoPath]);

  // ---- cliques da marcação ----
  const clicar = (e: React.MouseEvent<SVGSVGElement>) => {
    if (modo !== "calibrando") return;
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    const novos = [...pontos, { x: limitar((e.clientX - r.left) / r.width), y: limitar((e.clientY - r.top) / r.height) }];
    setPontos(novos);
    if (novos.length === 4) {
      setModo("on");
      gravarPontos(currentVideoPath, novos);
      setStatus("Régua marcada · arraste as alças para ajustar · T tira os nomes · R recolhe");
    } else {
      pedir(novos.length);
    }
  };

  if (modo === "off" || !caixa || !currentVideoPath) return null;

  const px = (p: Ponto) => ({ x: p.x * caixa.width, y: p.y * caixa.height });
  const pt = (p: Ponto) => `${p.x},${p.y}`;
  const noQuadro = (u: number, v: number) => px(bilinear(pontos, u, v));
  const pronto = modo === "on" && pontos.length === 4;

  // célula da região escolhida no painel; laço comum (e não forEach) para o
  // TypeScript enxergar a atribuição
  let destaque: string | null = null;
  if (pronto && selectedRegion) {
    for (let l = 0; l < 3; l++) {
      for (let c = 0; c < 3; c++) {
        if (CELS[l][c] !== selectedRegion) continue;
        destaque = [[c / 3, l / 3], [(c + 1) / 3, l / 3], [(c + 1) / 3, (l + 1) / 3], [c / 3, (l + 1) / 3]]
          .map(([u, v]) => pt(noQuadro(u, v))).join(" ");
      }
    }
  }

  const cantos = pontos.map(px);

  return (
    <>
      <svg
        ref={svgRef}
        className={`regua${modo === "calibrando" ? " regua--calibrando" : ""}`}
        style={{ left: caixa.left, top: caixa.top, width: caixa.width, height: caixa.height }}
        viewBox={`0 0 ${caixa.width} ${caixa.height}`}
        onClick={clicar}
      >
        {modo === "calibrando" && (
          <>
            {cantos.length > 1 && <polyline className="regua__div" fill="none" points={cantos.map(pt).join(" ")} />}
            {cantos.map((q, i) => <circle key={i} className="regua__pino" cx={q.x} cy={q.y} r={5} />)}
          </>
        )}

        {pronto && (
          <>
            {destaque && <polygon className="regua__marcada" points={destaque} />}

            {/* contorno do gol, na ordem TL → TR → BR → BL */}
            <path
              className="regua__quad"
              d={`M${pt(cantos[0])} L${pt(cantos[3])} L${pt(cantos[2])} L${pt(cantos[1])} Z`}
            />
            {[1 / 3, 2 / 3].map((t) => {
              const a = noQuadro(t, 0), b = noQuadro(t, 1);
              const c = noQuadro(0, t), d = noQuadro(1, t);
              return (
                <g key={t}>
                  <line className="regua__div" x1={a.x} y1={a.y} x2={b.x} y2={b.y} />
                  <line className="regua__div" x1={c.x} y1={c.y} x2={d.x} y2={d.y} />
                </g>
              );
            })}

            {nomes && CELS.flatMap((linha, l) => linha.map((cod, c) => {
              const q = noQuadro((c + 0.5) / 3, (l + 0.5) / 3);
              const [, altura, lado] = cod.split("_");
              return <text key={cod} className="regua__nome" x={q.x} y={q.y + 3}>{`${altura} ${CURTO[lado] ?? lado}`}</text>;
            }))}

            {nomes && (() => {
              const esq = noQuadro(0, 0.5), dir = noQuadro(1, 0.5), cima = noQuadro(0.5, 0);
              return (
                <>
                  <text className="regua__nome regua__nome--fora" textAnchor="end"
                        x={Math.max(esq.x - 8, 26)} y={esq.y}>fora esq</text>
                  <text className="regua__nome regua__nome--fora" textAnchor="start"
                        x={Math.min(dir.x + 8, caixa.width - 26)} y={dir.y}>fora dir</text>
                  <text className="regua__nome regua__nome--fora"
                        x={cima.x} y={Math.max(cima.y - 9, 12)}>fora cima</text>
                </>
              );
            })()}

            {cantos.map((q, i) => (
              <circle
                key={i}
                className="regua__alca"
                cx={q.x}
                cy={q.y}
                r={6}
                onPointerDown={(e) => { e.preventDefault(); e.stopPropagation(); arrastando.current = i; }}
              />
            ))}
          </>
        )}
      </svg>

      {(modo === "calibrando" || nomes) && (
        <div className="regua-info" style={{ left: caixa.left + 8, top: caixa.top + 8 }}>
          {modo === "calibrando" ? (
            <>clique na <b>{PEDIDOS[pontos.length]}</b> <span>({pontos.length}/4 · R cancela)</span></>
          ) : (
            <>régua das traves <span>· arraste as alças · E remarca · T só os traços · R recolhe · não é gravada</span></>
          )}
        </div>
      )}
    </>
  );
}
