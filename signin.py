#!/usr/bin/env python3
"""Daily Huawei Developer Growth check-in; no credentials or tokens in logs."""
import argparse
import fcntl
import json
import logging
import os
import re
import random
from pathlib import Path
import sys
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from http.cookies import SimpleCookie

import requests
import notify

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
GROW = 'https://developer.huaweicloud.com/grow'
BASE = 'https://devdata.huaweicloud.com'
PREFIX = '/rest/developer/fwdu/rest/developer/user/hdcommunitysoservice/v1/growth/'
PERSONAL = '/rest/developer/fwdu/rest/developer/user/hdcommunityservice/v1/member/get-personal-info'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36'
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stdout)
LOG = logging.getLogger('huawei-signin')
RUN_ID = os.environ.get('INVOCATION_ID') or uuid.uuid4().hex

class LoginRequired(Exception):
    pass

class PointsPendingError(Exception):
    pass

def pause(low=1.4, high=4.5):
    """Small randomized gaps avoid bursts of unnecessary API requests."""
    time.sleep(random.uniform(low, high))

def atomic_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)

def status(result, send_notification=True, **kwargs):
    data = {'time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'result':result,
            'run_id':RUN_ID, **kwargs}
    ledger_path = STATE/'points.json'
    previous_path = ledger_path if ledger_path.exists() else STATE/'status.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    atomic_json(STATE/'status.json', data)
    LOG.info('%s', json.dumps(data, ensure_ascii=False))
    if result in ('signed', 'already_signed'):
        atomic_json(ledger_path, {'points':data['points'], 'time':data['time']})
    if send_notification:
        notify.report(data)
        if result == 'already_signed' and isinstance(previous.get('points'), (int, float)):
            gained = data['points'] - previous['points']
            if gained > 0:
                notify.report({**data, 'result':'points_increased', 'points_added':gained})

def make_session():
    session = requests.Session()
    session.headers.update({'User-Agent':UA, 'Origin':'https://developer.huaweicloud.com', 'Referer':GROW,
                            'Content-Type':'application/json;charset=UTF-8'})
    if not (STATE/'session.json').exists():
        raise LoginRequired('No saved login')
    saved = json.loads((STATE/'session.json').read_text())['cookies']
    for cookie in saved:
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
    if (STATE/'request.json').exists():
        captured=json.loads((STATE/'request.json').read_text())
        parsed=SimpleCookie()
        parsed.load(captured.get('headers',{}).get('cookie',''))
        saved_names = {cookie['name'] for cookie in saved}
        for name,morsel in parsed.items():
            if name not in saved_names:
                session.cookies.set(name,morsel.value,domain='.huaweicloud.com',path='/')
        for name,value in captured.get('headers',{}).items():
            if name != 'cookie' and name != 'csrf':
                session.headers[name]=value
    return session

def api(session, path, method='GET', data=None):
    pause()
    response = session.request(method, BASE+path, json=data, timeout=(15,45))
    if response.status_code in (401,403):
        raise LoginRequired('Login or CSRF expired')
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError('API returned a non-JSON response') from None
    if body.get('error_code'):
        if str(body['error_code']) in ('92212002','HD.70111004'):
            raise LoginRequired('API session expired')
        raise RuntimeError('API error '+str(body['error_code']))
    return body

def save_session(session):
    # Persist response cookie renewals, including the fresh CSRF cookie, atomically.
    previous=json.loads((STATE/'session.json').read_text())
    previous['cookies']=[{
        'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,
        'expires':c.expires if c.expires is not None else -1,
        'httpOnly':bool(c.has_nonstandard_attr('HttpOnly')),'secure':c.secure,'sameSite':'Lax'
    } for c in session.cookies]
    atomic_json(STATE/'session.json',previous)
    if (STATE/'request.json').exists():
        captured=json.loads((STATE/'request.json').read_text())
        prepared=session.prepare_request(requests.Request('GET',BASE+PREFIX+'sign'))
        captured['headers']['cookie']=prepared.headers.get('Cookie','')
        captured['headers'].pop('csrf',None)
        atomic_json(STATE/'request.json',captured)

