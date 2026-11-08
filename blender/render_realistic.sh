#!/usr/bin/env bash
# Реалистичный ролик стройки: сцена -> анимация -> рендер Cycles/EEVEE.
# Использование (все параметры — env, значения по умолчанию показаны):
#   BUNDLE=results/dynamics/scene_bundle.json SCENE=results/dynamics/crane_scene.blend \
#   OUT=results/dynamics/simulation_realistic.mp4 ENGINE=CYCLES SAMPLES=96 \
#   EVERY=6 FPS=24 WIDTH=1920 HEIGHT=1080 STILL_FRAMES="120,450" ./blender/render_realistic.sh
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

BUNDLE="${BUNDLE:-results/dynamics/scene_bundle.json}"
SCENE="${SCENE:-results/dynamics/crane_scene.blend}"
OUT="${OUT:-results/dynamics/simulation_realistic.mp4}"
ENGINE="${ENGINE:-CYCLES}"
SAMPLES="${SAMPLES:-96}"
WIDTH="${WIDTH:-1920}"
HEIGHT="${HEIGHT:-1080}"
EVERY="${EVERY:-6}"
FPS="${FPS:-24}"
STILL_FRAMES="${STILL_FRAMES:-}"   # список таймлайн-индексов для контрольных PNG

if [[ ! -f "$BUNDLE" ]]; then
  echo "Нет $BUNDLE — сначала выполните расчёт пилота" >&2
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
  echo "Blender не найден. Укажите BLENDER=/path/to/Blender" >&2
  exit 3
fi

"$BL" --version | head -1
mkdir -p "$(dirname "$OUT")" "$(dirname "$SCENE")"

echo "== [1/5] Реалистичная сцена из $BUNDLE"
"$BL" --factory-startup -b --python-exit-code 1 -P blender/scene_realistic.py -- \
  --bundle "$BUNDLE" --out "$SCENE" --fps "$FPS"

echo "== [2/5] Анимация (каждый $EVERY-й кадр таймлайна)"
"$BL" --factory-startup -b "$SCENE" --python-exit-code 1 -P blender/animate_realistic.py -- \
  --bundle "$BUNDLE" --every "$EVERY" --fps "$FPS"

echo "== [3/5] Инженерная сверка сцены с расчётом"
if ! "$BL" --factory-startup -b "$SCENE" --python-exit-code 1 -P blender/validate_scene.py -- \
    --bundle "$BUNDLE"; then
  echo "Валидация НЕ пройдена — рендер отменён, см. scene_validation.json" >&2
  exit 1
fi

echo "== [4/5] Рендер $ENGINE ${WIDTH}x${HEIGHT} -> $OUT"
"$BL" --factory-startup -b "$SCENE" -o "$OUT" -E "$ENGINE" --python-exit-code 1 --python-expr "
import bpy
s = bpy.context.scene
s.render.resolution_x = $WIDTH
s.render.resolution_y = $HEIGHT
s.render.fps = $FPS
if '$ENGINE' == 'CYCLES':
    s.cycles.samples = $SAMPLES
    s.cycles.use_adaptive_sampling = True
    s.cycles.use_denoising = True
    try:
        s.cycles.device = 'GPU'
    except Exception:
        pass
else:
    try:
        s.eevee.taa_render_samples = $SAMPLES
    except Exception:
        pass
if hasattr(s.render.image_settings, 'media_type'):
    s.render.image_settings.media_type = 'VIDEO'
s.render.image_settings.file_format = 'FFMPEG'
s.render.ffmpeg.format = 'MPEG4'
s.render.ffmpeg.codec = 'H264'
s.render.ffmpeg.constant_rate_factor = 'HIGH'
" -a
du -h "$OUT"
echo "== [5/5] Готово: $OUT"

if [[ -n "$STILL_FRAMES" ]]; then
  STILLS_DIR="$ROOT/$(dirname "$OUT")/stills"
  mkdir -p "$STILLS_DIR"
  echo "== Контрольные кадры -> $STILLS_DIR"
  for F in ${STILL_FRAMES//,/ }; do
    "$BL" --factory-startup -b "$SCENE" -E "$ENGINE" --python-exit-code 1 --python-expr "
import bpy
s = bpy.context.scene
s.render.resolution_x = $WIDTH
s.render.resolution_y = $HEIGHT
if '$ENGINE' == 'CYCLES':
    s.cycles.samples = $SAMPLES
    s.cycles.use_denoising = True
s.frame_set(min($F, s.frame_end))
s.render.filepath = '$STILLS_DIR/frame_$F.png'
bpy.ops.render.render(write_still=True)
"
  done
fi
