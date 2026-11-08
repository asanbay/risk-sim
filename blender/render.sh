#!/usr/bin/env bash
# Использование: FRAMES=24 EVERY=20 FPS=12 ./blender/render.sh
# BLENDER=/path/to/Blender ENGINE=CYCLES SAMPLES=64 ./blender/render.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

BUNDLE="${BUNDLE:-results/pilot/scene_bundle.json}"
SCENE="${SCENE:-results/pilot/building.blend}"
ENGINE="${ENGINE:-BLENDER_EEVEE_NEXT}"
SAMPLES="${SAMPLES:-32}"
WIDTH="${WIDTH:-1280}"
HEIGHT="${HEIGHT:-720}"
EVERY="${EVERY:-1}"
FPS="${FPS:-24}"
FRAMES="${FRAMES:-0}"
OUT="${OUT:-results/pilot/simulation.mp4}"

if [[ ! "$FRAMES" =~ ^[0-9]+$ ]]; then
  echo "FRAMES должен быть целым числом" >&2
  exit 2
fi
if [[ -n "${BLENDER:-}" ]]; then
  BL="$BLENDER"
elif [[ -x "$HOME/Library/Application Support/Steam/steamapps/common/Blender/Blender.app/Contents/MacOS/Blender" ]]; then
  BL="$HOME/Library/Application Support/Steam/steamapps/common/Blender/Blender.app/Contents/MacOS/Blender"
elif [[ -x "/Applications/Blender.app/Contents/MacOS/Blender" ]]; then
  BL="/Applications/Blender.app/Contents/MacOS/Blender"
elif command -v blender >/dev/null 2>&1; then
  BL="$(command -v blender)"
else
  echo "Blender не найден. Укажите BLENDER=/path/to/blender" >&2
  exit 3
fi

"$BL" --version
mkdir -p "$(dirname "$OUT")" "$(dirname "$SCENE")"

echo "== [1/3] Сцена из $BUNDLE"
"$BL" --factory-startup -b --python-exit-code 1 -P blender/scene_setup.py -- \
  --bundle "$BUNDLE" --out "$SCENE" --fps "$FPS"

echo "== [2/3] Анимация, каждый $EVERY-й кадр"
"$BL" --factory-startup -b "$SCENE" --python-exit-code 1 -P blender/animate_pilot.py -- \
  --bundle "$BUNDLE" --every "$EVERY" --fps "$FPS"

echo "== [3/3] Рендер $ENGINE ${WIDTH}x${HEIGHT} -> $OUT"
"$BL" --factory-startup -b "$SCENE" -o "$OUT" -E "$ENGINE" --python-exit-code 1 --python-expr "
import bpy
s = bpy.context.scene
s.render.resolution_x = $WIDTH
s.render.resolution_y = $HEIGHT
s.render.fps = $FPS
if hasattr(s.render.image_settings, 'media_type'):
    s.render.image_settings.media_type = 'VIDEO'
s.render.image_settings.file_format = 'FFMPEG'
s.render.ffmpeg.format = 'MPEG4'
s.render.ffmpeg.codec = 'H264'
s.render.ffmpeg.constant_rate_factor = 'HIGH'
if $FRAMES > 0:
    s.frame_end = min(s.frame_end, $FRAMES - 1)
try:
    s.cycles.samples = $SAMPLES
    s.cycles.device = 'GPU'
except Exception:
    pass
try:
    s.eevee.taa_render_samples = $SAMPLES
except Exception:
    pass
" -a

du -h "$OUT"
echo "Результат симуляции — поддержка решений, а не расчёт по нормам."
