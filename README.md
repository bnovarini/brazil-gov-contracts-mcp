# brazil-gov-contracts-mcp

MCP server for due diligence on suppliers to the Brazilian federal government: one call returns a supplier's registry status, sanction history, federal contract history, and registered partners.

Current release: v0.5.0. The hosted instance below runs this release.

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
| Partners | Receita Federal open data (Socios), 2026-09 monthly release | 582,330 partner records covering 305,354 of the 433,579 supplier CNPJ roots (70.4%). Supplier roots with no partner rows include entities without Socios records in the 2026-09 release. |

All datasets are public data published by the Brazilian federal government under Brazil's open data framework (Lei de Acesso a Informacao, Lei 12.527/2011). Attribution: PNCP (pncp.gov.br), Portal da Transparencia (portaltransparencia.gov.br), Receita Federal (gov.br/receitafederal).

## Tools

- `dataset_info()` - dataset scope and per-year totals.
- `vendor_due_diligence(cnpj)` - registry record, CEIS/CNEP matches, contract summary (count, total value, first/last signature, distinct buyers), recent contracts, and registered partners for one 14-digit CNPJ.
- `partners_of(cnpj)` - registered partners (quadro societario) for the supplier's CNPJ root: name, masked CPF/CNPJ identifier, role (qualificacao), entry date, and age bracket.
- `search_contracts(query, cnpj, uf, year, buyer_name, min_valor, max_valor, limit)` - filtered contract search, ordered by signature date, capped at 100 rows.
- `get_contract(numero_controle_pncp)` - one contract by exact PNCP control number.
- `sanctions_search(cnpj, name, cadastro, limit)` - exact-identifier or name search across CEIS and CNEP.
- `related_company_funnel(limit, exclude_same_group, exclude_weak_keys, active_window_only, scope_mode)` - strict-by-default funnel over the full scan universe of sanctioned/linked pairs. Applies, in order: same-group drop (shared name token or corporate-shareholder-only link), weak-key drop (partner key at 10+ companies), scope fit (contract buyer must fall inside the sanction row's published scope - an approximation, see Limits), and active-window fit (contract date inside the matching sanction row's own start/end, not the union window). Every response reports pair counts after each stage, pairs/contracts excluded only because sanction scope is unpublished (reported as excluded_unverifiable, not evidence of absence), and per-pair partner join timing (Receita entry date at the linked company vs the sanction start; corporate-partner links report unknown). Strict uses only universal CEIS rows. scope_mode=approximate reproduces the earlier esfera+UF/name heuristic; scope_mode=loose skips scope checks. Each filter loosens independently. Factual records only - no suspicion or risk score. Computes on the fly; allow ~30 seconds.
- `related_company_contracts_after_sanction(cnpj, scan, limit, exclude_same_group, exclude_weak_keys)` - partner-linked companies that signed federal contracts after a sanction date. Company mode (`cnpj=...`): anchors on the entity's earliest CEIS/CNEP start date, finds companies sharing a registered partner (name + masked CPF key) computed on the fly, and returns their contracts signed inside the sanction window. Scan mode (`scan=true`): precomputed top sanctioned/linked company pairs ranked by in-window contract ceiling value across all current CEIS/CNEP company rows.

Entity-level matching: contract and sanction results aggregate all establishments (matriz and filiais) sharing the supplier's 8-digit CNPJ root (cnpj_basico). A CEIS/CNEP row is recorded against one establishment CNPJ, but the sanction applies to the legal entity as a whole. The registry record returned is for the exact establishment queried, with matriz fallback. Every affected response carries an entity-scope note.

Example questions once connected:
- "Run vendor due diligence on CNPJ 41.640.044/0001-63 - registry status, sanctions, partners, and their federal contract history."
- "Did any company sharing a registered partner with this sanctioned supplier sign federal contracts after the sanction date?" A real anonymized pattern found with this tool: one partner appears in the registry of both a sanctioned company and a second, unrelated company that signed 6 federal contracts totaling R$17.4M after the sanction date.

## Private v0.5.0 additions

