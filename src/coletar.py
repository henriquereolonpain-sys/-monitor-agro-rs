"""Coleta diária: preços de commodities (Notícias Agrícolas) + clima (Open-Meteo).

Desenho pensado para não repetir os erros da v1 (monitor-clima-pf):
- Cada página do Notícias Agrícolas traz ~10 dias de fechamentos, então uma
  execução recupera dias perdidos automaticamente (pipeline auto-cicatrizante).
- Cada commodity tem uma lista de fontes; se a primeira falhar ou sair da
  faixa plausível, tenta a próxima (fallback).
- A data gravada é a data do FECHAMENTO extraída da página, nunca a data do run.
- Escrita idempotente: append + deduplicação por (data, commodity).
- Falha com barulho: se alguma commodity não obtiver dado de nenhuma fonte,
  o script termina com exit code 1 e o GitHub Actions fica vermelho.
"""

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import (
    CLIMA_DATA_INICIAL,
    CLIMA_DEFASAGEM_DIAS,
    COMMODITIES,
    FUSO,
    HEADERS,
    LATITUDE,
    LIMIAR_FRESCOR_DIAS,
    LONGITUDE,
    URL_COTRIJAL,
)

RAIZ = Path(__file__).resolve().parents[1]
ARQ_PRECOS = RAIZ / "data" / "raw" / "precos.csv"
ARQ_CLIMA = RAIZ / "data" / "raw" / "clima.csv"


def baixar_html(url: str) -> str:
    res = requests.get(url, headers=HEADERS, timeout=30)
    res.raise_for_status()
    return res.text


def parsear_numero(texto: str) -> float:
    """Aceita '68.50', '122,00' e '1.234,56'."""
    t = texto.strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    return float(t)


def extrair_cotacoes(html: str, praca_regex: str) -> list[tuple[date, float]]:
    """Extrai (data_fechamento, preço) de todas as tabelas da página.

    Estrutura do site: cada dia é um bloco com <div class="fechamento">
    'Fechamento: dd/mm/aaaa' seguido de uma <table class="cot-fisicas"> cujo
    preço da praça está na 2ª coluna da linha correspondente.
    """
    soup = BeautifulSoup(html, "lxml")
    padrao_praca = re.compile(praca_regex, re.IGNORECASE)
    cotacoes = []

    for div in soup.find_all("div", class_="fechamento"):
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", div.get_text())
        if not m:
            continue
        data_fech = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))

        tabela = div.find_next("table", class_="cot-fisicas")
        if tabela is None:
            continue

        for linha in tabela.find_all("tr"):
            celulas = linha.find_all("td")
            if len(celulas) < 2:
                continue
            if padrao_praca.search(celulas[0].get_text()):
                try:
                    preco = parsear_numero(celulas[1].get_text())
                except ValueError:
                    continue
                cotacoes.append((data_fech, preco))
                break  # uma linha por tabela basta

    return cotacoes


def extrair_cotacoes_cotrijal(html: str, produto_regex: str) -> list[tuple[date, float]]:
    """Extrai a cotação do dia do JSON embutido na homepage da Cotrijal.

    Formato observado: `window.responseCotacoes = {"data_atual":"dd/mm/aaaa",
    "dados":[...], "mensagem":...}`. Em fim de semana/feriado `dados` vem vazio
    ("Mercado Fechado"). O schema dos itens não é documentado, então o parser é
    defensivo: acha o item cujo texto casa com o produto e pega o primeiro
    número plausível dele.
    """
    m = re.search(r"window\.responseCotacoes\s*=\s*(\{.*?\})\s*;", html, re.DOTALL)
    if not m:
        raise ValueError("JSON responseCotacoes não encontrado na página da Cotrijal")
    payload = json.loads(m.group(1))

    dados = payload.get("dados") or []
    if not dados:
        print(f"    (Cotrijal sem pregão: {payload.get('mensagem', 'dados vazios')})")
        return []

    m_data = re.search(r"(\d{2})/(\d{2})/(\d{4})", str(payload.get("data_atual", "")))
    data_cot = (date(int(m_data.group(3)), int(m_data.group(2)), int(m_data.group(1)))
                if m_data else date.today())

    padrao = re.compile(produto_regex, re.IGNORECASE)
    for item in dados:
        texto = json.dumps(item, ensure_ascii=False)
        if not padrao.search(texto):
            continue
        # 1º: campo com nome de preço; 2º: número com casa decimal no texto
        # (evita capturar "60" de "saca 60kg" como se fosse preço)
        candidatos = []
        if isinstance(item, dict):
            for chave, valor in item.items():
                if re.search(r"pre[çc]o|valor|price|cota", chave, re.IGNORECASE):
                    candidatos.append(str(valor))
        candidatos += re.findall(r"\d+[.,]\d{1,2}", texto)
        for bruto in candidatos:
            try:
                preco = parsear_numero(bruto)
            except ValueError:
                continue
            if preco > 1:  # descarta índices/percentuais pequenos
                return [(data_cot, preco)]
    return []


