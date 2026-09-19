import { useEffect } from "react";
import { VideoList }   from "./components/VideoList";
import { VideoPlayer } from "./components/VideoPlayer";
import { LabelPanel }  from "./components/LabelPanel";
import { ConfigModal } from "./components/ConfigModal";
import { useStore }    from "./store/useStore";
import { api }         from "./api/client";
import "./App.css";

export default function App() {
  const { statusMsg, showConfig, setSourceDir, setOutputBase, setStatus } = useStore();

  // Carrega config do backend na inicialização
  useEffect(() => {
    api.getConfig().then((cfg) => {
      setSourceDir(cfg.source_dir);
      setOutputBase(cfg.output_base);
    }).catch(() => {
      setStatus("Backend não encontrado. Inicie api.py antes de usar.");
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Atalhos Ctrl+N / Ctrl+P para navegar entre vídeos.
  // Navega pelos itens realmente renderizados (e não por currentVideoIdx),
  // para continuar correto quando a lista está filtrada por competição.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (!e.ctrlKey || (e.key !== "n" && e.key !== "p")) return;

      e.preventDefault();
      const items = Array.from(
        document.querySelectorAll<HTMLElement>(".video-list__item"),
      );
      if (items.length === 0) return;

      const active = items.findIndex((el) =>
        el.classList.contains("video-list__item--active"),
      );
      const target = e.key === "n" ? active + 1 : active - 1;
      if (target >= 0 && target < items.length) items[target].click();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div className="app">
      <VideoList />
      <VideoPlayer />
      <LabelPanel />

      {showConfig && <ConfigModal />}

      <footer className="status-bar">
        <span className="status-bar__msg">{statusMsg}</span>
        <span className="status-bar__shortcuts">
          Espaço: Play/Pause &nbsp;·&nbsp; ←→: Frame &nbsp;·&nbsp;
          PgUp/Dn: ±30 &nbsp;·&nbsp; I: Início &nbsp;·&nbsp; F: Chute &nbsp;·&nbsp;
          Enter: Salvar &nbsp;·&nbsp; Ctrl+N/P: Navegar vídeos &nbsp;·&nbsp;
          E: Régua das traves &nbsp;·&nbsp; T: só os traços &nbsp;·&nbsp; R: recolher
        </span>
      </footer>
    </div>
  );
}
