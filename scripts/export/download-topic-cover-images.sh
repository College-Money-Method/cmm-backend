#!/usr/bin/env bash
#
# Download every published topic's cover image from production, convert each to
# PNG at the largest available size, and save as {slug}.png.
#
# Source: public topics endpoint (no auth). Images are served as originals via
# the CDN — there are no size variants, so the original IS the largest size.
# Raster images (jpg/png/gif/webp) are converted as-is with ImageMagick.
# SVG images (vector) are rasterized at high density to the target width.
#
# Usage:  ./download-topic-cover-images.sh [output_dir]
# Deps:   curl, jq, magick (ImageMagick)

set -euo pipefail

API_BASE="${API_BASE:-https://api.next.collegemoneymethod.com}"
TOPICS_URL="${API_BASE}/api/v1/content/topics/public"
# Default output lives under scripts/output/ (gitignored).
OUT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/output/topic-cover-images}"
SVG_WIDTH="${SVG_WIDTH:-2048}"   # rasterization width for vector (SVG) sources

mkdir -p "$OUT_DIR"
tmp_json="$(mktemp)"
trap 'rm -f "$tmp_json"' EXIT

echo "Fetching topic list: $TOPICS_URL"
curl -fsSL "$TOPICS_URL" -o "$tmp_json"

total="$(jq 'length' "$tmp_json")"
echo "Topics returned: $total"

ok=0; skipped=0; failed=0

# Emit tab-separated slug<TAB>image_url for topics that have an image.
while IFS=$'\t' read -r slug url; do
  [ -z "$slug" ] && continue
  if [ -z "$url" ] || [ "$url" = "null" ]; then
    echo "SKIP  $slug (no image_url)"
    skipped=$((skipped+1))
    continue
  fi

  out="$OUT_DIR/${slug}.png"

  # Detect vector vs raster by URL extension (lowercased). Keep the extension on
  # the temp file so qlmanage can identify the SVG content type.
  ext="${url##*.}"; ext="$(printf '%s' "$ext" | tr '[:upper:]' '[:lower:]')"
  work="$(mktemp -d)"
  src="$work/src.$ext"

  if ! curl -fsSL "$url" -o "$src"; then
    echo "FAIL  $slug (download error) $url"
    failed=$((failed+1)); rm -rf "$work"; continue
  fi

  if [ "$ext" = "svg" ]; then
    # Rasterize with WebKit (qlmanage) for faithful colors/patterns/gradients —
    # ImageMagick's internal SVG renderer drops background fills and patterns.
    # qlmanage fits the image into a square SVG_WIDTH canvas with white padding,
    # so crop back to the SVG's real aspect ratio (from viewBox) around center.
    rendered="$work/src.$ext.png"
    if qlmanage -t -s "$SVG_WIDTH" -o "$work" "$src" >/dev/null 2>&1 && [ -f "$rendered" ]; then
      vb="$(grep -oE 'viewBox="[^"]*"' "$src" | head -1 | sed -E 's/viewBox="([^"]*)"/\1/')"
      # Compute rendered content box: largest viewBox dim scales to SVG_WIDTH.
      dims="$(awk -v s="$SVG_WIDTH" '{ w=$3; h=$4; f=(w>h)?s/w:s/h; printf "%dx%d", int(w*f+0.5), int(h*f+0.5) }' <<< "$vb")"
      if [ -n "$dims" ] && [ "$dims" != "x" ]; then
        magick "$rendered" -gravity center -crop "${dims}+0+0" +repage "$out"
      else
        cp "$rendered" "$out"   # fallback: no viewBox, keep square render
      fi
      echo "OK    $slug.png (svg -> ${SVG_WIDTH}px, WebKit)"
      ok=$((ok+1))
    else
      echo "FAIL  $slug (svg render error)"
      failed=$((failed+1))
    fi
  else
    # Convert raster to PNG at original resolution (largest available).
    if magick "$src" "$out" 2>/dev/null; then
      echo "OK    $slug.png ($ext -> png)"
      ok=$((ok+1))
    else
      echo "FAIL  $slug (convert error) $url"
      failed=$((failed+1))
    fi
  fi
  rm -rf "$work"
done < <(jq -r '.[] | [.slug, (.image_url // "")] | @tsv' "$tmp_json")

echo
echo "Done. saved=$ok skipped=$skipped failed=$failed  ->  $OUT_DIR"
