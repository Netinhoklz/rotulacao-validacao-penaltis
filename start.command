#!/bin/bash
# Atalho de duplo clique para o macOS. O lancador de verdade e o run.py:
# as pastas (videos, saida, cache) se ajustam nas primeiras linhas dele.
#
# Primeira vez: botao direito > Abrir (o Gatekeeper pede confirmacao). Se o
# Finder reclamar de permissao, no Terminal:  chmod +x start.command
cd "$(dirname "$0")" || exit 1
python3 run.py "$@" || read -r -p "Deu erro acima. Enter para fechar."
