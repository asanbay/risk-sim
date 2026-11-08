PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
DATA ?= data/sample
OUT ?= results/pilot

.PHONY: all scenario pilot realistic test check panel pilot-panel serve video usd import-ifc

all: scenario pilot

scenario:
	$(PY) -m risk_sim.cli run --data data --output results --viewer viewer

pilot:
	$(PY) -m risk_sim.cli pilot --data $(DATA) --output $(OUT) --gif

# Фотореалистичный ролик стройки (Blender 5.x): env EVERY/ENGINE/SAMPLES/WIDTH/HEIGHT
realistic:
	BUNDLE=$(OUT)/scene_bundle.json SCENE=$(OUT)/crane_scene.blend \
	  OUT=$(OUT)/simulation_realistic.mp4 ./blender/render_realistic.sh

test:
	PYTHONPATH=. $(PY) -m pytest tests -q

check: test
	$(PY) -m risk_sim.cli run --data data --output .smoke/scenario --viewer .smoke/viewer
	$(PY) -m risk_sim.cli pilot --data $(DATA) --output .smoke/pilot --mc-lifts 200 --mc-schedule 100 --skip-lift-swing --no-report --no-viewer
	rm -rf .smoke

panel:
	$(PY) -m streamlit run dashboard.py

pilot-panel:
	$(PY) -m streamlit run app_streamlit.py

serve:
	$(PY) -m risk_sim.cli serve --viewer viewer --port 8765

video:
	./blender/render.sh

usd:
	$(PY) -m risk_sim.cli export-usd --bundle $(OUT)/scene_bundle.json --output $(OUT)/usd --flatten

import-ifc:
	@test -n "$(IN)" || (echo "Укажите IN=/путь/к/модели.ifc" && exit 2)
	$(PY) -m risk_sim.cli import-ifc "$(IN)" --data "$(DATA)"
