# brazil-gov-contracts-mcp

MCP server for due diligence on suppliers to the Brazilian federal government: one call returns a supplier's registry status, sanction history, and federal contract history.

## Hosted MCP

A hosted instance is available at:

```
https://brazil-gov-contracts-mcp.fly.dev/mcp
```

Streamable HTTP transport. Example client configuration:

```json
{
  "mcpServers": {
    "brazil-gov-contracts": {
      "url": "https://brazil-gov-contracts-mcp.fly.dev/mcp"
    }
  }
}
```

## Data

| Dataset | Source | Coverage |
| --- | --- | --- |
| Contracts | PNCP - Portal Nacional de Contratacoes Publicas | Full available history, 2021-2026: 5,043,759 contracts (2021: 5,308 / 2022: 40,644 / 2023: 242,956 / 2024: 1,087,188 / 2025: 2,023,046 / 2026: 1,644,617). Row counts tie exactly to the publisher's totals. 2026 is a partial year. |
| CEIS | Portal da Transparencia | Current snapshot: 14,573 records (sanctioned entities) |
| CNEP | Portal da Transparencia | Current snapshot: 1,797 records (fined entities) |
| CNPJ registry | Receita Federal open data, 2026-09 monthly release | 444,475 records: suppliers appearing in the contract history that have a registry record in the 2026-09 release (99.95% of the 444,703 distinct supplier CNPJs; 228 suppliers have no 2026-09 registry record). Simples Nacional / MEI flags are not included. |

All datasets are public data published by the Brazilian federal government under Brazil's open data framework (Lei de Acesso a Informacao, Lei 12.527/2011). Attribution: PNCP (pncp.gov.br), Portal da Transparencia (portaltransparencia.gov.br), Receita Federal (gov.br/receitafederal).

## Tools

- `dataset_info()` - dataset scope and per-year totals.
- `vendor_due_diligence(cnpj)` - registry record, CEIS/CNEP matches, contract summary (count, total value, first/last signature, distinct buyers) and recent contracts for one 14-digit CNPJ.
- `search_contracts(query, cnpj, uf, year, buyer_name, min_valor, max_valor, limit)` - filtered contract search, ordered by signature date, capped at 100 rows.
- `get_contract(numero_controle_pncp)` - one contract by exact PNCP control number.
- `sanctions_search(cnpj, name, cadastro, limit)` - exact-identifier or name search across CEIS and CNEP.

Example question once connected: "Run vendor due diligence on CNPJ 41.640.044/0001-63 - registry status, sanctions, and their federal contract history."

## Limits

- CNPJ matching is exact (14 digits, punctuation stripped). No fuzzy matching, no historical registry snapshots.
- A CEIS/CNEP row describes one sanction record with its own scope and dates; it is not a company-wide clearance or a universal ban, and no match is not clearance.
- PNCP coverage starts in 2021; 2026 is a partial year.
- The registry subset covers only CNPJs that appear as suppliers in the 2021-2026 contract history; 228 of those suppliers have no record in the 2026-09 release (mostly closed companies).

## Running your own

The server reads parquet files from `DATA_DIR` (default `/data`) with this layout:

```
/data/out/year=YYYY/part-N.parquet        # contracts
/data/out_sanctions/ceis.parquet
/data/out_sanctions/cnep.parquet
/data/cnpj_subset.parquet                 # optional registry subset
```

```bash
pip install .
DATA_DIR=/path/to/data python -m brazil_gov_contracts
```

Transport: streamable HTTP on `PORT` (default 8080), with `/healthz` for health checks. A Dockerfile and `fly.toml` for Fly.io are included.

Usage logging writes JSON lines (timestamp, tool, client name and version, duration, status) to stderr, or to `USAGE_LOG` when set. No query text, IP addresses, or user agents are logged.
