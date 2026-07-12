# CMA (Passo Fundo) × Cotrijal (Não-Me-Toque): comercial ou metodológico?

Comparação semanal (quartas-feiras, out/2025–jul/2026, n=41 semanas, 25 pares
completos) do preço da saca de milho nas duas fontes do pipeline, coletada das
páginas datadas do Notícias Agrícolas (`/cotacoes/.../AAAA-MM-DD`). Dados em
[`comparacao_cma_cotrijal_milho.csv`](comparacao_cma_cotrijal_milho.csv).

## Resultados

| Métrica | CMA · Passo Fundo | Cotrijal · Não-Me-Toque |
|---|---|---|
| Semanas sem cotação | 16/41 (congelamento fev–jun + feriados) | 2/41 (só feriados de fim de ano) |
| Semanas sem mudança de preço | 46% | 75% |
| Valores distintos em 10 meses | 10 | 5 |
| Passo médio quando muda | R$ 1,27 (granularidade 0,50) | R$ 1,00 (sempre inteiro) |

- **Spread CMA − Cotrijal:** média **R$ 8,86 (~15%)**, dp R$ 1,51, faixa 5,00–10,50
- **Correlação de níveis:** r = 0,78 · **de variações semanais:** r = 0,53
- **Beta (ΔCotrijal ~ ΔCMA): 0,19** — cada R$ 1 de movimento no indicador vira ~R$ 0,19 no balcão na mesma semana
- **Spread não é constante:** comprimiu até R$ 5,00 no fundo do mercado (fev/26) e reabriu para ~R$ 10 na retomada — o balcão amortece quedas E altas

## Conclusão

As duas coisas são verdade, em camadas:

1. **Camada comercial (nível):** existe um basis de ~R$ 9/saca (~15%) explicável
   por natureza do preço — indicador de mercado vs. preço de compra de balcão
   (margem da cooperativa, frete, praça).
2. **Camada metodológica (dinâmica):** a Cotrijal é um **preço administrado** —
   fica semanas parada, move em degraus de R$ 1,00 e absorve só ~19% do movimento
   do mercado na semana; o CMA é um **indicador acompanhando mercado** — move mais
   vezes, em passos de R$ 0,50, e amplifica quedas. Se fosse só margem comercial
   constante, o beta seria ~1 e o spread estável; não é o caso.

## Implicação para o pipeline e a econometria

- **Nunca misturar as fontes na mesma série sem dummy/ajuste**: um failover
  CMA→Cotrijal injeta um degrau de ~−R$ 9 que viraria "choque de preço" espúrio
  em qualquer regressão. A coluna `fonte` do CSV existe para isso.
- Para modelos clima→preço, o **CMA é o sinal preferível** (reage a mercado);
  a Cotrijal subestimaria a elasticidade por ser suavizada (atenuação de ~80%
  nos choques semanais).
- A Cotrijal é superior em **continuidade** (atravessou o congelamento do CMA
  inteiro) — papel ideal: fallback de disponibilidade e série de validação, não
  substituta silenciosa.
