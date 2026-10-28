import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import notify
import requests
import signin


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root/'state'
        self.state.mkdir()
        (self.root/'telegram.json').write_text(json.dumps({'bot_token':'unit-test-token','chat_id':'123'}))
        self.patches = [patch.object(notify,'ROOT',self.root),patch.object(notify,'STATE',self.state),
                        patch.object(signin,'ROOT',self.root),patch.object(signin,'STATE',self.state),
                        patch.dict(os.environ,{},clear=True)]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temp.cleanup)
        for item in reversed(self.patches):
            self.addCleanup(item.stop)
        self.event={'time':'2026-10-27T09:42:00+08:00','result':'signed','points_added':10,
                    'points':80,'consecutive_days':1,'run_id':'run-a'}

    def successful_response(self):
        return Mock(ok=True,status_code=200,json=Mock(return_value={'ok':True}))

    def test_success_message_is_short_and_daily_deduped(self):
        message=notify.render(self.event)
        self.assertIn('+10分',message)
        self.assertIn('当前 80 分',message)
        self.assertLess(len(message),100)
        with patch.object(notify.requests,'post',return_value=self.successful_response()) as post:
            notify.report(self.event)
            notify.report({**self.event,'run_id':'run-b'})
            self.assertEqual(post.call_count,1)

    def test_delivery_failure_preserves_message_and_hides_token_url(self):
        secret_url='https://api.telegram.org/bot123456:very-secret-token/sendMessage'
        with patch.object(notify.requests,'post',side_effect=requests.ConnectionError(secret_url)):
            with self.assertLogs(notify.LOG,level='WARNING') as captured:
                notify.report(self.event)
        self.assertNotIn('very-secret-token',' '.join(captured.output))
        queued=list((self.state/'outbox').glob('*.json'))
        self.assertEqual(len(queued),1)
        item=json.loads(queued[0].read_text())
        self.assertEqual(item['attempts'],1)
        item['next_attempt']=0
        queued[0].write_text(json.dumps(item))
        with patch.object(notify.requests,'post',return_value=self.successful_response()):
            self.assertEqual(notify.flush(),1)
        self.assertEqual(list((self.state/'outbox').glob('*.json')),[])

    def test_telegram_rate_limit_is_respected(self):
        response=Mock(ok=False,status_code=429,json=Mock(return_value={
            'ok':False,'error_code':429,'parameters':{'retry_after':9000}}))
        with patch.object(notify.time,'time',return_value=1000),patch.object(notify.requests,'post',return_value=response):
            notify.report(self.event)
        item=json.loads(next((self.state/'outbox').glob('*.json')).read_text())
        self.assertGreaterEqual(item['next_attempt'],10000)

    def test_each_failed_execution_alerts_but_stop_hook_does_not_duplicate(self):
        event={**self.event,'result':'failed','error_type':'ReadTimeout'}
        (self.state/'status.json').write_text(json.dumps(event))
        with patch.object(notify.requests,'post',return_value=self.successful_response()) as post, \
             patch.object(notify.subprocess,'check_output',return_value='Result=exit-code\nInvocationID=run-a\n'), \
             patch.dict(os.environ,{'SERVICE_RESULT':'exit-code','INVOCATION_ID':'run-a'}):
            notify.report(event)
            notify.service_failure()
            self.assertEqual(post.call_count,1)
            notify.report({**event,'run_id':'run-b'})
            self.assertEqual(post.call_count,2)

    def test_crash_hook_covers_oom_and_ignores_success(self):
        with patch.object(notify.requests,'post',return_value=self.successful_response()) as post, \
             patch.object(notify.subprocess,'check_output',return_value='Result=oom-kill\nInvocationID=crashed\n'), \
             patch.dict(os.environ,{'SERVICE_RESULT':'oom-kill','INVOCATION_ID':'crashed'}):
            notify.service_failure()
            self.assertIn('内存不足',post.call_args.kwargs['json']['text'])
            with patch.dict(os.environ,{'SERVICE_RESULT':'success'}):
                notify.service_failure()
            self.assertEqual(post.call_count,1)

    def test_existing_checkin_is_quiet_but_point_gain_is_reported(self):
        (self.state/'points.json').write_text(json.dumps({'points':80}))
        with patch.object(notify.requests,'post',return_value=self.successful_response()) as post:
            signin.status('already_signed',points=80,consecutive_days=1)
            post.assert_not_called()
            signin.status('already_signed',points=100,consecutive_days=1)
            self.assertEqual(post.call_count,1)
            self.assertIn('积分 +20',post.call_args.kwargs['json']['text'])

    def test_delayed_point_credit_is_reconciled_without_another_post(self):
        day=signin.datetime.now(signin.ZoneInfo('Asia/Shanghai')).date().isoformat()
        (self.state/'pending-sign.json').write_text(json.dumps({'day':day,'points_before':80}))
        session=Mock()
        session.headers={}
        session.get.return_value=Mock(status_code=200,headers={'csrf':'test-csrf'})
        with patch.object(signin,'make_session',return_value=session),patch.object(signin,'pause'), \
             patch.object(signin,'save_session'),patch.object(notify.requests,'post',return_value=self.successful_response()), \
             patch.object(signin,'api',side_effect=[{'sign_flag':1,'sign_continuity_count':2},{'beans':90}]) as api:
            signin.checkin()
        result=json.loads((self.state/'status.json').read_text())
        self.assertEqual(result['result'],'signed')
        self.assertEqual(result['points_added'],10)
        self.assertFalse((self.state/'pending-sign.json').exists())
        self.assertTrue(all(call.args[2:] == () for call in api.call_args_list))

    def test_error_details_are_sanitized(self):
        event={**self.event,'result':'failed','detail':'cookie=secret password=hunter2 https://example.test/?token=private'}
        text=notify.render(event)
        self.assertNotIn('secret',text)
        self.assertNotIn('hunter2',text)
        self.assertNotIn('private',text)

    def test_new_session_cookies_take_precedence_over_old_request_capture(self):
        (self.state/'session.json').write_text(json.dumps({'cookies':[
            {'name':'SessionID','value':'new','domain':'.huaweicloud.com','path':'/'}]}))
        (self.state/'request.json').write_text(json.dumps({'headers':{
            'cookie':'SessionID=old; extra=bootstrap','user-agent':'browser-ua'}}))
        session=signin.make_session()
        self.assertEqual(session.cookies.get('SessionID'),'new')
        self.assertEqual(session.cookies.get('extra'),'bootstrap')
        self.assertEqual(session.headers['user-agent'],'browser-ua')

    def test_browser_renewal_discards_captured_cookies_and_csrf(self):
        captured={'headers':{'cookie':'SessionID=old','csrf':'old-csrf','user-agent':'browser-ua'}}
        (self.state/'request.json').write_text(json.dumps(captured))
        saved={'cookies':[{'name':'SessionID','value':'fresh'}],'origins':[]}
        signin.save_browser_session(Mock(storage_state=Mock(return_value=saved)))
        self.assertEqual(json.loads((self.state/'session.json').read_text()),saved)
        self.assertEqual(json.loads((self.state/'request.json').read_text())['headers'],
                         {'user-agent':'browser-ua'})

    def test_challenge_inside_iframe_is_reported_without_solving_it(self):
        main=Mock()
        main.locator.return_value.inner_text.return_value='密码登录'
        challenge=Mock()
        challenge.locator.return_value.inner_text.return_value='请完成安全验证 向右拖动滑块'
        self.assertTrue(signin.security_challenge(Mock(frames=[main,challenge])))
        text=notify.render({**self.event,'result':'needs_verification',
                            'detail':'CAPTCHA security verification required'})
        self.assertIn('安全验证',text)


if __name__ == '__main__':
    unittest.main()
