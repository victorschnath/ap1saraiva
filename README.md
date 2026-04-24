# Comparador de Preços — Arquitetura Hexagonal

Aplicação de rastreamento e comparação de preços entre marketplaces (Mercado
Livre, Amazon, Magazine Luiza). Estrutura em arquitetura hexagonal (ports &
adapters) dividida em três componentes independentes que se comunicam apenas
via contratos (interfaces abstratas):

1. **Crawler** (adapter de fonte) — busca preços nos sites e produz snapshots.
2. **Storage** (adapter de persistência) — grava produtos, listings e histórico
   de preços em SQLite.
3. **Dashboard** (adapter web) — interface estilo Zoom pra visualizar produtos,
   comparar lojas e ver histórico de preço em gráfico.

Novas fontes (API oficial, importador de planilha, outro crawler) pluga no
mesmo contrato `PriceSource` sem mexer no resto.

## Setup

Requer Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
# 1. rastrear preços (lê products.csv, grava em data/comparador.db)
python -m comparador track

# 2. servir o dashboard
python -m comparador serve
# abre http://127.0.0.1:8000
```

Rodar `track` várias vezes acumula histórico — cada execução adiciona um novo
ponto no gráfico por listing.

### Opções úteis

```bash
python -m comparador track --headful          # navegador visível (debug)
python -m comparador track --sites amazon,magalu
python -m comparador track --top 10
python -m comparador track --min-delay 8 --max-delay 15   # mais conservador
python -m comparador serve --port 8080 --reload
```

## Entrada — [products.csv](products.csv)

```csv
name,reference_model,notes
iPhone 15 Pro 256GB Titanio Natural,,Apple 2023
Samsung Galaxy S24 Ultra 512GB,SM-S928B,
```

- `name` (obrigatório) — usado para matching semântico contra títulos.
- `reference_model` (opcional) — modelo de fábrica / ASIN / código. Quando
  presente, é usado como termo de busca (mais preciso que o nome).
- `notes` (opcional) — texto livre, só pra sua referência.

## Arquitetura

```
comparador/
├── __main__.py                  # python -m comparador {track|serve}
├── domain/                      # núcleo, SEM deps externas
│   ├── models.py                # Product, Listing, PriceSnapshot, ...
│   └── identity.py              # normalização, thresholds de linking
├── ports/                       # contratos (interfaces abstratas)
│   ├── price_source.py          # PriceSource.search(query) -> listings
│   └── repository.py            # ProductRepository (comandos + queries)
├── application/
│   └── track_prices.py          # TrackPricesUseCase — orquestra ports
└── adapters/                    # implementações plugáveis
    ├── sources/crawler/         # IMPLEMENTA PriceSource
    │   ├── crawler_source.py
    │   ├── fetcher.py           # Playwright + stealth + rate limit
    │   ├── matcher.py           # rapidfuzz + extração de features
    │   └── sites/{ml,amazon,magalu}.py
    ├── storage/sqlite/          # IMPLEMENTA ProductRepository
    │   ├── schema.sql
    │   └── repository.py
    ├── web/                     # dashboard FastAPI + Jinja + Chart.js
    │   ├── app.py               # rotas /public/* e /admin/*
    │   ├── auth.py              # login hardcoded + session guard
    │   ├── templates/
    │   │   ├── base.html
    │   │   ├── public/{index,product}.html
    │   │   └── admin/{login,index,product}.html
    │   └── static/app.css
    └── cli/
        ├── track_cmd.py
        └── serve_cmd.py
```

### Contrato pra novas fontes

Para adicionar uma fonte (ex: API oficial da Amazon, planilha, outro scraper),
basta implementar:

```python
class PriceSource(ABC):
    name: str

    async def search(
        self, query: ProductQuery, max_results: int = 5
    ) -> list[ListingSnapshot]:
        ...
