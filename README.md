# 华为云成长中心自动签到

每日自动签到、核实积分到账，并推送简短 Telegram 通知。已在 Debian 12 / 约 456 MB 内存的服务器实测：2026-10-27 积分从 **70 增加到 80**，连续签到 1 天。

## 运行行为

- 北京时间每天 **08:00–10:00** 随机执行，**18:00–20:00** 随机补查。每日重新取随机延迟。
- API 请求之间随机间隔约 1.4–4.5 秒，提交签到前再等待 6–17 秒；仅请求完成签到所需的接口。
- 已签到不重复提交、不重复推送；如果补查发现总积分增加，会推送增加量。
- 签到后积分尚未到账时保留基准值，后续确认到账再通知，不把重复查询误报为新签到。
- 普通错误约 30 分钟后重试；需要身份验证时停止自动恢复并通知，不循环发送短信。
- Telegram 暂时不可达时，消息存入本地队列，约每 5–6 分钟重试；遵守 Telegram 的限流等待时间。
- `ExecStopPost` 还会报告进程异常退出、超时、内存不足；应用已经报告的同次错误不会重复通知。

示例通知：

```text
✅ 华为云签到 +10分
当前 80 分｜连续 1 天
10-27 09:42
```

```text
⚠️ 华为云登录失效
自动恢复未完成，需要更新登录态
10-28 09:16
```

随机执行与请求间隔用于避免固定整点和突发请求，不保证平台永不要求身份验证。

## 部署和更新

源码目录为 `/opt/huaweisign`；实际运行目录为 `/opt/huawei-signin`。运行目录保存凭据、登录态、虚拟环境和日志状态；更新源码不会覆盖这些文件。

```sh
apt-get update
apt-get install -y git python3 python3-venv
git clone https://github.com/fengbule/huaweisign.git /opt/huaweisign
sh /opt/huaweisign/install.sh
```

首次配置将示例文件中的占位值替换成真实信息，放在运行目录；已有部署无需重复操作：

```sh
cp /opt/huaweisign/credentials.example.json /opt/huawei-signin/credentials.json
cp /opt/huaweisign/telegram.example.json /opt/huawei-signin/telegram.json
chmod 600 /opt/huawei-signin/credentials.json /opt/huawei-signin/telegram.json
# 编辑两个配置文件，并导入有效登录状态后再运行。
```

维护更新只需：

```sh
sh /opt/huawei-signin/update.sh
```

等价于 `git pull --ff-only` 后运行安装脚本；仓库有本地冲突时停止更新，不强制覆盖。

## 检查与通知测试

```sh
systemctl list-timers huawei-signin.timer huawei-signin-notify.timer
systemctl start huawei-signin.service
journalctl -u huawei-signin.service -n 40 --no-pager
journalctl -u huawei-signin-notify.service -n 20 --no-pager
cat /opt/huawei-signin/state/status.json
/opt/huawei-signin/venv/bin/python /opt/huawei-signin/notify.py --test
```

测试通知返回成功日志 `Telegram notification delivered`；投递失败会保留在 `state/outbox/`。已有签到状态的常规复查保持安静。

## 登录态与私密文件

首次使用的是本机普通 Chrome 的华为云登录状态和成长中心实际请求头，经 SSH 导入服务器。日常签到独立运行，不需要本机在线。

以下文件只留在服务器，禁止提交 Git：

| 文件 | 用途 |
| --- | --- |
| `credentials.json` | 华为云账号、密码，仅用于必要的登录恢复 |
| `telegram.json` | `bot_token`、`chat_id` |
| `state/session.json` | 浏览器 Cookie / 登录状态 |
| `state/request.json` | 当前有效请求头和 Cookie |
| `state/browser/` | 登录恢复使用的持久化浏览器配置 |

配置文件权限为 `600`，运行目录及状态目录为 `700`。每次成功查询刷新 CSRF、保存服务器返回的 Cookie。登录失效先尝试已有状态恢复，失败会推送通知；密码登录仍可能被华为要求短信或滑块验证，代码不承诺永久免验证。

运行结果：`signed` 为签到及积分到账已确认，`already_signed` 为今日已签到，`signed_points_pending` 为积分待到账，`needs_verification` 为登录恢复未完成，`failed` 为请求或运行错误。

## 验证代码

```sh
cd /opt/huaweisign
/opt/huawei-signin/venv/bin/python -m unittest discover -s tests -v
```

覆盖通知去重、失败保留与重试、限流等待、敏感信息隐藏、进程故障补报、已签到静默和延迟到账确认。测试使用临时目录和模拟网络，不向实际 Telegram 聊天发送故障通知。

## 参考资料

- [华为云开发者成长中心](https://developer.huaweicloud.com/grow)
- [已有 Cookie 签到项目](https://gitcode.com/2501_92692422/huawei-checkin/blob/master/README.md)
- [浏览器桥接项目](https://github.com/Dwight2458/developer-grow-mcp-extension)
- [Telegram Bot API](https://core.telegram.org/bots/api#sendmessage)

仓库不包含网站前端代码、Cookie、密码、Bot Token、账号截图或服务器地址。
