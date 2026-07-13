"""Previsão de preço a partir de clima: ridge sobre variações diárias.

O objetivo de projeto (herdado da v1): estimar o preço da saca nos próximos
dias dado o que se sabe de chuva e temperatura — treinando com clima
OBSERVADO (ERA5) e projetando com a PREVISÃO da Open-Meteo (16 dias).
A previsão de clima nunca toca os CSVs (princípio da v2: observação e
previsão não se misturam no banco); ela vive só neste script e no JSON
do dashboard.

Escolhas de modelagem, na ordem do que importa:
- O alvo é a VARIAÇÃO diária (Δpreço), não o nível. Séries de balcão são
  administradas (ficam dias paradas); prever nível deixaria o modelo
  aprender a identidade. A regularização do ridge puxa os coeficientes
  para zero = "preço não muda", que é exatamente o baseline honesto.
- Dummy de fonte fallback absorve o degrau de ~R$9 do bloco em que o CMA
  congelou e o milho veio da Cotrijal/Não-Me-Toque (fev–jun/2026), sem
  descartar esses 70 dias de dinâmica.
- Backtest walk-forward SEM re-treino contra o baseline ingênuo, publicado
  no JSON: se o modelo não bate "preço fica parado", o dashboard mostra.
- Ridge fechado em numpy puro — nenhuma dependência nova.

Saída: docs/previsao.json (consumido pelo dashboard ao lado de dados.json).
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from config import FUSO, LATITUDE, LONGITUDE

ARQ_PRECOS = RAIZ / "data" / "raw" / "precos.csv"
ARQ_CLIMA = RAIZ / "data" / "raw" / "clima.csv"
ARQ_SAIDA = RAIZ / "docs" / "previsao.json"

COMMODITIES = ["milho", "soja", "trigo"]
HORIZONTE_DIAS_UTEIS = 10   # limitado pelos 16 dias do forecast Open-Meteo
RIDGE_LAMBDA = 5.0
JANELA_BACKTEST = 60        # dias úteis finais reservados para validação
Z_80 = 1.282                # banda de ~80% assumindo resíduos ~normais


def carregar_series() -> pd.DataFrame:
    """Série diária (dias úteis) wide: preços + clima + dummy de fallback."""
    precos = pd.read_csv(ARQ_PRECOS, parse_dates=["data"])
    clima = pd.read_csv(ARQ_CLIMA, parse_dates=["data"])

    wide = precos.pivot_table(index="data", columns="commodity",
                              values="preco", aggfunc="first")
    fontes = precos.pivot_table(index="data", columns="commodity",
                                values="fonte", aggfunc="first")
    for c in COMMODITIES:
        # fallback = fonte explicitamente não-primária; dia sem cotação NÃO é
        # fallback (senão feriado viraria falso degrau de fonte)
        wide[f"fb_{c}"] = (fontes[c].str.contains("Sindicatos|Cotrijal", na=False)
                           if c == "milho" else False)

    dias = pd.bdate_range(wide.index.min(), wide.index.max())
    df = wide.reindex(dias)
    # preço parado em feriado/falha curta = repete o último (série administrada);
    # clima é diário e vem do join abaixo, sem ffill
    cols_precos = COMMODITIES + (["dolar"] if "dolar" in df.columns else [])
    df[cols_precos] = df[cols_precos].ffill(limit=5)
    df["fb_milho"] = df["fb_milho"].astype("boolean").ffill(limit=5).fillna(False).astype(bool)

    # Emenda de nível no bloco de fallback do milho: o degrau CMA→Cotrijal
    # (~R$9–10, ver analises/comparacao_cma_cotrijal.md) não é movimento de
    # mercado; sem o ajuste ele contamina o treino e o backtest do modelo.
    # O spread é estimado nas bordas do próprio bloco (último CMA antes vs.
    # primeiro fallback, e vice-versa no fim), não cravado em constante.
    fb = df["fb_milho"].fillna(False).astype(bool)
    if fb.any() and (~fb).any():
        bordas = []
        blocos = (fb != fb.shift()).cumsum()[fb]
        for _, idx in fb[fb].groupby(blocos).groups.items():
            antes = df.loc[:idx[0], "milho"][~fb.loc[:idx[0]]].dropna()
            depois = df.loc[idx[-1]:, "milho"][~fb.loc[idx[-1]:]].dropna()
            if len(antes):
                bordas.append(float(antes.iloc[-1]) - float(df.loc[idx[0], "milho"]))
            if len(depois):
                bordas.append(float(depois.iloc[0]) - float(df.loc[idx[-1], "milho"]))
        if bordas:
            spread = float(np.median(bordas))
            df.loc[fb, "milho"] += spread
            print(f"[milho] bloco fallback ajustado ao nível CMA (+R$ {spread:.2f})")

    clima = clima.set_index("data")[["temp_max", "chuva_mm"]]
    # janelas climáticas calculadas no calendário COMPLETO (chuva de sábado
    # conta), depois reamostradas para os dias úteis da série de preços
    todos = pd.date_range(clima.index.min(), df.index.max())
    cl = clima.reindex(todos)
    df["chuva_30d"] = cl["chuva_mm"].rolling(30, min_periods=20).sum().reindex(df.index)
    df["temp_30d"] = cl["temp_max"].rolling(30, min_periods=20).mean().reindex(df.index)
    df["chuva_7d"] = cl["chuva_mm"].rolling(7, min_periods=5).sum().reindex(df.index)
    df["temp_7d"] = cl["temp_max"].rolling(7, min_periods=5).mean().reindex(df.index)
    return df


def montar_xy(df: pd.DataFrame, c: str):
    """Features em t → alvo Δpreço em t+1. Devolve (X, y, datas, feat_names)."""
    doy = df.index.dayofyear.to_numpy()
    feats = pd.DataFrame({
        "delta_5d": df[c].diff(5),
        "chuva_30d": df["chuva_30d"],
        "temp_30d": df["temp_30d"],
        "chuva_7d": df["chuva_7d"],
        "temp_7d": df["temp_7d"],
        "sin_ano": np.sin(2 * np.pi * doy / 365.25),
        "cos_ano": np.cos(2 * np.pi * doy / 365.25),
        "fallback": df[f"fb_{c}"].astype(float),
    }, index=df.index)
    # câmbio como exógena: retorno acumulado da PTAX em 5 e 20 dias úteis.
    # Commodity cotada em R$ com referência externa em US$ → depreciação
    # cambial tende a empurrar o preço interno com defasagem.
    if "dolar" in df.columns:
        feats["dolar_ret5"] = df["dolar"].pct_change(5)
        feats["dolar_ret20"] = df["dolar"].pct_change(20)
    alvo = df[c].shift(-1) - df[c]
    ok = feats.notna().all(axis=1) & alvo.notna() & df[c].notna()
    return feats[ok].to_numpy(float), alvo[ok].to_numpy(float), feats.index[ok], list(feats)


class Ridge:
    """Ridge fechado com padronização embutida (sem sklearn)."""

    def fit(self, X, y, lam=RIDGE_LAMBDA):
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-9
        Xs = (X - self.mu) / self.sd
        A = Xs.T @ Xs + lam * np.eye(X.shape[1])
        self.w = np.linalg.solve(A, Xs.T @ (y - y.mean()))
        self.b = y.mean()
        self.residuo_std = float(np.std(y - self.predict(X)))
        return self

    def predict(self, X):
        return ((X - self.mu) / self.sd) @ self.w + self.b


def baixar_forecast_clima() -> pd.DataFrame:
    """Previsão diária Open-Meteo (16 dias) — só para inferência, nunca gravada."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        "&daily=temperature_2m_max,precipitation_sum&forecast_days=16"
        f"&timezone={FUSO.replace('/', '%2F')}"
    )
    d = requests.get(url, timeout=60).json()["daily"]
    return pd.DataFrame({"temp_max": d["temperature_2m_max"],
                         "chuva_mm": d["precipitation_sum"]},
                        index=pd.to_datetime(d["time"]))


