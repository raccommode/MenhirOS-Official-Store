#!/bin/sh
set -eu

awk '
  /^[[:space:]]*- id:/ { id=$3 }
  /^[[:space:]]+path:/ { path=$2 }
  /^[[:space:]]+sha256:/ {
    if (id == "" || path == "") exit 2
    print id "\t" path "\t" $2
    id=""; path=""
  }
' store.yaml | while IFS="$(printf '\t')" read -r id path expected; do
  test -f "$path" || { echo "$id: missing manifest $path" >&2; exit 1; }
  actual=$(sha256sum "$path" | awk '{print $1}')
  test "$expected" = "$actual" || {
    echo "$id: manifest hash mismatch" >&2
    echo "expected: $expected" >&2
    echo "actual:   $actual" >&2
    exit 1
  }
done
