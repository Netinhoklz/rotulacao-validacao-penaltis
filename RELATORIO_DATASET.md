# Dataset de Cobranças de Pênalti do Futebol Brasileiro

## Caracterização e validação da base rotulada

*Relatório gerado automaticamente por `analise_dataset.py` em 10/08/2026.*

---

## 1. Resumo

A base reúne **1034 cobranças de pênalti** anotadas manualmente a partir de **456 partidas** distintas do futebol brasileiro. Cada cobrança é descrita por três rótulos independentes — a região do chute (12 classes), o ângulo de câmera (3 classes) e o desfecho binário (gol / não-gol) — e acompanhada de um videoclipe recortado do instante da corrida até o impacto, mais os dois quadros extremos desse intervalo em resolução plena.

| Indicador | Valor |
| :--- | ---: |
| Cobranças anotadas | 1034 |
| Partidas distintas | 456 |
| Competições com anotação | 2 |
| Temporadas cobertas | 6 |
| Gols convertidos | 809 (78.2%) |
| IC 95% da taxa de conversão | [75.6% – 80.6%] |
| Média de cobranças por partida | 2.27 |
| Arquivos de mídia | 1034 clipes + 2068 quadros |

## 2. Procedência e cobertura

O acervo de origem é composto por vídeos de melhores momentos de partidas oficiais, organizados em pastas por competição e temporada. A anotação avança por partida, de modo que a cobertura é parcial e progressiva.

| Competição | Partidas na base | Partidas anotadas | Cobertura | Cobranças | Gols |
| :--- | ---: | ---: | ---: | ---: | ---: |
| Brasileirão | 395 | 390 | 98.7% | 807 | 643 |
| Copa do Brasil | 72 | 66 | 91.7% | 227 | 166 |

### 2.1 Distribuição por temporada

| Temporada | Partidas na base | Anotadas | Cobertura | Cobranças | Gols | Conversão |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2020 | 83 | 83 | 100.0% | 188 | 152 | 80.9% |
| 2021 | 71 | 70 | 98.6% | 132 | 107 | 81.1% |
| 2022 | 23 | 23 | 100.0% | 47 | 34 | 72.3% |
| 2023 | 92 | 91 | 98.9% | 187 | 150 | 80.2% |
| 2024 | 61 | 59 | 96.7% | 126 | 99 | 78.6% |
| 2025 | 65 | 64 | 98.5% | 127 | 101 | 79.5% |

A tabela cobre apenas as partidas cuja pasta de origem identifica a temporada. Outras **227 cobranças** (22.0%) vêm de acervos sem essa marcação e não aparecem acima — o total por competição da seção anterior permanece completo.

### 2.2 Diversidade de confrontos

O nome de arquivo de cada partida segue o padrão `MANDANTE P X P VISITANTE MELHORES MOMENTOS …`, o que permite extrair automaticamente os clubes envolvidos. O padrão foi reconhecido em **436 das 456 partidas anotadas** (95.6%); as demais usam notação de disputa por pênaltis e foram desconsideradas nesta subseção.

| Indicador | Valor |
| :--- | ---: |
| Partidas com confronto identificado | 436 |
| Clubes distintos | 49 |
| Aparições por clube (mediana) | 9.0 |
| Clube mais frequente | GREMIO (58 partidas) |
| Rodadas distintas | 38 |

Dez clubes mais representados:

| Clube | Partidas anotadas |
| :--- | ---: |
| GREMIO | 58 |
| ATLETICO MG | 54 |
| FLAMENGO | 53 |
| FORTALEZA | 50 |
| INTERNACIONAL | 49 |
| CORINTHIANS | 48 |
| FLUMINENSE | 46 |
| SANTOS | 43 |
| PALMEIRAS | 43 |
| SAO PAULO | 39 |

## 3. Taxonomia da anotação

Cada cobrança recebe três rótulos independentes, aplicados sobre o mesmo recorte temporal:

1. **Região do chute** — grade 3×3 sobre a baliza (nove zonas internas), acrescida de três zonas externas para finalizações que não encontram o gol. A referência espacial é a imagem exibida, não o lado do cobrador.
2. **Ângulo de câmera** — perspectiva predominante da tomada no instante do chute.
3. **Desfecho** — variável binária indicando se a cobrança resultou em gol.

O recorte temporal é delimitado por dois quadros marcados manualmente: o início da corrida do cobrador e o instante do impacto com a bola.

