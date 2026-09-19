"""
labeler.py
==========
Painel lateral de rotulagem de pênaltis.

Coleta:
  - Tipo de câmera: "visão do torcedor" | "visão cobrador" | "visão goleiro"
  - Região do chute: 9 regiões dentro do gol + 3 fora
      Dentro do gol (grade 3×3, canto sup-esq = 1):
          1 | 2 | 3
          4 | 5 | 6
          7 | 8 | 9
      Fora do gol:
          fora_esquerda | fora_cima | fora_direita
  - Observações livres (campo de texto opcional)
  - Resumo dos frames marcados (exibido em tempo real)
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Mapeamento de regiões
# ---------------------------------------------------------------------------
GOAL_REGIONS: list[tuple[str, str]] = [
    # (código, label exibido)
    ("gol_topo_esquerdo",    "↖ Topo Esq"),
    ("gol_topo_centro",      "↑ Topo Ctr"),
    ("gol_topo_direito",     "↗ Topo Dir"),
    ("gol_meio_esquerdo",    "← Meio Esq"),
    ("gol_meio_centro",      "● Centro"),
    ("gol_meio_direito",     "→ Meio Dir"),
    ("gol_baixo_esquerdo",   "↙ Baixo Esq"),
    ("gol_baixo_centro",     "↓ Baixo Ctr"),
    ("gol_baixo_direito",    "↘ Baixo Dir"),
]

OUT_REGIONS: list[tuple[str, str]] = [
    ("fora_esquerda", "⬅ Fora Esq"),
    ("fora_cima",     "⬆ Fora Cima"),
    ("fora_direita",  "➡ Fora Dir"),
]

CAMERA_TYPES: list[str] = [
    "visão do torcedor",
    "visão cobrador",
    "visão goleiro",
]

# Cores de destaque por zona
_COLOR_GOAL = "#89b4fa"    # azul claro – dentro do gol
_COLOR_OUT  = "#fab387"    # laranja – fora do gol
_COLOR_SEL  = "#1e1e2e"    # texto selecionado (fundo escuro)


class LabelPanel(tk.Frame):
    def __init__(
        self,
        master,
        marks: dict,
        on_save: Callable[[dict], None],
        **kwargs,
    ):
        super().__init__(master, bg="#181825", **kwargs)
        self.marks = marks
        self.on_save = on_save

        # Estado interno
        self._selected_region: Optional[str] = None
        self._goal_selected: Optional[bool] = None
        self._region_buttons: dict[str, tk.Button] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # Construção da UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        pad = dict(padx=10, pady=4)

        # Título
        tk.Label(self, text="Rotulagem", bg="#181825", fg="#cdd6f4",
                  font=("Helvetica", 12, "bold")).pack(**pad)

        ttk.Separator(self, orient="horizontal").pack(fill=tk.X, padx=10)

        # ── Frames marcados ────────────────────────────────────────────
        mark_frame = tk.LabelFrame(self, text=" Marcações ", bg="#181825",
                                    fg="#a6adc8", font=("Helvetica", 9),
                                    bd=1, relief=tk.GROOVE)
        mark_frame.pack(fill=tk.X, **pad)

        self.lbl_inicio_mark = tk.Label(
            mark_frame, text="Início:  —", bg="#181825",
            fg="#a6e3a1", font=("Courier", 9)
        )
        self.lbl_inicio_mark.pack(anchor="w", padx=6, pady=2)

        self.lbl_chute_mark = tk.Label(
            mark_frame, text="Chute:   —", bg="#181825",
            fg="#f38ba8", font=("Courier", 9)
        )
        self.lbl_chute_mark.pack(anchor="w", padx=6, pady=(0, 4))

        # ── Tipo de câmera ─────────────────────────────────────────────
        cam_frame = tk.LabelFrame(self, text=" Tipo de câmera ", bg="#181825",
                                   fg="#a6adc8", font=("Helvetica", 9),
                                   bd=1, relief=tk.GROOVE)
        cam_frame.pack(fill=tk.X, **pad)

        self.camera_var = tk.StringVar(value=CAMERA_TYPES[0])
        for ct in CAMERA_TYPES:
            tk.Radiobutton(
                cam_frame, text=ct, variable=self.camera_var, value=ct,
                bg="#181825", fg="#cdd6f4", selectcolor="#313244",
                activebackground="#181825", activeforeground="#cdd6f4",
                font=("Helvetica", 9)
            ).pack(anchor="w", padx=8, pady=1)

        # ── Região do chute ────────────────────────────────────────────
        reg_frame = tk.LabelFrame(self, text=" Região do chute ", bg="#181825",
                                   fg="#a6adc8", font=("Helvetica", 9),
                                   bd=1, relief=tk.GROOVE)
        reg_frame.pack(fill=tk.X, **pad)

        # Grade 3×3 dentro do gol
        goal_grid = tk.Frame(reg_frame, bg="#181825")
        goal_grid.pack(padx=6, pady=(4, 2))

        for idx, (code, label) in enumerate(GOAL_REGIONS):
            row_g = idx // 3
            col_g = idx % 3
            btn = tk.Button(
                goal_grid, text=label, width=10,
                bg="#313244", fg=_COLOR_GOAL,
                activebackground="#45475a", activeforeground=_COLOR_GOAL,
                relief=tk.FLAT, font=("Helvetica", 8),
                cursor="hand2",
                command=lambda c=code: self._select_region(c)
            )
            btn.grid(row=row_g, column=col_g, padx=2, pady=2)
            self._region_buttons[code] = btn

        # Linha de separação
        tk.Label(reg_frame, text="— Fora do gol —", bg="#181825",
                  fg="#585b70", font=("Helvetica", 8)).pack(pady=(4, 2))

        # Fora: linha horizontal
        out_row = tk.Frame(reg_frame, bg="#181825")
        out_row.pack(padx=6, pady=(0, 6))

        for code, label in OUT_REGIONS:
            btn = tk.Button(
                out_row, text=label, width=10,
                bg="#313244", fg=_COLOR_OUT,
                activebackground="#45475a", activeforeground=_COLOR_OUT,
                relief=tk.FLAT, font=("Helvetica", 8),
                cursor="hand2",
                command=lambda c=code: self._select_region(c)
            )
            btn.pack(side=tk.LEFT, padx=2)
            self._region_buttons[code] = btn

        # Indicador da região selecionada
        self.lbl_selected_region = tk.Label(
            self, text="Região: nenhuma selecionada",
            bg="#181825", fg="#cba6f7", font=("Helvetica", 9, "italic")
        )
        self.lbl_selected_region.pack(pady=(0, 4))

        # ── Resultado: Gol ou Não Gol ──────────────────────────────────
        resultado_frame = tk.LabelFrame(self, text=" Resultado ", bg="#181825",
                                         fg="#a6adc8", font=("Helvetica", 9),
                                         bd=1, relief=tk.GROOVE)
        resultado_frame.pack(fill=tk.X, **pad)

        btn_row = tk.Frame(resultado_frame, bg="#181825")
        btn_row.pack(padx=6, pady=6)

        self.btn_gol = tk.Button(
            btn_row, text="⚽  GOL",
            bg="#313244", fg="#a6e3a1",
            activebackground="#45475a", activeforeground="#a6e3a1",
            font=("Helvetica", 10, "bold"), relief=tk.FLAT,
            padx=12, pady=5, cursor="hand2", width=10,
            command=lambda: self._set_goal(True)
        )
        self.btn_gol.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_nao_gol = tk.Button(
            btn_row, text="✖  NÃO GOL",
            bg="#313244", fg="#f38ba8",
            activebackground="#45475a", activeforeground="#f38ba8",
            font=("Helvetica", 10, "bold"), relief=tk.FLAT,
            padx=12, pady=5, cursor="hand2", width=10,
            command=lambda: self._set_goal(False)
        )
        self.btn_nao_gol.pack(side=tk.LEFT)

        self.lbl_resultado = tk.Label(
            resultado_frame, text="Resultado: não informado",
            bg="#181825", fg="#585b70", font=("Helvetica", 9, "italic")
        )
        self.lbl_resultado.pack(pady=(0, 4))

        # ── Observações ────────────────────────────────────────────────
        obs_frame = tk.LabelFrame(self, text=" Observações (opcional) ",
                                   bg="#181825", fg="#a6adc8",
                                   font=("Helvetica", 9), bd=1, relief=tk.GROOVE)
        obs_frame.pack(fill=tk.X, **pad)

        self.obs_text = tk.Text(
            obs_frame, height=3, bg="#313244", fg="#cdd6f4",
            insertbackground="#cdd6f4", relief=tk.FLAT,
            font=("Helvetica", 9), padx=4, pady=4
        )
        self.obs_text.pack(fill=tk.X, padx=4, pady=4)

        # ── Botão Salvar ───────────────────────────────────────────────
        self.btn_save = tk.Button(
            self, text="💾  Salvar Pênalti  (Enter)",
            command=self.trigger_save,
            bg="#89b4fa", fg="#1e1e2e",
            activebackground="#74c7ec", activeforeground="#1e1e2e",
            font=("Helvetica", 10, "bold"), relief=tk.FLAT,
            padx=10, pady=6, cursor="hand2"
        )
        self.btn_save.pack(fill=tk.X, padx=10, pady=(6, 4))

        # Botão Limpar
        tk.Button(
            self, text="✖  Limpar campos",
            command=self.reset_fields,
            bg="#313244", fg="#a6adc8",
            activebackground="#45475a", activeforeground="#cdd6f4",
            font=("Helvetica", 9), relief=tk.FLAT,
            padx=6, pady=3, cursor="hand2"
        ).pack(fill=tk.X, padx=10, pady=(0, 8))

    # ------------------------------------------------------------------
    # Seleção de resultado (gol / não gol)
    # ------------------------------------------------------------------
    def _set_goal(self, is_goal: bool):
        self._goal_selected = is_goal
        if is_goal:
            self.btn_gol.config(bg="#a6e3a1", fg="#1e1e2e")
            self.btn_nao_gol.config(bg="#313244", fg="#f38ba8")
            self.lbl_resultado.config(text="Resultado: ⚽ GOL", fg="#a6e3a1")
        else:
            self.btn_nao_gol.config(bg="#f38ba8", fg="#1e1e2e")
            self.btn_gol.config(bg="#313244", fg="#a6e3a1")
            self.lbl_resultado.config(text="Resultado: ✖ Não gol", fg="#f38ba8")

    # ------------------------------------------------------------------
    # Seleção de região
    # ------------------------------------------------------------------
    def _select_region(self, code: str):
        # Desseleciona anterior
        if self._selected_region and self._selected_region in self._region_buttons:
            old_btn = self._region_buttons[self._selected_region]
            is_goal = self._selected_region.startswith("gol_")
            old_btn.config(
                bg="#313244",
                fg=_COLOR_GOAL if is_goal else _COLOR_OUT
            )

        self._selected_region = code
        btn = self._region_buttons[code]
        btn.config(bg="#cba6f7", fg=_COLOR_SEL)   # destaque roxo

        # Nome legível para exibição
        all_regions = dict(GOAL_REGIONS + OUT_REGIONS)
        self.lbl_selected_region.config(
            text=f"Região: {all_regions.get(code, code)}"
        )

    # ------------------------------------------------------------------
    # Atualiza labels de marcação (chamado pelo App)
    # ------------------------------------------------------------------
    def refresh_marks(self):
        inicio = self.marks.get("inicio_frame")
        chute  = self.marks.get("chute_frame")
        self.lbl_inicio_mark.config(
            text=f"Início:  frame {inicio}" if inicio is not None else "Início:  —"
        )
        self.lbl_chute_mark.config(
            text=f"Chute:   frame {chute}" if chute is not None else "Chute:   —"
        )

    # ------------------------------------------------------------------
    # Salvar
    # ------------------------------------------------------------------
    def trigger_save(self):
        """Chamado pelo botão ou pelo atalho Enter."""
        if self._selected_region is None:
            from tkinter import messagebox
            messagebox.showwarning("Aviso", "Selecione a região do chute antes de salvar.")
            return

        if self._goal_selected is None:
            from tkinter import messagebox
            messagebox.showwarning("Aviso", "Informe se foi GOL ou NÃO GOL antes de salvar.")
            return

        label_data = {
            "camera_type":     self.camera_var.get(),
            "region":          self._selected_region,
            "is_goal":         self._goal_selected,
            "observations":    self.obs_text.get("1.0", tk.END).strip(),
        }
        self.on_save(label_data)

    # ------------------------------------------------------------------
    # Estado do botão durante exportação
    # ------------------------------------------------------------------
    def set_saving(self, saving: bool):
        if saving:
            self.btn_save.config(state=tk.DISABLED, text="⏳ Salvando…")
        else:
            self.btn_save.config(state=tk.NORMAL, text="💾  Salvar Pênalti  (Enter)")

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------
    def reset_fields(self):
        # Desseleciona região
        if self._selected_region and self._selected_region in self._region_buttons:
            old_btn = self._region_buttons[self._selected_region]
            is_goal = self._selected_region.startswith("gol_")
            old_btn.config(
                bg="#313244",
                fg=_COLOR_GOAL if is_goal else _COLOR_OUT
            )
        self._selected_region = None
        self.lbl_selected_region.config(text="Região: nenhuma selecionada")
        self.camera_var.set(CAMERA_TYPES[0])
        self.obs_text.delete("1.0", tk.END)
        # Reset resultado
        self._goal_selected = None
        self.btn_gol.config(bg="#313244", fg="#a6e3a1")
        self.btn_nao_gol.config(bg="#313244", fg="#f38ba8")
        self.lbl_resultado.config(text="Resultado: não informado", fg="#585b70")
        self.refresh_marks()
