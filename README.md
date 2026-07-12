# Monitor Agro RS — Preços de Commodities × Clima (Passo Fundo/RS)

Pipeline diário que coleta preços do mercado físico de **milho, soja e trigo** em praças
do Planalto Médio gaúcho e cruza com **clima observado** (chuva e temperatura máxima) de
Passo Fundo/RS. Sucessor do [monitor-clima-pf](https://github.com/henriquereolonpain-sys/monitor-clima-pf),
redesenhado a partir das lições aprendidas na v1.

**Dashboard:** publicado via GitHub Pages a partir de [`docs/`](docs/) · atualizado automaticamente em dias úteis.

---

## Por que uma v2? (postmortem da v1)

A v1 morreu silenciosamente: um refactor removeu o `from io import StringIO` que o scraper
usava, e o `NameError` resultante era engolido por um `try/except` genérico que apenas dava
`print` no erro. O GitHub Actions ficou **verde por meses** enquanto nenhum dado era coletado.
Outros problemas de projeto: `if_exists='replace'` apagava o histórico de clima a cada run,
o endpoint de *forecast* misturava previsão com observação, e `append` sem deduplicação
duplicava cotações.

Cada decisão da v2 responde a um desses erros:

| Problema na v1 | Solução na v2 |
|---|---|
| Erro engolido, Actions verde com pipeline morto | Coleta sem dado = **exit 1** → Actions vermelho → e-mail |
| Fonte única (page única do CMA) | **Fallback**: lista ordenada de fontes por commodity |
| Um dia perdido = buraco permanente na série | Páginas trazem ~10 fechamentos → **auto-backfill** a cada run |
| `replace` destruía o histórico de clima | Escrita **incremental idempotente** (append + dedup por chave) |
| Previsão misturada com observação | Só **Open-Meteo Archive (ERA5)** — dado observado, com defasagem respeitada |
| Data da cotação = data do run (errado em fins de semana) | Data extraída do **"Fechamento: dd/mm/aaaa"** da própria página |
| Preço com layout mudado gravava lixo | **Faixa de plausibilidade** por commodity descarta valores absurdos |
| Fonte congelada passava despercebida (CMA ficou ~4 meses sem atualizar em 2026 e a página seguia no ar) | **Detector de estagnação**: cotação mais nova com >7 dias = fonte morta → fallback; todas mortas → alerta |
| BigQuery + service account + Looker para 3 séries | **CSV versionado no git + DuckDB + dashboard estático** — zero credencial, zero custo |

## Arquitetura

```
GitHub Actions (dias úteis, 18h BRT)
  └─ src/coletar.py
       ├─ Notícias Agrícolas (scraping, ~10 fechamentos/página, com fallback)
       ├─ Open-Meteo Archive API (clima observado, incremental)
       └─ grava data/raw/*.csv  (append + dedup → git é o banco de dados)
  └─ src/transformar.py
       ├─ DuckDB: join clima × preços, pivot, correlações móveis
       ├─ data/processed/serie_completa.csv
       └─ docs/dados.json  → dashboard estático (GitHub Pages)
  └─ commit & push dos dados atualizados
```

O padrão é o de **git scraping**: o repositório é ao mesmo tempo código, banco de dados e
histórico auditável — cada commit diário documenta o estado da fonte naquele dia.

## Fontes

| Série | Fonte primária | Fallbacks (em ordem) |
|---|---|---|
| Milho | Notícias Agrícolas · Milho CMA · praça Passo Fundo/RS | NA · Sindicatos/Cooperativas · Não-Me-Toque/RS → site da Cotrijal |
| Soja | NA · Sindicatos e Cooperativas · Não-Me-Toque/RS (Cotrijal) | mesma página · Nonoai/RS → site da Cotrijal |
| Trigo | NA · Trigo Mercado Físico · Não-Me-Toque/RS (Cotrijal) | mesma página · Nonoai/RS → site da Cotrijal |
| Clima | Open-Meteo Archive API (ERA5) · lat/lon de Passo Fundo | — |

O último fallback é o **site oficial da Cotrijal** (cotrijal.com.br), que embute as cotações
do dia num JSON server-side — independência total do domínio Notícias Agrícolas. Limitações:
só o dia corrente (sem backfill) e vazio em dias sem pregão ("Mercado Fechado"), por isso é
o último da fila e não a primária.

A coluna `fonte` em `data/raw/precos.csv` registra qual fonte forneceu cada linha, então
trocas de fonte ficam documentadas na própria série.

## Rodando localmente

```bash
pip install -r requirements.txt
python src/coletar.py      # coleta preços + clima → data/raw/
python src/transformar.py  # DuckDB → data/processed/ + docs/dados.json
python -m http.server 8000 --directory docs   # dashboard em http://localhost:8000
```

Não há credencial nenhuma para configurar — era um dos objetivos.

## Configuração no GitHub

1. Crie o repositório e faça push.
2. **Actions**: já funciona — o workflow [`coleta_diaria.yml`](.github/workflows/coleta_diaria.yml)
   roda em dias úteis às 18h (BRT) e commita os dados. Rode manualmente pela aba Actions
   (workflow_dispatch) para testar.
3. **Pages**: Settings → Pages → Source: *Deploy from a branch* → branch `main`, pasta `/docs`.

## Importando o histórico da v1

A série antiga de milho (BigQuery) pode ser incorporada exportando a tabela como CSV e
convertendo para o schema de `data/raw/precos.csv`
(`data,commodity,preco,fonte,coletado_em`) — a deduplicação por `(data, commodity)` cuida
de eventuais sobreposições:

```sql
-- no BigQuery
SELECT CAST(data AS DATE) AS data, 'milho' AS commodity,
       preco_saca_reais AS preco, 'v1 · BigQuery histórico' AS fonte,
       CURRENT_TIMESTAMP() AS coletado_em
FROM `monitor-passofundo.clima_dados.precos_milho_cepea`
ORDER BY data
```

Baixe o resultado como CSV, concatene ao `data/raw/precos.csv` e rode `src/transformar.py`.

## Roadmap analítico

- [ ] Backfill do histórico da v1 (milho desde 2025)
- [ ] Correlações cruzadas entre commodities (milho × soja competem por área plantada)
- [ ] Modelos econométricos: defasagens distribuídas de chuva sobre preço, sazonalidade
- [ ] Câmbio USD/BRL como variável exógena (API do BCB/SGS)