def coletar_precos() -> tuple[pd.DataFrame, list[str]]:
    """Percorre commodities e fontes; devolve (novos_registros, falhas).

    Uma fonte só é considerada viva se a cotação mais recente dela tiver menos
    de LIMIAR_FRESCOR_DIAS. Página no ar servindo tabelas velhas (como o CMA
    congelado de fev–jun/2026) conta como fonte morta e cai para o fallback.
    """
    registros = []
    falhas = []
    cache_html: dict[str, str] = {}  # evita baixar a mesma URL duas vezes
    limite_frescor = date.today() - timedelta(days=LIMIAR_FRESCOR_DIAS)

    for commodity, cfg in COMMODITIES.items():
        minimo, maximo = cfg["faixa_plausivel"]
        obtido = False

        for fonte in cfg["fontes"]:
            url = fonte.get("url", URL_COTRIJAL)
            try:
                if url not in cache_html:
                    cache_html[url] = baixar_html(url)
                if fonte.get("tipo") == "cotrijal":
                    cotacoes = extrair_cotacoes_cotrijal(cache_html[url], fonte["produto_regex"])
                else:
                    cotacoes = extrair_cotacoes(cache_html[url], fonte["praca_regex"])
            except Exception as e:
                print(f"[{commodity}] fonte '{fonte['nome']}' falhou: {e}")
                continue

            validas = [(d, p) for d, p in cotacoes if minimo <= p <= maximo]
            descartadas = len(cotacoes) - len(validas)
            if descartadas:
                print(f"[{commodity}] {descartadas} cotações fora da faixa plausível descartadas")

            if not validas:
                continue

            # grava mesmo se estagnada (dado real; dedup neutraliza), mas só
            # uma fonte fresca encerra a busca — senão o alerta dispara
            for d, p in validas:
                registros.append({
                    "data": d.isoformat(),
                    "commodity": commodity,
                    "preco": p,
                    "fonte": fonte["nome"],
                    "coletado_em": datetime.now().isoformat(timespec="seconds"),
                })

            mais_nova = max(d for d, _ in validas)
            if mais_nova < limite_frescor:
                print(f"[{commodity}] fonte '{fonte['nome']}' ESTAGNADA: cotação mais "
                      f"recente é de {mais_nova} (limite: {limite_frescor}); tentando fallback")
                continue

            print(f"[{commodity}] {len(validas)} fechamentos via '{fonte['nome']}' "
                  f"(último: {mais_nova} = R$ {dict(validas)[mais_nova]:.2f})")
            obtido = True
            break

        if not obtido:
            falhas.append(commodity)

    return pd.DataFrame(registros), falhas


def coletar_clima() -> pd.DataFrame:
    """Baixa dados OBSERVADOS (archive/ERA5) de forma incremental.

    Diferente da v1, nunca mistura previsão com observação e nunca apaga
    histórico: só acrescenta datas que ainda não estão no CSV.
    """
    if ARQ_CLIMA.exists():
        existente = pd.read_csv(ARQ_CLIMA, parse_dates=["data"])
        inicio = (existente["data"].max() + timedelta(days=1)).date()
    else:
        inicio = date.fromisoformat(CLIMA_DATA_INICIAL)

    fim = date.today() - timedelta(days=CLIMA_DEFASAGEM_DIAS)
    if inicio > fim:
        print(f"[clima] série já atualizada até {inicio - timedelta(days=1)}; nada a fazer")
        return pd.DataFrame()

    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude={LATITUDE}&longitude={LONGITUDE}"
        f"&start_date={inicio}&end_date={fim}"
        "&daily=temperature_2m_max,precipitation_sum"
        f"&timezone={FUSO.replace('/', '%2F')}"
    )
    res = requests.get(url, timeout=60)
    res.raise_for_status()
    diario = res.json().get("daily", {})

    df = pd.DataFrame({
        "data": diario.get("time", []),
        "temp_max": diario.get("temperature_2m_max", []),
        "chuva_mm": diario.get("precipitation_sum", []),
    })
    # a API devolve null para dias ainda não consolidados no ERA5
    df = df.dropna(subset=["temp_max", "chuva_mm"])
    df["coletado_em"] = datetime.now().isoformat(timespec="seconds")
    print(f"[clima] {len(df)} dias observados baixados ({inicio} a {fim})")
    return df


def gravar_incremental(arquivo: Path, novos: pd.DataFrame, chaves: list[str]) -> None:
    """Append + dedup pelas chaves, mantendo o registro mais recente."""
    if novos.empty:
        return
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    if arquivo.exists():
        base = pd.read_csv(arquivo, dtype=str)
        novos = pd.concat([base, novos.astype(str)], ignore_index=True)
    else:
        novos = novos.astype(str)
    antes = len(novos)
    novos = novos.drop_duplicates(subset=chaves, keep="last").sort_values(chaves)
    novos.to_csv(arquivo, index=False)
    print(f"[gravação] {arquivo.name}: {len(novos)} linhas ({antes - len(novos)} duplicatas removidas)")


def main() -> int:
    print(f"=== Coleta {datetime.now():%Y-%m-%d %H:%M} ===")

    df_precos, falhas = coletar_precos()
    gravar_incremental(ARQ_PRECOS, df_precos, ["data", "commodity"])

    erro_clima = None
    try:
        df_clima = coletar_clima()
        gravar_incremental(ARQ_CLIMA, df_clima, ["data"])
    except Exception as e:
        erro_clima = e
        print(f"[clima] ERRO: {e}")

    # Falha com barulho — a lição mais cara da v1. O dado que funcionou já
    # foi salvo acima; o exit 1 serve para o Actions ficar vermelho e avisar.
    if falhas or erro_clima:
        if falhas:
            print(f"\nERRO: nenhuma fonte fresca (<{LIMIAR_FRESCOR_DIAS} dias) "
                  f"para: {', '.join(falhas)}")
        return 1

    print("\nColeta concluída sem falhas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
