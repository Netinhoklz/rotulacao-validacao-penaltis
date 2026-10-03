# Rotulação e validação de pênaltis

Ferramentas locais, no navegador, para montar um dataset de cobranças de pênalti a
partir de vídeos de melhores momentos: marcar o início e o chute de cada cobrança,
dizer para qual região do gol a bola foi, e depois conferir tudo contra o vídeo
original.

## Como usar

**1. Instale** (Python 3.10 ou mais novo):

```
pip install -r requirements.txt
```

**2. Aponte para os seus vídeos.** As pastas ficam nas primeiras linhas do
[`run.py`](run.py):

```python
PASTA_VIDEOS = r"E:\base_videos_penaltis"    # onde estão os vídeos (subpastas valem)
PASTA_SAIDA  = r"E:\penaltis_rotulados"      # onde os rótulos serão gravados
PASTA_CACHE  = r"E:\pasta_ref_mais_rapida"   # cache de navegação (qualquer pasta)
```

**3. Rode:**

```
python run.py
```

No Windows dá para dar dois cliques no `start.bat`. O navegador abre na tela de
rotulação. Se a pasta de vídeos estiver errada, o `run.py` avisa qual linha
corrigir.

## As telas

Todas sobem juntas; a barra do topo troca entre elas.

| tela | endereço | para quê |
|---|---|---|
| Rotulação | http://127.0.0.1:5000 | criar rótulos: início, chute, região, câmera, gol |
| Validação | http://127.0.0.1:5007 | conferir cada rótulo no vídeo original e corrigir |
| Painel | http://127.0.0.1:5009 | visão geral do dataset (só leitura) |

Se uma porta estiver ocupada, a tela vai para a próxima livre. Numa instalação nova
só a Rotulação aparece; Validação e Painel surgem depois do primeiro pênalti salvo
(rode o `run.py` de novo).

**Atalhos da rotulação:** `Espaço` play/pause · `←` `→` frame · `I` marca início ·
`F` marca chute · `Enter` salva · `E` régua das traves (4 cliques nos cantos do gol
dividem-no nas 9 regiões) · `T` só os traços · `R` recolhe a régua.

**Na Validação:** cada rótulo é conferido num passo a passo obrigatório, no
centro da tela: **1** confirmar o frame de início · **2** confirmar o frame final
(chute) · **3** assistir ao clip até o fim · **4** ver o vídeo (30 s a partir do
início) com a régua das traves sobre o gol · **5** confirmar região e gol. Cada
passo só libera o seguinte. `Enter` confirma o passo e abre o próximo; `←` `→`
andam 1 frame (`Shift` = 10); `PgUp` `PgDn` trocam de rótulo. Se um frame mudar,
o clip e os frames exportados são regerados a partir do vídeo original.

## Competição pelo nome da pasta

A competição de cada vídeo sai do nome das pastas do caminho:

- `...copa_do_brasil...` → Copa do Brasil
- `...brasileirao...` ou `videos_penaltis_2023` → Brasileirão
- qualquer outro nome → "outros"

Cada competição ganha o seu próprio `labels.csv` dentro de `PASTA_SAIDA`.

## O que é gravado onde

```
PASTA_SAIDA/
    labels.csv, clips/, frames/, labels/      rótulos (a rotulação escreve aqui)
    copa_do_brasil/...                        idem, por competição
    _revisao_manual/
        revisao.csv                           correções da validação (o original nunca é escrito)
        original/                             cópia de segurança dos labels.csv
        export/labels_revisado_*.csv          o dataset validado, gerado sob demanda
```

- `python revisao.py exportar` gera os CSVs validados; `python revisao.py caminhos`
  mostra onde estão e se estão em dia.
- `python separar_pendentes.py` copia os vídeos que ainda faltam rotular para uma
  pasta separada, por competição e ano.

## Opcional

- **ffmpeg** no PATH deixa a navegação frame a frame mais rápida.
- **Node.js** só é preciso para alterar a interface da rotulação: o build pronto já
  vem em `frontend/dist/`.
