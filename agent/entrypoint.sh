#!/bin/sh
set -e
trap 'python3 -m agent.cli shutdown --grace-period=30' TERM
exec "$@"
