import * as THREE from 'three';
import { OrbitControls } from './vendor/OrbitControls.js';
import { GLTFLoader } from './vendor/GLTFLoader.js';

const $ = (id) => document.getElementById(id);
const RULE_TITLES = {
  no_two_crews_same_zone: 'Две бригады в одной зоне',
  no_work_under_crane_hazard: 'Работа в зоне опасности крана',
  edge_work_requires_guardrail: 'Край без подтверждённого ограждения',
  stop_crane_ops_above_wind_limit: 'Ветер выше рабочего лимита',
};
const ZONE_COLORS = { work: 0x0ea5e9, crane_hazard: 0xef4444, edge: 0xf59e0b, passage: 0x10b981, storage: 0x14b8a6 };
const state = { current: 0, playing: false, speed: 60, liftId: null };
let data;
let scene, camera, renderer, controls;
let buildingGroup, zonesGroup, agentsGroup, cranesGroup;
let activeEvents = [];
const agentMeshes = new Map();
const zoneMeshes = [];
const liftVisuals = new Map();
const eventButtons = new Map();
const timelineStartMs = { value: 0 };
const totalDuration = { value: 0 };

const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[char]));
const formatTime = (milliseconds, seconds = false) => new Intl.DateTimeFormat('ru-RU', {
  day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}),
}).format(new Date(milliseconds));
const percent = (value) => `${(100 * value).toFixed(1)}%`;
const eventStart = (event) => (Date.parse(event.from) - timelineStartMs.value) / 1000;
const eventEnd = (event) => (Date.parse(event.to) - timelineStartMs.value) / 1000;

function material(color, opacity = 1) {
  return new THREE.MeshStandardMaterial({ color, transparent: opacity < 1, opacity, depthWrite: opacity >= 1, roughness: 0.72, metalness: 0.08 });
}

function addBox(group, name, center, dimensions, mat, info = null) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(...dimensions), mat);
  mesh.name = name;
  mesh.position.set(...center);
  if (info) mesh.userData.info = info;
  group.add(mesh);
  return mesh;
}

function createScene() {
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x081421);
  scene.fog = new THREE.Fog(0x081421, 150, 620);
  camera = new THREE.PerspectiveCamera(48, 1, 0.1, 1400);
  camera.up.set(0, 0, 1);
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  $('viewport').prepend(renderer.domElement);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.07;
  controls.maxDistance = 560;
  controls.minDistance = 4;
  scene.add(new THREE.HemisphereLight(0xc7e5ff, 0x1d2935, 2.2));
  const sun = new THREE.DirectionalLight(0xfff1d6, 3.2);
  sun.position.set(60, -50, 360);
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0x68b7ff, 1.0);
  fill.position.set(-60, 70, 270);
  scene.add(fill);
  buildingGroup = new THREE.Group();
  zonesGroup = new THREE.Group();
  agentsGroup = new THREE.Group();
  cranesGroup = new THREE.Group();
  scene.add(buildingGroup, zonesGroup, agentsGroup, cranesGroup);
  resetCamera();
  new ResizeObserver(() => {
    const width = $('viewport').clientWidth;
    const height = $('viewport').clientHeight;
    if (!width || !height) return;
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }).observe($('viewport'));
}

function resetCamera() {
  const z = data.building.active_z_m;
  const f = data.building.footprint;
  const cx = (f.x_min + f.x_max) / 2;
  const cy = (f.y_min + f.y_max) / 2;
  const distance = Math.max(30, Math.max(f.x_max - f.x_min, f.y_max - f.y_min) * 1.25);
  camera.position.set(cx + distance, cy - distance, z + distance * 0.65);
  controls.target.set(cx, cy, z + 2);
  controls.update();
}

function topCamera() {
  const z = data.building.active_z_m;
  const f = data.building.footprint;
  const cx = (f.x_min + f.x_max) / 2;
  const cy = (f.y_min + f.y_max) / 2;
  const distance = Math.max(30, Math.max(f.x_max - f.x_min, f.y_max - f.y_min) * 1.4);
  camera.position.set(cx, cy - 0.1, z + distance);
  controls.target.set(cx, cy, z);
  controls.update();
}

