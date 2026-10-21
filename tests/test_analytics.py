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
