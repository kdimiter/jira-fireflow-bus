import tempfile
import unittest
from pathlib import Path
from test_sync import MAPPING, issue, Jira as Intake
from algosec_jira_bus.sync import State, run, mirror

class BoardStatus(unittest.TestCase):
    def test_confirmed_create_moves_even_when_fireflow_read_fails(self):
        with tempfile.TemporaryDirectory() as d:
            state=State(Path(d)/'state.json')
            class FF:
                def create(self,*args): return {'response':{'data':{'changeRequestId':33}}}
                def get(self,*args): raise ValueError('not visible yet')
            class Jira(Intake):
                moves=[]
                def transition(self,key,target): self.moves.append(target)
            jira=Jira([issue()]);ff=FF()
            cfg={'jira':{'jql':'project=NET'},'mapping':MAPPING,'fireflow':{'template':'T','devices':['fw1']},'mirror':{'submitted_transition':'In Progress'}}
            run(cfg,ff,state,jira,dry_run=False,log=lambda *a:None)
            self.assertEqual(state.entries()['NET-12']['pending_transition'],'In Progress')
            mirror(cfg,ff,state,jira,dry_run=False,log=lambda *a:None)
            self.assertEqual(jira.moves,['In Progress'])

    def test_mapping_correction_and_later_phases_move_only_once(self):
        with tempfile.TemporaryDirectory() as d:
            state=State(Path(d)/'state.json')
            state.record('NET-1',{'change_request_id':33,'status':'plan','workflow_details':{}})
            class FF:
                status='plan'
                def get(self,*a): return {'response':{'data':{'status':self.status}}}
            class Jira:
                moves=[]
                comments=[]
                def transition(self,key,target): self.moves.append(target)
                def comment(self,key,text): self.comments.append(text)
            ff,jira=FF(),Jira()
            cfg={'jira':{},'mirror':{'transitions':dict.fromkeys(['plan','approve','implement'],'In Progress')}}
            for phase in ['plan','plan','approve','implement']:
                ff.status=phase
                mirror(cfg,ff,state,jira,dry_run=False,log=lambda *a:None)
            self.assertEqual(jira.moves,['In Progress'])
            self.assertEqual(len(jira.comments),2)