async function createBuilding() {
  const building = data.building;
  const f = building.footprint;
  const width = f.x_max - f.x_min;
  const depth = f.y_max - f.y_min;
  const cx = (f.x_max + f.x_min) / 2;
  const cy = (f.y_max + f.y_min) / 2;
  const grid = new THREE.GridHelper(Math.max(width, depth), 16, 0x4b8aa8, 0x28475d);
  grid.rotation.x = Math.PI / 2;
  grid.position.set(cx, cy, building.active_z_m + 0.14);
  if (data.model) {
    const gltf = await new GLTFLoader().loadAsync(`./${data.model.url}`);
    gltf.scene.name = data.model.source_file;
    gltf.scene.traverse((object) => {
      if (!object.isMesh) return;
      object.userData.info = `${data.model.source_file} · ${object.name || 'IFC element'}`;
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      for (const item of materials) item.side = THREE.DoubleSide;
    });
    buildingGroup.add(gltf.scene);
    buildingGroup.userData.model = data.model;
  } else {
    const floors = new THREE.InstancedMesh(
      new THREE.BoxGeometry(width, depth, 0.12),
      material(0x6b8297, 0.17), building.floor_count + 1,
    );
    const matrix = new THREE.Matrix4();
    for (let floor = 0; floor <= building.floor_count; floor += 1) {
      matrix.makeTranslation(cx, cy, floor * building.floor_height_m);
      floors.setMatrixAt(floor, matrix);
    }
    floors.userData.info = `${building.name}: ${building.floor_count} уровней, высота ${building.height_m} м`;
    buildingGroup.add(floors);
    addBox(buildingGroup, 'ActiveSlab', [cx, cy, building.active_z_m], [width, depth, 0.22], material(0x8fa7b8, 0.72), `Активный уровень ${building.active_floor}, отметка ${building.active_z_m} м`);
    const core = building.core;
    addBox(buildingGroup, 'Core', [(core.x_min + core.x_max) / 2, (core.y_min + core.y_max) / 2, building.height_m / 2], [core.x_max - core.x_min, core.y_max - core.y_min, building.height_m], material(0x556b7c, 0.22), 'Упрощённое жёсткое ядро здания');
  }
  buildingGroup.add(grid);
}

function createZones() {
  for (const zone of data.zones) {
    const dimensions = [zone.x_max - zone.x_min, zone.y_max - zone.y_min, zone.z_top - zone.z_floor];
    const center = [(zone.x_min + zone.x_max) / 2, (zone.y_min + zone.y_max) / 2, (zone.z_floor + zone.z_top) / 2];
    const mesh = addBox(zonesGroup, zone.zone_id, center, dimensions, material(ZONE_COLORS[zone.type] || 0x0ea5e9, zone.type === 'crane_hazard' ? 0.13 : 0.09), `${zone.zone_id} · ${zone.type} · уровень ${zone.level}`);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(mesh.geometry), new THREE.LineBasicMaterial({ color: ZONE_COLORS[zone.type] || 0x0ea5e9, transparent: true, opacity: 0.8 }));
    edges.position.copy(mesh.position);
    zonesGroup.add(edges);
    zoneMeshes.push(mesh);
  }
}

function createAgents() {
  const geometry = new THREE.CapsuleGeometry(0.22, 0.8, 4, 8);
  geometry.rotateX(Math.PI / 2);
  for (const agent of data.timeline.agents) {
    const agentMaterial = material(agent.color, 0.95);
    agentMaterial.emissive = new THREE.Color(0x000000);
    const mesh = new THREE.Mesh(geometry, agentMaterial);
    mesh.name = agent.id;
    mesh.userData.info = `${agent.id} · бригада ${agent.crew_id} · ${agent.task_id}`;
    mesh.userData.agent = agent;
    agentsGroup.add(mesh);
    agentMeshes.set(agent.id, mesh);
  }
}

