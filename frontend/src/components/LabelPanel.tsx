import { api } from "../api/client";
import { useStore } from "../store/useStore";
import { useEffect } from "react";

const GOAL_REGIONS = [
  { code: "gol_topo_esquerdo",   label: "↖ Topo Esq" },
  { code: "gol_topo_centro",     label: "↑ Topo Ctr" },
  { code: "gol_topo_direito",    label: "↗ Topo Dir" },
  { code: "gol_meio_esquerdo",   label: "← Meio Esq" },
  { code: "gol_meio_centro",     label: "● Centro"   },
  { code: "gol_meio_direito",    label: "→ Meio Dir" },
  { code: "gol_baixo_esquerdo",  label: "↙ Baixo Esq" },
  { code: "gol_baixo_centro",    label: "↓ Baixo Ctr" },
  { code: "gol_baixo_direito",   label: "↘ Baixo Dir" },
];

const OUT_REGIONS = [
  { code: "fora_esquerda", label: "⬅ Fora Esq" },
  { code: "fora_cima",     label: "⬆ Fora Cima" },
  { code: "fora_direita",  label: "➡ Fora Dir" },
];

const CAMERA_TYPES = ["visão do torcedor", "visão cobrador", "visão goleiro"];

const ALL_REGION_LABELS: Record<string, string> = Object.fromEntries(
  [...GOAL_REGIONS, ...OUT_REGIONS].map((r) => [r.code, r.label]),
);

