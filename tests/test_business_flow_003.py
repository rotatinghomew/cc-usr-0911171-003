import datetime as dt
import unittest
from one_stop_cases.graph_runtime.orchestrator import CaseOrchestrator
class OrchestratorTests(unittest.TestCase):
 def setUp(self):self.flow=CaseOrchestrator();self.steps=[{'id':'intake','requires':[],'materials':['id']},{'id':'review','requires':['intake'],'materials':['degree'],'compensate':'reopen_review'},{'id':'notify','requires':['review'],'materials':[]}]
 def test_topological_order(self):self.assertEqual(self.flow.topological(self.steps),['intake','review','notify'])
 def test_unknown_dependency(self):
  with self.assertRaisesRegex(ValueError,'unknown_dependency'):self.flow.topological([{'id':'x','requires':['missing']}])
 def test_cycle(self):
  with self.assertRaisesRegex(ValueError,'cyclic_workflow'):self.flow.topological([{'id':'a','requires':['b']},{'id':'b','requires':['a']}])
 def test_material_gap_blocks(self):self.assertEqual(self.flow.material_gaps({'completed':['intake'],'materials':[{'kind':'id','verified':True}]},self.steps)['review'],['degree'])
 def test_review_available_after_evidence(self):self.assertEqual(self.flow.available({'completed':['intake'],'materials':[{'kind':'id','verified':True},{'kind':'degree','verified':True}]},self.steps),['review'])
 def test_compensation_reverses_completion(self):self.assertEqual(self.flow.compensation_plan(['intake','review'],self.steps),[{'step':'review','action':'reopen_review'}])
 def test_business_deadline_skips_weekend(self):self.assertEqual(self.flow.deadline(dt.datetime(2026,11,20,16,tzinfo=dt.timezone.utc),2,[]),dt.datetime(2026,11,23,10,tzinfo=dt.timezone.utc))