function createCranes() {
  for (const lift of data.lifts) {
    const source = lift.input;
    const result = lift.summary;
    const group = new THREE.Group();
    cranesGroup.add(group);
    const pivot = new THREE.Vector3(...result.pivot_m);
    const base = new THREE.Vector3(source.crane_x_m || 0, source.crane_y_m || 0, 0);
    const craneMaterial = material(0xf5b82e, 0.9);
    addBox(group, `Tower_${result.lift_id}`, [base.x, base.y, pivot.z / 2], [1.2, 1.2, pivot.z], craneMaterial, `Кран ${source.equipment_id || 'CR-320'} · ${result.lift_id}`);
    const jibLength = Math.hypot(pivot.x - base.x, pivot.y - base.y);
    const jib = addBox(group, `Jib_${result.lift_id}`, [(pivot.x + base.x) / 2, (pivot.y + base.y) / 2, pivot.z], [jibLength, 0.9, 0.9], craneMaterial, `Стрела · радиус ${source.radius_m} м`);
    jib.rotation.z = Math.atan2(pivot.y - base.y, pivot.x - base.x);
    const load = addBox(group, `Load_${result.lift_id}`, lift.track[0].pos, result.load_dimensions_m, material(0xf97316, 1), `${source.load_name} · масса ${source.mass_kg} кг`);
    load.userData.liftId = result.lift_id;
    const hook = new THREE.Mesh(new THREE.SphereGeometry(0.28, 10, 8), craneMaterial);
    group.add(hook);
    const cableGeometry = new THREE.BufferGeometry();
    cableGeometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(6), 3));
    const cable = new THREE.Line(cableGeometry, new THREE.LineBasicMaterial({ color: 0xf8e4aa }));
    cable.frustumCulled = false;
    group.add(cable);
    liftVisuals.set(result.lift_id, { group, load, hook, cable, result, source, track: lift.track });
  }
}

function findFramePair(value, frames, key = 't') {
  let low = 0;
  let high = frames.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (frames[middle][key] < value) low = middle + 1;
    else high = middle - 1;
  }
  const right = Math.min(Math.max(low, 1), frames.length - 1);
  return [frames[right - 1], frames[right]];
}

function updateAgents() {
  const frames = data.timeline.frames;
  const pair = findFramePair(state.current, frames);
  const fraction = pair[1].t === pair[0].t ? 0 : THREE.MathUtils.clamp((state.current - pair[0].t) / (pair[1].t - pair[0].t), 0, 1);
  const previous = new Map(pair[0].agents.map((values) => [values[0], values]));
  const next = new Map(pair[1].agents.map((values) => [values[0], values]));
  for (const [id, mesh] of agentMeshes) {
    const a = previous.get(id);
    const b = next.get(id) || a;
    if (!a) continue;
    mesh.position.set(
      THREE.MathUtils.lerp(a[1], b[1], fraction),
      THREE.MathUtils.lerp(a[2], b[2], fraction),
      THREE.MathUtils.lerp(a[3], b[3], fraction) + 0.64,
    );
    const isDanger = activeEvents.some((event) => [event.task_a, event.task_b].includes(mesh.userData.agent.task_id));
    mesh.material.emissive.setHex(isDanger && Math.floor(performance.now() / 300) % 2 ? 0xb91c1c : 0x000000);
    mesh.material.emissiveIntensity = 0.8;
    mesh.userData.state = a[4];
  }
}

function updateLifts() {
  for (const [id, visual] of liftVisuals) {
    const selected = id === state.liftId;
    visual.load.visible = false;
    visual.hook.visible = false;
    visual.cable.visible = false;
    if (!selected) continue;
    const offset = (Date.parse(visual.result.start) - timelineStartMs.value) / 1000;
    const local = state.current - offset;
    if (local < 0 || local > visual.result.duration_s) continue;
    const [a, b] = findFramePair(local, visual.track);
    const fraction = b.t === a.t ? 0 : THREE.MathUtils.clamp((local - a.t) / (b.t - a.t), 0, 1);
    const position = new THREE.Vector3(...a.pos).lerp(new THREE.Vector3(...b.pos), fraction);
    const qa = new THREE.Quaternion(...a.quat.slice(1), a.quat[0]);
    const qb = new THREE.Quaternion(...b.quat.slice(1), b.quat[0]);
    const quaternion = new THREE.Quaternion().slerpQuaternions(qa, qb, fraction);
    visual.load.position.copy(position);
    visual.load.quaternion.copy(quaternion);
    const end = position.clone().add(new THREE.Vector3(0, 0, visual.result.load_dimensions_m[2] / 2));
    visual.hook.position.copy(end);
    const attribute = visual.cable.geometry.attributes.position;
    attribute.setXYZ(0, ...visual.result.pivot_m);
    attribute.setXYZ(1, end.x, end.y, end.z);
    attribute.needsUpdate = true;
    visual.cable.geometry.computeBoundingSphere();
    visual.load.visible = true;
    visual.hook.visible = true;
    visual.cable.visible = true;
  }
}