def projetar(df, c, modelo, feat_names, clima_futuro):
    """Itera o modelo dia a dia usando clima observado + previsto."""
    obs = pd.read_csv(ARQ_CLIMA, parse_dates=["data"]).set_index("data")[["temp_max", "chuva_mm"]]
    cl = pd.concat([obs, clima_futuro[~clima_futuro.index.isin(obs.index)]]).sort_index()
    cl = cl.reindex(pd.date_range(cl.index.min(), cl.index.max())).interpolate(limit=3)

    ultimo_dia = df[df[c].notna()].index.max()
    preco = float(df.loc[ultimo_dia, c])
    fb = float(df.loc[ultimo_dia, f"fb_{c}"])
    historico = df[c].dropna().copy()
    # câmbio futuro é desconhecido: projeta com PTAX parada no último valor
    # (o retorno acumulado decai a zero conforme a janela sai do observado)
    dolar_vals = (df["dolar"].ffill().loc[:ultimo_dia].dropna().tolist()
                  if "dolar_ret5" in feat_names else [])

    datas, valores, bandas = [], [], []
    dia, h = ultimo_dia, 0
    while h < HORIZONTE_DIAS_UTEIS:
        dia += timedelta(days=1)
        if dia.weekday() >= 5:
            continue
        if dia > cl.index.max():  # forecast de clima acabou
            break
        h += 1
        jan30 = cl.loc[dia - timedelta(days=29): dia]
        jan7 = cl.loc[dia - timedelta(days=6): dia]
        serie5 = historico.iloc[-5] if len(historico) >= 5 else historico.iloc[0]
        x = {
            "delta_5d": preco - float(serie5),
            "chuva_30d": float(jan30["chuva_mm"].sum()),
            "temp_30d": float(jan30["temp_max"].mean()),
            "chuva_7d": float(jan7["chuva_mm"].sum()),
            "temp_7d": float(jan7["temp_max"].mean()),
            "sin_ano": float(np.sin(2 * np.pi * dia.dayofyear / 365.25)),
            "cos_ano": float(np.cos(2 * np.pi * dia.dayofyear / 365.25)),
            "fallback": fb,
        }
        if dolar_vals:
            dolar_vals.append(dolar_vals[-1])
            x["dolar_ret5"] = dolar_vals[-1] / dolar_vals[-6] - 1 if len(dolar_vals) > 5 else 0.0
            x["dolar_ret20"] = dolar_vals[-1] / dolar_vals[-21] - 1 if len(dolar_vals) > 20 else 0.0
        preco += float(modelo.predict(np.array([[x[f] for f in feat_names]]))[0])
        historico.loc[dia] = preco
        datas.append(dia.strftime("%Y-%m-%d"))
        valores.append(round(preco, 2))
        bandas.append(round(Z_80 * modelo.residuo_std * np.sqrt(h), 2))
    return datas, valores, bandas


