#!/bin/sh
# Build the native-messaging launcher. The Linux counterpart of build.ps1.
#
# The output name carries no .exe here, but the directory layout is the same
# one everywhere: cmd/nativehost/main.go derives the repository root as the
# binary's grandparent, so the launcher must land in <root>/bin/.
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
bin="$root/bin"
mkdir -p "$bin"

cd "$root"
go build -o "$bin/externalcall-host" ./cmd/nativehost
echo "$bin/externalcall-host"
