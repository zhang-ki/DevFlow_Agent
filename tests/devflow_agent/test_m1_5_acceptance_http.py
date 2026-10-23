from __future__ import annotations
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from scripts import m1_5_http_support as support


class Response(io.BytesIO):
    status = 200


class AcceptanceHTTPTests(unittest.TestCase):
    def test_version_stderr_is_evidence_not_powershell_exception(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(support.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='version: 11424 (6c59c4007)\n')
            result = support.check_version(Path(directory))
            self.assertEqual(result['status'], 'PASS')
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(run.call_args.kwargs['timeout'], 10)
            self.assertEqual((Path(directory)/'runtime-version.stderr.log').read_text(), result['stderr'])

    def test_version_nonzero_and_timeout_fail_closed(self):
        for outcome in (SimpleNamespace(returncode=2, stdout='11424', stderr='failed'), subprocess.TimeoutExpired('version',10)):
            with self.subTest(outcome=type(outcome).__name__), tempfile.TemporaryDirectory() as directory, patch.object(support.subprocess, 'run') as run:
                if isinstance(outcome, Exception): run.side_effect=outcome
                else: run.return_value=outcome
                self.assertEqual(support.check_version(Path(directory))['status'], 'FAIL')

    def test_explicit_empty_proxy_and_reject_redirect(self):
        with patch.object(support, 'build_opener') as build:
            support.local_opener()
            self.assertEqual(build.call_args.args[0].proxies, {})
            self.assertIsInstance(build.call_args.args[1], support.NoRedirect)
        self.assertIsNone(support.NoRedirect().redirect_request(None,None,302,'',{},'https://example.invalid'))

    def test_health_polls_then_checks_exact_model_identity(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(support, 'request_json') as request:
            request.side_effect=[{'http_status':503,'error':None}, {'http_status':200,'error':None,'json':{'status':'ok'}}, {'http_status':200,'error':None,'json':{'data':[{'id':support.ALIAS}]}}]
            result=support.check_health(Path(directory),opener=object(),budget_seconds=1,poll_seconds=0)
            self.assertEqual((result['health'],result['models']),('PASS','PASS'))
            self.assertEqual(len(result['attempts']),2)
            self.assertEqual([c.args[1] for c in request.call_args_list],['/health','/health','/v1/models'])
        with tempfile.TemporaryDirectory() as directory, patch.object(support, 'request_json') as request:
            request.side_effect=[{'http_status':200,'error':None,'json':{'status':'ok'}}, {'http_status':200,'error':None,'json':{'data':[{'id':'wrong-model'}]}}]
            result=support.check_health(Path(directory),opener=object(),budget_seconds=1,poll_seconds=0)
            self.assertEqual(result['models'],'FAIL')

    def test_health_deadline_prevents_models_and_preserves_connection_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(support,'request_json') as request:
            result=support.check_health(Path(directory),opener=object(),budget_seconds=0)
            self.assertEqual((result['health'],result['models']),('FAIL','NOT_RUN'))
            request.assert_not_called()
        opener=SimpleNamespace(open=lambda *a,**kw: (_ for _ in ()).throw(URLError(ConnectionRefusedError(10061,'refused'))))
        event=support.request_json(opener,'/health')
        self.assertIsNone(event['http_status'])
        self.assertEqual(event['error']['chain'][1]['errno'],10061)
        self.assertIn('URLError',event['error']['chain'][0]['type'])
        with self.assertRaises(ValueError): support.request_json(opener,'/outside')

if __name__=='__main__': unittest.main()
