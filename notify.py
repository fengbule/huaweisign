#!/usr/bin/env python3
"""Concise Telegram notifications with a persistent, retryable outbox."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import subprocess
import time
from zoneinfo import ZoneInfo

import requests

ROOT = Path(os.environ.get('HUAWEI_SIGNIN_ROOT', Path(__file__).resolve().parent))
STATE = ROOT / 'state'
LOG = logging.getLogger('huawei-signin.notify')
ERROR_RESULTS = {'failed', 'needs_verification', 'signed_points_pending', 'service_failed'}


def write_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.chmod(0o600)
    temp.replace(path)


def config():
    path = ROOT / 'telegram.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    token = os.environ.get('TELEGRAM_BOT_TOKEN') or data.get('bot_token')
    chat = os.environ.get('TELEGRAM_CHAT_ID') or data.get('chat_id')
    return {'bot_token': token, 'chat_id': str(chat)} if token and chat else None


@contextmanager
def locked():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (STATE / 'notify.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def safe_reason(text):
    text = re.sub(r'https?://\S+', '[链接已省略]', str(text))
    text = re.sub(r'\b\d{6,}:[A-Za-z0-9_-]{25,}', '[凭据已隐藏]', text)
    text = re.sub(r'(?i)(cookie|authorization|password|token|csrf)\s*[:=]\s*[^\s,;]+', r'\1=[已隐藏]', text)
    return ' '.join(text.split())[:110]


def render(data):
    result = data['result']
    stamp = datetime.fromisoformat(data['time']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')
    points = data.get('points', '?')
    days = data.get('consecutive_days', '?')
    if result == 'signed':
        message = f"✅ 华为云签到 +{data.get('points_added', '?')}分\n当前 {points} 分｜连续 {days} 天"
    elif result == 'points_increased':
        message = f"💰 华为云积分 +{data['points_added']}\n当前 {points} 分｜今日已签到"
    elif result == 'needs_verification':
        detail = data.get('detail', '')
        if 'CAPTCHA' in detail:
            message = '⚠️ 华为云登录失效\n重新登录被安全验证拦住，今日签到未确认'
        elif 'SMS' in detail:
            message = '⚠️ 华为云登录失效\n重新登录需要短信验证，今日签到未确认'
        else:
            message = '⚠️ 华为云登录失效\n自动恢复未完成，需要更新登录态'
    elif result == 'login_recovered':
        message = '✅ 华为云登录已自动恢复'
    elif result == 'signed_points_pending':
        message = '⚠️ 华为云已签到，积分未到账\n稍后重查，避免重复领取'
    elif result == 'service_failed':
        reason = {'oom-kill':'内存不足，进程被终止', 'timeout':'执行超时',
                  'signal':'进程异常中断', 'exit-code':'进程异常退出'}.get(data.get('service_result'), '服务异常退出')
        message = f'❌ 华为云签到进程异常\n{reason}，请查看服务日志'
    elif result == 'configured':
        message = (f'✅ 华为云通知已启用\n当前 {points} 分｜今日已签到\n'
                   '签到 08:00–10:00，补查 18:00–20:00（随机）')
    elif result == 'failed':
        kind = data.get('error_type', 'Error')
        reason = {'Timeout':'请求超时', 'ConnectTimeout':'连接超时', 'ReadTimeout':'响应超时',
                  'ConnectionError':'网络连接失败', 'SSLError':'TLS 连接失败',
                  'HTTPError':'接口请求失败'}.get(kind)
        message = '❌ 华为云签到失败\n' + (reason or safe_reason(data.get('detail') or kind))
    else:
        return None
    return f'{message}\n{stamp}'


def enqueue(data, key=None):
    if not config():
        return False
    message = render(data)
    if not message:
        return False
    day = data['time'][:10]
    if key is None:
        if data['result'] == 'signed':
            key = f'signed:{day}'
        else:
            key = f"{data['result']}:{data.get('run_id') or data['time']}"
    event_id = hashlib.sha256(key.encode()).hexdigest()
    with locked():
        sent_path = STATE / 'notify_sent.json'
        sent = json.loads(sent_path.read_text()) if sent_path.exists() else {}
        outbox = STATE / 'outbox'
        outbox.mkdir(mode=0o700, exist_ok=True)
        path = outbox / (event_id + '.json')
        if event_id in sent or path.exists():
            return False
        write_json(path, {'id': event_id, 'text': message, 'attempts': 0, 'next_attempt': 0,
                          'created_at': time.time()})
    return True


def flush(limit=10):
    settings = config()
    if not settings:
        return 0
    delivered = 0
    with locked():
        outbox = STATE / 'outbox'
        if not outbox.exists():
            return 0
        sent_path = STATE / 'notify_sent.json'
        sent = json.loads(sent_path.read_text()) if sent_path.exists() else {}
        pending = sorted(outbox.glob('*.json'), key=lambda p: p.stat().st_mtime)
        for path in pending[:limit]:
            item = json.loads(path.read_text())
            if item['id'] in sent:
                path.unlink()
                continue
            if item.get('next_attempt', 0) > time.time():
                continue
            retry_after = 0
            error = None
            try:
                response = requests.post(
                    f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage",
                    json={'chat_id': settings['chat_id'], 'text': item['text'],
                          'link_preview_options': {'is_disabled': True}}, timeout=(5, 12))
                body = response.json()
                if response.ok and body.get('ok'):
                    sent[item['id']] = time.time()
                    # Record delivery before removing the queued file.
                    sent = dict(sorted(sent.items(), key=lambda pair: pair[1])[-1000:])
                    write_json(sent_path, sent)
                    path.unlink()
                    delivered += 1
                    LOG.info('Telegram notification delivered')
                    continue
                retry_after = int(body.get('parameters', {}).get('retry_after', 0))
                error = f"HTTP {response.status_code}, API code {body.get('error_code', 'unknown')}"
            except (requests.RequestException, ValueError, TypeError) as exc:
                # Never stringify requests exceptions: their URL contains the bot token.
                error = type(exc).__name__
            item['attempts'] += 1
            delay = min(3600, 45 * 2 ** min(item['attempts'] - 1, 7)) + random.uniform(5, 40)
            item['next_attempt'] = time.time() + max(delay, retry_after)
            write_json(path, item)
            LOG.warning('Telegram delivery deferred (%s); message retained for retry', error)
            break
    return delivered


def report(data, key=None):
    try:
        enqueue(data, key)
        flush()
    except Exception as exc:
        LOG.error('Notification subsystem error: %s', type(exc).__name__)


def service_failure():
    command = ['systemctl', 'show', 'huawei-signin.service', '--property=Result',
               '--property=InvocationID', '--property=ExecMainStatus']
    info = {}
    if not os.environ.get('SERVICE_RESULT') or not os.environ.get('INVOCATION_ID'):
        try:
            output = subprocess.check_output(command, text=True, timeout=10)
            info = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        except (subprocess.SubprocessError, OSError):
            info = {'Result':'unknown'}
    result = os.environ.get('SERVICE_RESULT') or info.get('Result')
    if result in (None, 'success'):
        return
    current = STATE / 'status.json'
    data = json.loads(current.read_text()) if current.exists() else {}
    invocation = os.environ.get('INVOCATION_ID') or info.get('InvocationID')
    if invocation and data.get('run_id') == invocation and data.get('result') in ERROR_RESULTS:
        # The application already queued the error; cover OOM, signals, and timeouts here.
        report(data)
        return
    report({'time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'result': 'service_failed', 'service_result': result,
            'run_id': invocation or str(time.time_ns())})


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser()
    parser.add_argument('--flush', action='store_true')
    parser.add_argument('--service-failure', action='store_true')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    if args.service_failure:
        service_failure()
    elif args.test:
        latest = json.loads((STATE / 'status.json').read_text())
        latest.update(result='configured', time=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat())
        report(latest, key='configured:' + str(time.time_ns()))
    else:
        flush()


if __name__ == '__main__':
    main()
