"""Brazilian federal government contracts MCP server.

Data (read-only DuckDB over parquet):
  contracts : PNCP full available history 2021-2026 (5,043,759 contracts)
  sanctions : current CEIS + CNEP (Portal da Transparencia snapshot)
  registry  : Receita Federal 2026-09 CNPJ subset for suppliers seen in contracts
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

_con = None
mcp = FastMCP(
    "brazil-gov-contracts-mcp",
    instructions=(
        "Brazilian federal government contract and supplier due-diligence data. "
        "Contracts: full available PNCP history 2021-2026. Sanctions: current CEIS/CNEP. "
        "Registry: Receita Federal 2026-09 records for CNPJs appearing as suppliers. "
        "CNPJ identifiers are matched exactly (14 digits, no fuzzy matching)."
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


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.tool()
def dataset_info(ctx: Context) -> dict:
    """Dataset scope and totals: per-year contract counts, sanctions counts, registry presence."""
    with log_tool("dataset_info", ctx):
        con = _con_init()
        per_year = con.execute(
            "SELECT year, count(*) AS contracts FROM contracts GROUP BY year ORDER BY year"
        ).fetchall()
        out = {
            "contracts": {
                "source": "PNCP (Portal Nacional de Contratacoes Publicas), full available history",
                "per_year": {int(y): int(c) for y, c in per_year},
                "total": int(sum(c for _, c in per_year)),
            },
            "registry_present": "registry" in {
                r[0] for r in con.execute("SHOW TABLES").fetchall()
            },
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
        return out


@mcp.tool()
def vendor_due_diligence(cnpj: str, ctx: Context, max_contracts: int = 25) -> dict:
    """Due-diligence bundle for one supplier CNPJ: Receita registry record, CEIS/CNEP
    sanction matches, and PNCP contract summary with recent contracts.
    cnpj: 14-digit CNPJ (punctuation optional). Exact match only."""
    cnpj = _cnpj14(cnpj)
    with log_tool("vendor_due_diligence", ctx) as rec:
        con = _con_init()
        result = {"cnpj": cnpj, "registry": None, "sanctions": {}, "contracts": {}}
        if "registry" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            rows = con.execute(
                "SELECT * FROM registry WHERE cnpj = ?", [cnpj]
            ).fetchall()
            if rows:
                cols = [d[0] for d in con.description]
                result["registry"] = dict(zip(cols, rows[0]))
        for cad in ("ceis", "cnep"):
            rows, _ = _rows(
                con,
                f"SELECT * FROM {cad} WHERE cnpj = ?",
                [cnpj],
                limit=MAX_ROWS,
            )
            result["sanctions"][cad] = {"count": len(rows), "rows": rows}
        agg = con.execute(
            "SELECT count(*) AS n, sum(valorGlobal) AS total_valor_global, "
            "min(dataAssinatura) AS first_signature, max(dataAssinatura) AS last_signature, "
            "count(DISTINCT orgaoCnpj) AS distinct_buyers "
            "FROM contracts WHERE niFornecedor = ?",
            [cnpj],
        ).fetchone()
        result["contracts"]["summary"] = {
            "count": int(agg[0] or 0),
            "total_valor_global_brl": agg[1],
            "first_signature": agg[2],
            "last_signature": agg[3],
            "distinct_buyers": int(agg[4] or 0),
        }
        recent, truncated = _rows(
            con,
            "SELECT numeroControlePNCP, orgaoRazaoSocial, uf, objetoContrato, "
            "valorGlobal, dataAssinatura, dataVigenciaFim, dataPublicacaoPncp "
            "FROM contracts WHERE niFornecedor = ? "
            "ORDER BY dataAssinatura DESC NULLS LAST",
            [cnpj],
            limit=max_contracts,
        )
        result["contracts"]["recent"] = recent
        result["contracts"]["recent_truncated"] = truncated
        rec["status_note"] = "ok"
        return result


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
    cnpj: exact 14-digit supplier CNPJ. uf: two-letter state. year: contract year.
    buyer_name: substring on orgaoRazaoSocial. Results ordered by signature date, capped at 100."""
    with log_tool("search_contracts", ctx):
        con = _con_init()
        where, params = [], []
        if query:
            where.append("objetoContrato ILIKE '%' || ? || '%'")
            params.append(query)
        if cnpj:
            where.append("niFornecedor = ?")
            params.append(_cnpj14(cnpj))
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
        return {"count": len(rows), "truncated": truncated, "rows": rows}


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
    """Search current CEIS and CNEP sanctions. cnpj: exact 14-digit CNPJ match.
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
            if cnpj:
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
        return out


def create_server() -> FastMCP:
    log_path = os.environ.get("USAGE_LOG")
    if log_path:
        usage_configure(log_path)
    return mcp
