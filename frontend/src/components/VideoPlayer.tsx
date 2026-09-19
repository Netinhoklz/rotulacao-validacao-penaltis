import { useEffect, useRef, useCallback, useState } from "react";
import { api } from "../api/client";
import { useStore } from "../store/useStore";
import { ReguaTraves } from "./ReguaTraves";

export function VideoPlayer() {
  const {
    currentVideoPath, totalFrames, fps,
    currentFrame, setCurrentFrame,
    marks, setMarkInicio, setMarkChute, setStatus,
  } = useStore();

  const imgRef = useRef<HTMLImageElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);   // a régua mede a imagem contra ele
  const playingRef = useRef(false);
  const frameTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [playing, setPlaying] = useState(false);
  const [loading, setLoading] = useState(false);

  // Exibe o frame atual
  const showFrame = useCallback(
    (idx: number) => {
      if (!currentVideoPath) return;
      const clamped = Math.max(0, Math.min(idx, totalFrames - 1));
      if (imgRef.current) {
        setLoading(true);
        imgRef.current.src = api.frameUrl(clamped, currentVideoPath);
      }
      setCurrentFrame(clamped);
    },
    [currentVideoPath, totalFrames, setCurrentFrame],
  );

  // Quando o vídeo muda, mostra frame 0
  useEffect(() => {
    stopPlay();
    if (currentVideoPath) showFrame(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentVideoPath]);

  // Play / pause
  const stopPlay = useCallback(() => {
    playingRef.current = false;
    if (frameTimerRef.current) {
      clearTimeout(frameTimerRef.current);
      frameTimerRef.current = null;
    }
    setPlaying(false);
  }, []);

  // Avança um frame e agenda o próximo após o intervalo correto
  const scheduleNextFrame = useCallback(() => {
    if (!playingRef.current) return;
    const interval = Math.round(1000 / fps);
    frameTimerRef.current = setTimeout(() => {
      if (!playingRef.current) return;
      const next = useStore.getState().currentFrame + 1;
      if (next >= totalFrames) {
        playingRef.current = false;
        setPlaying(false);
        return;
      }
      setCurrentFrame(next);
      if (imgRef.current) {
        setLoading(true);
        imgRef.current.src = api.frameUrl(next, useStore.getState().currentVideoPath);
        // onLoad no <img> chamará scheduleNextFrame
      }
    }, interval);
  }, [fps, totalFrames, setCurrentFrame]);

  const startPlay = useCallback(() => {
    if (!currentVideoPath) return;
    playingRef.current = true;
    setPlaying(true);
    scheduleNextFrame();
  }, [currentVideoPath, scheduleNextFrame]);

  const togglePlay = useCallback(() => {
    if (playing) stopPlay();
    else startPlay();
  }, [playing, stopPlay, startPlay]);

  // Navegação
  const nextFrame = useCallback(() => { stopPlay(); showFrame(currentFrame + 1); }, [currentFrame, showFrame, stopPlay]);
  const prevFrame = useCallback(() => { stopPlay(); showFrame(currentFrame - 1); }, [currentFrame, showFrame, stopPlay]);
  const jumpFrames = useCallback((n: number) => { stopPlay(); showFrame(currentFrame + n); }, [currentFrame, showFrame, stopPlay]);

  // Marcações
  const markInicio = useCallback(() => {
    if (!currentVideoPath) return;
    setMarkInicio(currentFrame);
    setStatus(`Início marcado: frame ${currentFrame}`);
  }, [currentVideoPath, currentFrame, setMarkInicio, setStatus]);

  const markChute = useCallback(() => {
    if (!currentVideoPath) return;
    setMarkChute(currentFrame);
    setStatus(`Chute marcado: frame ${currentFrame}`);
  }, [currentVideoPath, currentFrame, setMarkChute, setStatus]);

  // Atalhos de teclado
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      switch (e.key) {
        case " ":         e.preventDefault(); togglePlay();    break;
        case "ArrowRight": e.preventDefault(); nextFrame();     break;
        case "ArrowLeft":  e.preventDefault(); prevFrame();     break;
        case "i":          markInicio();                         break;
        case "f":          markChute();                          break;
        case "PageDown":   e.preventDefault(); jumpFrames(+30); break;
        case "PageUp":     e.preventDefault(); jumpFrames(-30); break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [togglePlay, nextFrame, prevFrame, markInicio, markChute, jumpFrames]);

  const pct = totalFrames > 0 ? (currentFrame / (totalFrames - 1)) * 100 : 0;
  const ts  = fps > 0 ? (currentFrame / fps).toFixed(2) : "0.00";

  const isOnInicio = marks.inicio_frame === currentFrame && currentFrame !== null;
  const isOnChute  = marks.chute_frame  === currentFrame && currentFrame !== null;

  return (
    <section className="video-player">
      {/* Canvas / imagem */}
      <div ref={canvasRef} className={`video-player__canvas ${isOnInicio ? "canvas--inicio" : ""} ${isOnChute ? "canvas--chute" : ""}`}>
        {!currentVideoPath ? (
          <p className="video-player__placeholder">Selecione um vídeo na lista à esquerda</p>
        ) : (
          <>
            <img
              ref={imgRef}
              alt="frame"
              className="video-player__img"
              onLoad={() => { setLoading(false); scheduleNextFrame(); }}
              onError={() => { setLoading(false); scheduleNextFrame(); }}
            />
            {loading && <div className="video-player__spinner" />}
            <span className="video-player__frame-overlay">Frame {currentFrame}</span>
            {isOnInicio && <span className="video-player__badge video-player__badge--inicio">◀ INÍCIO ▶</span>}
            {isOnChute  && <span className="video-player__badge video-player__badge--chute">◀ CHUTE ▶</span>}
            <ReguaTraves imgRef={imgRef} canvasRef={canvasRef} />
          </>
        )}
      </div>

      {/* Info de marcações */}
      <div className="video-player__marks-info">
        <span className="mark-inicio">
          Início: {marks.inicio_frame !== null ? `frame ${marks.inicio_frame}` : "—"}
        </span>
        <span className="mark-chute">
          Chute: {marks.chute_frame !== null ? `frame ${marks.chute_frame}` : "—"}
        </span>
        <span className="frame-info">
          Frame {currentFrame} / {totalFrames > 0 ? totalFrames - 1 : 0} &nbsp;|&nbsp; {ts} s
        </span>
      </div>

      {/* Slider */}
      <div className="video-player__slider-wrap">
        <input
          type="range"
          className="video-player__slider"
          min={0}
          max={Math.max(1, totalFrames - 1)}
          value={currentFrame}
          onChange={(e) => { stopPlay(); showFrame(Number(e.target.value)); }}
          style={{ "--pct": `${pct}%` } as React.CSSProperties}
        />
      </div>

      {/* Controles */}
      <div className="video-player__controls">
        <button className="btn-ctrl" onClick={() => jumpFrames(-30)} title="−30 frames">◀◀ −30</button>
        <button className="btn-ctrl" onClick={prevFrame}            title="Frame anterior">◀ Prev</button>
        <button className="btn-ctrl btn-ctrl--play" onClick={togglePlay}>
          {playing ? "⏸ Pause" : "▶ Play"}
        </button>
        <button className="btn-ctrl" onClick={nextFrame}            title="Próximo frame">Next ▶</button>
        <button className="btn-ctrl" onClick={() => jumpFrames(+30)} title="+30 frames">+30 ▶▶</button>

        <span className="btn-sep" />

        <button className="btn-ctrl btn-ctrl--inicio" onClick={markInicio} title="Tecla I">
          [I] Marcar Início
        </button>
        <button className="btn-ctrl btn-ctrl--chute" onClick={markChute} title="Tecla F">
          [F] Marcar Chute
        </button>
      </div>
    </section>
  );
}