## 4. Distribuição das classes

### 4.1 Região do chute

| Região | Código | n | % | Gols | Não-gols | Conversão | IC 95% |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Gol — superior esquerdo | `gol_topo_esquerdo` | 81 | 7.8% | 81 | 0 | 100.0% | [95–100] |
| Gol — superior central | `gol_topo_centro` | 47 | 4.5% | 45 | 2 | 95.7% | [86–99] |
| Gol — superior direito | `gol_topo_direito` | 58 | 5.6% | 58 | 0 | 100.0% | [94–100] |
| Gol — meio esquerdo | `gol_meio_esquerdo` | 82 | 7.9% | 65 | 17 | 79.3% | [69–87] |
| Gol — meio central | `gol_meio_centro` | 36 | 3.5% | 29 | 7 | 80.6% | [65–90] |
| Gol — meio direito | `gol_meio_direito` | 82 | 7.9% | 73 | 9 | 89.0% | [80–94] |
| Gol — inferior esquerdo | `gol_baixo_esquerdo` | 286 | 27.7% | 224 | 62 | 78.3% | [73–83] |
| Gol — inferior central | `gol_baixo_centro` | 46 | 4.4% | 33 | 13 | 71.7% | [57–83] |
| Gol — inferior direito | `gol_baixo_direito` | 245 | 23.7% | 201 | 44 | 82.0% | [77–86] |
| Fora — esquerda | `fora_esquerda` | 20 | 1.9% | 0 | 20 | 0.0% | [0–16] |
| Fora — por cima | `fora_cima` | 36 | 3.5% | 0 | 36 | 0.0% | [0–10] |
| Fora — direita | `fora_direita` | 15 | 1.5% | 0 | 15 | 0.0% | [0–20] |

**Desbalanceamento.** A distribuição é fortemente assimétrica:

| Métrica | Valor | Interpretação |
| :--- | ---: | :--- |
| Razão de desbalanceamento (IR) | 19.1:1 | classe mais frequente (gol_baixo_esquerdo) vs. mais rara (fora_direita) |
| Entropia de Shannon | 3.04 bits | máximo possível: 3.58 bits |
| Entropia normalizada | 0.849 | 1,0 = classes equiprováveis |
| Número efetivo de classes | 8.3 | de 12 classes observadas |
| Coeficiente de Gini | 0.457 | 0 = uniforme |

#### Agregações da grade

Das 1034 cobranças, **963** (93.1%) foram no alvo e **71** (6.9%) saíram da meta. As agregações abaixo tomam apenas as 963 finalizações no alvo, para que altura e lateralidade permaneçam comparáveis entre si.

| Altura (no alvo) | n | % do alvo | % do total |
| :--- | ---: | ---: | ---: |
| Inferior | 577 | 59.9% | 55.8% |
| Meio | 200 | 20.8% | 19.3% |
| Superior | 186 | 19.3% | 18.0% |

| Lateralidade (no alvo) | n | % do alvo | % do total |
| :--- | ---: | ---: | ---: |
| Esquerda | 449 | 46.6% | 43.4% |
| Direita | 385 | 40.0% | 37.2% |
| Centro | 129 | 13.4% | 12.5% |

| Finalização fora da meta | n | % do total |
| :--- | ---: | ---: |
| Fora — por cima | 36 | 3.5% |
| Fora — esquerda | 20 | 1.9% |
| Fora — direita | 15 | 1.5% |

### 4.2 Ângulo de câmera

| Ângulo | n | % | Gols | Conversão |
| :--- | ---: | ---: | ---: | ---: |
| visão do torcedor | 646 | 62.5% | 488 | 75.5% |
| visão cobrador | 224 | 21.7% | 181 | 80.8% |
| visão goleiro | 164 | 15.9% | 140 | 85.4% |

Entropia normalizada de 0.835 — a distribuição também é desigual, com predominância de `visão do torcedor`.

### 4.3 Desfecho

| Desfecho | n | % | IC 95% |
| :--- | ---: | ---: | ---: |
| Gol | 809 | 78.2% | [75.6% – 80.6%] |
| Não-gol | 225 | 21.8% | [19.4% – 24.4%] |

Entre as **225 cobranças não convertidas**, **71** terminaram fora da meta e **154** foram finalizações no alvo que não resultaram em gol (defesa do goleiro ou trave).

### 4.4 Tabulação cruzada — região × ângulo de câmera

