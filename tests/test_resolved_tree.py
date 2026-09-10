import unittest
from algosec_jira_bus.sync import resolved_tree_verified

def ticket(id, status='resolved', result='SUCCESS', children=None):
 return {'response':{'status':'Success','data':{'id':id,'subChangeRequests':children,'fields':[{'name':'status','values':[status]},{'name':'Validation Result Details','values':[result]}]}}}
class ResolvedTree(unittest.TestCase):
 def test_all_children_must_be_resolved_and_validated(self):
  class FF:
   child=ticket(35)
   def get(self,id): return self.child
  ff=FF(); parent=ticket(33,children=[35])
  self.assertTrue(resolved_tree_verified(ff,parent,33))
  for status,result in [('review','SUCCESS'),('resolved','FAILURE'),('rejected','SUCCESS')]:
   ff.child=ticket(35,status,result)
   self.assertFalse(resolved_tree_verified(ff,parent,33))
 def test_cycle_and_wrong_identity_fail_closed(self):
  class FF:
   def get(self,id): return ticket(35,children=[33])
  self.assertFalse(resolved_tree_verified(FF(),ticket(33,children=[35]),33))
  self.assertFalse(resolved_tree_verified(FF(),ticket(34),33))