function eventDetails(event) {
  const d = event.details;
  if (event.rule === 'stop_crane_ops_above_wind_limit') return `Порыв до ${d.max_wind_ms.toFixed(1)} м/с при лимите ${d.limit_ms.toFixed(1)} м/с`;
  if (event.rule === 'no_two_crews_same_zone') return `${d.crew_a} + ${d.crew_b}, всего ${d.people_total} человек`;
  if (event.rule === 'no_work_under_crane_hazard') return `Пересечение зон ${d.intersection_area_m2.toFixed(1)} м²`;
  if (event.rule === 'edge_work_requires_guardrail') return d.guardrail_status === 'not_declared' ? 'Статус ограждения не задан' : 'Ограждение отсутствует';
  return '';
}

function updateEvents() {
  activeEvents = data.events.filter((event) => state.current >= eventStart(event) && state.current <= eventEnd(event));
  $('active-count').textContent = activeEvents.length;
  $('active-banner').hidden = activeEvents.length === 0;
  $('active-banner').textContent = [...new Set(activeEvents.map((event) => RULE_TITLES[event.rule]))].join(' · ');
  for (const [id, button] of eventButtons) {
    const active = activeEvents.some((event) => event.conflict_id === id);
    button.classList.toggle('active', active);
    const marker = document.querySelector(`[data-marker="${id}"]`);
    if (marker) marker.classList.toggle('active', active);
  }
}

function updateWindCursor() {
  const cursor = $('wind-cursor');
  if (!cursor) return;
  const x = 30 + Math.min(1, Math.max(0, state.current / totalDuration.value)) * 300;
  cursor.setAttribute('x1', x);
  cursor.setAttribute('x2', x);
}

function updateScene() {
  $('current-time').textContent = formatTime(timelineStartMs.value + state.current * 1000, true);
  $('timeline').value = Math.round(state.current);
  updateEvents();
  updateAgents();
  updateLifts();
  updateWindCursor();
}

function renderEvents() {
  $('events-list').innerHTML = data.events.map((event) => `
    <button class="event-card ${event.severity}" data-event="${event.conflict_id}">
      <div class="event-title">${escapeHtml(RULE_TITLES[event.rule])}</div>
      <div class="event-meta">${escapeHtml(formatTime(Date.parse(event.from)))}–${escapeHtml(formatTime(Date.parse(event.to)))} · ${escapeHtml(event.level)} / ${escapeHtml(event.zone_id)}</div>
      <div class="event-details">${escapeHtml(eventDetails(event))}</div>
      <div class="event-meta">${escapeHtml(event.task_a)}${event.task_b ? ` ↔ ${escapeHtml(event.task_b)}` : ''}</div>
    </button>`).join('');
  for (const event of data.events) {
    const button = document.querySelector(`[data-event="${event.conflict_id}"]`);
    button.addEventListener('click', () => {
      state.current = (eventStart(event) + eventEnd(event)) / 2;
      state.playing = false;
      $('play').textContent = 'Запустить';
      updateScene();
    });
    eventButtons.set(event.conflict_id, button);
    const marker = document.createElement('span');
    marker.className = `event-marker ${event.severity}`;
    marker.dataset.marker = event.conflict_id;
    marker.style.left = `${Math.max(0, Math.min(100, eventStart(event) / totalDuration.value * 100))}%`;
    marker.title = RULE_TITLES[event.rule];
    $('event-markers').append(marker);
  }
}

