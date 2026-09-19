"""
Aplicativo de Rotulagem de Pênaltis
=====================================
Permite abrir vídeos de pênaltis, navegar frame a frame,
marcar início e fim (chute) da cobrança, identificar a região
do chute no gol (9 regiões internas + 3 externas) e o tipo de
câmera. Salva clips, frames e labels na base de destino.

Estrutura de saída:
    output_base/
        clips/
            <video>_penalti_<N>.mp4       -> trecho início→chute
        frames/
            <video>_penalti_<N>_inicio.png
            <video>_penalti_<N>_chute.png
        labels/
            <video>_penalti_<N>.json
        labels.csv                         -> consolidado de todos os labels
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import threading
import cv2

from video_player import VideoPlayer
from labeler import LabelPanel
from exporter import Exporter

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
SOURCE_DIR   = r"E:\base_videos_penaltis"
OUTPUT_BASE  = r"E:\base_rotulada_penaltis"
SUPPORTED_EXT = (".mp4", ".avi", ".mov", ".mkv", ".wmv")


# ---------------------------------------------------------------------------
# Janela principal
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Rotulador de Pênaltis")
        self.geometry("1280x820")
        self.resizable(True, True)
        self.configure(bg="#1e1e2e")

        # Estado global
        self.video_list: list[str] = []
        self.current_video_idx: int = -1
        self.penalty_count: int = 0          # contador por vídeo

        # Frames marcados (dicionário mutável compartilhado com sub-painéis)
        self.marks: dict = {
            "inicio_frame": None,
            "chute_frame":  None,
        }

        self._build_menu()
        self._build_layout()
        self._load_video_list()

    # ------------------------------------------------------------------
    # Menu superior
    # ------------------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Abrir pasta de vídeos...", command=self._choose_source_dir)
        file_menu.add_command(label="Definir pasta de saída...",  command=self._choose_output_dir)
        file_menu.add_separator()
        file_menu.add_command(label="Sair", command=self.destroy)
        menubar.add_cascade(label="Arquivo", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Atalhos de teclado", command=self._show_shortcuts)
        menubar.add_cascade(label="Ajuda", menu=help_menu)

        self.config(menu=menubar)

    # ------------------------------------------------------------------
    # Layout principal
    # ------------------------------------------------------------------
    def _build_layout(self):
        # ── Coluna esquerda: lista de vídeos ───────────────────────────
        left = tk.Frame(self, bg="#181825", width=220)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 0), pady=6)
        left.pack_propagate(False)

        tk.Label(left, text="Vídeos", bg="#181825", fg="#cdd6f4",
                 font=("Helvetica", 11, "bold")).pack(pady=(8, 4))

        self.video_listbox = tk.Listbox(
            left, bg="#313244", fg="#cdd6f4", selectbackground="#89b4fa",
            selectforeground="#1e1e2e", font=("Courier", 9),
            activestyle="none", relief=tk.FLAT, borderwidth=0
        )
        self.video_listbox.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        self.video_listbox.bind("<<ListboxSelect>>", self._on_video_select)

        sb = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.video_listbox.yview)
        self.video_listbox.configure(yscrollcommand=sb.set)
        sb.place(relx=1, rely=0, relheight=1, anchor="ne")

        # Barra de progresso da lista
        prog_frame = tk.Frame(left, bg="#181825")
        prog_frame.pack(fill=tk.X, padx=6, pady=(0, 6))
        self.lbl_progress = tk.Label(prog_frame, text="0 / 0", bg="#181825",
                                      fg="#a6adc8", font=("Helvetica", 9))
        self.lbl_progress.pack()

        # ── Centro: player ─────────────────────────────────────────────
        center = tk.Frame(self, bg="#1e1e2e")
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6, pady=6)

        self.player = VideoPlayer(center, marks=self.marks,
                                   on_mark_changed=self._on_mark_changed)
        self.player.pack(fill=tk.BOTH, expand=True)

        # ── Coluna direita: painel de label ────────────────────────────
        right = tk.Frame(self, bg="#181825", width=280)
        right.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)
        right.pack_propagate(False)

        self.label_panel = LabelPanel(
            right,
            marks=self.marks,
            on_save=self._save_current_label
        )
        self.label_panel.pack(fill=tk.BOTH, expand=True)

        # ── Barra de status (rodapé) ───────────────────────────────────
        self.status_var = tk.StringVar(value="Pronto. Selecione um vídeo para começar.")
        status_bar = tk.Label(self, textvariable=self.status_var,
                               bg="#11111b", fg="#a6adc8",
                               font=("Helvetica", 9), anchor="w", padx=8)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # Atalhos globais
        self.bind("<space>",      lambda e: self.player.toggle_play())
        self.bind("<Right>",      lambda e: self.player.next_frame())
        self.bind("<Left>",       lambda e: self.player.prev_frame())
        self.bind("<i>",          lambda e: self.player.mark_inicio())
        self.bind("<f>",          lambda e: self.player.mark_chute())
        self.bind("<Return>",     lambda e: self.label_panel.trigger_save())
        self.bind("<Control-n>",  lambda e: self._next_video())
        self.bind("<Control-p>",  lambda e: self._prev_video())
        self.bind("<Prior>",      lambda e: self.player.jump_frames(-30))   # Page Up
        self.bind("<Next>",       lambda e: self.player.jump_frames(+30))   # Page Down

    # ------------------------------------------------------------------
    # Carga da lista de vídeos
    # ------------------------------------------------------------------
    @staticmethod
    def _get_video_resolution(path: str) -> int:
        """Retorna largura * altura do vídeo (0 se não conseguir ler)."""
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            return 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()
        return w * h

    def _load_video_list(self, directory: str = SOURCE_DIR):
        self.source_dir = directory
        self.video_list = []

        if not os.path.isdir(directory):
            self.set_status(f"Pasta não encontrada: {directory}")
            return

        for fname in sorted(os.listdir(directory)):
            if fname.lower().endswith(SUPPORTED_EXT):
                self.video_list.append(os.path.join(directory, fname))

        self.video_list.sort(key=self._get_video_resolution)

        self.video_listbox.delete(0, tk.END)
        for path in self.video_list:
            self.video_listbox.insert(tk.END, os.path.basename(path))

        total = len(self.video_list)
        self.lbl_progress.config(text=f"0 / {total}")
        self.set_status(f"{total} vídeo(s) encontrado(s) em {directory}")

    # ------------------------------------------------------------------
    # Seleção de vídeo na lista
    # ------------------------------------------------------------------
    def _on_video_select(self, event=None):
        sel = self.video_listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        if idx == self.current_video_idx:
            return
        self.current_video_idx = idx
        self.penalty_count = 0
        video_path = self.video_list[idx]
        self._reset_marks()
        self.player.load_video(video_path)
        total = len(self.video_list)
        self.lbl_progress.config(text=f"{idx + 1} / {total}")
        self.set_status(f"Vídeo carregado: {os.path.basename(video_path)}")

    def _next_video(self):
        if self.current_video_idx < len(self.video_list) - 1:
            self.video_listbox.selection_clear(0, tk.END)
            self.video_listbox.selection_set(self.current_video_idx + 1)
            self._on_video_select()

    def _prev_video(self):
        if self.current_video_idx > 0:
            self.video_listbox.selection_clear(0, tk.END)
            self.video_listbox.selection_set(self.current_video_idx - 1)
            self._on_video_select()

    # ------------------------------------------------------------------
    # Callbacks de marcação
    # ------------------------------------------------------------------
    def _reset_marks(self):
        self.marks["inicio_frame"] = None
        self.marks["chute_frame"]  = None
        self.label_panel.refresh_marks()

    def _on_mark_changed(self):
        """Chamado pelo VideoPlayer quando início ou chute são marcados."""
        self.label_panel.refresh_marks()
        inicio = self.marks["inicio_frame"]
        chute  = self.marks["chute_frame"]
        parts = []
        if inicio is not None:
            parts.append(f"Início: frame {inicio}")
        if chute is not None:
            parts.append(f"Chute: frame {chute}")
        self.set_status("  |  ".join(parts) if parts else "Sem marcações ainda.")

    # ------------------------------------------------------------------
    # Salvar label
    # ------------------------------------------------------------------
    def _save_current_label(self, label_data: dict):
        """Recebe dados do LabelPanel e aciona o Exporter em thread separada."""
        if self.current_video_idx < 0:
            messagebox.showwarning("Aviso", "Nenhum vídeo selecionado.")
            return

        inicio = self.marks.get("inicio_frame")
        chute  = self.marks.get("chute_frame")

        if inicio is None or chute is None:
            messagebox.showwarning("Aviso",
                "Marque o frame de INÍCIO (tecla I) e o frame do CHUTE (tecla F) antes de salvar.")
            return

        if inicio >= chute:
            messagebox.showwarning("Aviso",
                "O frame de início deve ser ANTERIOR ao frame do chute.")
            return

        video_path = self.video_list[self.current_video_idx]
        self.penalty_count += 1
        penalty_id = self.penalty_count

        self.set_status("Exportando… aguarde.")
        self.label_panel.set_saving(True)

        def _export():
            exporter = Exporter(output_base=OUTPUT_BASE)
            exporter.export(
                video_path=video_path,
                penalty_id=penalty_id,
                inicio_frame=inicio,
                chute_frame=chute,
                fps=self.player.fps,
                label_data=label_data,
            )
            self.after(0, self._on_export_done)

        threading.Thread(target=_export, daemon=True).start()

    def _on_export_done(self):
        self.label_panel.set_saving(False)
        self.label_panel.reset_fields()
        self._reset_marks()
        self.set_status(
            f"Pênalti #{self.penalty_count} salvo com sucesso! "
            "Marque o próximo ou avance para o próximo vídeo (Ctrl+N)."
        )

    # ------------------------------------------------------------------
    # Diálogos de pasta
    # ------------------------------------------------------------------
    def _choose_source_dir(self):
        d = filedialog.askdirectory(title="Selecionar pasta de vídeos",
                                    initialdir=SOURCE_DIR)
        if d:
            self._load_video_list(d)

    def _choose_output_dir(self):
        global OUTPUT_BASE
        d = filedialog.askdirectory(title="Selecionar pasta de saída",
                                    initialdir=OUTPUT_BASE)
        if d:
            OUTPUT_BASE = d
            self.set_status(f"Pasta de saída definida: {OUTPUT_BASE}")

    # ------------------------------------------------------------------
    # Utilitários
    # ------------------------------------------------------------------
    def set_status(self, msg: str):
        self.status_var.set(msg)

    def _show_shortcuts(self):
        text = (
            "Atalhos de teclado\n"
            "──────────────────────────────────\n"
            "Espaço        Pausar / Reproduzir\n"
            "← / →         Frame anterior / próximo\n"
            "Page Up/Down  Pular 30 frames\n"
            "I             Marcar frame de INÍCIO\n"
            "F             Marcar frame do CHUTE\n"
            "Enter         Salvar label atual\n"
            "Ctrl + N      Próximo vídeo\n"
            "Ctrl + P      Vídeo anterior\n"
        )
        messagebox.showinfo("Atalhos", text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = App()
    app.mainloop()
