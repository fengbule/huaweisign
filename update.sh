#!/bin/sh
# Keep the editable checkout separate from the private runtime state.
set -eu
CHECKOUT=/opt/huaweisign
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo or as root.' >&2
    exit 1
fi
if [ ! -d "$CHECKOUT/.git" ]; then
    echo 'Missing checkout /opt/huaweisign. Clone the GitHub repository there first.' >&2
    exit 1
fi
git -C "$CHECKOUT" pull --ff-only
sh "$CHECKOUT/install.sh"
