"""Transformação analítica com DuckDB (substitui as views do BigQuery da v1).

Lê os CSVs brutos, monta a série unificada clima × preços e exporta:
- data/processed/serie_completa.csv  (consumo analítico / notebooks)
- docs/dados.json                    (consumido pelo dashboard estático)
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from config import COMMODITIES as CFG_COMMODITIES, URL_COTRIJAL
ARQ_PRECOS = RAIZ / "data" / "raw" / "precos.csv"
ARQ_CLIMA = RAIZ / "data" / "raw" / "clima.csv"
ARQ_SERIE = RAIZ / "data" / "processed" / "serie_completa.csv"
ARQ_JSON = RAIZ / "docs" / "dados.json"

COMMODITIES = ["milho", "soja", "trigo"]
SERIES_PRECO = COMMODITIES + ["dolar"]  # dólar entra nas séries/resumo/cruzadas,
                                        # mas fica fora das correlações clima→preço


def main() -> int:
    if not ARQ_PRECOS.exists():
        print("Sem data/raw/precos.csv — rode src/coletar.py primeiro.")
        return 1

    con = duckdb.connect()  # em memória; os CSVs no git são a fonte da verdade

    con.execute(f"""
        CREATE VIEW precos AS
        SELECT CAST(data AS DATE) AS data, commodity, CAST(preco AS DOUBLE) AS preco, fonte
        FROM read_csv_auto('{ARQ_PRECOS.as_posix()}');

        CREATE VIEW clima AS
        SELECT CAST(data AS DATE) AS data,
               CAST(temp_max AS DOUBLE) AS temp_max,
               CAST(chuva_mm AS DOUBLE) AS chuva_mm
        FROM read_csv_auto('{ARQ_CLIMA.as_posix()}');
    """)

    # Série unificada: um registro por dia, preços em colunas (formato wide).
    # FULL JOIN preserva dias com clima e sem cotação (fins de semana) e vice-versa.
    tem_dolar = con.execute(
        "SELECT COUNT(*) FROM precos WHERE commodity = 'dolar'").fetchone()[0] > 0
    col_dolar = "w.dolar AS dolar_ptax," if tem_dolar else "NULL AS dolar_ptax,"
    serie = con.execute(f"""
        WITH wide AS (
            PIVOT precos ON commodity USING first(preco) GROUP BY data
        )
        SELECT COALESCE(c.data, w.data) AS data,
               c.chuva_mm, c.temp_max, {col_dolar}
               w.milho AS preco_milho, w.soja AS preco_soja, w.trigo AS preco_trigo
        FROM clima c
        FULL JOIN wide w USING (data)
        ORDER BY data
    """).fetchdf()

    ARQ_SERIE.parent.mkdir(parents=True, exist_ok=True)
    serie.to_csv(ARQ_SERIE, index=False)
    print(f"[transform] {ARQ_SERIE.name}: {len(serie)} dias "
          f"({serie['data'].min():%Y-%m-%d} a {serie['data'].max():%Y-%m-%d})")

    # ---------- payload do dashboard ----------
    payload = {
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
        "precos": {},
        "clima": {},
        "resumo": {},
        "correlacoes": {},
        # nome da fonte -> URL base; o dashboard monta o link datado
        # (url + /AAAA-MM-DD) para provar a origem de cada ponto do gráfico
        "fontes_urls": {
            f["nome"]: f.get("url", URL_COTRIJAL)
            for cfg in CFG_COMMODITIES.values() for f in cfg["fontes"]
        },
    }

    for c in SERIES_PRECO:
        df = con.execute(
            "SELECT data, preco, fonte FROM precos WHERE commodity = ? ORDER BY data", [c]
        ).fetchdf()
        if df.empty:
            continue
        casas = 4 if c == "dolar" else 2  # PTAX tem 4 casas significativas
        payload["precos"][c] = {
            "datas": df["data"].dt.strftime("%Y-%m-%d").tolist(),
            "valores": df["preco"].round(casas).tolist(),
            "fontes": df["fonte"].tolist(),
        }
        ultimo = df.iloc[-1]
        base_30d = df[df["data"] >= ultimo["data"] - pd.Timedelta(days=30)].iloc[0]
        payload["resumo"][c] = {
            "ultimo": round(float(ultimo["preco"]), casas),
            "data": ultimo["data"].strftime("%Y-%m-%d"),
            "var_30d_pct": round(
                (float(ultimo["preco"]) / float(base_30d["preco"]) - 1) * 100, 1
            ) if float(base_30d["preco"]) else None,
        }

    clima = con.execute("SELECT data, chuva_mm, temp_max FROM clima ORDER BY data").fetchdf()
    if not clima.empty:
        payload["clima"] = {
            "datas": clima["data"].dt.strftime("%Y-%m-%d").tolist(),
            "chuva_mm": clima["chuva_mm"].round(1).tolist(),
            "temp_max": clima["temp_max"].round(1).tolist(),
        }

    # Correlação de Pearson preço × clima acumulado em 30 dias — mesma linha
    # analítica da v1, agora computada no DuckDB a cada atualização.
    for c in COMMODITIES:
        r = con.execute(f"""
            WITH clima_30d AS (
                SELECT data,
                       SUM(chuva_mm) OVER (ORDER BY data ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS chuva_30d,
                       AVG(temp_max) OVER (ORDER BY data ROWS BETWEEN 29 PRECEDING AND CURRENT ROW) AS temp_30d
                FROM clima
            )
            SELECT corr(p.preco, c.chuva_30d) AS r_chuva,
                   corr(p.preco, c.temp_30d)  AS r_temp,
                   COUNT(*) AS n
            FROM precos p JOIN clima_30d c USING (data)
            WHERE p.commodity = '{c}'
        """).fetchone()
        if r and r[2] and r[2] >= 10:  # só publica com amostra mínima
            payload["correlacoes"][c] = {
                "chuva_30d": round(r[0], 3) if r[0] is not None else None,
                "temp_30d": round(r[1], 3) if r[1] is not None else None,
                "n": r[2],
            }

    # Correlações cruzadas entre séries (retornos diários, não níveis: níveis
    # de séries com tendência correlacionam por construção — retorno é o que
    # separa "andam juntas" de "só sobem juntas"). Duas janelas: história
    # completa e últimos 90 dias, para expor mudança de regime.
    wide = con.execute("""
        PIVOT precos ON commodity USING first(preco) GROUP BY data ORDER BY data
    """).fetchdf().set_index("data")
    presentes = [c for c in SERIES_PRECO if c in wide.columns]
    # dias úteis + ffill curto: retorno de segunda usa sexta como base, e a
    # série administrada (parada) vira retorno 0 em vez de buraco
    ret = (wide[presentes].asfreq("B").ffill(limit=5)
           .pct_change().dropna(how="all"))
    payload["cruzadas"] = {"pares": [], "janelas": ["completa", "90d"]}
    for i, a in enumerate(presentes):
        for b in presentes[i + 1:]:
            par = {"a": a, "b": b, "valores": []}
            for janela in (ret, ret.tail(63)):  # ~90 dias corridos = 63 úteis
                amostra = janela[[a, b]].dropna()
                # descarta dias em que ambos ficaram parados (0×0 infla n
                # sem informação) — só conta dia com movimento em ao menos um
                amostra = amostra[(amostra[a] != 0) | (amostra[b] != 0)]
                r = amostra[a].corr(amostra[b]) if len(amostra) >= 20 else None
                par["valores"].append(round(float(r), 2) if pd.notna(r) else None)
            par["n"] = int(len(amostra))
            payload["cruzadas"]["pares"].append(par)
    if payload["cruzadas"]["pares"]:
        print(f"[transform] correlações cruzadas: {len(payload['cruzadas']['pares'])} pares")

    ARQ_JSON.parent.mkdir(parents=True, exist_ok=True)
    ARQ_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[transform] {ARQ_JSON.name} atualizado para o dashboard")
    return 0


if __name__ == "__main__":
    sys.exit(main())