def save_browser_session(context):
    """Browser renewal replaces the old session, including captured Cookie values."""
    atomic_json(STATE/'session.json', context.storage_state())
    captured_path = STATE/'request.json'
    if captured_path.exists():
        captured = json.loads(captured_path.read_text())
        captured['headers'] = {name:value for name,value in captured.get('headers',{}).items()
                               if name.lower() not in ('cookie', 'csrf')}
        atomic_json(captured_path, captured)

def security_challenge(page):
    # Challenges may live in a cross-origin iframe, outside the main body text.
    for frame in page.frames:
        try:
            text = frame.locator('body').inner_text(timeout=1500)
        except Exception:
            continue
        if any(term in text for term in ('请完成安全验证', '向右拖动滑块',
                                         'Drag the slider', 'Complete the security verification')):
            return True
    return False

def checkin():
    session = make_session()
    token = None
    for origin in ['https://developer.huaweicloud.com', BASE]:
        pause()
        response = session.get(origin+'/api/get-ainfo', timeout=(15,45),allow_redirects=False)
        if response.status_code == 200 and response.headers.get('csrf'):
            token = response.headers['csrf']
            break
    if not token:
        raise LoginRequired('No CSRF token')
    session.headers['csrf'] = token
    before = api(session, PREFIX+'sign')
    beans_before = api(session, PREFIX+'bonus-beans')['beans']
    pending_path = STATE/'pending-sign.json'
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else {}
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    if before.get('sign_flag') == 1:
        save_session(session)
        if pending.get('day') == today:
            baseline = pending['points_before']
            if beans_before > baseline:
                pending_path.unlink(missing_ok=True)
                status('signed', points_before=baseline, points=beans_before,
                       points_added=beans_before-baseline, consecutive_days=before.get('sign_continuity_count'))
                return
            status('signed_points_pending', points_before=baseline, points=beans_before,
                   consecutive_days=before.get('sign_continuity_count'))
            raise PointsPendingError('Point credit not yet confirmed')
        status('already_signed', points=beans_before, consecutive_days=before.get('sign_continuity_count'))
        return
    personal = api(session, PERSONAL)
    alias = personal.get('memAlias')
    if not alias or personal.get('parentMemID'):
        raise RuntimeError('An eligible personal account is required')
    atomic_json(pending_path, {'day':today, 'points_before':beans_before})
    pause(6, 17)
    api(session, PREFIX+'sign', 'POST', {'account_name':alias})
    for attempt in range(6):
        pause(4, 9)
        after = api(session, PREFIX+'sign')
        beans_after = api(session, PREFIX+'bonus-beans')['beans']
        if after.get('sign_flag') == 1 and beans_after > beans_before:
            save_session(session)
            pending_path.unlink(missing_ok=True)
            status('signed', points_before=beans_before, points=beans_after,
                   points_added=beans_after-beans_before, consecutive_days=after.get('sign_continuity_count'))
            return
    if after.get('sign_flag') != 1:
        raise RuntimeError('Check-in POST did not produce signed status')
    save_session(session)
    status('signed_points_pending', points_before=beans_before, points=beans_after,
           consecutive_days=after.get('sign_continuity_count'))
    raise PointsPendingError('Signed, but point credit not yet confirmed')

