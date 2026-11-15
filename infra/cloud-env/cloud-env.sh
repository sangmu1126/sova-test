#!/bin/sh
# One switch for the cloud test environments (AWS and GCP together); see
# cloud_env.py for the commands. Uses only the system python3 and the aws and
# gcloud command lines.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
exec "${SOVA_CLOUD_PYTHON:-python3}" "$HERE/cloud_env.py" "$@"
