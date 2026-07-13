"""Backfill histórico via páginas datadas do Notícias Agrícolas.

O site serve o fechamento de qualquer dia passado em `{url}/AAAA-MM-DD`
(1 fechamento por página, diferente da página atual que traz ~10). Este
script percorre os dias úteis de um intervalo e preenche a série de preços
usando a mesma ordem de fontes do config.py (exceto Cotrijal, que só tem
o dia corrente).

Salvaguardas:
- Só aceita cotação cuja data extraída da página == data pedida. Durante o
  congelamento do CMA (fev–jun/2026) as URLs datadas podem servir a última
  tabela velha; sem essa checagem o fallback nunca dispararia.
- Retomável: dias já presentes em data/raw/precos.csv são pulados, então
  interromper e rodar de novo continua de onde parou.
- Grava em lotes com o mesmo append+dedup do coletor diário.

Uso:
    python src/backfill.py                       # 2025-01-01 até ontem
    python src/backfill.py 2025-06-01            # início customizado
    python src/backfill.py 2025-06-01 2025-12-31 # intervalo fechado
"""

import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import COMMODITIES
from coletar import ARQ_PRECOS, baixar_html, extrair_cotacoes, gravar_incremental

INICIO_PADRAO = date(2025, 1, 1)  # alinhado ao início da série de clima
PAUSA_SEGUNDOS = 0.5              # cortesia com o servidor
LOTE_GRAVACAO = 20                # grava a cada N dias processados


def dias_uteis(inicio: date, fim: date):
    d = inicio
    while d <= fim:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def carregar_existentes() -> set[tuple[str, str]]:
    """Pares (data_iso, commodity) já na série — para retomada."""
    if not ARQ_PRECOS.exists():
        return set()
    df = pd.read_csv(ARQ_PRECOS, dtype=str)
    return set(zip(df["data"], df["commodity"]))


def coletar_dia(alvo: date, existentes: set, cache: dict) -> list[dict]:
    """Tenta cada commodity/fonte para uma data; devolve registros novos."""
    registros = []
    for commodity, cfg in COMMODITIES.items():
        if (alvo.isoformat(), commodity) in existentes:
            continue
        minimo, maximo = cfg["faixa_plausivel"]

        for fonte in cfg["fontes"]:
            if fonte.get("tipo") == "cotrijal":
                continue  # site da Cotrijal não expõe histórico
            # retry antes de desistir da fonte: um timeout transitório na
            # primária não pode misturar fontes na série (degrau de ~R$9
            # CMA×Cotrijal — ver analises/comparacao_cma_cotrijal.md)
            url = f"{fonte['url']}/{alvo.isoformat()}"
            cotacoes = None
            for tentativa in range(2):
                try:
                    if url not in cache:
                        cache[url] = baixar_html(url)
                        time.sleep(PAUSA_SEGUNDOS)
                    cotacoes = extrair_cotacoes(cache[url], fonte["praca_regex"])
                    break
                except Exception as e:
                    print(f"  [{commodity}] '{fonte['nome']}' falhou em {alvo} "
                          f"(tentativa {tentativa + 1}/2): {e}")
                    time.sleep(2)
            if cotacoes is None:
                continue

            # só a cotação do dia pedido conta: página datada pode servir
            # tabela velha (CMA congelado) ou redirecionar para outro dia
            validas = [p for d, p in cotacoes if d == alvo and minimo <= p <= maximo]
            if not validas:
                continue

            registros.append({
                "data": alvo.isoformat(),
                "commodity": commodity,
                "preco": validas[0],
                "fonte": fonte["nome"],
                "coletado_em": datetime.now().isoformat(timespec="seconds"),
            })
            break
    return registros


def main() -> int:
    inicio = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else INICIO_PADRAO
    fim = (date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2
           else date.today() - timedelta(days=1))

    existentes = carregar_existentes()
    print(f"=== Backfill {inicio} a {fim} ({len(existentes)} registros já na série) ===")

    pendentes: list[dict] = []
    processados = novos_total = 0
    for alvo in dias_uteis(inicio, fim):
        cache: dict[str, str] = {}  # por dia: soja e trigo compartilham página
        novos = coletar_dia(alvo, existentes, cache)
        pendentes += novos
        novos_total += len(novos)
        processados += 1

        if novos:
            resumo = ", ".join(f"{r['commodity']} R${r['preco']:.2f}" for r in novos)
            print(f"{alvo}: {resumo}")

        if processados % LOTE_GRAVACAO == 0 and pendentes:
            gravar_incremental(ARQ_PRECOS, pd.DataFrame(pendentes), ["data", "commodity"])
            pendentes = []

    if pendentes:
        gravar_incremental(ARQ_PRECOS, pd.DataFrame(pendentes), ["data", "commodity"])

    print(f"\nBackfill concluído: {novos_total} cotações novas em {processados} dias úteis.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