| Região | visão do torcedor | visão cobrador | visão goleiro | Total |
| :--- | ---: | ---: | ---: | ---: |
| Gol — superior esquerdo | 45 | 22 | 14 | 81 |
| Gol — superior central | 27 | 11 | 9 | 47 |
| Gol — superior direito | 32 | 15 | 11 | 58 |
| Gol — meio esquerdo | 47 | 18 | 17 | 82 |
| Gol — meio central | 24 | 6 | 6 | 36 |
| Gol — meio direito | 49 | 21 | 12 | 82 |
| Gol — inferior esquerdo | 177 | 58 | 51 | 286 |
| Gol — inferior central | 34 | 9 | 3 | 46 |
| Gol — inferior direito | 159 | 52 | 34 | 245 |
| Fora — esquerda | 13 | 5 | 2 | 20 |
| Fora — por cima | 28 | 5 | 3 | 36 |
| Fora — direita | 11 | 2 | 2 | 15 |
| **Total** | 646 | 224 | 164 | 1034 |

## 5. Propriedades dos clipes

Duração do recorte (início da corrida → impacto), em segundos:

| Estatística | Valor (s) |
| :--- | ---: |
| n | 1034 |
| Média | 1.29 |
| Desvio-padrão | 0.84 |
| Mínimo | 0.03 |
| 1º quartil | 0.90 |
| Mediana | 1.13 |
| 3º quartil | 1.43 |
| Máximo | 9.31 |

| Faixa (s) | n | % |
| :--- | ---: | ---: |
| 0.0 – 0.5 | 57 | 5.5% |
| 0.5 – 1.0 | 263 | 25.4% |
| 1.0 – 1.5 | 474 | 45.8% |
| 1.5 – 2.0 | 129 | 12.5% |
| 2.0 – 3.0 | 75 | 7.3% |
| ≥ 3.0 | 36 | 3.5% |

### 5.1 Características técnicas verificadas na decodificação

| Resolução | Clipes | % |
| :--- | ---: | ---: |
| 1920x1080 | 779 | 75.3% |
| 1280x720 | 252 | 24.4% |
| 3840x2160 | 3 | 0.3% |

| Taxa de quadros | Clipes | % |
| :--- | ---: | ---: |
| 29.97 fps | 786 | 76.0% |
| 30.00 fps | 200 | 19.3% |
| 29.95 fps | 20 | 1.9% |
| 29.94 fps | 12 | 1.2% |
| 25.00 fps | 5 | 0.5% |
| 23.98 fps | 5 | 0.5% |
| 59.94 fps | 4 | 0.4% |
| 50.00 fps | 2 | 0.2% |

### 5.2 Volume de dados

| Componente | Arquivos | Tamanho |
| :--- | ---: | ---: |
| Videoclipes | 1034 | 0.81 GB |
| Quadros extremos | 2068 | 1.18 GB |
| **Total** | 3102 | **1.99 GB** |

## 6. Processo de anotação

| Cobranças na partida | Partidas | % |
| :--- | ---: | ---: |
| 1 | 124 | 27.2% |
| 2 | 221 | 48.5% |
| 3 | 64 | 14.0% |
| 4 | 30 | 6.6% |
| 5 | 6 | 1.3% |
| 6 | 2 | 0.4% |
| 8 | 3 | 0.7% |
| 10 | 1 | 0.2% |
| 12 | 2 | 0.4% |
| 13 | 1 | 0.2% |
| 21 | 1 | 0.2% |
| 22 | 1 | 0.2% |

Média de 2.27 cobranças por partida (mediana 2, máximo 22).

### 6.1 Esforço de anotação

| Indicador | Valor |
| :--- | ---: |
| Primeira anotação | 08/04/2026 16:31 |
| Última anotação | 10/08/2026 06:47 |
| Período total | 123 dias |
| Dias com atividade | 21 |
| Sessões de trabalho (intervalo > 30 min) | 36 |
| Cobranças por dia ativo (mediana) | 34 |
| Dia mais produtivo | 20/07/2026 (137 cobranças) |
| Intervalo mediano entre anotações | 44 s |

## 7. Validação da integridade

Todos os **1034 registros** foram verificados individualmente. Cada clipe foi aberto e teve o primeiro quadro efetivamente decodificado; cada um dos **2068 quadros extremos** foi decodificado por completo. Não se trata de conferência de listagem de diretório: um arquivo presente porém corrompido é reprovado nesta etapa.