def _mae_iterada(m, X, serie, datas, i0, i1, h):
    """MAE de previsão iterada h passos (modelo, ingênuo) nas origens [i0, i1)."""
    e_mod, e_naive = [], []
    for i in range(i0, i1 - h):
        if datas[i + h] not in serie.index:
            continue
        p = float(serie.loc[datas[i]])
        origem = p
        for j in range(i, i + h):
            p += float(m.predict(X[j:j + 1])[0])
        real = float(serie.loc[datas[i + h]])
        e_mod.append(abs(p - real))
        e_naive.append(abs(origem - real))
    if not e_mod:
        return None
    return float(np.mean(e_mod)), float(np.mean(e_naive)), len(e_mod)


def escolher_lambda(X, y, datas, serie):
    """Grade de λ avaliada numa janela ANTERIOR à do backtest final.

    A janela de seleção termina onde a de backtest começa, então o número
    publicado no dashboard nunca foi usado para escolher hiperparâmetro.
    """
    corte_bt = len(y) - JANELA_BACKTEST
    corte_val = corte_bt - JANELA_BACKTEST
    if corte_val < 60:
        return RIDGE_LAMBDA
    # λ enorme ≈ baseline ingênuo (coeficientes ~0, só a deriva média): se
    # nenhum λ bater o ingênuo na validação, o modelo assume "sem sinal" em
    # vez de projetar movimento que historicamente não se confirmou
    melhor, melhor_mae = None, np.inf
    for lam in (2.0, 5.0, 20.0, 100.0, 500.0, 5000.0, 1e9):
        m = Ridge().fit(X[:corte_val], y[:corte_val], lam)
        r = _mae_iterada(m, X, serie, datas, corte_val, corte_bt, 10)
        if r and r[0] < melhor_mae:
            melhor_mae, melhor = r[0], lam
    return melhor if melhor is not None else RIDGE_LAMBDA


