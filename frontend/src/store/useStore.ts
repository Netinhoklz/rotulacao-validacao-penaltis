import { create } from "zustand";
import type { CompetitionProgress } from "../api/client";

export interface Marks {
  inicio_frame: number | null;
  chute_frame: number | null;
}

export interface VideoItem {
  name: string;
  path: string;
  labeled?: boolean;
  cached?: boolean;
  competition?: string;
  competition_label?: string;
}

interface AppState {
  // Vídeos
  videoList: VideoItem[];
  currentVideoIdx: number;
  currentVideoPath: string;

  // Metadados do vídeo carregado
  totalFrames: number;
  fps: number;

  // Frame exibido
  currentFrame: number;

  // Marcações
  marks: Marks;

  // Painel de label
  selectedRegion: string | null;
  isGoal: boolean | null;
  cameraType: string;
  observations: string;

  // Estado de exportação
  exporting: boolean;
  penaltyCount: number;

  // Status
  statusMsg: string;

  // Configuração
  sourceDir: string;
  outputBase: string;
  showConfig: boolean;

  // Progresso
  progressTotal: number;
  progressLabeled: number;
  progressByCompetition: CompetitionProgress[];

  // Segmentacao: "" = todas as competicoes
  competitionFilter: string;

  // Pre-extracao
  preExtractStatus: "idle" | "running" | "done";
  preExtractCurrentVideo: string;
  preExtractVideosDone: number;
  preExtractVideosTotal: number;
  preExtractFrameProgress: number;
  preExtractFrameTotal: number;

  // Ações
  setVideoList: (list: VideoItem[]) => void;
  setProgress: (total: number, labeled: number, byCompetition?: CompetitionProgress[]) => void;
  setCompetitionFilter: (slug: string) => void;
  setPreExtractStatus: (s: { status: "idle" | "running" | "done"; current_video: string; videos_done: number; videos_total: number; current_video_progress: number; current_video_total: number }) => void;
  setCurrentVideoIdx: (idx: number) => void;
  setCurrentVideoPath: (path: string) => void;
  setVideoMeta: (totalFrames: number, fps: number) => void;
  setCurrentFrame: (frame: number) => void;
  setMarkInicio: (frame: number | null) => void;
  setMarkChute: (frame: number | null) => void;
  resetMarks: () => void;
  setSelectedRegion: (region: string | null) => void;
  setIsGoal: (v: boolean | null) => void;
  setCameraType: (cam: string) => void;
  setObservations: (obs: string) => void;
  setExporting: (v: boolean) => void;
  incrementPenalty: () => void;
  setPenaltyCount: (n: number) => void;
  resetPenaltyCount: () => void;
  setStatus: (msg: string) => void;
  setSourceDir: (dir: string) => void;
  setOutputBase: (dir: string) => void;
  setShowConfig: (v: boolean) => void;
  resetLabelFields: () => void;
}

export const useStore = create<AppState>((set) => ({
  videoList: [],
  currentVideoIdx: -1,
  currentVideoPath: "",
  totalFrames: 0,
  fps: 25,
  currentFrame: 0,
  marks: { inicio_frame: null, chute_frame: null },
  selectedRegion: null,
  isGoal: null,
  cameraType: "visão do torcedor",
  observations: "",
  exporting: false,
  penaltyCount: 0,
  statusMsg: "Pronto. Selecione um vídeo para começar.",
  sourceDir: "E:\\base_videos_penaltis",
  outputBase: "E:\\penaltis_rotulados",
  showConfig: false,
  progressTotal: 0,
  progressLabeled: 0,
  progressByCompetition: [],
  competitionFilter: "",

  preExtractStatus: "idle",
  preExtractCurrentVideo: "",
  preExtractVideosDone: 0,
  preExtractVideosTotal: 0,
  preExtractFrameProgress: 0,
  preExtractFrameTotal: 0,

  setVideoList: (list) => set({ videoList: list }),
  setProgress: (total, labeled, byCompetition) =>
    set(byCompetition
      ? { progressTotal: total, progressLabeled: labeled, progressByCompetition: byCompetition }
      : { progressTotal: total, progressLabeled: labeled }),
  setCompetitionFilter: (slug) => set({ competitionFilter: slug }),
  setPreExtractStatus: (s) => set({
    preExtractStatus: s.status,
    preExtractCurrentVideo: s.current_video,
    preExtractVideosDone: s.videos_done,
    preExtractVideosTotal: s.videos_total,
    preExtractFrameProgress: s.current_video_progress,
    preExtractFrameTotal: s.current_video_total,
  }),
  setCurrentVideoIdx: (idx) => set({ currentVideoIdx: idx }),
  setCurrentVideoPath: (path) => set({ currentVideoPath: path }),
  setVideoMeta: (totalFrames, fps) => set({ totalFrames, fps }),
  setCurrentFrame: (frame) => set({ currentFrame: frame }),
  setMarkInicio: (frame) =>
    set((s) => ({ marks: { ...s.marks, inicio_frame: frame } })),
  setMarkChute: (frame) =>
    set((s) => ({ marks: { ...s.marks, chute_frame: frame } })),
  resetMarks: () =>
    set({ marks: { inicio_frame: null, chute_frame: null } }),
  setSelectedRegion: (region) => set({ selectedRegion: region }),
  setIsGoal: (v) => set({ isGoal: v }),
  setCameraType: (cam) => set({ cameraType: cam }),
  setObservations: (obs) => set({ observations: obs }),
  setExporting: (v) => set({ exporting: v }),
  incrementPenalty: () => set((s) => ({ penaltyCount: s.penaltyCount + 1 })),
  setPenaltyCount: (n) => set({ penaltyCount: n }),
  resetPenaltyCount: () => set({ penaltyCount: 0 }),
  setStatus: (msg) => set({ statusMsg: msg }),
  setSourceDir: (dir) => set({ sourceDir: dir }),
  setOutputBase: (dir) => set({ outputBase: dir }),
  setShowConfig: (v) => set({ showConfig: v }),
  resetLabelFields: () =>
    set({
      selectedRegion: null,
      isGoal: null,
      cameraType: "visão do torcedor",
      observations: "",
    }),
}));