export function LabelPanel() {
  const {
    marks, selectedRegion, isGoal, cameraType, observations,
    exporting, penaltyCount, currentVideoPath, videoList,
    setSelectedRegion, setIsGoal, setCameraType, setObservations,
    setExporting, incrementPenalty, setPenaltyCount, setStatus,
    resetMarks, resetLabelFields, setVideoList, setProgress,
  } = useStore();

  // Competição do vídeo atual: mostra o destino ANTES de salvar.
  const currentVideo = videoList.find((v) => v.path === currentVideoPath);

  // Atalho Enter → salvar
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "TEXTAREA") return;
      if (e.key === "Enter") handleSave();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [marks, selectedRegion, cameraType, observations, currentVideoPath, penaltyCount]);

  const handleSave = async () => {
    if (!currentVideoPath) { alert("Nenhum vídeo selecionado."); return; }
    if (marks.inicio_frame === null || marks.chute_frame === null) {
      alert("Marque o frame de INÍCIO (I) e o frame do CHUTE (F) antes de salvar.");
      return;
    }
    if (marks.inicio_frame >= marks.chute_frame) {
      alert("O frame de início deve ser anterior ao frame do chute.");
      return;
    }
    if (!selectedRegion) { alert("Selecione a região do chute."); return; }
    if (isGoal === null) { alert("Informe se foi GOL ou NÃO GOL antes de salvar."); return; }

    setExporting(true);
    setStatus("Exportando… aguarde.");

    try {
      incrementPenalty();
      const store = useStore.getState();
      const res = await api.exportPenalty({
        video_path:   currentVideoPath,
        penalty_id:   store.penaltyCount,
        inicio_frame: marks.inicio_frame,
        chute_frame:  marks.chute_frame,
        camera_type:  cameraType,
        region:       selectedRegion,
        is_goal:      isGoal!,
        observations,
      });
      resetLabelFields();
      resetMarks();

      // O backend pode ter deslocado o id para não sobrescrever um pênalti
      // já gravado; o contador local acompanha para os próximos saves.
      if (typeof res.penalty_id === "number" && res.penalty_id !== store.penaltyCount) {
        setPenaltyCount(res.penalty_id);
      }
      const avisos = [res.aviso, res.aviso_competicao].filter(Boolean).join(" ");
      setStatus(
        `Pênalti #${res.penalty_id} salvo em ${res.competition_label}.`
        + (avisos ? ` ⚠ ${avisos}` : "")
        + " Marque o próximo ou avance o vídeo (Ctrl+N).",
      );
      // Atualiza progresso e marca o vídeo como rotulado na lista
      api.getProgress().then((prog) => {
        setProgress(prog.total, prog.labeled, prog.by_competition);
        setVideoList(
          videoList.map((v) => {
            const match = prog.videos.find((p) => p.path === v.path);
            return match ? { ...v, labeled: match.labeled } : v;
          }),
        );
      }).catch(() => {/* silencioso */});
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`Erro ao exportar: ${msg}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <aside className="label-panel">
      <h2 className="label-panel__title">Rotulagem</h2>

      {/* Destino da segmentação — evita salvar na competição errada */}
      {currentVideo?.competition && (
        <div
          className={`label-dest label-dest--${currentVideo.competition}`}
          title="Competição detectada pela pasta de origem do vídeo"
        >
          Salvando em <strong>{currentVideo.competition_label}</strong>
          {currentVideo.competition === "outros" && (
            <span className="label-dest__warn">
              {" "}⚠ competição não identificada pela pasta de origem
            </span>
          )}
        </div>
      )}

      {/* Marcações */}
      <section className="label-section">
        <h3 className="label-section__title">Marcações</h3>
        <p className="mark-text mark-text--inicio">
          Início: {marks.inicio_frame !== null ? `frame ${marks.inicio_frame}` : "—"}
        </p>
        <p className="mark-text mark-text--chute">
          Chute: {marks.chute_frame !== null ? `frame ${marks.chute_frame}` : "—"}
        </p>
      </section>

      {/* Câmera */}
      <section className="label-section">
        <h3 className="label-section__title">Tipo de câmera</h3>
        {CAMERA_TYPES.map((ct) => (
          <label key={ct} className="radio-label">
            <input
              type="radio"
              name="camera"
              value={ct}
              checked={cameraType === ct}
              onChange={() => setCameraType(ct)}
            />
            {ct}
          </label>
        ))}
      </section>

      {/* Região */}
      <section className="label-section">
        <h3 className="label-section__title">Região do chute</h3>

        {/* Grade 3×3 gol */}
        <div className="region-grid">
          {GOAL_REGIONS.map((r) => (
            <button
              key={r.code}
              className={`btn-region btn-region--goal${selectedRegion === r.code ? " btn-region--selected" : ""}`}
              onClick={() => setSelectedRegion(r.code)}
            >
              {r.label}
            </button>
          ))}
        </div>

        <p className="region-divider">— Fora do gol —</p>

        {/* Fora */}
        <div className="region-row">
          {OUT_REGIONS.map((r) => (
            <button
              key={r.code}
              className={`btn-region btn-region--out${selectedRegion === r.code ? " btn-region--selected" : ""}`}
              onClick={() => setSelectedRegion(r.code)}
            >
              {r.label}
            </button>
          ))}
        </div>

        <p className="region-selected">
          {selectedRegion ? `Região: ${ALL_REGION_LABELS[selectedRegion]}` : "Nenhuma selecionada"}
        </p>
      </section>

      {/* Resultado */}
      <section className="label-section">
        <h3 className="label-section__title">Resultado</h3>
        <div className="resultado-row">
          <button
            className={`btn-resultado btn-resultado--gol${isGoal === true ? " btn-resultado--active" : ""}`}
            onClick={() => setIsGoal(true)}
          >
            ⚽ GOL
          </button>
          <button
            className={`btn-resultado btn-resultado--naogol${isGoal === false ? " btn-resultado--active" : ""}`}
            onClick={() => setIsGoal(false)}
          >
            ✖ NÃO GOL
          </button>
        </div>
        {isGoal !== null && (
          <p className={`resultado-status${isGoal ? " resultado-status--gol" : " resultado-status--naogol"}`}>
            {isGoal ? "⚽ GOL" : "✖ Não gol"}
          </p>
        )}
      </section>

      {/* Observações */}
      <section className="label-section">
        <h3 className="label-section__title">Observações (opcional)</h3>
        <textarea
          className="obs-textarea"
          rows={3}
          value={observations}
          onChange={(e) => setObservations(e.target.value)}
          placeholder="Notas livres…"
        />
      </section>

      {/* Botões */}
      <button
        className={`btn-save${exporting ? " btn-save--loading" : ""}`}
        onClick={handleSave}
        disabled={exporting}
      >
        {exporting ? "⏳ Salvando…" : "💾  Salvar Pênalti  (Enter)"}
      </button>

      <button
        className="btn-clear"
        onClick={() => { resetLabelFields(); resetMarks(); }}
        disabled={exporting}
      >
        ✖  Limpar campos
      </button>
    </aside>
  );
}