def backtest(X, y, datas, serie, lam):
    """Walk-forward sem re-treino: treina até o corte, projeta iterado no resto.

    Compara MAE do modelo vs. baseline ingênuo (Δ=0) em horizontes de 5 e 10
    dias úteis. Iterado de verdade: erros de um passo se propagam ao seguinte.
    """
    if len(y) < JANELA_BACKTEST + 60:
        return None
    corte = len(y) - JANELA_BACKTEST
    m = Ridge().fit(X[:corte], y[:corte], lam)
    out = {}
    for h in (5, 10):
        r = _mae_iterada(m, X, serie, datas, corte, len(y), h)
        if r:
            out[f"h{h}"] = {"mae_modelo": round(r[0], 2),
                            "mae_ingenuo": round(r[1], 2), "n": r[2]}
    return out or None


def main() -> int:
    df = carregar_series()
    clima_futuro = baixar_forecast_clima()

    payload = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "metodo": ("Ridge sobre Δpreço diário · features: momentum 5d, chuva/temp "
                   "acumuladas 7/30d (observado ERA5 + forecast Open-Meteo), "
                   "câmbio PTAX (retornos 5/20d, projetado parado), "
                   "sazonalidade anual, dummy de fonte · banda ≈80% (±1,28σ√h)"),
        "series": {},
    }

    for c in COMMODITIES:
        X, y, datas, feat_names = montar_xy(df, c)
        if len(y) < 100:
            print(f"[{c}] série curta demais ({len(y)}), pulando")
            continue
        serie = df[c].dropna()
        lam = escolher_lambda(X, y, datas, serie)
        modelo = Ridge().fit(X, y, lam)
        datas_fut, valores, bandas = projetar(df, c, modelo, feat_names, clima_futuro)
        bt = backtest(X, y, datas, serie, lam)

        coefs = dict(zip(feat_names, [round(float(w), 4) for w in modelo.w]))
        payload["series"][c] = {
            "datas": datas_fut,
            "valores": valores,
            "banda": bandas,
            "ultimo_observado": {
                "data": df[df[c].notna()].index.max().strftime("%Y-%m-%d"),
                "preco": round(float(df[c].dropna().iloc[-1]), 2),
            },
            "backtest": bt,
            "coeficientes_padronizados": coefs,
            "lambda": lam,
            "n_treino": len(y),
        }
        resumo_bt = (f"MAE h10 modelo {bt['h10']['mae_modelo']} vs ingênuo "
                     f"{bt['h10']['mae_ingenuo']}" if bt and "h10" in bt else "backtest n/d")
        print(f"[{c}] n={len(y)} · projeção {len(valores)} dias úteis "
              f"({valores[0]} -> {valores[-1]}) · {resumo_bt}")

    ARQ_SAIDA.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print(f"[prever] {ARQ_SAIDA.name} gravado")
    return 0


if __name__ == "__main__":
    sys.exit(main())
