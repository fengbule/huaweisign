#!/bin/sh
# Install code and units; preserve every runtime credential and session file.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo or as root.' >&2
    exit 1
fi
SOURCE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TARGET=/opt/huawei-signin
umask 077
install -d -m 700 "$TARGET" "$TARGET/state"
if systemctl is-active --quiet huawei-signin.service; then
    echo 'A check-in is running. Retry after it finishes.' >&2
    exit 1
fi
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y --no-install-recommends python3 python3-venv
fi
if [ ! -x "$TARGET/venv/bin/python" ]; then
    python3 -m venv "$TARGET/venv"
fi
"$TARGET/venv/bin/pip" install --disable-pip-version-check -r "$SOURCE/requirements.txt"
for file in signin.py notify.py requirements.txt README.md update.sh; do
    if [ "$SOURCE" != "$TARGET" ]; then
        install -m 600 "$SOURCE/$file" "$TARGET/$file"
    fi
done
chmod 700 "$TARGET/update.sh"
for unit in huawei-signin.service huawei-signin.timer huawei-signin-notify.service huawei-signin-notify.timer huawei-signin-refresh.service huawei-signin-refresh.timer; do
    install -m 644 "$SOURCE/$unit" "/etc/systemd/system/$unit"
done
for config in credentials.json telegram.json; do
    if [ -e "$TARGET/$config" ]; then chmod 600 "$TARGET/$config"; fi
done
"$TARGET/venv/bin/python" -m py_compile "$TARGET/signin.py" "$TARGET/notify.py"
systemd-analyze verify /etc/systemd/system/huawei-signin*.service /etc/systemd/system/huawei-signin*.timer
systemctl daemon-reload
systemctl enable huawei-signin.timer huawei-signin-notify.timer
systemctl restart huawei-signin.timer huawei-signin-notify.timer
systemctl enable huawei-signin-refresh.timer
systemctl restart huawei-signin-refresh.timer
systemctl list-timers huawei-signin.timer huawei-signin-notify.timer --no-pager
echo 'Installed. Credentials and sessions preserved.'
