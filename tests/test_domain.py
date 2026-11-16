import decimal
import unittest
from one_stop_cases.orchestration.engine import WorkflowEngine

class DomainTests(unittest.TestCase):
    def setUp(self): self.engine=WorkflowEngine()

    def item(self,**changes):
        value={'id':'case-1','steps':[{'id':'intake','requires':[]},{'id':'review','requires':['intake']},{'id':'deliver','requires':['review']}],'completed':[]};value.update(changes);return value
    def test_initial_step_available(self):self.assertEqual(self.engine.evaluate(self.item(),{}).facts['available'],['intake'])
    def test_dependency_unlock(self):self.assertEqual(self.engine.evaluate(self.item(completed=['intake']),{}).facts['available'],['review'])
    def test_unknown_dependency(self):self.assertIn('blocked:x:unknown_dependency',self.engine.evaluate(self.item(steps=[{'id':'x','requires':['missing']}]),{}).reasons)
    def test_progress(self):self.assertEqual(self.engine.evaluate(self.item(completed=['intake']),{}).value,decimal.Decimal(1)/decimal.Decimal(3))
    def test_completed_has_no_available(self):self.assertFalse(self.engine.evaluate(self.item(completed=['intake','review','deliver']),{}).accepted)
    def test_rank_ready_first(self):self.assertTrue(self.engine.rank([self.item(id='done',completed=['intake','review','deliver']),self.item(id='ready')],{})[0].accepted)
    def test_compare_progress(self):self.assertEqual(self.engine.compare(self.engine.rank([self.item()],{}),self.engine.rank([self.item(completed=['intake'])],{}))['change_count'],1)

