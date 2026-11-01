import json
import os
import subprocess
import tempfile
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[2]

class PolicyTests(unittest.TestCase):
    def test_org_policy_contract(self):
        rule=json.loads((ROOT/'rulesets/aaa-delivery-v3.json').read_text())
        self.assertEqual(rule['bypass_actors'], [])
        self.assertEqual(rule['conditions']['repository_name']['include'], ['~ALL'])
        self.assertEqual(rule['conditions']['repository_name']['exclude'], [])
        self.assertEqual(rule['conditions']['ref_name'], {'include':['~DEFAULT_BRANCH'],'exclude':[]})
        rules={r['type']:r.get('parameters',{}) for r in rule['rules']}
        self.assertTrue({'non_fast_forward','deletion','pull_request','required_status_checks'} <= rules.keys())
        self.assertEqual(rules['pull_request']['allowed_merge_methods'], ['squash'])
        self.assertTrue(rules['pull_request']['required_review_thread_resolution'])
        self.assertEqual(rules['pull_request']['required_approving_review_count'],0)
        self.assertTrue(rules['required_status_checks']['strict_required_status_checks_policy'])
        self.assertEqual(rules['required_status_checks']['required_status_checks'],[{'context':'agency-delivery/gate','integration_id':5021608}])

    def test_256_label_failures_remain_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub=Path(tmp)/'gh'
            stub.write_text('#!/usr/bin/env python3\nimport sys\nif sys.argv[1:3]==["repo","list"]:\n print("\\n".join("acme/repo"+str(i) for i in range(32)))\nelse: sys.exit(1)\n')
            stub.chmod(0o755)
            env={**os.environ,'PATH':tmp+os.pathsep+os.environ['PATH'],'ORG':'acme'}
            result=subprocess.run(['bash',str(ROOT/'scripts/ensure-pr-labels.sh'),'--apply'],env=env,capture_output=True,text=True)
            self.assertEqual(result.stderr.count('FAIL '),256)
            self.assertNotEqual(result.returncode,0)

    def test_legacy_doc_mergers_are_archived(self):
        for name in ['doc-auto-merge.yml','doc-auto-merge-sweep.yml','doc-auto-merge-tests.yml','doc-auto-merge-sweep-tests.yml']:
            self.assertFalse((ROOT/'.github/workflows'/name).exists())
            self.assertTrue((ROOT/'archives/gate-v3-2026-11-01'/name).is_file())

if __name__=='__main__':unittest.main()
