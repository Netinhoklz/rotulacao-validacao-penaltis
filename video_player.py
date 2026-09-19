"""
video_player.py
===============
Widget de reprodução de vídeo com navegação frame a frame.

Estratégia de carregamento:
  • Ao abrir um vídeo, uma thread de fundo lê TODOS os frames sequencialmente
    e os armazena em memória (numpy arrays BGR).
  • Ao abrir outro vídeo, a thread para imediatamente, a memória é liberada
    e o novo vídeo começa a ser carregado do zero.
  • Barra de progresso indica quantos frames já estão em memória.
  • Navegação durante o carregamento: cache hit → seq. read → seek direto.
  • Display: cv2.resize INTER_LINEAR (~3-5× mais rápido que PIL LANCZOS).
"""

import tkinter as tk
from tkinter import ttk
import cv2
import numpy as np
from PIL import Image, ImageTk
from typing import Callable, Optional
import threading
from collections import OrderedDict


_PLAY_INTERVAL_MS = 33
_CACHE_SIZE    = 100_000   # efetivamente ilimitado para clips curtos
_SEQ_THRESHOLD = 60        # salto ≤ este valor para frente → leitura sequencial
_PROGRESS_STEP = 20        # atualiza barra a cada N frames carregados


# ---------------------------------------------------------------------------
# Cache LRU thread-safe com controle de geração
# ---------------------------------------------------------------------------
class _FrameCache:
    """Cache thread-safe de frames BGR.

    Cada vídeo recebe um ID de geração crescente.
    Workers passam seu ID no put() — frames de vídeos anteriores
    são descartados silenciosamente, sem afetar o vídeo atual.
    """

    def __init__(self, max_size: int = _CACHE_SIZE):
        self._data: OrderedDict[int, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self._max  = max_size
        self._generation: int = 0

    def new_generation(self) -> int:
        """Limpa o cache e retorna o novo ID de geração (operação atômica)."""
        with self._lock:
            self._generation += 1
            self._data.clear()
            return self._generation

    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    def get(self, idx: int) -> Optional[np.ndarray]:
        with self._lock:
            if idx not in self._data:
                return None
            self._data.move_to_end(idx)
            return self._data[idx]

    def put(self, idx: int, frame: np.ndarray, generation: Optional[int] = None):
        """Insere frame. Com `generation`, descarta se o vídeo já mudou."""
        with self._lock:
            if generation is not None and generation != self._generation:
                return  # frame obsoleto — vídeo mudou
            if idx in self._data:
                self._data.move_to_end(idx)
            self._data[idx] = frame
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def __contains__(self, idx: int) -> bool:
        with self._lock:
            return idx in self._data

    def size(self) -> int:
        with self._lock:
            return len(self._data)


# ---------------------------------------------------------------------------
# Thread de carregamento completo
# ---------------------------------------------------------------------------
class _VideoLoader(threading.Thread):
    """Thread daemon que carrega o vídeo atual completo na memória.

    - Leitura sequencial pura (sem seek intermediário) → máximo desempenho.
    - Ao receber um novo vídeo, para imediatamente via verificação de geração.
    - Reporta progresso via callback a cada _PROGRESS_STEP frames.
    """

    def __init__(self, cache: _FrameCache, on_progress: Callable):
        super().__init__(daemon=True, name="video-loader")
        self._cache       = cache
        self._on_progress = on_progress   # fn(loaded, total, generation)

        self._path: Optional[str] = None
        self._total      = 0
        self._generation = 0

        self._lock    = threading.Lock()
        self._wake    = threading.Event()
        self._stopped = False

    def load(self, path: str, total_frames: int, generation: int):
        """Solicita carregamento de um novo vídeo (cancela o anterior)."""
        with self._lock:
            self._path       = path
            self._total      = total_frames
            self._generation = generation
        self._wake.set()

    def stop(self):
        self._stopped = True
        self._wake.set()

    def run(self):
        local_cap: Optional[cv2.VideoCapture] = None

        while not self._stopped:
            self._wake.wait(timeout=1.0)
            self._wake.clear()

            if self._stopped:
                break

            with self._lock:
                path       = self._path
                total      = self._total
                generation = self._generation

            if path is None:
                continue

            # Abre o novo VideoCapture (libera o anterior)
            if local_cap:
                local_cap.release()
            local_cap = cv2.VideoCapture(path)
            if not local_cap.isOpened():
                continue

            # Lê todos os frames sequencialmente
            loaded = 0
            for i in range(total):
                # Novo vídeo solicitado → para imediatamente
                with self._lock:
                    if self._generation != generation:
                        break

                if i in self._cache:
                    local_cap.read()   # avança posição sem guardar o frame
                    loaded += 1
                    continue

                ret, frame = local_cap.read()
                if not ret:
                    break

                self._cache.put(i, frame, generation)
                loaded += 1

                if loaded % _PROGRESS_STEP == 0 or loaded == total:
                    self._on_progress(loaded, total, generation)

            # Notificação final (garante que UI mostre 100%)
            self._on_progress(loaded, total, generation)

        if local_cap:
            local_cap.release()


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class VideoPlayer(tk.Frame):
    def __init__(
        self,
        master,
        marks: dict,
        on_mark_changed: Callable,
        **kwargs,
    ):
        super().__init__(master, bg="#1e1e2e", **kwargs)
        self.marks = marks
        self.on_mark_changed = on_mark_changed

        self._cap: Optional[cv2.VideoCapture] = None
        self._total_frames: int = 0
        self._current_frame: int = 0
        self._playing: bool = False
        self._play_job = None
        self.fps: float = 25.0

        self._cache  = _FrameCache(_CACHE_SIZE)
        self._loader = _VideoLoader(self._cache, self._on_load_progress)
        self._loader.start()

        self._build_ui()

    # ------------------------------------------------------------------
    # Construção da UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        # Canvas de vídeo
        self.canvas = tk.Canvas(self, bg="#000000", cursor="crosshair",
                                 highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self._placeholder()

        # ── Barra de carregamento (sempre visível, texto vazio quando ociosa) ──
        loading_frame = tk.Frame(self, bg="#1e1e2e")
        loading_frame.pack(fill=tk.X, padx=8, pady=(4, 0))

        self.lbl_loading = tk.Label(
            loading_frame, text="",
            bg="#1e1e2e", fg="#89b4fa", font=("Courier", 9), width=34, anchor="w"
        )
        self.lbl_loading.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(
            loading_frame, orient=tk.HORIZONTAL,
            mode="determinate", length=180
        )
        self.progress_bar.pack(side=tk.LEFT, padx=(4, 0))

        # ── Informação de frames marcados ─────────────────────────────
        info_frame = tk.Frame(self, bg="#1e1e2e")
        info_frame.pack(fill=tk.X, padx=8, pady=(2, 0))

        self.lbl_inicio = tk.Label(
            info_frame, text="Início: —", bg="#1e1e2e", fg="#a6e3a1",
            font=("Courier", 9, "bold")
        )
        self.lbl_inicio.pack(side=tk.LEFT, padx=(0, 20))

        self.lbl_chute = tk.Label(
            info_frame, text="Chute: —", bg="#1e1e2e", fg="#f38ba8",
            font=("Courier", 9, "bold")
        )
        self.lbl_chute.pack(side=tk.LEFT)

        self.lbl_frame_info = tk.Label(
            info_frame, text="Frame: 0 / 0  |  0.00 s",
            bg="#1e1e2e", fg="#a6adc8", font=("Courier", 9)
        )
        self.lbl_frame_info.pack(side=tk.RIGHT)

        # ── Slider ────────────────────────────────────────────────────
        slider_frame = tk.Frame(self, bg="#1e1e2e")
        slider_frame.pack(fill=tk.X, padx=8, pady=2)

        self.slider_var = tk.IntVar(value=0)
        self.slider = ttk.Scale(
            slider_frame, from_=0, to=1, orient=tk.HORIZONTAL,
            variable=self.slider_var, command=self._on_slider_move
        )
        self.slider.pack(fill=tk.X)
        self._slider_dragging = False
        self.slider.bind("<ButtonPress-1>",   self._slider_press)
        self.slider.bind("<ButtonRelease-1>", self._slider_release)

        # ── Botões de controle ─────────────────────────────────────────
        ctrl = tk.Frame(self, bg="#1e1e2e")
        ctrl.pack(pady=4)

        btn_style = dict(bg="#313244", fg="#cdd6f4", activebackground="#45475a",
                          activeforeground="#cdd6f4", relief=tk.FLAT,
                          font=("Helvetica", 9), padx=8, pady=3, cursor="hand2")

        tk.Button(ctrl, text="◀◀ -30",   command=lambda: self.jump_frames(-30), **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(ctrl, text="◀ Prev",   command=self.prev_frame,               **btn_style).pack(side=tk.LEFT, padx=2)
        self.btn_play = tk.Button(ctrl, text="▶ Play", command=self.toggle_play, **btn_style)
        self.btn_play.pack(side=tk.LEFT, padx=2)
        tk.Button(ctrl, text="Next ▶",   command=self.next_frame,               **btn_style).pack(side=tk.LEFT, padx=2)
        tk.Button(ctrl, text="+30 ▶▶",   command=lambda: self.jump_frames(+30), **btn_style).pack(side=tk.LEFT, padx=2)

        tk.Label(ctrl, text="  ", bg="#1e1e2e").pack(side=tk.LEFT)

        tk.Button(ctrl, text="[I] Marcar Início",
                   command=self.mark_inicio,
                   bg="#40a02b", fg="white", activebackground="#2d7a1f",
                   activeforeground="white", relief=tk.FLAT,
                   font=("Helvetica", 9, "bold"), padx=8, pady=3,
                   cursor="hand2").pack(side=tk.LEFT, padx=2)

        tk.Button(ctrl, text="[F] Marcar Chute",
                   command=self.mark_chute,
                   bg="#d20f39", fg="white", activebackground="#a50d2c",
                   activeforeground="white", relief=tk.FLAT,
                   font=("Helvetica", 9, "bold"), padx=8, pady=3,
                   cursor="hand2").pack(side=tk.LEFT, padx=2)

    # ------------------------------------------------------------------
    # Carregamento de vídeo
    # ------------------------------------------------------------------
    def load_video(self, path: str):
        self._stop_play()
        if self._cap:
            self._cap.release()

        self._cap = cv2.VideoCapture(path)
        if not self._cap.isOpened():
            self._cap = None
            self._placeholder(msg=f"Erro ao abrir:\n{path}")
            return

        self._total_frames = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.fps            = self._cap.get(cv2.CAP_PROP_FPS) or 25.0
        self._current_frame = 0
        self.slider.config(to=max(1, self._total_frames - 1))
        self.slider_var.set(0)

        # Descarta memória do vídeo anterior e obtém ID da nova geração
        generation = self._cache.new_generation()

        # Reinicia barra de progresso
        self.lbl_loading.config(text=f"Carregando 0 / {self._total_frames} (0%)")
        self.progress_bar['value'] = 0

        # Dispara carregamento completo em background
        self._loader.load(path, self._total_frames, generation)

        self._seek(0)

    # ------------------------------------------------------------------
    # Callback de progresso — chamado do worker, agenda update no main thread
    # ------------------------------------------------------------------
    def _on_load_progress(self, loaded: int, total: int, generation: int):
        def _update():
            # Ignora se o vídeo já mudou desde que este update foi agendado
            if self._cache.current_generation() != generation:
                return
            pct = int(100 * loaded / total) if total else 0
            self.progress_bar['value'] = pct
            if loaded >= total:
                self.lbl_loading.config(
                    text=f"✓ {total} frames em memória"
                )
            else:
                self.lbl_loading.config(
                    text=f"Carregando {loaded} / {total} ({pct}%)"
                )
        self.after(0, _update)

    # ------------------------------------------------------------------
    # Navegação
    # ------------------------------------------------------------------
    def toggle_play(self):
        if self._cap is None:
            return
        if self._playing:
            self._stop_play()
        else:
            self._start_play()

    def _start_play(self):
        self._playing = True
        self.btn_play.config(text="⏸ Pause")
        self._schedule_next_frame()

    def _stop_play(self):
        self._playing = False
        self.btn_play.config(text="▶ Play")
        if self._play_job:
            self.after_cancel(self._play_job)
            self._play_job = None

    def _schedule_next_frame(self):
        if not self._playing:
            return
        self._advance_one()
        if self._current_frame >= self._total_frames - 1:
            self._stop_play()
            return
        self._play_job = self.after(_PLAY_INTERVAL_MS, self._schedule_next_frame)

    def next_frame(self):
        if self._cap is None:
            return
        self._stop_play()
        self._seek(min(self._current_frame + 1, self._total_frames - 1))

    def prev_frame(self):
        if self._cap is None:
            return
        self._stop_play()
        self._seek(max(self._current_frame - 1, 0))

    def jump_frames(self, n: int):
        if self._cap is None:
            return
        self._stop_play()
        target = max(0, min(self._current_frame + n, self._total_frames - 1))
        self._seek(target)

    def _advance_one(self):
        """Avança 1 frame durante play — cache first."""
        next_idx = self._current_frame + 1
        frame = self._cache.get(next_idx)
        if frame is not None:
            self._current_frame = next_idx
        else:
            ret, frame = self._cap.read()
            if not ret:
                return
            self._current_frame = next_idx
            self._cache.put(next_idx, frame)
        self._display_frame(frame)
        self._update_ui_state()

    def _seek(self, target: int):
        """Seek com 3 estratégias em cascata: cache → seq → seek direto."""

        # 1. Cache hit — frame já em memória (caminho mais comum após carregamento)
        frame = self._cache.get(target)
        if frame is not None:
            self._current_frame = target
            self._display_frame(frame)
            self._update_ui_state()
            return

        # 2. Salto curto para frente → leitura sequencial
        #    (evita busca por I-frame em H.264/H.265)
        delta = target - self._current_frame
        if 0 < delta <= _SEQ_THRESHOLD:
            frame = self._seq_read_to(target)
            if frame is not None:
                self._current_frame = target
                self._display_frame(frame)
                self._update_ui_state()
                return

        # 3. Seek direto (salto grande ou para trás)
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, target)
        ret, frame = self._cap.read()
        if ret:
            self._current_frame = target
            self._cache.put(target, frame)
            self._display_frame(frame)
        self._update_ui_state()

    def _seq_read_to(self, target: int) -> Optional[np.ndarray]:
        """Lê sequencialmente até target, cacheando intermediários."""
        result = None
        for i in range(self._current_frame + 1, target + 1):
            if i in self._cache:
                self._cap.read()          # avança posição sem usar o frame
                result = self._cache.get(i)
            else:
                ret, frame = self._cap.read()
                if not ret:
                    return result
                self._cache.put(i, frame)
                result = frame
        return result

    # ------------------------------------------------------------------
    # Marcações
    # ------------------------------------------------------------------
    def mark_inicio(self):
        if self._cap is None:
            return
        self.marks["inicio_frame"] = self._current_frame
        self.lbl_inicio.config(text=f"Início: {self._current_frame}")
        self.on_mark_changed()

    def mark_chute(self):
        if self._cap is None:
            return
        self.marks["chute_frame"] = self._current_frame
        self.lbl_chute.config(text=f"Chute:  {self._current_frame}")
        self.on_mark_changed()

    def get_frame_image(self, frame_idx: int) -> Optional[np.ndarray]:
        """Retorna frame BGR sem alterar posição atual (usa cache se disponível)."""
        frame = self._cache.get(frame_idx)
        if frame is not None:
            return frame
        if self._cap is None:
            return None
        pos = self._cap.get(cv2.CAP_PROP_POS_FRAMES)
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = self._cap.read()
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, pos)
        return frame if ret else None

    # ------------------------------------------------------------------
    # Exibição
    # ------------------------------------------------------------------
    def _display_frame(self, frame: np.ndarray):
        h_canvas = self.canvas.winfo_height() or 480
        w_canvas = self.canvas.winfo_width()  or 640

        h_f, w_f = frame.shape[:2]
        scale = min(w_canvas / w_f, h_canvas / h_f)
        new_w = max(1, int(w_f * scale))
        new_h = max(1, int(h_f * scale))

        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        self._tk_img = ImageTk.PhotoImage(Image.fromarray(rgb))

        x, y = w_canvas // 2, h_canvas // 2
        self.canvas.delete("video")
        self.canvas.create_image(x, y, anchor="center",
                                   image=self._tk_img, tags="video")

        self.canvas.delete("overlay")
        self.canvas.create_text(
            8, h_canvas - 8, anchor="sw",
            text=f"Frame {self._current_frame}",
            fill="#a6adc8", font=("Courier", 8), tags="overlay"
        )

        if self.marks.get("inicio_frame") == self._current_frame:
            self.canvas.create_rectangle(2, 2, w_canvas - 2, h_canvas - 2,
                                           outline="#a6e3a1", width=3, tags="overlay")
            self.canvas.create_text(w_canvas // 2, 16, text="◀ INÍCIO ▶",
                                     fill="#a6e3a1", font=("Helvetica", 10, "bold"),
                                     tags="overlay")
        if self.marks.get("chute_frame") == self._current_frame:
            self.canvas.create_rectangle(2, 2, w_canvas - 2, h_canvas - 2,
                                           outline="#f38ba8", width=3, tags="overlay")
            self.canvas.create_text(w_canvas // 2, 16, text="◀ CHUTE ▶",
                                     fill="#f38ba8", font=("Helvetica", 10, "bold"),
                                     tags="overlay")

    def _placeholder(self, msg: str = "Selecione um vídeo na lista à esquerda"):
        self.canvas.delete("all")
        w = self.canvas.winfo_width()  or 640
        h = self.canvas.winfo_height() or 480
        self.canvas.create_text(w // 2, h // 2, text=msg,
                                  fill="#585b70", font=("Helvetica", 13),
                                  justify="center")

    def _on_canvas_resize(self, event):
        if self._cap and self._total_frames > 0:
            frame = self._cache.get(self._current_frame)
            if frame is not None:
                self._display_frame(frame)
            else:
                self._seek(self._current_frame)

    # ------------------------------------------------------------------
    # Slider
    # ------------------------------------------------------------------
    def _slider_press(self, event):
        self._stop_play()
        self._slider_dragging = True

    def _slider_release(self, event):
        self._slider_dragging = False
        self._seek(int(self.slider_var.get()))

    def _on_slider_move(self, value):
        if self._slider_dragging:
            self._seek(int(float(value)))

    # ------------------------------------------------------------------
    # Atualização de estado da UI
    # ------------------------------------------------------------------
    def _update_ui_state(self):
        if not self._slider_dragging:
            self.slider_var.set(self._current_frame)
        ts = self._current_frame / self.fps if self.fps else 0
        self.lbl_frame_info.config(
            text=f"Frame: {self._current_frame} / {self._total_frames - 1}"
                 f"  |  {ts:.2f} s"
        )
        inicio = self.marks.get("inicio_frame")
        chute  = self.marks.get("chute_frame")
        self.lbl_inicio.config(
            text=f"Início: {inicio}" if inicio is not None else "Início: —"
        )
        self.lbl_chute.config(
            text=f"Chute: {chute}" if chute is not None else "Chute: —"
        )
