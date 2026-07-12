# Configuração central do pipeline.
# Para adicionar uma commodity nova: inclua uma entrada em COMMODITIES com ao
# menos uma fonte. A ordem da lista "fontes" define a prioridade (fallback).

BASE_NA = "https://www.noticiasagricolas.com.br/cotacoes"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

# Passo Fundo/RS
LATITUDE = -28.2628
LONGITUDE = -52.4087
FUSO = "America/Sao_Paulo"

# Início da série climática no primeiro run (backfill automático via archive API)
CLIMA_DATA_INICIAL = "2025-01-01"

# A archive API da Open-Meteo tem defasagem de alguns dias para dados observados
CLIMA_DEFASAGEM_DIAS = 3

COMMODITIES = {
    "milho": {
        "unidade": "R$/saca 60kg",
        # Faixa de sanidade: preço fora disso é descartado (protege contra
        # mudança de layout do site gravando lixo na série)
        "faixa_plausivel": (20.0, 300.0),
        "fontes": [
            {
                "nome": "NA · Milho CMA · Passo Fundo/RS",
                "url": f"{BASE_NA}/milho/milho-cma",
                "praca_regex": r"Passo Fundo",
            },
            {
                "nome": "NA · Sindicatos e Cooperativas · Não-Me-Toque/RS",
                "url": f"{BASE_NA}/milho/milho-mercado-fisico-sindicatos-e-cooperativas",
                "praca_regex": r"N[ãa]o[- ]?Me[- ]?Toque",
            },
        ],
    },
    "soja": {
        "unidade": "R$/saca 60kg",
        "faixa_plausivel": (50.0, 400.0),
        "fontes": [
            {
                "nome": "NA · Sindicatos e Cooperativas · Não-Me-Toque/RS",
                "url": f"{BASE_NA}/soja/soja-mercado-fisico-sindicatos-e-cooperativas",
                "praca_regex": r"N[ãa]o[- ]?Me[- ]?Toque",
            },
            {
                "nome": "NA · Sindicatos e Cooperativas · Nonoai/RS",
                "url": f"{BASE_NA}/soja/soja-mercado-fisico-sindicatos-e-cooperativas",
                "praca_regex": r"Nonoai",
            },
        ],
    },
    "trigo": {
        "unidade": "R$/saca 60kg",
        "faixa_plausivel": (30.0, 300.0),
        "fontes": [
            {
                "nome": "NA · Trigo Mercado Físico · Não-Me-Toque/RS",
                "url": f"{BASE_NA}/trigo/trigo-mercado-fisico",
                "praca_regex": r"N[ãa]o[- ]?Me[- ]?Toque",
            },
            {
                "nome": "NA · Trigo Mercado Físico · Nonoai/RS",
                "url": f"{BASE_NA}/trigo/trigo-mercado-fisico",
                "praca_regex": r"Nonoai",
            },
        ],
    },
}