function renderLiftPanel() {
  const lift = data.lifts.find((item) => item.summary.lift_id === state.liftId);
  const s = lift.summary;
  const monte = data.monte_carlo.wind_and_lift.find((item) => item.lift_id === state.liftId);
  const metrics = [
    ['Максимальный порыв', `${s.max_wind_ms.toFixed(2)} м/с / лимит ${s.wind_limit_ms.toFixed(1)}`, !s.within_wind_limit],
    ['Сила ветра (квазистатика)', `${(s.max_wind_force_n / 1000).toFixed(2)} кН / ${(s.tagline_capacity_n / 1000).toFixed(2)}`, !s.within_quasi_static_tagline_capacity],
    ['Усилие на ветвь (статика)', `${(s.sling_tension_per_leg_n / 1000).toFixed(2)} кН / WLL ${(s.sling_wll_per_leg_n / 1000).toFixed(2)}`, !s.within_static_sling_wll],
    ['Использование WLL', percent(s.sling_utilization), s.sling_utilization > 1],
    ['Максимальная раскачка', `${s.max_abs_swing_deg.toFixed(2)}°`, false],
    ['Горизонтальное смещение', `${s.max_horizontal_displacement_m.toFixed(2)} м`, false],
    ['Ветер по порогу оттяжки', `${s.tagline_wind_capacity_ms_by_cd.cd_nominal.toFixed(2)} м/с`, false],
    ['Диапазон при Cd', `${s.tagline_wind_capacity_ms_by_cd.cd_max.toFixed(2)}–${s.tagline_wind_capacity_ms_by_cd.cd_min.toFixed(2)} м/с`, false],
  ];
  $('lift-metrics').innerHTML = metrics.map(([name, value, bad]) => `<div><dt>${escapeHtml(name)}</dt><dd class="${bad ? 'bad' : ''}">${escapeHtml(value)}</dd></div>`).join('');
  $('mc-summary').innerHTML = `<strong>Monte Carlo · ${monte.samples} прогонов</strong><br>Ветер выше лимита: <b>${percent(monte.probabilities.operational_wind_limit.probability)}</b><br>Порог оттяжки: <b>${percent(monte.probabilities.tagline_capacity.probability)}</b><br>Статический WLL: <b>${percent(monte.probabilities.static_sling_wll.probability)}</b><br><small>Это превышения порогов, не вероятность происшествия.</small>`;
  drawWindChart();
}

function drawWindChart() {
  const lift = data.lifts.find((item) => item.summary.lift_id === state.liftId);
  const limit = lift.summary.wind_limit_ms;
  const width = 340, height = 145, left = 30, right = 330, top = 10, bottom = 122;
  const yMax = Math.max(limit, ...data.wind.map((point) => point.gust_ms)) * 1.12;
  const x = (timestamp) => left + ((Date.parse(timestamp) - timelineStartMs.value) / 1000 / totalDuration.value) * (right - left);
  const y = (value) => bottom - value / yMax * (bottom - top);
  const path = (metric) => data.wind.map((point, index) => `${index ? 'L' : 'M'}${x(point.timestamp).toFixed(1)},${y(point[metric]).toFixed(1)}`).join(' ');
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
    const px = left + fraction * (right - left);
    return `<line x1="${px}" y1="${bottom}" x2="${px}" y2="${bottom + 3}" stroke="#5d7187"/><text x="${px}" y="${bottom + 13}" text-anchor="middle" fill="#91a4ba" font-size="8">${escapeHtml(formatTime(timelineStartMs.value + fraction * totalDuration.value * 1000))}</text>`;
  }).join('');
  $('wind-chart').innerHTML = `
    <line x1="${left}" y1="${bottom}" x2="${right}" y2="${bottom}" stroke="#5d7187"/>
    <text x="4" y="${y(yMax) + 8}" fill="#91a4ba" font-size="8">${yMax.toFixed(0)}</text><text x="4" y="${y(limit) + 3}" fill="#fb7185" font-size="8">${limit}</text><text x="4" y="${bottom}" fill="#91a4ba" font-size="8">0</text>
    <line x1="${left}" y1="${y(limit)}" x2="${right}" y2="${y(limit)}" stroke="#fb4d5e" stroke-dasharray="5 3"/>
    <path d="${path('gust_ms')}" fill="none" stroke="#fb923c" stroke-width="2"/><path d="${path('mean_ms')}" fill="none" stroke="#38bdf8" stroke-width="1.7"/>
    ${ticks}<line id="wind-cursor" x1="${left}" y1="${top}" x2="${left}" y2="${bottom}" stroke="#e8f0f8" opacity=".55"/>`;
}

function renderMetadata() {
  $('scenario-name').textContent = data.metadata.name;
  $('source-badge').textContent = data.metadata.source_type === 'synthetic_demo' ? 'ДЕМО-ДАННЫЕ' : data.metadata.source_type;
  $('disclaimer').textContent = data.metadata.disclaimer;
  $('event-count').textContent = data.events.length;
  $('critical-count').textContent = data.events.filter((event) => event.severity === 'critical').length;
  $('timeline-start').textContent = formatTime(timelineStartMs.value);
  $('timeline-end').textContent = formatTime(Date.parse(data.timeline.end));
  $('timeline').max = totalDuration.value;
  $('pdf-link').href = `./reports/${data.artifacts.report_pdf}`;
  $('docx-link').href = `./reports/${data.artifacts.report_docx}`;
  $('assumptions-list').innerHTML = data.assumptions.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
  $('validation-list').innerHTML = Object.entries(data.validation.checks).map(([name, check]) => `<div class="validation-row"><span>${escapeHtml(name)}</span><b class="${check.pass ? 'good' : 'bad'}">${check.pass ? 'пройдено' : 'ошибка'}</b></div>`).join('');
  $('source-hashes').innerHTML = Object.entries(data.metadata.source_sha256).map(([name, hash]) => `<div>${escapeHtml(name)}<br>${escapeHtml(hash)}</div>`).join('');
}

