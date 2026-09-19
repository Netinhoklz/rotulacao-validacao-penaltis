import { useEffect } from "react";
import { api } from "../api/client";
import { useStore } from "../store/useStore";

export function VideoList() {
  const {
    videoList, currentVideoIdx, sourceDir,
    progressTotal, progressLabeled, progressByCompetition,
    competitionFilter,
    preExtractStatus, preExtractCurrentVideo,
    preExtractVideosDone, preExtractVideosTotal,
    preExtractFrameProgress, preExtractFrameTotal,
    setVideoList, setCurrentVideoIdx, setCurrentVideoPath,
    setVideoMeta, setCurrentFrame, resetMarks, resetPenaltyCount,
    setStatus, setShowConfig, setProgress, setPreExtractStatus,
    setCompetitionFilter,
  } = useStore();

  const load = async () => {
    const [res, prog] = await Promise.all([
      api.listVideos(),
      api.getProgress(),
    ]);
    setVideoList(res.videos ?? []);
    setProgress(prog.total, prog.labeled, prog.by_competition);
    if (res.error) setStatus(res.error);
    else setStatus(`${res.videos.length} vídeo(s) em ${res.directory}`);
  };

  useEffect(() => { load(); }, [sourceDir]);

  // Poll pre-extract status every 2s while running
  useEffect(() => {
    const poll = async () => {
      try {
        const s = await api.getPreExtractStatus();
        setPreExtractStatus(s);
      } catch { /* ignore */ }
    };
    poll();
    const id = setInterval(poll, 2000);
    return () => clearInterval(id);
  }, [setPreExtractStatus]);

  const selectVideo = async (idx: number) => {
    if (idx === currentVideoIdx) return;
    const video = videoList[idx];
    setCurrentVideoIdx(idx);
    resetMarks();
    resetPenaltyCount();
    setStatus(`Carregando ${video.name}…`);
    try {
      const meta = await api.loadVideo(video.path);
      setCurrentVideoPath(video.path);
      setVideoMeta(meta.total_frames ?? 0, meta.fps ?? 25);
      setCurrentFrame(0);
      setStatus(`Vídeo carregado: ${video.name}`);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      setStatus(`Erro: ${msg}`);
    }
  };

  // Mantém o índice da lista completa: currentVideoIdx e a seleção continuam
  // válidos mesmo com o filtro ativo.
  const visible = videoList
    .map((v, idx) => ({ v, idx }))
    .filter(({ v }) => !competitionFilter || v.competition === competitionFilter);

  const selected = progressByCompetition.find(
    (c) => c.competition === competitionFilter,
  );
  const labeledCount = selected ? selected.labeled : progressLabeled;
  const totalCount = selected
    ? selected.total
    : progressTotal || videoList.length;
  const pct = totalCount > 0 ? Math.round((labeledCount / totalCount) * 100) : 0;

  const extractPct = preExtractVideosTotal > 0
    ? Math.round((preExtractVideosDone / preExtractVideosTotal) * 100)
    : 0;
  const framePct = preExtractFrameTotal > 0
    ? Math.round((preExtractFrameProgress / preExtractFrameTotal) * 100)
    : 0;

  return (
    <aside className="video-list">
      <div className="video-list__header">
        <span>Vídeos</span>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <button
            className="btn-icon"
            title="Recarregar lista"
            onClick={load}
          >↻</button>
          <button
            className="btn-icon"
            title="Configurações"
            onClick={() => setShowConfig(true)}
          >⚙</button>
        </div>
      </div>

      {/* Filtro por competição — segmenta a lista e o progresso */}
      {progressByCompetition.length > 1 && (
        <div className="competition-filter">
          <select
            className="competition-filter__select"
            value={competitionFilter}
            onChange={(e) => setCompetitionFilter(e.target.value)}
            title="Filtrar vídeos por competição"
          >
            <option value="">
              Todas as competições ({progressTotal})
            </option>
            {progressByCompetition.map((c) => (
              <option key={c.competition} value={c.competition}>
                {c.competition_label} ({c.labeled}/{c.total})
              </option>
            ))}
          </select>
          {selected && (
            <div className="competition-filter__dest" title={selected.csv_path}>
              → {selected.csv_path}
            </div>
          )}
        </div>
      )}

      {/* Barra de progresso de rotulagem */}
      <div className="progress-bar-wrap">
        <div className="progress-bar-labels">
          <span className="progress-labeled">{labeledCount} rotulados</span>
          <span className="progress-unlabeled">{totalCount - labeledCount} pendentes</span>
        </div>
        <div className="progress-bar-track">
          <div
            className="progress-bar-fill"
            style={{ width: `${pct}%` }}
            title={`${pct}% concluído`}
          />
        </div>
        <div className="progress-pct">{pct}%</div>
      </div>

      {/* Status de pre-extracao */}
      {preExtractStatus === "running" && (
        <div className="preextract-status">
          <div className="preextract-status__title">
            Pré-extraindo frames ({preExtractVideosDone}/{preExtractVideosTotal})
          </div>
          <div className="progress-bar-track" style={{ height: 4 }}>
            <div
              className="progress-bar-fill progress-bar-fill--extract"
              style={{ width: `${extractPct}%` }}
            />
          </div>
          {preExtractCurrentVideo && (
            <div className="preextract-status__detail">
              {preExtractCurrentVideo.length > 30
                ? preExtractCurrentVideo.slice(0, 27) + "…"
                : preExtractCurrentVideo}
              <span style={{ marginLeft: 4, opacity: 0.7 }}>
                {framePct}%
              </span>
            </div>
          )}
        </div>
      )}
      {preExtractStatus === "done" && (
        <div className="preextract-status preextract-status--done">
          Frames pré-extraídos ({preExtractVideosTotal})
        </div>
      )}

      <ul className="video-list__items">
        {videoList.length === 0 && (
          <li className="video-list__empty">Nenhum vídeo encontrado.<br />Configure a pasta.</li>
        )}
        {videoList.length > 0 && visible.length === 0 && (
          <li className="video-list__empty">Nenhum vídeo nesta competição.</li>
        )}
        {visible.map(({ v, idx }) => (
          <li
            key={v.path}
            className={`video-list__item${idx === currentVideoIdx ? " video-list__item--active" : ""}`}
            onClick={() => selectVideo(idx)}
            title={v.path}
          >
            <span
              className={`video-status-dot ${v.labeled ? "video-status-dot--done" : "video-status-dot--pending"}`}
              title={v.labeled ? "Rotulado" : "Não rotulado"}
            />
            <span className="video-list__name">{v.name}</span>
            {v.competition && (
              <span
                className={`competition-badge competition-badge--${v.competition}`}
                title={`Será salvo em: ${v.competition_label}`}
              >
                {v.competition === "copa_do_brasil" ? "CDB"
                  : v.competition === "brasileirao" ? "BR"
                  : "?"}
              </span>
            )}
            {v.cached === true && (
              <span className="video-cache-badge" title="Frames pré-extraídos">⚡</span>
            )}
          </li>
        ))}
      </ul>

      <div className="video-list__footer">
        {visible.findIndex(({ idx }) => idx === currentVideoIdx) + 1} / {visible.length}
        {competitionFilter && <span className="video-list__footer-filter"> (filtrado)</span>}
      </div>
    </aside>
  );
}