- `supplier_partner_sanctions(cnpj, limit=25)`: reverse supplier onboarding check. Looks directly in the installed partner and sanction files, without a contract requirement or a funnel exclusion. Individual matches require published name plus masked CPF; corporate matches require the same stored corporate identifier. Returns both sides of each partner link, separate direct supplier sanctions, complete sanction rows (dates, scope, category and organ), weak-key flags and explicit truncation. Foreign-partner rows without usable identifiers are not matched. The partner file is a supplier subset, not the full Brazilian registry. No match is not clearance. Registered administrators are not necessarily owners; no historical or ultimate ownership is inferred.
- `supplier_buyer_concentration(as_of, signature_year, min_contracts=10, min_total_value=1000000, limit=25)`: dataset-wide ranking by top buyer share of positive finite recorded contract ceilings, not revenue dependence. Supplier establishments aggregate to legal-entity root; buyer identity uses full CNPJ. Thresholds apply after excluding future/invalid signatures, invalid identifiers and nonpositive/invalid values. Reports exclusion counts, qualifying suppliers and truncation. `signature_year` filters actual signature dates rather than publication-year partitions. `as_of` defaults to current UTC date. Ceiling outliers remain in the data and can distort ranks; no paid amounts or private-customer revenue is available. Query may take about 25 seconds on the existing resource size.

No suspicion score. No new external service, paid feed, recurring refresh, or hosting resource. CNAE entrant analysis is feasibility-only and is not included in this build.

## Limits

- No fuzzy matching, no historical registry snapshots.
- A CEIS/CNEP row describes one sanction record with its own scope and dates; it is not a company-wide clearance or a universal ban, and no match is not clearance.
- Partner CPFs are masked by Receita Federal (only 6 middle digits are published). A cross-company match on partner name plus those 6 digits is a lead, not a confirmed identity: different people can share a name, and the published digits are partial. Partner data is personal data under LGPD.
- Sanction scope and dates come from each CEIS/CNEP row (abrangencia, orgao sancionador, start/end). Many sanctions apply only within the sanctioning organ's sphere; an expired or narrow sanction still appears in results.
- `valorGlobal` is a contract's ceiling value, not the amount actually paid. Rankings and totals in `related_company_contracts_after_sanction` sum ceilings.
- Scan mode is precomputed against the current CEIS/CNEP snapshot and ranks by ceiling value; company mode is computed live and is capped at 50 linked companies per query.
- Scope fit in `related_company_funnel` is an approximation: CEIS/CNEP does not publish the sanctioning organ's CNPJ or poder, so sphere matching uses esfera + UF and organ-only matching uses name comparison. Sanction rows with unpublished or unclassifiable scope fit nothing in strict mode and are counted under excluded_unverifiable.
- `weak_link=true` flags a partner key registered at 10 or more companies - a pattern typical of professional intermediaries (accountants, nominee administrators). Treat those links as weaker leads than low-degree keys.
- PNCP coverage starts in 2021; 2026 is a partial year.
- The registry subset covers only CNPJs that appear as suppliers in the 2021-2026 contract history; 228 of those suppliers have no record in the 2026-09 release (mostly closed companies).

## Running your own

The server reads parquet files from `DATA_DIR` (default `/data`) with this layout:

```
/data/out/year=YYYY/part-N.parquet        # contracts
/data/out_sanctions/ceis.parquet
/data/out_sanctions/cnep.parquet
/data/cnpj_subset.parquet                 # optional registry subset
/data/partners.parquet                    # optional partners subset
/data/related_edges.parquet             # optional, enables scan mode
/data/related_contracts_daily.parquet   # optional, enables scan mode
```

```bash
pip install .
DATA_DIR=/path/to/data python -m brazil_gov_contracts
```

Transport: streamable HTTP on `PORT` (default 8080), with `/healthz` for health checks. A Dockerfile and `fly.toml` for Fly.io are included.

Usage logging writes JSON lines (timestamp, tool, client name and version, duration, status) to stderr, or to `USAGE_LOG` when set. No query text, IP addresses, or user agents are logged.
