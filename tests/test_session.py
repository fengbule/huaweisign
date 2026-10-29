import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import requests
import signin

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.patches=[patch.object(signin,'STATE',self.state),patch.object(signin,'pause')]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)

    def response(self, code, text='', body=None):
        r=requests.Response(); r.status_code=code; r._content=(json.dumps(body) if body is not None else text).encode()
        return r

    def test_csrf_failure_refreshes_once_without_password_login(self):
        s=Mock(); s.headers={}
        s.request.side_effect=[self.response(403,'csrf is nil'),self.response(200,body={'beans':100})]
        s.get.return_value=Mock(status_code=200,headers={'csrf':'renewed'})
        self.assertEqual(signin.api(s,signin.PREFIX+'bonus-beans'),{'beans':100})
        self.assertEqual(s.headers['csrf'],'renewed')
        self.assertEqual(s.request.call_count,2)

    def test_csrf_retry_is_bounded(self):
        s=Mock(); s.headers={}
        s.request.return_value=self.response(403,'csrf is nil')
        s.get.return_value=Mock(status_code=200,headers={'csrf':'renewed'})
        with self.assertRaises(signin.CsrfUnavailable): signin.api(s,signin.PREFIX+'sign')
        self.assertEqual(s.request.call_count,2)

    def test_unknown_403_does_not_trigger_login(self):
        s=Mock(); s.request.return_value=self.response(403,'access denied')
        with self.assertRaises(requests.HTTPError): signin.api(s,signin.PREFIX+'sign')
        s.get.assert_not_called()
        s.request.return_value=self.response(403,'not login')
        with self.assertRaises(signin.LoginRequired): signin.api(s,signin.PREFIX+'sign')

    def test_missing_csrf_is_not_proof_of_expired_login(self):
        s=Mock(); s.get.return_value=Mock(status_code=302,headers={'location':'/common/404.html'})
        with self.assertRaises(signin.CsrfUnavailable): signin.refresh_csrf(s)

    def test_refresh_is_read_only_and_preserves_main_result(self):
        main={'result':'signed','points':90}
        (self.state/'status.json').write_text(json.dumps(main))
        s=Mock()
        with patch.object(signin,'make_session',return_value=s),patch.object(signin,'refresh_csrf'), \
             patch.object(signin,'save_session') as save,patch.object(signin.notify,'report') as report, \
             patch.object(signin,'api',return_value={'beans':90}) as api:
            self.assertEqual(signin.maintain_session(),0)
            self.assertEqual(api.call_args.args,(s,signin.PREFIX+'bonus-beans'))
            save.assert_called_once_with(s); report.assert_not_called()
        self.assertEqual(json.loads((self.state/'status.json').read_text()),main)

    def test_refresh_failure_alerts_only_on_change_without_login_attempt(self):
        with patch.object(signin,'make_session',side_effect=signin.LoginRequired('expired')), \
             patch.object(signin.notify,'report') as report, patch.object(signin,'renew_login') as login:
            self.assertEqual(signin.maintain_session(),2)
            self.assertEqual(signin.maintain_session(),2)
            self.assertEqual(report.call_count,1); login.assert_not_called()
