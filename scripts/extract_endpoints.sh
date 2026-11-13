#!/usr/bin/env bash
set -euo pipefail

# Helper script to decompile com.sgk.eganna.in APK via jadx and extract
# Retrofit/Volley/OkHttp endpoints, base URLs, and header keys.

usage() {
  cat <<'EOF'
Usage: ./scripts/extract_endpoints.sh [path/to/eganna.apk] [output_dir]

Extracts API endpoints, base URLs, Retrofit annotations, and headers from an
eGANNA Android APK using jadx and rg (or grep fallback).

Arguments:
  path/to/eganna.apk   Path to APK file (default: ./eganna.apk)
  output_dir           Decompilation directory (default: ./decompiled_eganna)
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

APK_PATH="${1:-./eganna.apk}"
OUT_DIR="${2:-./decompiled_eganna}"
REPORT_FILE="./extracted_endpoints_report.txt"

if [[ ! -f "$APK_PATH" ]]; then
  echo "Error: APK file not found at '$APK_PATH'" >&2
  echo "Place your eganna.apk in the project root or pass its path as the first argument." >&2
  exit 1
fi

if ! command -v jadx >/dev/null 2>&1; then
  echo "Error: 'jadx' is not installed or not in PATH." >&2
  echo "Install on macOS via: brew install jadx" >&2
  exit 1
fi

echo "[1/3] Decompiling '$APK_PATH' into '$OUT_DIR'..."
jadx -d "$OUT_DIR" "$APK_PATH" --no-res --show-bad-code || true

SRC_DIR="$OUT_DIR/sources"
if [[ ! -d "$SRC_DIR" ]]; then
  SRC_DIR="$OUT_DIR"
fi

search_cmd() {
  local pattern="$1"
  if command -v rg >/dev/null 2>&1; then
    rg -n --no-heading -e "$pattern" "$SRC_DIR" || true
  else
    grep -rnE "$pattern" "$SRC_DIR" || true
  fi
}

echo "[2/3] Scanning decompiled Java/Kotlin sources for endpoints and headers..."
{
  echo "=================================================================="
  echo "eGANNA APK Endpoint Extraction Report"
  echo "APK: $APK_PATH"
  echo "Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "=================================================================="
  echo ""
  echo "## 1. Retrofit Annotations (@GET, @POST, @PUT, @FormUrlEncoded, @Field, @Query, @Header)"
  search_cmd '@(GET|POST|PUT|DELETE|PATCH|FormUrlEncoded|Field|Query|Header|Headers|Url)\b'
  echo ""
  echo "## 2. Base URLs, NIC IPs, and CaneUP Domains"
  search_cmd 'https?://[A-Za-z0-9._:/-]+|164\.100\.[0-9.]+|caneup\.in|enquiry\.caneup'
  echo ""
  echo "## 3. Common API / ASMX / ASPX / JSON Route Strings"
  search_cmd '"[^"]*(/api/|\.asmx|\.aspx|Purchy|Calendar|Grower|Survey|Taul|Weighment|Factory|Village)[^"]*"'
  echo ""
  echo "## 4. OkHttp / Volley / User-Agent / SSL / Header Builders"
  search_cmd '(User-Agent|okhttp|StringRequest|JsonObjectRequest|Retrofit\.Builder|baseUrl\(|addHeader\()'
} > "$REPORT_FILE"

echo "[3/3] Done! Report saved to: $REPORT_FILE"
echo "Review '$REPORT_FILE' and plug the extracted endpoints into EGannaUpstreamAdapter in app.py."
