"""Brazilian federal government contracts MCP server.

Data (read-only DuckDB over parquet):
  contracts : PNCP full available history 2021-2026 (5,043,759 contracts)
  sanctions : current CEIS + CNEP (Portal da Transparencia snapshot)
  registry  : Receita Federal 2026-09 CNPJ subset for suppliers seen in contracts
  partners  : Receita Federal 2026-09 Socios subset for those suppliers

Entity matching: a CNPJ's first 8 digits (cnpj_basico) identify the legal entity;
the last 6 identify establishments (matriz/filiais). Contracts, sanctions and
partners are matched at the entity (8-digit root) level so a sanction recorded
against one establishment is not missed when querying another.
"""
import os
import re

import duckdb
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .usage import configure as usage_configure, log_tool

DATA_DIR = os.environ.get("DATA_DIR", "/data")
MAX_ROWS = 100

ENTITY_SCOPE_NOTE = (
    "Entity-level match on the 8-digit CNPJ root (cnpj_basico): results aggregate "
    "all establishments (matriz and filiais) of the same legal entity. A CEIS/CNEP "
    "row is recorded against one establishment CNPJ but the sanction applies to the "
    "legal entity as a whole."
)
PARTNER_IDENTITY_NOTE = (
    "Partner CPFs are masked by Receita Federal (only 6 middle digits are "
    "published). A cross-company match on partner name + those 6 digits is a lead, "
    "not a confirmed identity: different people can share a name, and the published "
    "digits are partial. Partner data is personal data under LGPD."
)

SANCTION_SCOPE_NOTE = (
    "Sanction scope and dates come from each CEIS/CNEP row (abrangencia, "
    "orgao sancionador, start/end). Many sanctions apply only within the "
    "sanctioning organ's sphere; a row is not automatically a universal ban, "
    "and an expired or narrow sanction still appears here."
)
VALUE_CEILING_NOTE = (
    "valorGlobal is the contract's ceiling value, not the amount actually paid."
)
WEAK_LINK_NOTE = (
    "weak_link=true flags a partner key registered at 10 or more companies - a "
    "pattern typical of professional intermediaries (accountants, nominee "
    "administrators). Treat those links as weaker leads than low-degree keys."
)
WEAK_LINK_DEGREE = 10
MAX_LINKED = 50

