import unittest
from brazil_gov_contracts import mcp_server as m

class FunnelTests(unittest.TestCase):
    def c(self, date='2026-06-01', **kw):
        return dict(numeroControlePNCP='contract-1', d=date, vg=10, esf='FEDERAL', uf='DF', buyer='ORG A', **kw)
    def s(self, cls='universal', **kw):
        d=dict(cls=cls, cadastro='CEIS', d0='2026-01-01', d1='2026-12-31', esf='FEDERAL', uf='DF',org='ORG A'); d.update(kw); return d
    def fit(self,c,s,mode='strict',active=True):return m._pair_contract_fit([c],s,mode,active)[0]
    def test_universal(self):self.assertTrue(self.fit(self.c(),[self.s()]))
    def test_scope_strict(self):self.assertFalse(self.fit(self.c(),[self.s('sphere')]))
    def test_cnep_fine(self):self.assertFalse(self.fit(self.c(),[self.s(cadastro='CNEP')]))
    def test_approximate(self):self.assertTrue(self.fit(self.c(),[self.s('sphere')],'approximate'))
    def test_unknown(self):self.assertFalse(self.fit(self.c(),[self.s('unknown')],'approximate'))
    def test_loose(self):self.assertTrue(self.fit(self.c(),[self.s('unknown')],'loose'))
    def test_end(self):self.assertFalse(self.fit(self.c('2027-01-01'),[self.s()]))
    def test_end_inclusive(self):self.assertTrue(self.fit(self.c('2026-12-31'),[self.s()]))
    def test_start_inclusive(self):self.assertTrue(self.fit(self.c('2026-01-01'),[self.s()]))
    def test_before(self):self.assertFalse(self.fit(self.c('2025-12-31'),[self.s()]))
    def test_open(self):self.assertTrue(self.fit(self.c('2027-01-01'),[self.s(d1=None)]))
    def test_loosen_end(self):self.assertTrue(self.fit(self.c('2027-01-01'),[self.s()],active=False))
    def test_no_union_gap(self):self.assertFalse(self.fit(self.c(),[self.s(d1='2026-02-01'),self.s(d0='2026-08-01')]))
    def test_other_row_matches(self):self.assertTrue(self.fit(self.c(),[self.s(d1='2026-02-01'),self.s(d0='2026-05-01')]))
    def test_same_group(self):self.assertTrue(m._same_group('ACME LTDA','ACME SERVICOS'))
    def test_stopwords(self):self.assertFalse(m._same_group('ALFA LTDA','BETA LTDA'))
    def test_accents(self):self.assertEqual(m._scope_class('Todas as Esferas em todos os Poderes'),'universal')

if __name__=='__main__':unittest.main()
