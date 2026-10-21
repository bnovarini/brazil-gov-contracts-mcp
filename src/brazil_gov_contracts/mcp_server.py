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


def create_server() -> FastMCP:
    log_path = os.environ.get("USAGE_LOG")
    if log_path:
        usage_configure(log_path)
    return mcp
