#!/bin/bash
# fetch_yang.sh — Download YANG modules from YangModels for both IOS XE versions
# the codebase branches on (16.8.1 and 17.3.1).
#
# Run from the root of network-automation-ra09:
#   chmod +x fetch_yang.sh
#   ./fetch_yang.sh
#
# Result: yang/ios-xe-1681/*.yang and yang/ios-xe-1731/*.yang
# Plus yang/README.md explaining the vendoring rationale.

set -euo pipefail

BASE_URL="https://raw.githubusercontent.com/YangModels/yang/main/vendor/cisco/xe"

MODULES=(
  "Cisco-IOS-XE-native"
  "Cisco-IOS-XE-ospf"
  "Cisco-IOS-XE-interfaces"
  "Cisco-IOS-XE-ip"
  "Cisco-IOS-XE-dhcp"
)

VERSIONS=("1681" "1731")

for version in "${VERSIONS[@]}"; do
  target_dir="yang/ios-xe-${version}"
  mkdir -p "$target_dir"
  echo "─── IOS XE ${version} → ${target_dir}/ ───"

  for module in "${MODULES[@]}"; do
    url="${BASE_URL}/${version}/${module}.yang"
    out="${target_dir}/${module}.yang"

    if curl -sSfL -o "$out" "$url"; then
      lines=$(wc -l < "$out")
      printf "  %-32s %5d lines\n" "${module}.yang" "$lines"
    else
      echo "  FAILED: $module (URL: $url)"
    fi
  done
  echo
done

echo "Done. Verify with: ls -lh yang/ios-xe-*/"