function bindControls() {
  $('play').addEventListener('click', () => {
    state.playing = !state.playing;
    $('play').textContent = state.playing ? 'Пауза' : 'Запустить';
  });
  $('speed').addEventListener('change', (event) => { state.speed = Number(event.target.value); });
  $('timeline').addEventListener('input', (event) => { state.current = Number(event.target.value); updateScene(); });
  $('lift-select').addEventListener('change', (event) => {
    state.liftId = event.target.value;
    const lift = data.lifts.find((item) => item.summary.lift_id === state.liftId);
    state.current = Math.max(0, (Date.parse(lift.summary.start) - timelineStartMs.value) / 1000);
    renderLiftPanel();
    updateScene();
  });
  for (const layer of ['building', 'zones', 'agents', 'cranes']) {
    $(`layer-${layer}`).addEventListener('change', (event) => {
      const groups = { building: buildingGroup, zones: zonesGroup, agents: agentsGroup, cranes: cranesGroup };
      groups[layer].visible = event.target.checked;
    });
  }
  $('camera-reset').addEventListener('click', resetCamera);
  $('camera-top').addEventListener('click', topCamera);
  document.querySelectorAll('.tab').forEach((button) => button.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((tab) => tab.classList.toggle('active', tab === button));
    document.querySelectorAll('.tab-content').forEach((content) => { content.hidden = content.id !== `tab-${button.dataset.tab}`; });
  }));
  document.addEventListener('keydown', (event) => {
    if (event.code === 'Space' && !['INPUT', 'SELECT', 'BUTTON'].includes(document.activeElement.tagName)) {
      event.preventDefault();
      $('play').click();
    }
  });
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  let down = null;
  renderer.domElement.addEventListener('pointerdown', (event) => { down = [event.clientX, event.clientY]; });
  renderer.domElement.addEventListener('pointerup', (event) => {
    if (!down || Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 5) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const objects = [...agentMeshes.values(), ...zoneMeshes, ...[...liftVisuals.values()].map((visual) => visual.load)];
    const hit = raycaster.intersectObjects(objects, false).find((item) => item.object.visible);
    if (hit) {
      const agentState = hit.object.userData.state ? ` · состояние ${hit.object.userData.state}` : '';
      $('selection').textContent = hit.object.userData.info + agentState;
    }
  });
}

async function init() {
  if (location.protocol === 'file:') throw new Error('Откройте страницу через risk-sim serve, а не двойным нажатием на HTML.');
  const response = await fetch('./scenario.json');
  if (!response.ok) throw new Error(`scenario.json: HTTP ${response.status}`);
  data = await response.json();
  timelineStartMs.value = Date.parse(data.timeline.start);
  totalDuration.value = (Date.parse(data.timeline.end) - timelineStartMs.value) / 1000;
  state.liftId = data.lifts[0].summary.lift_id;
  state.current = Math.max(0, (Date.parse(data.lifts[0].summary.start) - timelineStartMs.value) / 1000);
  createScene();
  await createBuilding();
  createZones();
  createAgents();
  createCranes();
  $('lift-select').innerHTML = data.lifts.map((lift) => `<option value="${escapeHtml(lift.summary.lift_id)}">${escapeHtml(lift.summary.lift_id)} · ${escapeHtml(lift.input.load_name)}</option>`).join('');
  renderMetadata();
  renderEvents();
  renderLiftPanel();
  bindControls();
  updateScene();
  $('loading').hidden = true;
  let previous = performance.now();
  function animate(now) {
    const delta = Math.min((now - previous) / 1000, 0.2);
    previous = now;
    if (state.playing) {
      state.current += delta * state.speed;
      if (state.current > totalDuration.value) state.current = 0;
      updateScene();
    } else {
      updateAgents();
    }
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);
}

init().catch((error) => {
  console.error(error);
  $('loading').textContent = `Не удалось открыть сцену: ${error.message}`;
  $('loading').style.color = '#fda4af';
});
