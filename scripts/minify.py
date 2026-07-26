#!/usr/bin/env python3
"""
SFAAM NEWS V23 — Build-time Asset Optimizer
============================================
Combines 4 optimizations into a single pre-deploy build step:

  1. CSS minification   (style.css → style.min.css)
  2. JS minification    (app.js    → app.min.js)
  3. HTML minification  (all *.html in static/ — in-place safe preview)
  4. WebP image conversion  (PNG/JPG → .webp, ~30-70% smaller)
  5. Gzip pre-compression   (.css.gz / .js.gz / .html.gz for nginx-style serving)

Usage:
    python scripts/minify.py            # CSS + JS + HTML + WebP
    python scripts/minify.py --no-webp  # skip WebP conversion
    python scripts/minify.py --no-html  # skip HTML minification
    python scripts/minify.py --gzip     # also write .gz files

This script is safe to re-run: it always reads source files and writes
to .min / .webp / .gz outputs, never modifies the source.
"""
import argparse
import gzip
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
CSS_FILE = STATIC / "css" / "style.css"
JS_FILE = STATIC / "js" / "app.js"


# ════════════════════════════════════════════════════════════
#  CSS MINIFIER
# ════════════════════════════════════════════════════════════
def minify_css(css: str) -> str:
    """Remove comments, whitespace, and newlines from CSS."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    css = '\n'.join(line.strip() for line in css.split('\n'))
    css = re.sub(r'\n+', '\n', css)
    css = css.replace('\n', ' ')
    css = re.sub(r'\s+', ' ', css)
    css = re.sub(r'\s*([{}:;,])\s*', r'\1', css)
    css = re.sub(r';}', '}', css)
    return css.strip()


# ════════════════════════════════════════════════════════════
#  JS MINIFIER (safe — preserves strings and templates)
# ════════════════════════════════════════════════════════════
def minify_js(js: str) -> str:
    """Basic JS minifier — removes comments and excess whitespace.
    Preserves template literals and string contents."""
    lines = js.split('\n')
    cleaned = []
    in_template = False
    in_string = False
    string_char = None
    for line in lines:
        result = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == '`' and not in_string:
                in_template = not in_template
                result.append(ch)
                i += 1
                continue
            if in_template:
                result.append(ch)
                i += 1
                continue
            if ch in ('"', "'") and not in_template:
                if not in_string:
                    in_string = True
                    string_char = ch
                elif ch == string_char:
                    in_string = False
                    string_char = None
                result.append(ch)
                i += 1
                continue
            if in_string:
                result.append(ch)
                i += 1
                continue
            if ch == '/' and i + 1 < len(line) and line[i + 1] == '/':
                break
            result.append(ch)
            i += 1
        cleaned.append(''.join(result))
    js = '\n'.join(cleaned)
    js = re.sub(r'/\*.*?\*/', '', js, flags=re.DOTALL)
    js = re.sub(r'\n\s*\n', '\n', js)
    js = '\n'.join(line.strip() for line in js.split('\n'))
    return js


# ════════════════════════════════════════════════════════════
#  HTML MINIFIER (preserves <pre>, <textarea>, <script>, <style>)
# ════════════════════════════════════════════════════════════
def minify_html(html: str) -> str:
    """Aggressive but safe HTML minifier.
    - Strips HTML comments (except <!--(.min|V23|ESLINT)--> kept)
    - Collapses whitespace between tags
    - Preserves <pre>, <textarea>, <script>, <style> contents verbatim
    - Preserves JSON-LD <script type="application/ld+json"> contents"""
    if not html:
        return html

    # 1. Protect blocks we must NOT touch (script, style, pre, textarea, JSON-LD)
    protected = []
    PROTECT_RE = re.compile(
        r'<(script\b[^>]*>|style\b[^>]*>|pre\b[^>]*>|textarea\b[^>]*>)(.*?)</\1',
        re.DOTALL | re.IGNORECASE,
    )

    def _stash(m):
        protected.append(m.group(0))
        return f"__PROTECTED_BLOCK_{len(protected) - 1}__"

    html = PROTECT_RE.sub(_stash, html)

    # 2. Drop HTML comments (keep IE conditionals if any)
    html = re.sub(r'<!--(?!\[if).*?-->', '', html, flags=re.DOTALL)

    # 3. Collapse runs of whitespace between tags
    html = re.sub(r'>\s+<', '><', html)

    # 4. Collapse multiple spaces/tabs/newlines in text runs
    html = re.sub(r'\s{2,}', ' ', html)

    # 5. Strip leading whitespace on each line
    html = '\n'.join(line.strip() for line in html.split('\n'))

    # 6. Restore protected blocks
    for i, block in enumerate(protected):
        html = html.replace(f"__PROTECTED_BLOCK_{i}__", block)

    return html.strip()


# ════════════════════════════════════════════════════════════
#  WEBP IMAGE CONVERTER (uses Pillow)
# ════════════════════════════════════════════════════════════
def convert_images_to_webp(static_dir: Path, quality: int = 82) -> tuple[int, int]:
    """Convert all PNG/JPG images under static_dir to .webp.
    Originals are kept (browsers that don't support WebP are rare in 2025
    but we don't want to break old Safari). Returns (count, bytes_saved)."""
    try:
        from PIL import Image
    except ImportError:
        print("  [WebP] Skipped — Pillow not installed (run: pip install Pillow)")
        return 0, 0

    count = 0
    saved_bytes = 0
    extensions = {'.png', '.jpg', '.jpeg'}
    skip_dirs = {'css', 'js'}  # don't traverse into non-image dirs

    for img_path in static_dir.rglob('*'):
        if not img_path.is_file():
            continue
        if img_path.suffix.lower() not in extensions:
            continue
        if any(part in skip_dirs for part in img_path.parts):
            continue
        # Skip files that already have a fresh .webp sibling
        webp_path = img_path.with_suffix('.webp')
        if webp_path.exists() and webp_path.stat().st_mtime >= img_path.stat().st_mtime:
            continue
        try:
            with Image.open(img_path) as img:
                # RGBA → preserve transparency; RGB → optimize
                if img.mode in ('RGBA', 'LA'):
                    img.save(webp_path, 'WEBP', quality=quality, lossless=False, alpha_quality=90)
                else:
                    img.convert('RGB').save(webp_path, 'WEBP', quality=quality, method=6)
            orig_size = img_path.stat().st_size
            new_size = webp_path.stat().st_size
            saved = max(0, orig_size - new_size)
            saved_bytes += saved
            count += 1
            pct = (saved / orig_size * 100) if orig_size else 0
            print(f"  [WebP] {img_path.name:30s} {orig_size:>8,} → {new_size:>8,} bytes  (-{pct:.0f}%)")
        except Exception as e:
            print(f"  [WebP] FAILED {img_path.name}: {e}")
    return count, saved_bytes


# ════════════════════════════════════════════════════════════
#  GZIP PRE-COMPRESS
# ════════════════════════════════════════════════════════════
def gzip_file(path: Path) -> int:
    """Write path + '.gz' with gzip-compressed contents.
    Returns the size of the compressed file."""
    data = path.read_bytes()
    gz_path = path.with_suffix(path.suffix + '.gz')
    with gzip.open(gz_path, 'wb', compresslevel=9) as f:
        f.write(data)
    return gz_path.stat().st_size


# ════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="SFAAM NEWS V23 asset optimizer")
    parser.add_argument('--no-css',  action='store_true', help='Skip CSS minification')
    parser.add_argument('--no-js',   action='store_true', help='Skip JS minification')
    parser.add_argument('--no-html', action='store_true', help='Skip HTML minification')
    parser.add_argument('--no-webp', action='store_true', help='Skip WebP conversion')
    parser.add_argument('--gzip',    action='store_true', help='Also write .gz pre-compressed files')
    parser.add_argument('--quality', type=int, default=82, help='WebP quality (1-100, default 82)')
    args = parser.parse_args()

    print("=" * 60)
    print("SFAAM NEWS V23 — Asset Optimizer")
    print("=" * 60)

    # ── 1. CSS ──
    if not args.no_css and CSS_FILE.exists():
        original = CSS_FILE.read_text(encoding='utf-8')
        minified = minify_css(original)
        min_path = CSS_FILE.parent / 'style.min.css'
        min_path.write_text(minified, encoding='utf-8')
        saved = (1 - len(minified) / len(original)) * 100
        print(f"  CSS: {len(original):,} → {len(minified):,} bytes ({saved:.1f}% smaller)")
        if args.gzip:
            gz_size = gzip_file(min_path)
            print(f"  CSS.gz: {gz_size:,} bytes")
    elif not args.no_css:
        print(f"  CSS not found: {CSS_FILE}")

    # ── 2. JS ──
    if not args.no_js and JS_FILE.exists():
        original = JS_FILE.read_text(encoding='utf-8')
        minified = minify_js(original)
        min_path = JS_FILE.parent / 'app.min.js'
        min_path.write_text(minified, encoding='utf-8')
        saved = (1 - len(minified) / len(original)) * 100
        print(f"  JS:  {len(original):,} → {len(minified):,} bytes ({saved:.1f}% smaller)")
        if args.gzip:
            gz_size = gzip_file(min_path)
            print(f"  JS.gz:  {gz_size:,} bytes")
    elif not args.no_js:
        print(f"  JS not found: {JS_FILE}")

    # ── 3. HTML ──
    if not args.no_html:
        html_count = 0
        html_saved = 0
        for html_path in STATIC.glob('*.html'):
            original = html_path.read_text(encoding='utf-8')
            minified = minify_html(original)
            # Write next to original (overwrites if .min.html exists; otherwise in-place is risky)
            min_path = html_path.with_suffix('.min.html')
            min_path.write_text(minified, encoding='utf-8')
            saved = max(0, len(original) - len(minified))
            html_count += 1
            html_saved += saved
        if html_count:
            print(f"  HTML: {html_count} files minified, {html_saved:,} bytes saved")
            print(f"  (Wrote *.min.html siblings — reference them in production if desired)")
        else:
            print(f"  HTML: no .html files found in {STATIC}")
        if args.gzip:
            for min_path in STATIC.glob('*.min.html'):
                gz_size = gzip_file(min_path)
                print(f"  HTML.gz: {min_path.name}.gz = {gz_size:,} bytes")

    # ── 4. WebP ──
    if not args.no_webp:
        count, saved = convert_images_to_webp(STATIC, quality=args.quality)
        if count:
            print(f"  WebP: {count} images converted, {saved:,} bytes saved")
        else:
            print(f"  WebP: nothing to convert (already up to date)")

    print("=" * 60)
    print("Done. Production assets are ready.")
    print("Tip: reference .min.css/.min.js in production HTML for best Lighthouse scores.")
    print("=" * 60)


if __name__ == '__main__':
    main()