def renew_login(interactive=False):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(STATE/'browser'), executable_path='/usr/bin/chromium', headless=not (interactive and os.environ.get('DISPLAY')),
            args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--renderer-process-limit=2'],
            locale='zh-CN', timezone_id='Asia/Shanghai',
            viewport={'width':1280,'height':900})
        try:
            if (STATE/'session.json').exists():
                saved=json.loads((STATE/'session.json').read_text())
                valid=[c for c in saved.get('cookies',[]) if c.get('expires',-1)<0 or c['expires']>time.time()]
                context.add_cookies(valid)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            LOG.info('Loading growth center in browser')
            page.goto(GROW, wait_until='domcontentloaded', timeout=120000)
            page.wait_for_timeout(12000)
            if '已签到' not in page.locator('body').inner_text() and '立即签到' not in page.locator('body').inner_text():
                LOG.info('Opening Huawei login')
                page.goto('https://auth.huaweicloud.com/authui/login?locale=zh-cn&service=https%3A%2F%2Fdeveloper.huaweicloud.com%2Fgrow',wait_until='domcontentloaded',timeout=120000)
                page.wait_for_timeout(10000)
                if urlparse(page.url).hostname == 'auth.huaweicloud.com':
                    LOG.info('Submitting account password')
                    config = json.loads((ROOT/'credentials.json').read_text())
                    page.get_by_text(re.compile(r'^(密码登录|Password Login)$',re.I)).click()
                    page.locator('input[name="userAccount"]').filter(visible=True).fill(config['username'])
                    page.locator('input[type="password"]').filter(visible=True).fill(config['password'])
                    page.locator('[ht="click_pwdlogin_submitLogin"]').click()
                    page.wait_for_timeout(15000)
                    body = page.locator('body').inner_text()
                    if interactive and os.environ.get('DISPLAY'):
                        LOG.info('MANUAL_LOGIN: complete any CAPTCHA or SMS verification in the browser window')
                        status('needs_verification', send_notification=False, detail='Complete login in the remote browser window')
                        for _ in range(360):
                            if urlparse(page.url).hostname == 'developer.huaweicloud.com':
                                break
                            page.wait_for_timeout(5000)
                        else:
                            raise LoginRequired('Timed out waiting for manual login')
                        body = page.locator('body').inner_text()
                    sms_input = page.get_by_placeholder(re.compile(r'短信验证码|SMS code',re.I)).filter(visible=True)
                    if '需要验证您的身份' in body or '身份验证' in body or sms_input.count():
                        status('needs_verification', send_notification=False, detail='Huawei requires SMS identity verification')
                        page.screenshot(path=str(STATE/'verification.png'))
                        if not interactive:
                            raise LoginRequired('SMS identity verification required; run --bootstrap')
                        otp_path = STATE/'otp.txt'
                        otp_path.unlink(missing_ok=True)
                        send = page.get_by_text(re.compile(r'^(获取验证码|Get code)$',re.I)).filter(visible=True)
                        send.click()
                        LOG.info('SMS_REQUESTED: waiting up to 20 minutes for state/otp.txt')
                        for _ in range(240):
                            if otp_path.exists():
                                code = otp_path.read_text().strip()
                                otp_path.unlink()
                                if not code.isdigit() or not 4 <= len(code) <= 8:
                                    LOG.warning('Invalid code format; waiting for another code')
                                    continue
                                sms_input.fill(code)
                                page.get_by_text(re.compile(r'^(验证|Verify)$',re.I)).filter(visible=True).click()
                                page.wait_for_timeout(15000)
                                if urlparse(page.url).hostname == 'developer.huaweicloud.com':
                                    break
                                LOG.warning('Verification not completed; waiting for another code')
                            page.wait_for_timeout(5000)
                        else:
                            raise LoginRequired('Timed out waiting for SMS verification')
            if urlparse(page.url).hostname != 'developer.huaweicloud.com':
                page.screenshot(path=str(STATE/'login-failed.png'))
                if security_challenge(page):
                    raise LoginRequired('CAPTCHA security verification required')
                safe_text = page.locator('body').inner_text()
                LOG.error('Login page signals: verification=%s captcha=%s password_error=%s',
                          '身份验证' in safe_text,'验证码' in safe_text,'密码错误' in safe_text)
                raise LoginRequired('Login was not completed')
            page.wait_for_timeout(10000)
            save_browser_session(context)
            page.screenshot(path=str(STATE/'growth.png'))
            LOG.info('Browser login saved')
        except Exception:
            try:
                page.screenshot(path=str(STATE/'browser-error.png'))
                (STATE/'browser-error.html').write_text(page.content(),encoding='utf-8')
            except Exception:
                pass
            raise
        finally:
            context.close()

def main():
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    with (STATE/'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info('Another run is active; skipping')
            return
        try:
            if args.bootstrap:
                renew_login(interactive=True)
            try:
                checkin()
            except LoginRequired:
                if args.bootstrap:
                    raise
                LOG.info('Refreshing browser login')
                renew_login()
                checkin()
                notify.report({'time':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                               'result':'login_recovered','run_id':RUN_ID})
        except LoginRequired as error:
            status('needs_verification', detail=str(error))
            sys.exit(2)
        except PointsPendingError:
            sys.exit(1)
        except Exception as error:
            # Avoid logging request headers, cookies, credentials, or URL query tokens.
            status('failed', error_type=type(error).__name__, detail=str(error).split('?')[0][:180])
            sys.exit(1)

if __name__ == '__main__':
    main()
