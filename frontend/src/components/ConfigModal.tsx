import { useState } from "react";
import { api } from "../api/client";
import { useStore } from "../store/useStore";

export function ConfigModal() {
  const { sourceDir, outputBase, setSourceDir, setOutputBase, setShowConfig, setStatus, setVideoList } = useStore();
  const [src, setSrc]  = useState(sourceDir);
  const [out, setOut]  = useState(outputBase);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    setSaving(true);
    try {
      await api.setConfig({ source_dir: src, output_base: out });
      setSourceDir(src);
      setOutputBase(out);
      // Recarrega lista de vídeos
      const res = await api.listVideos();
      setVideoList(res.videos ?? []);
      setStatus(`Configuração salva. ${res.videos.length} vídeo(s) encontrado(s).`);
      setShowConfig(false);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e);
      alert(`Erro: ${msg}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => setShowConfig(false)}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2 className="modal__title">⚙ Configuração</h2>

        <label className="modal__label">
          Pasta de vídeos (origem)
          <input
            className="modal__input"
            value={src}
            onChange={(e) => setSrc(e.target.value)}
            placeholder="Ex: E:\base_videos_penaltis"
          />
        </label>

        <label className="modal__label">
          Pasta de saída (labels / clips / frames)
          <input
            className="modal__input"
            value={out}
            onChange={(e) => setOut(e.target.value)}
            placeholder="Ex: E:\base_rotulada_penaltis"
          />
        </label>

        <div className="modal__actions">
          <button className="btn-modal-cancel" onClick={() => setShowConfig(false)}>
            Cancelar
          </button>
          <button className="btn-modal-save" onClick={handleSave} disabled={saving}>
            {saving ? "Salvando…" : "Salvar"}
          </button>
        </div>
      </div>
    </div>
  );
}