| Verificação | Resultado | Ocorrências |
| :--- | :--- | ---: |
| Referência do CSV com mídia ausente ou vazia | ✔ conforme | 0 |
| Clipe que não abre | ✔ conforme | 0 |
| Clipe sem quadros decodificáveis | ✔ conforme | 0 |
| Duração do clipe divergente da marcação (> ±2 quadros) | ✔ conforme | 0 |
| Quadro extremo corrompido | ✔ conforme | 0 |
| Arquivo em disco sem registro no CSV | ✔ conforme | 0 |
| Registro no CSV sem o JSON correspondente | ✔ conforme | 0 |
| Marcação temporal incoerente (início ≥ chute) | ✔ conforme | 0 |
| Região fora do vocabulário de 12 classes | ✔ conforme | 0 |
| Ângulo de câmera fora do vocabulário | ✔ conforme | 0 |
| Contradição lógica: chute fora da meta registrado como gol | ✔ conforme | 0 |
| Par (partida, identificador) duplicado | ✔ conforme | 0 |

**Nenhuma inconsistência.** Os 1034 registros do CSV, os 1034 arquivos de metadados, os 1034 clipes e os 2068 quadros estão em correspondência exata e íntegros.

A diferença entre a duração real do clipe e o intervalo declarado tem média de +0.02 quadro(s) (mediana +0, amplitude -1 a +1). O desvio é esperado: o corte é feito por tempo pelo codificador, e não por índice exato de quadro.

## 8. Limitações

- **Desbalanceamento acentuado de classes.** Com IR de 19.1:1 e apenas 8.3 classes efetivas de 12, treinar classificadores de região exige reamostragem, ponderação da função de perda ou agrupamento de zonas.
- **Viés de seleção pela fonte.** Os vídeos são compilações de melhores momentos, que privilegiam lances decisivos. A taxa de conversão observada não deve ser lida como estimativa populacional da conversão de pênaltis na competição.
- **Anotador único.** Todos os rótulos vêm de um mesmo anotador, o que impede o cálculo de concordância entre avaliadores. A consistência interna é assegurada por verificação automática de vocabulário e de coerência lógica, não por dupla anotação.
- **Referencial espacial da grade.** As zonas são definidas sobre a imagem exibida. Em tomadas de trás da meta, a lateralidade fica espelhada em relação à perspectiva do cobrador.

## 9. Reprodutibilidade

Este relatório é gerado inteiramente por código, a partir dos artefatos em disco. Para reproduzi-lo:

```bash
python analise_dataset.py                 # relatório completo, com decodificação
python analise_dataset.py --rapido        # sem decodificar as mídias
python dataset_tools.py check             # apenas a verificação de integridade
python dataset_tools.py stats             # contagens resumidas por competição
```

### Organização dos arquivos

```
E:\penaltis_rotulados\
    (raiz)                     -> Brasileirão
    clips\<partida>_penalti_<NNN>.mp4
    frames\<partida>_penalti_<NNN>_{inicio,chute}.jpg
    labels\<partida>_penalti_<NNN>.json
    labels.csv
    copa_do_brasil\                     -> Copa do Brasil
    copa_do_brasil\clips\<partida>_penalti_<NNN>.mp4
    copa_do_brasil\frames\<partida>_penalti_<NNN>_{inicio,chute}.jpg
    copa_do_brasil\labels\<partida>_penalti_<NNN>.json
    copa_do_brasil\labels.csv
```

Campos comuns a todos os arquivos `labels.csv`:

`video_file`, `penalty_id`, `inicio_frame`, `chute_frame`, `inicio_time_s`, `chute_time_s`, `camera_type`, `region`, `region_label`, `is_goal`, `observations`, `timestamp_rotulagem`, `clip_path`, `frame_inicio_path`, `frame_chute_path`.

A segmentação por competição foi introduzida após o início da anotação, de modo que os arquivos não têm exatamente o mesmo conjunto de colunas:

| Arquivo | Colunas | Campos adicionais |
| :--- | ---: | :--- |
| `penaltis_rotulados/labels.csv` | 15 | — |
| `copa_do_brasil/labels.csv` | 18 | `competition`, `competition_label`, `source_folder` |

Para o registro da raiz a competição é implícita pela localização do arquivo; o código de leitura em `competitions.py` resolve os dois casos.

---

*1034 cobranças · 456 partidas · gerado em 10/08/2026.*