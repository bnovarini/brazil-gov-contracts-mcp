"""Read-only supplier relationship and contract concentration queries."""
from datetime import date, datetime, timezone
import math
import re

NO_CLEARANCE = (
    "No match means no matching registered-partner link was found in the installed "
    "snapshots and subset. It is not clearance, proof of no relationship, or a live "
    "sanctions check. Partner data covers only supplier roots present in the "
    "September 2026 Receita subset, not every Brazilian company; historical "
    "ownership and ultimate beneficial ownership are not available. Individual "
    "matches use the published name plus masked CPF, not a full CPF."
)

def records(con, sql, params=None):
    cur = con.execute(sql, params or [])
    names = [x[0] for x in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def reverse_supplier_check(con, cnpj, limit=25):
    """Query partner rows directly, independently of contracts and precomputed edges."""
    root = cnpj[:8]
    limit = max(1, min(int(limit), 100))
    tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
    out = dict(cnpj=cnpj, cnpj_root=root, no_match_note=NO_CLEARANCE,
               snapshot_note="Receita partners: September 2026 supplier subset. CEIS/CNEP: installed snapshot, not live source refresh.",
               match_note="Individual: exact published name plus masked CPF. Corporate: exact corporate identifier (8-digit root or 14-digit CNPJ as stored). A registered administrator is not necessarily an owner.",
               no_score_note="Factual matches only; no suspicion or risk score.")
    if not {'partners','ceis','cnep'} <= tables:
        out.update(available=False, match_status='unavailable', count=0, rows=[], truncated=False)
        return out
    out['available'] = True
    partners = records(con, 'SELECT DISTINCT * FROM partners WHERE cnpj_root=? ORDER BY identificador_socio,nome_socio,cnpj_cpf_socio,data_entrada', [root])
    out['supplier_partners'] = partners
    out['supplier_partner_count'] = len(partners)
    out['unsupported_partner_rows'] = sum(1 for p in partners if p['identificador_socio'] not in (1,2))
    # No interpolation of identifiers or names. Exact keys, no name-only fallback.
    matched = records(con, """
        WITH keys AS (
          SELECT DISTINCT identificador_socio idf, nome_socio AS partner_name, cnpj_cpf_socio AS partner_key
          FROM partners WHERE cnpj_root=? AND identificador_socio IN (1,2)
        ), valid AS (
          SELECT * FROM keys WHERE
            (idf=2 AND coalesce(trim(partner_name),'')<>'' AND regexp_full_match(partner_key,'[0-9*]+')
             AND contains(partner_key,'*') AND length(regexp_replace(partner_key,'[^0-9]','','g'))=6)
            OR (idf=1 AND regexp_full_match(partner_key,'([0-9]{8}|[0-9]{14})'))
        ), sanctioned AS (
          SELECT DISTINCT substr(cnpj,1,8) root FROM ceis WHERE regexp_full_match(cnpj,'[0-9]{14}')
          UNION SELECT DISTINCT substr(cnpj,1,8) FROM cnep WHERE regexp_full_match(cnpj,'[0-9]{14}')
        )
        SELECT DISTINCT p.cnpj_root sanctioned_root,k.idf,k.partner_name,k.partner_key,
          CASE WHEN k.idf=2 THEN 'pf_name_masked_cpf' ELSE 'pj_exact_identifier' END match_type
        FROM valid k JOIN partners p
          ON p.identificador_socio=k.idf AND p.cnpj_cpf_socio=k.partner_key
          AND (k.idf=1 OR p.nome_socio=k.partner_name)
        JOIN sanctioned s ON s.root=p.cnpj_root
        WHERE p.cnpj_root<>? ORDER BY sanctioned_root,idf,partner_name,partner_key
    """, [root, root])
    out['direct_sanctions'] = {cad: records(con, f"SELECT * FROM {cad} WHERE substr(cnpj,1,8)=? ORDER BY codigo_da_sancao", [root]) for cad in ('ceis','cnep')}
    roots = sorted({r['sanctioned_root'] for r in matched})
    out['count'] = len(roots)
    out['match_status'] = 'matches_found' if roots else ('no_partner_data' if not partners else 'no_match_in_snapshot')
    out['truncated'] = len(roots)>limit
    out['limit'] = limit
    out['rows'] = []
    for sr in roots[:limit]:
        links = [r for r in matched if r['sanctioned_root']==sr]
        for link in links:
            link['supplier_partner_rows'] = [p for p in partners if p['identificador_socio']==link['idf'] and p['cnpj_cpf_socio']==link['partner_key'] and (link['idf']==1 or p['nome_socio']==link['partner_name'])]
            link['sanctioned_partner_rows'] = records(con,
                'SELECT DISTINCT * FROM partners WHERE cnpj_root=? AND identificador_socio=? AND cnpj_cpf_socio=? AND (?=1 OR nome_socio=?) ORDER BY data_entrada',
                [sr,link['idf'],link['partner_key'],link['idf'],link['partner_name']])
            link['key_degree'] = con.execute('SELECT count(DISTINCT cnpj_root) FROM partners WHERE identificador_socio=? AND cnpj_cpf_socio=? AND (?=1 OR nome_socio=?)', [link['idf'],link['partner_key'],link['idf'],link['partner_name']]).fetchone()[0]
            link['weak_link'] = link['key_degree']>=10
        sanctions = {cad:records(con, f'SELECT * FROM {cad} WHERE substr(cnpj,1,8)=? ORDER BY codigo_da_sancao', [sr]) for cad in ('ceis','cnep')}
        names = [s.get('razao_social_cadastro_receita') or s.get('nome_do_sancionado') for rows in sanctions.values() for s in rows]
        out['rows'].append(dict(sanctioned_root=sr, sanctioned_name=next((n for n in names if n),None), matched_partners=links, sanctions=sanctions))
    out['sanction_note'] = 'Rows retain their own dates, scope, category and organ. Expired rows and CNEP fines are not automatically current contracting bans. Direct supplier sanctions are separate from cross-company partner matches.'
    return out


def buyer_concentration(con, as_of=None, signature_year=None, min_contracts=10,
                        min_total_value=1000000.0, limit=25):
    """Rank suppliers by top buyer share of recorded positive ceilings, not revenue."""
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    as_of = date.fromisoformat(as_of).isoformat()
    if signature_year is not None and not 2021<=int(signature_year)<=date.fromisoformat(as_of).year:
        raise ValueError('signature_year must be between 2021 and the as_of year.')
    if int(min_contracts)<1: raise ValueError('min_contracts must be positive.')
    if not math.isfinite(float(min_total_value)) or float(min_total_value)<0:
        raise ValueError('min_total_value must be finite and nonnegative.')
    limit=max(1,min(int(limit),100))
    # Apply year to actual signature dates, never PNCP publication-year partitions.
    prefix = """WITH source AS (
      SELECT *, try_cast(dataAssinatura AS DATE) signature_date FROM contracts
    ), period AS (
      SELECT * FROM source WHERE (? IS NULL OR year(signature_date)=?)
    ), eligible AS (
      SELECT * FROM period WHERE signature_date<=?::DATE
        AND regexp_full_match(niFornecedor,'[0-9]{14}')
        AND regexp_full_match(orgaoCnpj,'[0-9]{14}')
        AND valorGlobal IS NOT NULL AND isfinite(valorGlobal) AND valorGlobal>0
    ), buyers AS (
      SELECT substr(niFornecedor,1,8) supplier_root,orgaoCnpj buyer_cnpj,
        min(orgaoRazaoSocial) buyer_name,count(*) n,sum(valorGlobal) v
      FROM eligible GROUP BY 1,2
    ), suppliers AS (
      SELECT supplier_root,sum(n) n,sum(v) v,count(*) n_buyers FROM buyers GROUP BY 1
    ), ranked_buyers AS (
      SELECT *,row_number() OVER(PARTITION BY supplier_root ORDER BY v DESC,buyer_cnpj) rn
      FROM buyers
    ), ranking AS (
      SELECT s.supplier_root,s.n contracts,s.v total_ceiling_brl,s.n_buyers distinct_buyers,
        b.buyer_cnpj top_buyer_cnpj,b.buyer_name top_buyer_name,b.n top_buyer_contracts,
        b.v top_buyer_ceiling_brl,b.v/s.v top_buyer_share
      FROM suppliers s JOIN ranked_buyers b USING(supplier_root)
      WHERE b.rn=1 AND s.n>=? AND s.v>=?
    ) """
    params=[signature_year,signature_year,as_of,int(min_contracts),float(min_total_value)]
    rows=records(con,prefix+'SELECT * FROM ranking ORDER BY top_buyer_share DESC,total_ceiling_brl DESC,supplier_root LIMIT ?',params+[limit])
    total=con.execute(prefix+'SELECT count(*) FROM ranking',params).fetchone()[0]
    quality=records(con,"""WITH s AS (SELECT *,try_cast(dataAssinatura AS DATE) d FROM contracts)
      SELECT count(*) dataset_rows,
        count(*) FILTER(WHERE d>?::DATE) future_signature_rows,
        count(*) FILTER(WHERE d IS NULL) missing_or_invalid_signature_rows,
        count(*) FILTER(WHERE (? IS NULL OR year(d)=?) AND d>?::DATE) future_rows_in_requested_period,
        count(*) FILTER(WHERE (? IS NULL OR year(d)=?) AND d<=?::DATE
          AND (NOT coalesce(regexp_full_match(niFornecedor,'[0-9]{14}'),false)
            OR NOT coalesce(regexp_full_match(orgaoCnpj,'[0-9]{14}'),false))) invalid_identifier_rows,
        count(*) FILTER(WHERE (? IS NULL OR year(d)=?) AND d<=?::DATE
          AND (valorGlobal IS NULL OR NOT isfinite(valorGlobal) OR valorGlobal<=0)) invalid_or_nonpositive_ceiling_rows
      FROM s""",[as_of,signature_year,signature_year,as_of,signature_year,signature_year,as_of,signature_year,signature_year,as_of])[0]
    quality['eligible_contract_rows']=con.execute(prefix+'SELECT count(*) FROM eligible',params).fetchone()[0]
    names={}
    if rows and 'registry' in {r[0] for r in con.execute('SHOW TABLES').fetchall()}:
        for n in records(con,'SELECT substr(cnpj,1,8) root,min(razao_social) AS company_name FROM registry GROUP BY 1'):
            names[n['root']]=n['company_name']
    for r in rows:
        r['supplier_name']=names.get(r['supplier_root'])
        for k in ('total_ceiling_brl','top_buyer_ceiling_brl'):r[k]=round(r[k],2)
        r['top_buyer_share_pct']=round(r['top_buyer_share']*100,4)
    return dict(metric='concentration of recorded contract ceilings, not revenue dependence',
      as_of=as_of,signature_year=signature_year,min_contracts=int(min_contracts),min_total_value=float(min_total_value),
      qualifying_suppliers=total,count=len(rows),limit=limit,truncated=total>limit,quality=quality,rows=rows,
      value_note='valorGlobal is a ceiling, not payments or company revenue. Positive finite ceilings only; outliers are retained and may distort ranking.',
      coverage_note='Installed PNCP snapshot, history starts in 2021; partial coverage is not total public/private revenue. Buyers are grouped by full CNPJ, suppliers by 8-digit legal-entity root. Thresholds apply after exclusions.',
      date_note='Future signatures excluded relative to as_of (default: current UTC date). signature_year uses actual signature year, not the PNCP partition/year. Quality exclusion categories can overlap; missing dates cannot be assigned to a signature year.')


def cnae_new_entrants(con, cnae, as_of=None, entry_since='2026-01-01',
                      definition='first_observed', limit=25):
    """Current principal-CNAE cohort, with separate observed-entry/opening definitions."""
    cnae=re.sub(r'[^0-9]','',cnae or '')
    if len(cnae)!=7:raise ValueError('cnae must contain a seven-digit principal CNAE code.')
    as_of=date.fromisoformat(as_of or datetime.now(timezone.utc).date().isoformat()).isoformat()
    entry_since=date.fromisoformat(entry_since).isoformat()
    if entry_since>as_of:raise ValueError('entry_since must not be later than as_of.')
    if definition not in ('first_observed','newly_opened'):
        raise ValueError('definition must be first_observed or newly_opened.')
    limit=max(1,min(int(limit),100))
    tables={r[0] for r in con.execute('SHOW TABLES').fetchall()}
    out=dict(cnae=cnae,as_of=as_of,entry_since=entry_since,definition=definition,
        label=('new to public contract data (observed history from 2021)' if definition=='first_observed'
               else 'newly opened matriz with recorded public contracts in the entry period'),
        coverage_note='Current September 2026 principal CNAE, not historical industry or secondary CNAEs. Sector membership means at least one recorded supplier establishment has this principal CNAE; first signature is checked across all establishments and sectors of its legal-entity root. First observed since 2021 is not first-ever contracting. Registry/partner files are supplier subsets; individuals and foreign/unmatched identifiers cannot be CNAE classified.',
        opening_note='Company age uses only the matriz opening date when present; branch opening dates are never substituted. newly_opened requires a dated matriz in the requested opening period plus at least one contract signature in that period, and does not require absence of earlier contracts.',
        partner_note='Registered partners/administrators, not ultimate owners. Missing partner rows mean unavailable in the installed subset, not no owners. Individual identifiers are masked. No risk or suspicion score.',
        value_note='valorGlobal is a recorded ceiling, not payments or company revenue; sums use finite positive values only.')
    if not {'registry','contracts'}<=tables:
        out.update(available=False,count=0,rows=[],truncated=False);return out
    out['available']=True
    prefix="""WITH reg AS (
      SELECT *,substr(cnpj,1,8) root,
        try_strptime(data_inicio_atividade,'%Y%m%d')::DATE opened
      FROM registry
    ), sector AS (
      SELECT root,min(razao_social) supplier_name,
        list(DISTINCT cnpj ORDER BY cnpj) cnae_matching_establishments
      FROM reg WHERE cnae_fiscal_principal=? GROUP BY root
    ), matrix AS (
      SELECT root,min(opened) FILTER(WHERE matriz_filial='1') matriz_opened
      FROM reg GROUP BY root
    ), signatures AS (
      SELECT *,substr(niFornecedor,1,8) root,try_cast(dataAssinatura AS DATE) d
      FROM contracts
    ), history AS (
      SELECT s.root,min(s.d) first_observed_signature,
        count(*) FILTER(WHERE s.d BETWEEN ?::DATE AND ?::DATE) contracts_in_entry_period,
        sum(CASE WHEN s.d BETWEEN ?::DATE AND ?::DATE AND isfinite(s.valorGlobal)
            AND s.valorGlobal>0 THEN s.valorGlobal ELSE 0 END) period_ceiling_brl
      FROM signatures s JOIN sector sec ON sec.root=s.root
      WHERE regexp_full_match(s.niFornecedor,'[0-9]{14}') AND s.d<=?::DATE
      GROUP BY s.root
    ), cohort AS (
      SELECT sec.*,m.matriz_opened,h.first_observed_signature,h.contracts_in_entry_period,
        h.period_ceiling_brl
      FROM sector sec JOIN history h USING(root) LEFT JOIN matrix m USING(root)
      WHERE h.contracts_in_entry_period>0 AND
        ((?='first_observed' AND h.first_observed_signature>=?::DATE)
          OR (?='newly_opened' AND m.matriz_opened BETWEEN ?::DATE AND ?::DATE))
    ) """
    params=[cnae,entry_since,as_of,entry_since,as_of,as_of,
            definition,entry_since,definition,entry_since,as_of]
    rows=records(con,prefix+'SELECT * FROM cohort ORDER BY first_observed_signature,root LIMIT ?',params+[limit])
    total=con.execute(prefix+'SELECT count(*) FROM cohort',params).fetchone()[0]
    quality=records(con,prefix+"""SELECT
      (SELECT count(*) FROM sector) sector_supplier_roots,
      (SELECT count(*) FROM sector sec JOIN matrix m USING(root) WHERE m.matriz_opened IS NULL) sector_roots_missing_matriz_opening_date,
      (SELECT count(*) FROM sector sec LEFT JOIN history h USING(root) WHERE h.root IS NULL) sector_roots_without_valid_observed_signature,
      (SELECT count(*) FROM signatures WHERE d>?::DATE) future_signature_rows_dataset,
      (SELECT count(*) FROM signatures WHERE d IS NULL) missing_or_invalid_signature_rows_dataset,
      (SELECT count(*) FROM signatures WHERE NOT coalesce(regexp_full_match(niFornecedor,'[0-9]{14}'),false)) non_cnpj_supplier_rows_dataset,
      (SELECT count(DISTINCT s.niFornecedor) FROM signatures s
        LEFT JOIN registry r ON s.niFornecedor=r.cnpj
        WHERE regexp_full_match(s.niFornecedor,'[0-9]{14}') AND r.cnpj IS NULL) supplier_cnpjs_without_registry,
      (SELECT count(*) FROM registry WHERE coalesce(trim(cnae_fiscal_principal),'')='') registry_rows_missing_principal_cnae,
      (SELECT count(*) FROM cohort) qualifying_supplier_roots
    """,params+[as_of])[0]
    for row in rows:
        root=row.pop('root');row['supplier_root']=root
        row['period_ceiling_brl']=round(row['period_ceiling_brl'],2)
        # Count separately from returned rows so truncation never implies absence.
        if 'partners' not in tables:
            row['partners']=dict(available=False,count=0,truncated=False,rows=[])
        else:
            n=con.execute('SELECT count(*) FROM partners WHERE cnpj_root=?',[root]).fetchone()[0]
            ps=records(con,'SELECT * FROM partners WHERE cnpj_root=? ORDER BY identificador_socio,nome_socio,cnpj_cpf_socio,data_entrada LIMIT 101',[root])
            row['partners']=dict(available=bool(n),count=n,truncated=n>100,rows=ps[:100],
                note='No rows means unavailable in this subset, not absence of partners.')
    out.update(count=len(rows),qualifying_suppliers=total,truncated=total>limit,limit=limit,quality=quality,rows=rows,
        date_note='Future/missing signatures are excluded. as_of defaults to UTC today. A backdated as_of still uses the current installed registry/partner snapshot, not historical ownership or CNAE.')
    return out
