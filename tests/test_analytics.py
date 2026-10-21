import unittest,duckdb
from brazil_gov_contracts.analytics import reverse_supplier_check,buyer_concentration

class AnalyticsTests(unittest.TestCase):
 def setUp(self):
  self.c=duckdb.connect()
  self.c.execute('create table contracts(niFornecedor varchar, orgaoCnpj varchar,orgaoRazaoSocial varchar,dataAssinatura varchar,valorGlobal double)')
  self.c.execute("insert into contracts values ('11111111000111','22222222000122','BUYER A','2026-01-01',80),('11111111000111','33333333000133','BUYER B','2026-01-02',20),('11111111000111','22222222000122','BUYER A','2027-01-01',1000)")
 def rank(self,**kw):return buyer_concentration(self.c,'2026-10-21',**kw)
 def test_ratio(self):self.assertEqual(self.rank(min_contracts=1,min_total_value=0)['rows'][0]['top_buyer_share'],.8)
 def test_future(self):self.assertEqual(self.rank()['quality']['future_signature_rows'],1)
 def test_threshold(self):self.assertEqual(self.rank()['count'],0)
 def test_value_threshold(self):self.assertEqual(self.rank(min_contracts=1,min_total_value=101)['count'],0)
 def test_signature_year(self):self.assertEqual(self.rank(signature_year=2025)['count'],0)
 def test_invalid_year(self):
  with self.assertRaises(ValueError):self.rank(signature_year=2027)
 def test_nonfinite_threshold(self):
  with self.assertRaises(ValueError):self.rank(min_total_value=float('nan'))
 def test_negative_contracts(self):
  with self.assertRaises(ValueError):self.rank(min_contracts=0)
 def test_invalid_dates(self):
  self.c.execute("insert into contracts values ('11111111000111','22222222000122','A','broken',500)")
  self.assertEqual(self.rank()['quality']['missing_or_invalid_signature_rows'],1)
 def test_nonpositive(self):
  self.c.execute("insert into contracts values ('11111111000111','22222222000122','A','2026-01-03',-1)")
  self.assertEqual(self.rank()['quality']['invalid_or_nonpositive_ceiling_rows'],1)
 def partner_tables(self):
  self.c.execute('create table partners(cnpj_root varchar,identificador_socio int,nome_socio varchar,cnpj_cpf_socio varchar,data_entrada varchar)')
  for t in ('ceis','cnep'):self.c.execute(f'create table {t}(cnpj varchar,codigo_da_sancao varchar,nome_do_sancionado varchar,razao_social_cadastro_receita varchar)')
  self.c.execute("insert into ceis values ('99999999000199','1','SANCTIONED','SANCTIONED')")
 def test_unavailable(self):self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['match_status'],'unavailable')
 def test_no_partner(self):
  self.partner_tables();self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['match_status'],'no_partner_data')
 def test_pf_no_contract_requirement(self):
  self.partner_tables();self.c.execute("insert into partners values ('11111111',2,'PERSON','***123456**','20200101'),('99999999',2,'PERSON','***123456**','20200101')")
  self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['count'],1)
 def test_homonym_different_digits(self):
  self.partner_tables();self.c.execute("insert into partners values ('11111111',2,'PERSON','***123456**','20200101'),('99999999',2,'PERSON','***654321**','20200101')")
  self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['count'],0)
 def test_pj_exact(self):
  self.partner_tables();self.c.execute("insert into partners values ('11111111',1,'CORP','55555555','20200101'),('99999999',1,'OTHER LABEL','55555555','20200101')")
  self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['count'],1)
 def test_foreign_not_matched(self):
  self.partner_tables();self.c.execute("insert into partners values ('11111111',3,'FOREIGN','','20200101'),('99999999',3,'FOREIGN','','20200101')")
  self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['count'],0)
 def test_blank_key_not_matched(self):
  self.partner_tables();self.c.execute("insert into partners values ('11111111',2,'PERSON','','20200101'),('99999999',2,'PERSON','','20200101')")
  self.assertEqual(reverse_supplier_check(self.c,'11111111000111')['count'],0)
 def test_self_sanction_separate(self):
  self.partner_tables();self.c.execute("insert into partners values ('99999999',2,'PERSON','***123456**','20200101')")
  r=reverse_supplier_check(self.c,'99999999000199');self.assertEqual(r['count'],0);self.assertEqual(len(r['direct_sanctions']['ceis']),1)
if __name__=='__main__':unittest.main()

class EntrantTests(unittest.TestCase):
 def setUp(self):
  from brazil_gov_contracts.analytics import cnae_new_entrants
  self.f=cnae_new_entrants;self.c=duckdb.connect()
  self.c.execute('create table registry(cnpj varchar,razao_social varchar,cnae_fiscal_principal varchar,matriz_filial varchar,data_inicio_atividade varchar)')
  self.c.execute("insert into registry values ('11111111000111','OLD FIRM','4771701','1','20000101'),('22222222000122','NEW FIRM','4771701','1','20260201'),('33333333000133','OTHER','9999999','1','20260101'),('44444444000244','BRANCH','4771701','2','20260101')")
  self.c.execute('create table contracts(niFornecedor varchar,dataAssinatura varchar,valorGlobal double)')
  self.c.execute("insert into contracts values ('11111111000111','2025-01-01',10),('11111111000111','2026-02-01',10),('22222222000122','2026-03-01',10),('33333333000133','2026-04-01',10),('44444444000244','2026-04-01',10)")
 def runq(self,**kw):return self.f(self.c,'4771701',as_of='2026-10-21',**kw)
 def test_prior_history_excluded(self):self.assertEqual({r['supplier_root'] for r in self.runq()['rows']},{'22222222','44444444'})
 def test_matriz_not_branch(self):self.assertEqual([r['supplier_root'] for r in self.runq(definition='newly_opened')['rows']],['22222222'])
 def test_future_excluded(self):
  self.c.execute("update contracts set dataAssinatura='2027-01-01' where niFornecedor='22222222000122'")
  r=self.runq();self.assertEqual(r['quality']['future_signature_rows_dataset'],1);self.assertNotIn('22222222',[x['supplier_root'] for x in r['rows']])
 def test_partner_absent(self):self.assertFalse(self.runq()['rows'][0]['partners']['available'])
 def test_sector(self):self.assertNotIn('33333333',[x['supplier_root'] for x in self.runq()['rows']])
 def test_limit(self):r=self.runq(limit=1);self.assertTrue(r['truncated']);self.assertEqual(r['qualifying_suppliers'],2)
 def test_invalid_definition(self):
  with self.assertRaises(ValueError):self.runq(definition='other')
 def test_invalid_date_range(self):
  with self.assertRaises(ValueError):self.runq(entry_since='2027-01-01')
 def test_missing_matriz_date(self):self.assertEqual(self.runq()['quality']['sector_roots_missing_matriz_opening_date'],1)
