const BASE = "";

export interface VideoInfo {
  name: string;
  path: string;
  labeled?: boolean;
  cached?: boolean;
  competition?: string;
  competition_label?: string;
}

export interface CompetitionProgress {
  competition: string;
  competition_label: string;
  total: number;
  labeled: number;
  unlabeled: number;
  output_dir: string;
  csv_path: string;
}

export interface ProgressInfo {
  total: number;
  labeled: number;
  unlabeled: number;
  by_competition: CompetitionProgress[];
  videos: {
    name: string;
    path: string;
    labeled: boolean;
    competition: string;
    competition_label: string;
  }[];
}

export interface VideoMeta {
  loaded: boolean;
  path?: string;
  total_frames?: number;
  fps?: number;
  width?: number;
  height?: number;
}

export interface ExportPayload {
  video_path: string;
  penalty_id: number;
  inicio_frame: number;
  chute_frame: number;
  camera_type: string;
  region: string;
  is_goal: boolean;
  observations: string;
}

export interface CompetitionPaths {
  slug: string;
  label: string;
  dir: string;
  clips: string;
  frames: string;
  labels: string;
  csv: string;
}

export interface Config {
  source_dir: string;
  output_base: string;
  layout?: CompetitionPaths[];
}

export interface ExportResult extends Record<string, unknown> {
  penalty_id: number;
  competition: string;
  competition_label: string;
  output_dir: string;
  csv_path: string;
  aviso?: string;
  aviso_competicao?: string;
}

export interface PreExtractStatus {
  status: "idle" | "running" | "done";
  current_video: string;
  current_video_progress: number;
  current_video_total: number;
  videos_done: number;
  videos_total: number;
  videos_skipped: number;
}

export const api = {
  async getConfig(): Promise<Config> {
    const r = await fetch(`${BASE}/config`);
    return r.json();
  },

  async setConfig(data: Partial<Config>): Promise<Config> {
    const r = await fetch(`${BASE}/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    return r.json();
  },

  async listVideos(): Promise<{ videos: VideoInfo[]; directory: string; error?: string }> {
    const r = await fetch(`${BASE}/videos`);
    return r.json();
  },

  async loadVideo(path: string): Promise<VideoMeta> {
    const r = await fetch(`${BASE}/video/load`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.detail ?? "Erro ao carregar vídeo.");
    }
    return r.json();
  },

  frameUrl(idx: number, videoPath?: string): string {
    // Use video path hash as cache key to avoid stale frames when switching videos
    const vkey = videoPath ? encodeURIComponent(videoPath) : "";
    return `${BASE}/video/frame/${idx}?v=${vkey}`;
  },

  async getProgress(): Promise<ProgressInfo> {
    const r = await fetch(`${BASE}/progress`);
    return r.json();
  },

  async exportPenalty(payload: ExportPayload): Promise<ExportResult> {
    const r = await fetch(`${BASE}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) {
      const err = await r.json();
      throw new Error(err.detail ?? "Erro ao exportar.");
    }
    return r.json();
  },

  async getPreExtractStatus(): Promise<PreExtractStatus> {
    const r = await fetch(`${BASE}/preextract/status`);
    return r.json();
  },

  async startPreExtract(): Promise<{ started: boolean; videos_to_process: number }> {
    const r = await fetch(`${BASE}/preextract/start`, { method: "POST" });
    return r.json();
  },
};
