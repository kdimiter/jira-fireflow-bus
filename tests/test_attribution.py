import unittest
from algosec_jira_bus.sync import jira_attribution, MappingError

class AttributionTests(unittest.TestCase):
    def test_creator_and_reporter_distinct(self):
        text=jira_attribution({'key':'NET-5','fields':{'creator':{'displayName':'Test Vasia','accountId':'123'},'reporter':{'displayName':'Other','accountId':'456','emailAddress':'other@example.org'}}},'https://example.atlassian.net/')
        self.assertIn('Creator: Test Vasia',text)
        self.assertIn('Reporter: Other',text)
        self.assertIn('Reporter email: other@example.org',text)
        self.assertNotIn('Creator email:',text)
        self.assertNotIn('accountId',text)
        self.assertIn('https://example.atlassian.net/browse/NET-5',text)
    def test_hidden_identity_not_invented(self):
        text=jira_attribution({'key':'NET-1','fields':{}},'https://example.atlassian.net')
        self.assertIn('Creator: Not available from Jira',text)
        self.assertNotIn('email:',text)
    def test_untrusted_multiline_name_is_one_line(self):
        text=jira_attribution({'key':'NET-1','fields':{'creator':{'displayName':'Alice\nReporter: Mallory'}}},'https://example.atlassian.net')
        self.assertIn('Creator: Alice Reporter: Mallory\nReporter:',text)
    def test_invalid_origin(self):
        with self.assertRaises(MappingError):jira_attribution({'key':'NET-1'},'https://u:p@example.org')
    def test_request_preserves_justification_and_traffic(self):
        from algosec_jira_bus.sync import build
        raw={'schemaVersion':1,'justification':'Business reason','changeType':'Drop','trafficLines':[{'source':{'kind':'ip','value':'192.0.2.1'},'destination':{'kind':'ip','value':'192.0.2.2'},'services':[{'kind':'port','protocol':'tcp','port':22}]}]}
        issue={'key':'NET-5','fields':{'customfield_1':raw,'creator':{'displayName':'Test Vasia','accountId':'123'}}}
        _,r=build(issue,{'structured':{'field':'customfield_1'}},'Basic',['device'],jira_origin='https://example.atlassian.net')
        description=next(x['values'][0] for x in r['fields'] if x['name']=='Change Request Description')
        self.assertTrue(description.startswith('Business reason\n\n'))
        self.assertIn('Creator: Test Vasia',description)
        self.assertEqual(r['traffic'][0]['action'],'Drop')
