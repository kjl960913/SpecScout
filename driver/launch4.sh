#!/bin/bash
# Start a docker-mode run of this framework (the directory above driver/) for one ProgramBench instance.
#   launch4.sh INSTANCE_ID MODEL [extra drive4.py args...]
#   e.g. launch4.sh mgdm__htmlq.6e31bc8 openai/gpt-6-sol --model-class litellm_response
# Workspace: ProgramBench_Guess_and_Check/<framework dir>_<name>_<model tag>_docker, a fresh copy
# of the committed framework. Image: programbench/<org>_1776_<name>:task_cleanroom_v6.
set -euo pipefail
FW=$(cd "$(dirname "$0")/.." && pwd)
B=$(dirname "$FW")
VENV=$B/mini_driver/.venv
iid=$1; model=$2; shift 2
name=${iid#*__}; name=${name%.*}
tag=${model##*/}; tag=${tag#gpt-6-}
image="programbench/${iid/__/_1776_}:task_cleanroom_v6"
ws=$B/$(basename "$FW")_${name}_${tag}_docker
if git -C "$FW" rev-parse >/dev/null 2>&1; then
    [ -z "$(git -C "$FW" status --short)" ] || { echo "framework has uncommitted changes" >&2; exit 1; }
fi
[ ! -e "$ws" ] || { echo "$ws already exists" >&2; exit 1; }
docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image"
mkdir -p "$ws"
rsync -a --exclude .git --exclude __pycache__ --exclude work --exclude tests "$FW/" "$ws/"
cd "$ws"
setsid nohup env -i PATH="$PATH" HOME="$HOME" USER="$USER" LOGNAME="$USER" SHELL=/bin/sh LANG=C.UTF-8 \
    "$VENV/bin/python" "$ws/driver/drive4.py" . --model "$model" --docker-image "$image" "$@" \
    > driver.out 2>&1 < /dev/null &
echo $! > driver.pid
echo "$ws  image=$image  pid=$!"