```

E registrar no CLI (`track_cmd.py`) ou composition root. Zero mudança em
domain/ports/application.

## Identidade de produto (pra histórico consistente)

Três níveis:

1. **`Product`** (UUID interno) — produto canônico que você rastreia. Vem da
   linha do CSV; deduplica por nome normalizado (case/accents-insensitive).
2. **`Listing`** (`UNIQUE(site, site_id)`) — anúncio específico num marketplace.
   `site_id` é o **MLB** (Mercado Livre), **ASIN** (Amazon) ou **SKU** (Magalu)
   extraído da URL — estável entre runs.
3. **`PriceSnapshot`** (append-only) — ponto do histórico: preço + timestamp
   ligado a um Listing.

Um Product tem N Listings. Histórico é **por Listing**; comparação entre lojas
é agregação **por Product**.

### Auto-link vs. pendente

Cada Listing encontrado recebe um `match_score` (0–100) do matcher semântico:

- **≥ 85** → `auto` (linkado automaticamente ao Product)
- **55–85** → `pending` (aparece só no admin pra você confirmar/rejeitar)
- **< 55** → descartado (não é persistido)

Ciclo de vida completo de um listing (admin gerencia via dashboard):

| Estado atual | Ações disponíveis          | Vai para    |
|-------------|---------------------------|-------------|
| `pending`   | Confirmar / Rejeitar      | `confirmed` / `rejected` |
| `auto`      | Parar de observar         | `rejected`  |
| `confirmed` | Parar de observar         | `rejected`  |
| `rejected`  | Reativar                  | `confirmed` |

"Parar de observar" preserva o histórico de preços do listing — só marca como
`rejected`, sumindo dos cálculos e do público. Reativar traz de volta.

## Dashboard

Dividido em duas áreas:

### Pública (sem login) — `/public/`

- **`/public/`** — grid de produtos com **foto** + nome + menor preço atual.
- **`/public/product/{id}`** — página de comparação: cada loja em uma linha
  com foto, título, vendedor, preço — **ordenadas do menor para o maior preço**.
  Clicar abre a loja em nova aba.

Mostra só listings em estado `auto` ou `confirmed` com preço disponível.

### Admin (login obrigatório) — `/admin/`

Credenciais hardcoded: **`admin@admin.com`** / **`password`**.

- **`/admin/login`** — formulário de login.
- **`/admin/`** — tabela completa de todos os produtos.
- **`/admin/product/{id}`** — detalhe com:
  - Cards por listing incluindo os em estado `pending` e `rejected`.
  - Botões de ação contextuais (Confirmar / Rejeitar / Parar de observar / Reativar).
  - Gráfico de histórico de preços (Chart.js + Luxon, via CDN).

Sessão via `SessionMiddleware` do Starlette. Secret configurável via env var
`COMPARADOR_SECRET` (gerado aleatoriamente se não definido).

## Storage

SQLite em [data/comparador.db](data/comparador.db) (criado na primeira execução).

Schema em [comparador/adapters/storage/sqlite/schema.sql](comparador/adapters/storage/sqlite/schema.sql):

- `products` — produtos canônicos (UUID, name, display_name, reference_model)
- `listings` — anúncios por marketplace (UNIQUE site + site_id)
- `price_snapshots` — série temporal append-only (FK → listing)

Migração para Postgres depois é trivial: implementa `ProductRepository` em
`adapters/storage/postgres/` e troca o wiring em `track_cmd.py` / `app.py`.
Zero mudança no domain/application/sources.

## Anti-bloqueio

Mantido do design anterior: Playwright headless + stealth JS, 1 request
paralelo por domínio, delay 3–8s, retry exponencial em 429/5xx, contexto de
browser persistente por domínio (cookies reais). Sem proxy.

## Limitações conhecidas

- Seletores DOM dos marketplaces mudam com frequência. Se um site parar de
  extrair preço, rode `--headful` e ajuste
  `comparador/adapters/sources/crawler/sites/<site>.py`.
- Remoção de produto: hoje editar `products.csv` e remover uma linha **não**
  apaga do DB — o produto e histórico continuam. Deleção via dashboard é
  próximo passo.
- Volume alto (centenas/dia): precisa de proxies residenciais plugados no
  `RateLimitedFetcher`.
# comparador-precos-arquitetura-software-quinta