_con = None
mcp = FastMCP(
    "brazil-gov-contracts-mcp",
    instructions=(
        "Brazilian federal government contract and supplier due-diligence data. "
        "Contracts: full available PNCP history 2021-2026. Sanctions: current CEIS/CNEP. "
        "Registry and partners: Receita Federal 2026-09 records for CNPJs appearing "
        "as suppliers. Companies are matched at the legal-entity level (8-digit CNPJ "
        "root), so contracts and sanctions of any establishment of the entity are "
        "included. Partner CPFs are masked by Receita; partner-based links are leads, "
        "not confirmed identities."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)


def _con_init():
    global _con
    if _con is not None:
        return _con
    con = duckdb.connect()
    con.execute("SET memory_limit='600MB'")
    con.execute("SET threads=2")
    contracts = os.path.join(DATA_DIR, "out", "year=*", "*.parquet")
    con.execute(
        f"CREATE VIEW contracts AS SELECT * EXCLUDE(raw) "
        f"FROM read_parquet('{contracts}', hive_partitioning=true)"
    )
    for cad in ("ceis", "cnep"):
        p = os.path.join(DATA_DIR, "out_sanctions", f"{cad}.parquet")
        if os.path.exists(p):
            con.execute(f"CREATE VIEW {cad} AS SELECT * FROM read_parquet('{p}')")
        else:
            con.execute(f"CREATE VIEW {cad} AS SELECT NULL WHERE false")
    reg = os.path.join(DATA_DIR, "cnpj_subset.parquet")
    if os.path.exists(reg):
        con.execute(f"CREATE VIEW registry AS SELECT * FROM read_parquet('{reg}')")
    partners = os.path.join(DATA_DIR, "partners.parquet")
    if os.path.exists(partners):
        con.execute(f"CREATE VIEW partners AS SELECT * FROM read_parquet('{partners}')")
    for name, fname in (("related_edges", "related_edges.parquet"),
                        ("related_daily", "related_contracts_daily.parquet")):
        p = os.path.join(DATA_DIR, fname)
        if os.path.exists(p):
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{p}')")
    _con = con
    return con


def _cnpj14(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 14:
        raise ValueError("CNPJ must be 14 digits (punctuation is stripped automatically).")
    return digits


def _rows(con, sql, params=None, limit=MAX_ROWS):
    limit = max(1, min(int(limit), MAX_ROWS))
    cur = con.execute(sql + f" LIMIT {limit + 1}", params or [])
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    truncated = len(rows) > limit
    return rows[:limit], truncated


def _tables(con):
    return {r[0] for r in con.execute("SHOW TABLES").fetchall()}


def _partners_block(con, root: str) -> dict:
    if "partners" not in _tables(con):
        return {"present": False, "rows": []}
    rows, truncated = _rows(
        con,
        "SELECT identificador_socio, nome_socio, cnpj_cpf_socio, qualificacao_codigo, "
        "qualificacao, data_entrada, faixa_etaria "
        "FROM partners WHERE cnpj_root = ? ORDER BY data_entrada NULLS LAST",
        [root],
        limit=MAX_ROWS,
    )
    return {"present": True, "count": len(rows), "truncated": truncated,
            "identity_note": PARTNER_IDENTITY_NOTE, "rows": rows}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.tool()
def dataset_info(ctx: Context) -> dict:
    """Dataset scope and totals: per-year contract counts, sanctions counts, registry and partners presence."""
    with log_tool("dataset_info", ctx):
        con = _con_init()
        tables = _tables(con)
        per_year = con.execute(
            "SELECT year, count(*) AS contracts FROM contracts GROUP BY year ORDER BY year"
        ).fetchall()
        out = {
            "contracts": {
                "source": "PNCP (Portal Nacional de Contratacoes Publicas), full available history",
                "per_year": {int(y): int(c) for y, c in per_year},
                "total": int(sum(c for _, c in per_year)),
            },
            "entity_matching": ENTITY_SCOPE_NOTE,
            "registry_present": "registry" in tables,
            "partners_present": "partners" in tables,
        }
        for cad in ("ceis", "cnep"):
            out[cad] = {
                "source": "Portal da Transparencia, current snapshot",
                "rows": int(con.execute(f"SELECT count(*) FROM {cad}").fetchone()[0]),
            }
        if out["registry_present"]:
            out["registry"] = {
                "source": "Receita Federal CNPJ open data, 2026-09 monthly release, subset to suppliers",
                "rows": int(con.execute("SELECT count(*) FROM registry").fetchone()[0]),
            }
        if out["partners_present"]:
            out["partners"] = {
                "source": "Receita Federal CNPJ open data (Socios), 2026-09 monthly release, subset to suppliers",
                "rows": int(con.execute("SELECT count(*) FROM partners").fetchone()[0]),
                "entities": int(con.execute("SELECT count(DISTINCT cnpj_root) FROM partners").fetchone()[0]),
                "identity_note": PARTNER_IDENTITY_NOTE,
            }
        return out


@mcp.tool()
def vendor_due_diligence(cnpj: str, ctx: Context, max_contracts: int = 25) -> dict:
    """Due-diligence bundle for one supplier CNPJ: Receita registry record, CEIS/CNEP
    sanction matches, PNCP contract summary with recent contracts, and partners.
    cnpj: 14-digit CNPJ (punctuation optional). Contracts, sanctions and partners are
    matched at the legal-entity level (8-digit CNPJ root): all establishments of the
    entity are included."""
    cnpj = _cnpj14(cnpj)
    root = cnpj[:8]
    with log_tool("vendor_due_diligence", ctx) as rec:
        con = _con_init()
        result = {
            "cnpj": cnpj,
            "cnpj_root": root,
            "match_scope": ENTITY_SCOPE_NOTE,
            "registry": None,
            "sanctions": {},
            "contracts": {},
        }
        if "registry" in _tables(con):
            rows = con.execute(
                "SELECT * FROM registry WHERE cnpj = ?", [cnpj]
            ).fetchall()
            scope = "exact establishment"
            if not rows:
                rows = con.execute(
                    "SELECT * FROM registry WHERE cnpj = ? OR substr(cnpj,1,8) = ? "
                    "ORDER BY (cnpj = ?) DESC, cnpj LIMIT 1",
                    [root + "0001", root, root + "0001"],
                ).fetchall()
                scope = "same legal entity, another establishment (queried CNPJ has no 2026-09 registry record)"
            if rows:
                cols = [d[0] for d in con.description]
                result["registry"] = dict(zip(cols, rows[0]))
                result["registry_scope"] = scope
        for cad in ("ceis", "cnep"):
            rows, _ = _rows(
                con,
                f"SELECT * FROM {cad} WHERE length(cnpj) = 14 AND substr(cnpj,1,8) = ?",
                [root],
                limit=MAX_ROWS,
            )
            result["sanctions"][cad] = {"count": len(rows), "rows": rows}
        agg = con.execute(
            "SELECT count(*) AS n, sum(valorGlobal) AS total_valor_global, "
            "min(dataAssinatura) AS first_signature, max(dataAssinatura) AS last_signature, "
            "count(DISTINCT orgaoCnpj) AS distinct_buyers, "
            "count(DISTINCT niFornecedor) AS establishments "
            "FROM contracts WHERE substr(niFornecedor,1,8) = ?",
            [root],
        ).fetchone()
        result["contracts"]["summary"] = {
            "count": int(agg[0] or 0),
            "total_valor_global_brl": round(agg[1], 2) if agg[1] is not None else None,
            "first_signature": agg[2],
            "last_signature": agg[3],
            "distinct_buyers": int(agg[4] or 0),
            "establishments": int(agg[5] or 0),
        }
        recent, truncated = _rows(
            con,
            "SELECT numeroControlePNCP, niFornecedor, orgaoRazaoSocial, uf, objetoContrato, "
            "valorGlobal, dataAssinatura, dataVigenciaFim, dataPublicacaoPncp "
            "FROM contracts WHERE substr(niFornecedor,1,8) = ? "
            "ORDER BY dataAssinatura DESC NULLS LAST",
            [root],
            limit=max_contracts,
        )
        result["contracts"]["recent"] = recent
        result["contracts"]["recent_truncated"] = truncated
        result["partners"] = _partners_block(con, root)
        rec["status_note"] = "ok"
        return result


@mcp.tool()
def partners_of(cnpj: str, ctx: Context) -> dict:
    """Partners (socios) of one supplier's legal entity, from the Receita Federal
    2026-09 release. cnpj: 14-digit CNPJ (punctuation optional); matched at the
    8-digit root level (partners are registered per CNPJ root, not per establishment).
    CPFs are masked by Receita; see identity_note."""
    cnpj = _cnpj14(cnpj)
    root = cnpj[:8]
    with log_tool("partners_of", ctx):
        con = _con_init()
        out = {"cnpj": cnpj, "cnpj_root": root}
        out.update(_partners_block(con, root))
        return out


@mcp.tool()
def search_contracts(
    ctx: Context,
    query: str | None = None,
    cnpj: str | None = None,
    uf: str | None = None,
    year: int | None = None,
    buyer_name: str | None = None,
    min_valor: float | None = None,
    max_valor: float | None = None,
    limit: int = 25,
) -> dict:
    """Search PNCP contracts. query: case-insensitive substring on objetoContrato.
    cnpj: supplier CNPJ, matched at the legal-entity level (8-digit root): contracts
    of every establishment of the entity are returned. uf: two-letter state.
    year: contract year. buyer_name: substring on orgaoRazaoSocial.
    Results ordered by signature date, capped at 100."""
    with log_tool("search_contracts", ctx):
        con = _con_init()
        where, params = [], []
        if query:
            where.append("objetoContrato ILIKE '%' || ? || '%'")
            params.append(query)
        if cnpj:
            where.append("substr(niFornecedor,1,8) = ?")
            params.append(_cnpj14(cnpj)[:8])
        if uf:
            where.append("uf = ?")
            params.append(uf.strip().upper())
        if year:
            where.append("year = ?")
            params.append(int(year))
        if buyer_name:
            where.append("orgaoRazaoSocial ILIKE '%' || ? || '%'")
            params.append(buyer_name)
        if min_valor is not None:
            where.append("valorGlobal >= ?")
            params.append(float(min_valor))
        if max_valor is not None:
            where.append("valorGlobal <= ?")
            params.append(float(max_valor))
        if not where:
            raise ValueError("Provide at least one filter (query, cnpj, uf, year, buyer_name, min_valor, max_valor).")
        sql = (
            "SELECT numeroControlePNCP, orgaoCnpj, orgaoRazaoSocial, uf, municipio, "
            "niFornecedor, nomeFornecedor, objetoContrato, valorGlobal, "
            "dataAssinatura, dataVigenciaInicio, dataVigenciaFim, dataPublicacaoPncp "
            "FROM contracts WHERE " + " AND ".join(where) +
            " ORDER BY dataAssinatura DESC NULLS LAST"
        )
        rows, truncated = _rows(con, sql, params, limit=limit)
        return {"count": len(rows), "truncated": truncated, "match_scope": ENTITY_SCOPE_NOTE if cnpj else None, "rows": rows}


@mcp.tool()
def get_contract(numero_controle_pncp: str, ctx: Context) -> dict:
    """Fetch one contract by its exact numeroControlePNCP identifier. Returns all fields."""
    with log_tool("get_contract", ctx):
        con = _con_init()
        cur = con.execute(
            "SELECT * FROM contracts WHERE numeroControlePNCP = ?",
            [numero_controle_pncp],
        )
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            return {"found": False, "numeroControlePNCP": numero_controle_pncp}
        return {"found": True, "contract": dict(zip(cols, rows[0]))}


@mcp.tool()
def sanctions_search(
    ctx: Context,
    cnpj: str | None = None,
    name: str | None = None,
    cadastro: str = "both",
    limit: int = 50,
) -> dict:
    """Search current CEIS and CNEP sanctions. cnpj: 14-digit CNPJ matched at the
    legal-entity level (8-digit root: a sanction recorded against any establishment
    of the entity is returned); an 11-digit CPF is matched exactly.
    name: case-insensitive substring on sanctioned name fields. cadastro: 'ceis', 'cnep', or 'both'."""
    if not cnpj and not name:
        raise ValueError("Provide cnpj or name.")
    cadastro = cadastro.lower()
    if cadastro not in ("ceis", "cnep", "both"):
        raise ValueError("cadastro must be 'ceis', 'cnep', or 'both'.")
    with log_tool("sanctions_search", ctx):
        con = _con_init()
        d14 = re.sub(r"\D", "", cnpj) if cnpj else None
        if cnpj and len(d14) not in (11, 14):
            raise ValueError("Identifier must be 11 digits (CPF) or 14 digits (CNPJ).")
        targets = ["ceis", "cnep"] if cadastro == "both" else [cadastro]
        out = {}
        for cad in targets:
            if cnpj and len(d14) == 14:
                sql = f"SELECT * FROM {cad} WHERE length(cnpj) = 14 AND substr(cnpj,1,8) = ?"
                params = [d14[:8]]
            elif cnpj:
                sql = f"SELECT * FROM {cad} WHERE cpf_ou_cnpj_do_sancionado LIKE '%' || ? || '%'"
                params = [d14]
            else:
                sql = (
                    f"SELECT * FROM {cad} WHERE nome_do_sancionado ILIKE '%' || ? || '%' "
                    f"OR razao_social_cadastro_receita ILIKE '%' || ? || '%' "
                    f"OR nome_fantasia_cadastro_receita ILIKE '%' || ? || '%'"
                )
                params = [name, name, name]
            rows, truncated = _rows(con, sql, params, limit=limit)
            out[cad] = {"count": len(rows), "truncated": truncated, "rows": rows}
        if cnpj and len(d14) == 14:
            out["match_scope"] = ENTITY_SCOPE_NOTE
        return out




def _sanction_rows(con, root: str) -> dict:
    """All CEIS/CNEP rows for an 8-digit root, with parsed window dates."""
    out = {}
    for cad in ("ceis", "cnep"):
        rows, truncated = _rows(
            con,
            f"SELECT * FROM {cad} WHERE length(cnpj) = 14 AND substr(cnpj,1,8) = ?",
            [root],
            limit=MAX_ROWS,
        )
        out[cad] = {"count": len(rows), "truncated": truncated, "rows": rows}
    return out


def _windows(sanctions: dict):
    """Union window [start, end-or-None] over all sanction rows; None start if no dated row."""
    starts, ends = [], []
    for cad in ("ceis", "cnep"):
        for r in sanctions[cad]["rows"]:
            d0 = r.get("data_inicio_sancao_iso")
            d1 = r.get("data_final_sancao_iso")
            if d0:
                starts.append(str(d0)[:10])
                ends.append(str(d1)[:10] if d1 else None)
    if not starts:
        return None, None
    start = min(starts)
    end = None if any(e is None for e in ends) else max(ends)
    return start, end


def _root_names(con, roots):
    """razao_social per root from the registry (matriz record preferred)."""
    names = {}
    if "registry" not in _tables(con) or not roots:
        return names
    cur = con.execute(
        "SELECT cnpj, razao_social FROM registry WHERE substr(cnpj,1,8) IN ("
        + ",".join(["?"] * len(roots))
        + ") ORDER BY (matriz_filial = 1) DESC, cnpj",
        list(roots),
    )
    for cnpj, razao in cur.fetchall():
        names.setdefault(cnpj[:8], razao)
    return names


@mcp.tool()
def related_company_contracts_after_sanction(
    ctx: Context,
    cnpj: str | None = None,
    scan: bool = False,
    limit: int = 25,
    exclude_same_group: bool = False,
    exclude_weak_keys: bool = False,
) -> dict:
    """Companies that share a registered partner with a sanctioned company, and the
    federal contracts they signed inside the sanction window. Company mode: pass
    cnpj (14 digits, punctuation optional) for one sanctioned company - computed
    on the fly. Scan mode: pass scan=true (no cnpj) for the ranked precomputed
    scan across all CEIS/CNEP company rows. Partner links require the same partner
    name AND the same masked CPF digits (or an exact CNPJ for corporate partners);
    they are leads, not confirmed identities - read identity_note and weak_link.
    Optional tightening (defaults preserve the v0.3.0 behavior exactly):
    exclude_same_group drops pairs whose two company names share a distinctive
    token or whose only link is a corporate shareholder; exclude_weak_keys drops
    pairs whose partner key is registered at 10+ companies. For the full
    strict-by-default funnel (scope fit, active-window fit, partner join
    timing), use related_company_funnel."""
    if not scan and not cnpj:
        raise ValueError("Provide cnpj for company mode, or scan=true for the ranked precomputed scan.")
    with log_tool("related_company_contracts_after_sanction", ctx):
        con = _con_init()
        if scan:
            return _related_scan(con, limit, exclude_same_group, exclude_weak_keys)
        return _related_company(con, _cnpj14(cnpj), limit, exclude_same_group, exclude_weak_keys)


def _related_company(con, cnpj: str, limit: int, exclude_same_group: bool = False, exclude_weak_keys: bool = False) -> dict:
    root = cnpj[:8]
    result = {
        "cnpj": cnpj,
        "cnpj_root": root,
        "mode": "company (computed on the fly)",
        "identity_note": PARTNER_IDENTITY_NOTE,
        "sanction_scope_note": SANCTION_SCOPE_NOTE,
        "value_note": VALUE_CEILING_NOTE,
        "weak_link_note": WEAK_LINK_NOTE,
        "match_scope": ENTITY_SCOPE_NOTE,
    }
    if "partners" not in _tables(con):
        result["present"] = False
        return result
    sanctions = _sanction_rows(con, root)
    result["sanctions"] = sanctions
    if sanctions["ceis"]["count"] == 0 and sanctions["cnep"]["count"] == 0:
        result["sanctioned"] = False
        result["linked_companies"] = []
        return result
    result["sanctioned"] = True
    w_start, w_end = _windows(sanctions)
    result["window"] = {
        "start": w_start,
        "end": w_end,
        "note": "Union of all CEIS/CNEP windows for this entity; end=null means at least one sanction is open-ended.",
    }
    # partner keys of the sanctioned entity
    keys = con.execute(
        "SELECT DISTINCT identificador_socio, nome_socio, cnpj_cpf_socio, faixa_etaria "
        "FROM partners WHERE cnpj_root = ? AND identificador_socio IN (1,2)",
        [root],
    ).fetchall()
    result["partner_keys_used"] = len(keys)
    if not keys or w_start is None:
        result["linked_companies"] = []
        result["linked_companies_note"] = (
            "No partners in the 2026-09 release for this entity." if not keys
            else "No dated sanction rows; window cannot be computed.")
        return result
    vals = ",".join(
        f"({idf}, '{nm.replace(chr(39), chr(39)*2) if nm else ''}', '{cpf}')" for idf, nm, cpf, _ in keys
    )
    linked = con.execute(
        f"""
        WITH keys(idf, nome, cpf) AS (VALUES {vals})
        SELECT k.idf, k.nome, k.cpf, p.cnpj_root, p.faixa_etaria,
               (SELECT COUNT(DISTINCT p3.cnpj_root) FROM partners p3
                WHERE p3.identificador_socio = k.idf AND p3.cnpj_cpf_socio = k.cpf
                  AND (k.idf = 1 OR p3.nome_socio = k.nome)) AS key_degree
        FROM keys k
        JOIN partners p ON p.identificador_socio = k.idf AND p.cnpj_cpf_socio = k.cpf
                       AND (k.idf = 1 OR p.nome_socio = k.nome)
        WHERE p.cnpj_root <> '{root}'
        """,
    ).fetchall()
    by_root = {}
    for idf, nome, cpf, lroot, faixa, degree in linked:
        e = by_root.setdefault(lroot, [])
        e.append({
            "edge_type": "pf_name_masked_cpf" if idf == 2 else "pj_exact_cnpj",
            "nome_socio": nome if idf == 2 else None,
            "cnpj_cpf_socio": cpf,
            "faixa_etaria": faixa,
            "key_degree": int(degree),
            "weak_link": int(degree) >= WEAK_LINK_DEGREE,
        })
    all_roots = sorted(by_root)
    truncated = len(all_roots) > MAX_LINKED
    use_roots = all_roots[:MAX_LINKED]
    result["linked_roots_found"] = len(all_roots)
    result["linked_companies_truncated"] = truncated
    if not use_roots:
        result["linked_companies"] = []
        return result
    names = _root_names(con, use_roots)
    # in-window contract aggregates (year partition pruning via w_start)
    y0 = int(w_start[:4])
    end_clause = "" if w_end is None else "AND c.dataAssinatura::DATE <= ?::DATE"
    params = use_roots + [w_start] + ([] if w_end is None else [w_end])
    agg = con.execute(
        "SELECT substr(c.niFornecedor,1,8) AS r, COUNT(*) AS n, SUM(c.valorGlobal) AS v, "
        "MIN(c.dataAssinatura) AS first_s, MAX(c.dataAssinatura) AS last_s "
        "FROM contracts c WHERE substr(c.niFornecedor,1,8) IN ("
        + ",".join(["?"] * len(use_roots)) + ") "
        f"AND c.year >= {y0} AND c.dataAssinatura::DATE >= ?::DATE {end_clause} "
        "GROUP BY 1",
        params,
    ).fetchall()
    agg_map = {r: (int(n), v, f, l) for r, n, v, f, l in agg}
    top = con.execute(
        "SELECT r, numeroControlePNCP, orgaoRazaoSocial, uf, objetoContrato, valorGlobal, dataAssinatura FROM ("
        "SELECT substr(c.niFornecedor,1,8) AS r, numeroControlePNCP, orgaoRazaoSocial, uf, "
        "objetoContrato, valorGlobal, dataAssinatura, "
        "ROW_NUMBER() OVER (PARTITION BY substr(c.niFornecedor,1,8) ORDER BY c.valorGlobal DESC NULLS LAST) AS rn "
        "FROM contracts c WHERE substr(c.niFornecedor,1,8) IN ("
        + ",".join(["?"] * len(use_roots)) + ") "
        f"AND c.year >= {y0} AND c.dataAssinatura::DATE >= ?::DATE {end_clause}) "
        "WHERE rn <= 5",
        params,
    ).fetchall()
    top_map = {}
    for r, *rest in top:
        top_map.setdefault(r, []).append({
            "numeroControlePNCP": rest[0], "orgaoRazaoSocial": rest[1], "uf": rest[2],
            "objetoContrato": rest[3], "valorGlobal": rest[4], "dataAssinatura": rest[5],
        })
    sanc_name = (_root_names(con, [root]) or {}).get(root)
    if exclude_same_group and sanc_name:
        use_roots = [r for r in use_roots if not _same_group(sanc_name, names.get(r) or "")
                     and {p["edge_type"] for p in by_root[r]} != {"pj_exact_cnpj"}]
    companies = []
    for lroot in use_roots:
        partners_l = by_root[lroot]
        if exclude_weak_keys:
            if any(p["weak_link"] for p in partners_l):
                continue
            if not partners_l:
                continue
        n, v, f, l = agg_map.get(lroot, (0, None, None, None))
        companies.append({
            "cnpj_root": lroot,
            "razao_social": names.get(lroot),
            "matched_partners": partners_l,
            "contracts_in_window": {
                "count": n,
                "total_valor_global_brl": round(v, 2) if v is not None else None,
                "first_signature": f,
                "last_signature": l,
            },
            "top_contracts": top_map.get(lroot, []),
        })
    companies.sort(key=lambda c: (c["contracts_in_window"]["total_valor_global_brl"] or 0), reverse=True)
    result["linked_companies"] = companies
    result["linked_without_contracts_in_window"] = sum(1 for c in companies if c["contracts_in_window"]["count"] == 0)
    return result


def _related_scan(con, limit: int, exclude_same_group: bool = False, exclude_weak_keys: bool = False) -> dict:
    out = {
        "mode": "scan (precomputed across all current CEIS/CNEP company rows)",
        "identity_note": PARTNER_IDENTITY_NOTE,
        "sanction_scope_note": SANCTION_SCOPE_NOTE,
        "value_note": VALUE_CEILING_NOTE,
        "weak_link_note": WEAK_LINK_NOTE,
        "match_scope": ENTITY_SCOPE_NOTE,
    }
    if "related_edges" not in _tables(con) or "related_daily" not in _tables(con):
        out["present"] = False
        out["note"] = "Precomputed related_edges / related_contracts_daily files are not installed on this server."
        return out
    out["present"] = True
    rows = con.execute(
        f"""
        WITH sanc AS (
          SELECT substr(cnpj,1,8) AS sanc_root,
                 MIN(data_inicio_sancao_iso::DATE) AS d0,
                 MAX(COALESCE(data_final_sancao_iso::DATE, DATE '2100-01-01')) AS d1,
                 MIN(nome_do_sancionado) AS sanc_name,
                 MIN(orgao_sancionador) AS orgao,
                 MIN(abragencia_da_sancao) AS abrangencia,
                 COUNT(*) AS sanction_rows
          FROM (
            SELECT cnpj, nome_do_sancionado, orgao_sancionador, abragencia_da_sancao,
                   data_inicio_sancao_iso, data_final_sancao_iso FROM ceis WHERE length(cnpj)=14
            UNION ALL
            SELECT cnpj, nome_do_sancionado, orgao_sancionador, abragencia_da_sancao,
                   data_inicio_sancao_iso, data_final_sancao_iso FROM cnep WHERE length(cnpj)=14
          ) WHERE data_inicio_sancao_iso IS NOT NULL AND data_inicio_sancao_iso <> ''
          GROUP BY 1
        ),
        agg AS (
          SELECT e.sanc_root, e.linked_root,
                 COUNT(DISTINCT e.cnpj_cpf_socio || coalesce(e.nome_socio,'')) AS shared_keys,
                 MAX(e.key_degree) AS max_key_degree,
                 list(DISTINCT e.edge_type) AS edge_types
          FROM related_edges e GROUP BY 1,2
        ),
        vals AS (
          SELECT a.sanc_root, a.linked_root, a.shared_keys, a.max_key_degree, a.edge_types,
                 COUNT(*) AS n_days, SUM(d.n) AS n_contracts, SUM(d.v) AS total_v
          FROM agg a
          JOIN sanc s ON s.sanc_root = a.sanc_root
          JOIN related_daily d ON d.cnpj_root = a.linked_root
               AND d.day >= s.d0 AND d.day <= s.d1
          GROUP BY 1,2,3,4,5
        )
        SELECT v.*, s.d0, s.d1, s.sanc_name, s.orgao, s.abrangencia, s.sanction_rows
        FROM vals v JOIN sanc s ON s.sanc_root = v.sanc_root
        WHERE v.n_contracts > 0
        ORDER BY v.total_v DESC NULLS LAST
        LIMIT {int(limit) if not (exclude_same_group or exclude_weak_keys) else 1000000}
        """,
    ).fetchall()
    cols = ["sanc_root", "linked_root", "shared_keys", "max_key_degree", "edge_types",
            "n_days", "n_contracts", "total_v", "window_start", "window_end",
            "sanc_name", "orgao", "abrangencia", "sanction_rows"]
    items = [dict(zip(cols, r)) for r in rows]
    names = _root_names(con, [i["linked_root"] for i in items])
    for i in items:
        i["linked_razao_social"] = names.get(i["linked_root"])
        i["total_valor_global_brl"] = round(i.pop("total_v"), 2) if i["total_v"] is not None else None
        i["contracts_in_window"] = i.pop("n_contracts")
        i["weak_link"] = (i["max_key_degree"] or 0) >= WEAK_LINK_DEGREE
        i["window_end"] = None if str(i["window_end"]) == "2100-01-01" else str(i["window_end"])
        i["window_start"] = str(i["window_start"])
        i.pop("n_days", None)
    if exclude_same_group:
        items = [i for i in items
                 if not _same_group(i["sanc_name"], i["linked_razao_social"] or "")
                 and set(i["edge_types"] or []) != {"pj_exact_cnpj"}]
    if exclude_weak_keys:
        items = [i for i in items if not i["weak_link"]]
    totals = con.execute(
        "SELECT COUNT(DISTINCT sanc_root), COUNT(DISTINCT linked_root), COUNT(*) FROM related_edges"
    ).fetchone()
    out["edges_total"] = {"sanctioned_roots": int(totals[0]), "linked_roots": int(totals[1]), "edges": int(totals[2])}
    out["count"] = len(items[:max(1, min(int(limit), MAX_ROWS))])
    out["limit"] = int(limit)
    out["rows"] = items[:max(1, min(int(limit), MAX_ROWS))]
    return out



# ---------------------------------------------------------------------------
# v0.4.0 - funnel filters (same-group, weak-key, active window, scope fit,
# partner join timing) baked in as a strict-by-default funnel endpoint.
# Semantics mirror the offline robustness funnel validated 2026-10-20
# (679 -> 457 -> 429 -> 39 -> 30 pairs on the 2026-10-13 sanction snapshot).
# ---------------------------------------------------------------------------
import unicodedata as _unicodedata

SAME_GROUP_STOP = {"LTDA", "LTDA.", "SERVICOS", "BRASIL", "COMERCIO", "SA",
                   "S.A", "EIRELI", "DE", "DA", "DO", "E"}

FUNNEL_SCOPE_NOTE = (
    "Strict is a conservative approximation, not a legal ruling: only rows "
    "explicitly stating Todas as Esferas em todos os Poderes qualify. "
    "Scoped, missing and unclassified rows are excluded as unverifiable. "
    "approximate mode reproduces the earlier esfera+UF/name heuristic; it "
    "cannot verify municipal jurisdiction, sanctioning-organ CNPJ or poder. "
    "loose mode skips scope checks. A CNEP fine alone is not a contracting ban."
)

FUNNEL_NO_SCORE_NOTE = (
    "This endpoint reports factual records only: shared registered partners, "
    "sanction rows, and contracts with their dates and ceiling values. It "
    "deliberately computes no suspicion, risk, or evasion score."
)
FUNNEL_TIMING_NOTE = (
    "partner_join_timing compares each shared partner's Receita entry date at "
    "the linked company with the sanction start (calendar-date comparison): "
    "'before' means the partner was already registered at the linked company "
    "when the sanction began. Corporate-partner links have no comparable "
    "entry date and are reported as 'unknown'."
)
OUTLIER_NOTE = (
    "outlier_dominated=true means a single contract holds more than 90% of "
    "the pair's total ceiling value; read totals with that in mind."
)


def _norm_txt(s) -> str:
    if not s:
        return ""
    return " ".join(
        "".join(c for c in _unicodedata.normalize("NFKD", str(s).upper())
                if not _unicodedata.combining(c)).split()
    )


def _same_group(name_a: str, name_b: str) -> bool:
    shared = set(_norm_txt(name_a).split()) & set(_norm_txt(name_b).split())
    return any(len(t) > 3 and t not in SAME_GROUP_STOP for t in shared)


def _scope_class(abr: str) -> str:
    a = _norm_txt(abr)
    if "TODAS AS ESFERAS" in a:
        return "universal"
    if "SEM INFORMACAO" in a or not a:
        return "unknown"
    if a.startswith("NO ORGAO SANCIONADOR"):
        return "organ_only"
    if "NA ESFERA E NO PODER" in a:
        return "sphere_and_power"
    if "EM TODOS OS PODERES DA ESFERA" in a:
        return "sphere"
    return "other"


def _scope_fit(srow: dict, crow: dict) -> bool:
    cls = srow["cls"]
    if cls == "universal":
        return True
    if cls in ("unknown", "other"):
        return False
    esf_ok = _norm_txt(crow.get("esf")) == _norm_txt(srow.get("esf"))
    if cls in ("sphere", "sphere_and_power"):
        # sanction-side poder is not published; esfera+UF is used (approximation).
        if _norm_txt(srow.get("esf")) == "FEDERAL":
            return esf_ok
        return esf_ok and _norm_txt(crow.get("uf")) == _norm_txt(srow.get("uf"))
    if cls == "organ_only":
        nb, no = _norm_txt(crow.get("buyer")), _norm_txt(srow.get("org"))
        return bool(nb and no) and (nb == no or no in nb or nb in no)
    return False


def _funnel_base_rows(con):
    """All sanctioned->linked pairs with >=1 contract in the union window
    (same universe as the v0.3.0 scan, unbounded)."""
    rows = con.execute(
        """
        WITH sanc AS (
          SELECT substr(cnpj,1,8) AS sanc_root,
                 MIN(data_inicio_sancao_iso::DATE) AS d0,
                 MAX(COALESCE(data_final_sancao_iso::DATE, DATE '2100-01-01')) AS d1,
                 MIN(nome_do_sancionado) AS sanc_name,
                 MIN(orgao_sancionador) AS orgao,
                 MIN(abragencia_da_sancao) AS abrangencia,
                 COUNT(*) AS sanction_rows
          FROM (
            SELECT cnpj, nome_do_sancionado, orgao_sancionador, abragencia_da_sancao,
                   data_inicio_sancao_iso, data_final_sancao_iso FROM ceis WHERE length(cnpj)=14
            UNION ALL
            SELECT cnpj, nome_do_sancionado, orgao_sancionador, abragencia_da_sancao,
                   data_inicio_sancao_iso, data_final_sancao_iso FROM cnep WHERE length(cnpj)=14
          ) WHERE data_inicio_sancao_iso IS NOT NULL AND data_inicio_sancao_iso <> ''
          GROUP BY 1
        ),
        agg AS (
          SELECT e.sanc_root, e.linked_root,
                 COUNT(DISTINCT e.cnpj_cpf_socio || coalesce(e.nome_socio,'')) AS shared_keys,
                 MAX(e.key_degree) AS max_key_degree,
                 list(DISTINCT e.edge_type) AS edge_types
          FROM related_edges e GROUP BY 1,2
        ),
        vals AS (
          SELECT a.sanc_root, a.linked_root, a.shared_keys, a.max_key_degree, a.edge_types,
                 SUM(d.n) AS n_contracts, SUM(d.v) AS total_v
          FROM agg a
          JOIN sanc s ON s.sanc_root = a.sanc_root
          JOIN related_daily d ON d.cnpj_root = a.linked_root
               AND d.day >= s.d0 AND d.day <= s.d1
          GROUP BY 1,2,3,4,5
        )
        SELECT v.*, s.d0, s.d1, s.sanc_name, s.orgao, s.abrangencia, s.sanction_rows
        FROM vals v JOIN sanc s ON s.sanc_root = v.sanc_root
        WHERE v.n_contracts > 0
        """,
    ).fetchall()
    cols = ["sanc_root", "linked_root", "shared_keys", "max_key_degree", "edge_types",
            "contracts_in_union_window", "total_v", "window_start", "window_end",
            "sanc_name", "orgao", "abrangencia", "sanction_rows"]
    return [dict(zip(cols, r)) for r in rows]


def _funnel_sanction_rows(con, roots):
    if not roots:
        return {}
    marks = ",".join(["?"] * len(roots))
    rows = con.execute(
        f"""
        SELECT * FROM (
          SELECT substr(cnpj,1,8) AS root, cadastro, data_inicio_sancao_iso AS d0,
                 data_final_sancao_iso AS d1, abragencia_da_sancao AS abr,
                 esfera_orgao_sancionador AS esf, uf_orgao_sancionador AS uf,
                 orgao_sancionador AS org
          FROM ceis WHERE tipo_de_pessoa='J'
          UNION ALL
          SELECT substr(cnpj,1,8), cadastro, data_inicio_sancao_iso, data_final_sancao_iso,
                 abragencia_da_sancao, esfera_orgao_sancionador, uf_orgao_sancionador,
                 orgao_sancionador
          FROM cnep WHERE tipo_de_pessoa='J'
        ) WHERE root IN ({marks})
        """,
        roots,
    ).fetchall()
    out = {}
    for root, cad, d0, d1, abr, esf, uf, org in rows:
        out.setdefault(root, []).append({
            "cadastro": cad, "d0": d0 or None, "d1": d1 or None, "abr": abr,
            "esf": esf, "uf": uf, "org": org, "cls": _scope_class(abr),
        })
    return out


def _funnel_contracts(con, roots):
    if not roots:
        return {}
    marks = ",".join(["?"] * len(roots))
    rows = con.execute(
        f"""
        SELECT substr(niFornecedor,1,8) AS root, numeroControlePNCP, dataAssinatura AS d,
               valorGlobal AS vg, esferaId AS esf, poderId AS poder, uf,
               orgaoRazaoSocial AS buyer
        FROM contracts WHERE substr(niFornecedor,1,8) IN ({marks})
        """,
        roots,
    ).fetchall()
    out = {}
    for root, ncid, d, vg, esf, poder, uf, buyer in rows:
        out.setdefault(root, []).append({
            "numeroControlePNCP": ncid, "d": (d or "")[:10] or None, "vg": vg,
            "esf": esf, "poder": poder, "uf": uf, "buyer": buyer,
        })
    return out


def _pair_contract_fit(contracts, srows, scope_mode: str, active_window_only: bool):
    """Returns (kept_contract_ids, n_scope, excluded_unverifiable) for one pair.
    A contract is kept when it fits at least one sanction row's scope (strict)
    and falls inside that same row's active window."""
    kept, n_scope = set(), set()
    excl_unver = set()
    for crow in contracts:
        d = crow["d"]
        for srow in srows:
            scope_ok = (scope_mode == "loose" or
                        (scope_mode == "strict" and srow["cadastro"] == "CEIS" and srow["cls"] == "universal") or
                        (scope_mode == "approximate" and _scope_fit(srow, crow)))
            if not scope_ok:
                if scope_mode == "strict" or srow["cls"] in ("unknown", "other"):
                    excl_unver.add(crow["numeroControlePNCP"])
                continue
            n_scope.add(crow["numeroControlePNCP"])
            d0, d1 = srow["d0"], srow["d1"]
            if d is None or d0 is None:
                continue
            in_win = d >= d0 and (active_window_only is False or d1 is None or d <= d1)
            if in_win:
                kept.add(crow["numeroControlePNCP"])
                break
    return kept, n_scope, excl_unver


@mcp.tool()
def related_company_funnel(
    ctx: Context,
    limit: int = 25,
    exclude_same_group: bool = True,
    exclude_weak_keys: bool = True,
    active_window_only: bool = True,
    scope_mode: str = "strict",
) -> dict:
    """Strict-by-default funnel over companies that share a registered partner
    with a sanctioned company and signed federal contracts inside the sanction
    window. Starts from the full scan universe and applies, in order:
    same-group drop (shared name token or corporate-shareholder-only links),
    weak-key drop (partner key registered at 10+ companies), scope fit (the
    contract's buyer must fall inside the sanction's published scope -
    approximation, see scope_note), and active-window fit (contract date inside
    the matching sanction row's own start/end, not the union window). Every
    response reports the pair count after each stage. scope_mode: 'strict'
    (default; universal CEIS rows only), 'approximate' (esfera+UF/name
    matching with unverifiable rows reported), or 'loose' (skip scope fit). Set exclude_same_group / exclude_weak_keys / active_window_only to
    false to loosen individual stages. Partner join timing per pair compares
    the shared partner's Receita entry date at the linked company with the
    sanction start. Factual records only: no suspicion or risk score. Values
    are contract ceilings (valorGlobal), not amounts paid."""
    if scope_mode not in ("strict", "approximate", "loose"):
        raise ValueError("scope_mode must be strict, approximate or loose.")
    with log_tool("related_company_funnel", ctx):
        con = _con_init()
        out = {
            "mode": "funnel (strict-by-default filters over the full scan universe)",
            "params": {
                "exclude_same_group": exclude_same_group,
                "exclude_weak_keys": exclude_weak_keys,
                "active_window_only": active_window_only,
                "scope_mode": scope_mode,
            },
            "identity_note": PARTNER_IDENTITY_NOTE,
            "sanction_scope_note": SANCTION_SCOPE_NOTE,
            "scope_note": FUNNEL_SCOPE_NOTE,
            "value_note": VALUE_CEILING_NOTE,
            "weak_link_note": WEAK_LINK_NOTE,
            "timing_note": FUNNEL_TIMING_NOTE,
            "no_score_note": FUNNEL_NO_SCORE_NOTE,
            "outlier_note": OUTLIER_NOTE,
            "match_scope": ENTITY_SCOPE_NOTE,
            "universe_note": "Candidate universe is fixed to the v0.3.0 union-window scan. Loosening filters does not add pairs outside that universe.",
            "same_group_note": "Name-token overlap and corporate-only links are heuristics, not proof of common control.",
        }
        tabs = _tables(con)
        if not {"related_edges", "related_daily", "partners"} <= tabs:
            out["present"] = False
            out["note"] = ("related_edges / related_contracts_daily / partners "
                           "files are not installed on this server.")
            return out
        out["present"] = True

        base = _funnel_base_rows(con)
        counts = {"raw_pairs": len(base)}
        _base_names = _root_names(con, sorted({r["linked_root"] for r in base}))
        for r in base:
            r["linked_razao_social"] = _base_names.get(r["linked_root"])

        s1 = base
        if exclude_same_group:
            s1 = [r for r in s1
                  if not _same_group(r["sanc_name"], r["linked_razao_social"] or "")
                  and set(r["edge_types"] or []) != {"pj_exact_cnpj"}]
        counts["after_same_group_drop"] = len(s1)

        s2 = s1
        if exclude_weak_keys:
            s2 = [r for r in s2 if (r["max_key_degree"] or 0) < WEAK_LINK_DEGREE]
        counts["after_weak_key_drop"] = len(s2)

        sanc_roots = sorted({r["sanc_root"] for r in s2})
        link_roots = sorted({r["linked_root"] for r in s2})
        sanc_by_root = _funnel_sanction_rows(con, sanc_roots)
        ctr_by_root = _funnel_contracts(con, link_roots)

        per_pair = {}
        excluded_pairs_unverifiable = 0
        excluded_contracts_unverifiable = 0
        for r in s2:
            key = (r["sanc_root"], r["linked_root"])
            contracts = ctr_by_root.get(r["linked_root"], [])
            srows = sanc_by_root.get(r["sanc_root"], [])
            kept, n_scope, excl = _pair_contract_fit(
                contracts, srows, scope_mode, active_window_only)
            per_pair[key] = {"kept": kept, "n_scope": len(n_scope),
                             "excl_unver": excl}
            if scope_mode != "loose" and not n_scope and excl:
                excluded_pairs_unverifiable += 1
                excluded_contracts_unverifiable += len(excl)

        s3 = [r for r in s2 if per_pair[(r["sanc_root"], r["linked_root"])]["n_scope"] > 0]
        counts["after_scope_fit"] = len(s3)
        s4 = [r for r in s3 if per_pair[(r["sanc_root"], r["linked_root"])]["kept"]]
        counts["after_active_window_fit"] = len(s4)
        counts["final_pairs"] = len(s4)
        out["funnel_counts"] = counts
        out["excluded_unverifiable"] = {
            "pairs": excluded_pairs_unverifiable,
            "pair_contract_occurrences": excluded_contracts_unverifiable,
            "sanction_rows": sum(1 for rows in sanc_by_root.values() for s in rows
                if (scope_mode == "strict" and (s["cadastro"] != "CEIS" or s["cls"] != "universal"))
                or (scope_mode == "approximate" and s["cls"] in ("unknown", "other"))),
            "note": ("Pairs with no verifiable scope-fit contract in this mode. "
                     "Counts cover all history of candidate linked roots and may repeat a contract across pairs. "
                     "Excluded scope is not evidence of absence; loose mode excludes none."),
        }

        # aggregate + timing on survivors
        names = _base_names
        timing_totals = {"before": 0, "after": 0, "unknown": 0}
        items = []
        for r in s4:
            key = (r["sanc_root"], r["linked_root"])
            kept_ids = per_pair[key]["kept"]
            contracts = list({c["numeroControlePNCP"]: c for c in ctr_by_root.get(r["linked_root"], [])
                         if c["numeroControlePNCP"] in kept_ids}.values())
            vals = [c["vg"] for c in contracts if c["vg"] is not None]
            total = round(sum(vals), 2) if vals else None
            mx = max(vals) if vals else 0
            dates = sorted(c["d"] for c in contracts if c["d"])
            # partner join timing at the linked root
            links = con.execute(
                "SELECT DISTINCT nome_socio, cnpj_cpf_socio, edge_type "
                "FROM related_edges WHERE sanc_root = ? AND linked_root = ?",
                [r["sanc_root"], r["linked_root"]],
            ).fetchall()
            timing = []
            w0 = str(r["window_start"])[:10]
            for nome, cpf, etype in links:
                if etype == "pf_name_masked_cpf":
                    row = con.execute(
                        "SELECT data_entrada, qualificacao FROM partners "
                        "WHERE cnpj_root = ? AND nome_socio = ? AND cnpj_cpf_socio = ?",
                        [r["linked_root"], nome, cpf],
                    ).fetchone()
                    entry_raw = str(row[0]) if row and row[0] else None
                    qual = row[1] if row else None
                else:
                    # corporate partner: entry date not comparable; reported as unknown
                    entry_raw, qual = None, "corporate"
                entry = None
                if entry_raw and not entry_raw.startswith("0000"):
                    d8 = re.sub(r"\D", "", entry_raw)
                    entry = (f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}" if len(d8) == 8
                             else entry_raw[:10])
                if not entry:
                    t = "unknown"
                else:
                    t = "before" if entry <= w0 else "after"
                timing_totals[t] += 1
                timing.append({
                    "nome_socio": nome if etype == "pf_name_masked_cpf" else None,
                    "edge_type": etype,
                    "qualificacao": qual,
                    "entry_date_at_linked": entry,
                    "joined_relative_to_sanction_start": t,
                })
            items.append({
                "sanc_root": r["sanc_root"],
                "sanc_name": r["sanc_name"],
                "linked_root": r["linked_root"],
                "linked_razao_social": names.get(r["linked_root"]),
                "shared_keys": r["shared_keys"],
                "max_key_degree": r["max_key_degree"],
                "window_start": str(r["window_start"])[:10],
                "window_end": None if str(r["window_end"]).startswith("2100")
                              else str(r["window_end"])[:10],
                "sanction_rows": sanc_by_root.get(r["sanc_root"], []),
                "window_note": "Union dates are context only; contract fit uses each sanction row separately.",
                "same_group_heuristic": _same_group(r["sanc_name"], names.get(r["linked_root"])) or set(r["edge_types"] or []) == {"pj_exact_cnpj"},
                "weak_link": (r["max_key_degree"] or 0) >= WEAK_LINK_DEGREE,
                "contracts_in_scope_window": len(kept_ids),
                "contract_ids": sorted(kept_ids),
                "total_valor_global_brl": total,
                "first_signature": dates[0] if dates else None,
                "last_signature": dates[-1] if dates else None,
                "outlier_dominated": bool(total and mx > 0.9 * total),
                "partner_join_timing": timing,
            })
        items.sort(key=lambda i: (-(i["total_valor_global_brl"] or 0), i["sanc_root"], i["linked_root"]))
        out["partner_join_timing_totals"] = timing_totals
        out["count"] = len(items)
        out["truncated"] = len(items) > max(1, min(int(limit), MAX_ROWS))
        out["timing_totals_unit"] = "partner links across all surviving pairs, not distinct people or pairs"
        out["limit"] = int(limit)
        out["rows"] = items[: max(1, min(int(limit), MAX_ROWS))]
        return out

def create_server() -> FastMCP:
    log_path = os.environ.get("USAGE_LOG")
    if log_path:
        usage_configure(log_path)
    return mcp
