#!/usr/bin/env bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 22.04 || $(uname -m) != x86_64 || $EUID != 0 ]]; then
    printf '%s\n' 'Run sudo bash install.sh on Ubuntu 22.04 x86_64.' >&2
    exit 2
fi
for tool in python3 curl systemctl systemd-tmpfiles nvidia-smi flock; do
    command -v "$tool" >/dev/null
done
systemctl show --property=Version --value >/dev/null
release=v1.7.0
archive=krig-miner-1.7.0-linux-x64.tar.gz
digest=fb380f1f4d7182b183419934c70c3514853da84a648dfe6cb5295f5e1268f524
stage=$(mktemp -d)
trap 'rm -f -- "$stage/miner.tar.gz" "$stage/krig-miner" "$stage/config.json"; rmdir -- "$stage"' EXIT
config=/etc/pearl-gpu-guard/config.json
if [[ $# -gt 0 || ! -f $config ]]; then
    python3 -I guard.py configure "$@" > "$stage/config.json"
fi
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 600 --retry 2 \
    "https://github.com/kryptex/krig-miner/releases/download/$release/$archive" -o "$stage/miner.tar.gz"
printf '%s  %s\n' "$digest" "$stage/miner.tar.gz" | sha256sum --check --status
python3 -I - "$stage/miner.tar.gz" "$stage/krig-miner" <<'PY'
import pathlib
import shutil
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    candidates = [
        item for item in archive.getmembers()
        if item.isfile() and pathlib.PurePosixPath(item.name).name == "krig-miner"
    ]
    if len(candidates) != 1:
        raise SystemExit("KRig archive must contain exactly one regular krig-miner file")
    with archive.extractfile(candidates[0]) as source, open(sys.argv[2], "xb") as target:
        shutil.copyfileobj(source, target)
PY
getent group pearl-gpu >/dev/null || groupadd --system pearl-gpu
id pearl-guard >/dev/null 2>&1 || useradd --system --gid pearl-gpu \
    --home-dir /var/lib/pearl-gpu-guard --no-create-home --shell /usr/sbin/nologin pearl-guard
install -d -m 0755 /opt/pearl-gpu-guard
install -d -m 0750 -o root -g pearl-gpu /etc/pearl-gpu-guard
systemctl stop pearl-gpu-guard.service 2>/dev/null || {
    if systemctl is-active --quiet pearl-gpu-guard.service; then exit 1; fi
}
if [[ -s $stage/config.json ]]; then
    install -m 0640 -o root -g pearl-gpu "$stage/config.json" "$config"
fi
install -m 0755 "$stage/krig-miner" /opt/pearl-gpu-guard/krig-miner
install -m 0755 guard.py /opt/pearl-gpu-guard/guard.py
ln -sfn /opt/pearl-gpu-guard/guard.py /usr/local/bin/pearl-gpu-guard
install -m 0644 pearl-gpu-guard.service /etc/systemd/system/pearl-gpu-guard.service
install -m 0644 pearl-gpu-guard.tmpfiles /etc/tmpfiles.d/pearl-gpu-guard.conf
systemd-tmpfiles --create /etc/tmpfiles.d/pearl-gpu-guard.conf
systemctl daemon-reload
if systemctl is-enabled --quiet pearl-gpu-meter.service || systemctl is-active --quiet pearl-gpu-meter.service; then
    systemctl disable --now pearl-gpu-meter.service
fi
systemctl enable --now pearl-gpu-guard.service
printf '%s\n' 'Check systemctl status pearl-gpu-guard and sudo pearl-gpu-guard status.'
