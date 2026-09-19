@echo off
rem Atalho de duplo clique para o Windows. O lancador de verdade e o run.py:
rem as pastas (videos, saida, cache) se ajustam nas primeiras linhas dele.
cd /d "%~dp0"
python run.py %*
if errorlevel 1 pause
