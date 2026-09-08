// Демо-режим: интерфейс без сервера.
//
// Подменяет fetch и WebSocket, а данные берёт из той же модели линии, что
// эмулирует tools/simulator.py. Нужен для витрины на GitHub Pages и чтобы
// можно было пощёлкать интерфейс, ничего не устанавливая.
//
// Значения — чистая функция от времени, поэтому «история» и «живой поток»
// согласованы между собой: график не прыгает при переключении периода.

const START = Date.now() / 1000;

// --- модель линии (повторяет tools/simulator.py) ----------------------------
const MACHINES = [
  { node_id: 3, name: 'Розлив' },
  { node_id: 4, name: 'Укупор' },
  { node_id: 5, name: 'Этикетировка' },
];

const TEMPLATE = [
  ['temp', 'Температура', '°C', 'float32', 'value', 100],
  ['press', 'Давление', 'bar', 'float32', 'value', 102],
  ['speed', 'Скорость', 'м/мин', 'float32', 'value', 104],
  ['count', 'Счётчик выпуска', 'шт', 'uint32', 'counter', 106],
  ['run', 'Работа', null, 'bool', 'state', 108],
  ['feed', 'Подача материала', null, 'bool', 'state', 108],
  ['alarm', 'Авария перегрева', null, 'bool', 'alarm', 108],
  ['setup', 'Режим наладки', null, 'bool', 'state', 108],
  ['current', 'Ток двигателя', 'A', 'float32', 'value', 110],
  ['vibro', 'Вибрация', 'мм/с', 'float32', 'value', 112],
];

const NODES = [
  { node_id: 1, parent_id: null, kind: 'site', name: 'Завод «Пример»' },
  { node_id: 2, parent_id: 1, kind: 'line', name: 'Линия розлива №1' },
  ...MACHINES.map((m) => ({ ...m, parent_id: 2, kind: 'machine' })),
];

const TAGS = [];
MACHINES.forEach((machine, i) => {
  TEMPLATE.forEach(([key, label, unit, datatype, role, address], j) => {
    TAGS.push({
      tag_id: i * 10 + j + 1,
      device_id: 1,
      device: 'plc-line1',
      name: `m${i + 1}_${key}`,
      description: `${machine.name}: ${label}`,
      unit,
      datatype,
      register: 'holding',
      address: address + i * 20,
      bit: datatype === 'bool' ? { run: 0, feed: 1, alarm: 3, setup: 5 }[key] : null,
      length: null,
      scale: 1,
      offset: 0,
      min_value: null,
      max_value: null,
      word_order: 'big',
      byte_order: 'big',
      poll_interval_ms: 1000,
      deadband: 0,
      deadband_mode: 'absolute',
      heartbeat_s: 60,
      labels: {},
      enabled: true,
      node_id: machine.node_id,
      group_name: label,
      role,
      _machine: i,
      _key: key,
    });
  });
});

const DEVICES = [{
  device_id: 1,
  name: 'plc-line1',
  description: 'Линия розлива, шлюз Modbus TCP',
  node_id: 2,
  transport: 'tcp',
  host: '192.168.10.24',
  port: 502,
  unit_id: 1,
  serial_port: null,
  baudrate: 9600,
  bytesize: 8,
  parity: 'N',
  stopbits: 1,
  timeout_s: 3,
  retries: 2,
  reconnect_delay_s: 2,
  reconnect_delay_max_s: 30,
  inter_request_delay_ms: 0,
  block_max_registers: 100,
  block_max_gap: 8,
  enabled: true,
  tag_count: TAGS.length,
}];

/** Значение тега в момент t (эпоха, секунды). Чистая функция. */
function valueAt(tag, t) {
  const i = tag._machine;
  const phase = i * 1.7;
  const running = ((t + i * 9) % 120) > 12;
  const temp = 42 + 18 * Math.sin(t / 25 + phase) + (running ? 0 : 6);
  switch (tag._key) {
    case 'temp': return temp;
    case 'press': return 3.2 + 1.4 * Math.sin(t / 9 + phase);
    case 'speed': return running ? 85 + 12 * Math.sin(t / 17 + phase) : 0;
    case 'count': return Math.floor((t - START + 3600) * (0.8 + 0.2 * i));
    case 'run': return running ? 1 : 0;
    case 'feed': return running ? 1 : 0;
    case 'alarm': return temp > 57 ? 1 : 0;
    case 'setup': return (!running && Math.floor(t / 7) % 3 === 0) ? 1 : 0;
    case 'current': return running ? 14 + 3.5 * Math.sin(t / 5 + phase) : 0.4;
    case 'vibro': return 1.2 + 0.6 * Math.sin(t / 3 + phase) + (temp > 57 ? 1.8 : 0);
    default: return 0;
  }
}

const byId = new Map(TAGS.map((t) => [t.tag_id, t]));
const now = () => Date.now() / 1000;

// --- вспомогательное --------------------------------------------------------
function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function nodePath(node) {
  const parts = [node.name];
  let current = node;
  while (current.parent_id) {
    current = NODES.find((n) => n.node_id === current.parent_id);
    if (!current) break;
    parts.unshift(current.name);
  }
  return parts.join(' / ');
}

function latestRow(tag) {
  const t = now();
  const value = valueAt(tag, t);
  return {
    tag_id: tag.tag_id,
    device_id: tag.device_id,
    device: tag.device,
    name: tag.name,
    description: tag.description,
    unit: tag.unit,
    datatype: tag.datatype,
    role: tag.role,
    group_name: tag.group_name,
    node_id: tag.node_id,
    min_value: null,
    max_value: null,
    ts: new Date(t * 1000).toISOString(),
    value,
    value_text: null,
    quality: 0,
    quality_name: 'GOOD',
    age_s: 0.4,
  };
}

// --- ответы API -------------------------------------------------------------
const grafana = { installed: true, running: false, downloading: false, percent: 0 };

const ROUTES = [
  ['GET', /^\/api\/status$/, () => json({
    configured: true,
    ready: true,
    startup_error: null,
    uptime_s: now() - START,
    database: {
      connected: true, error: null, server_version: '16.10',
      timescaledb: true, dsn: 'postgresql://plc:***@127.0.0.1:5432/plc',
    },
    runtime: runtimeStatus(),
    grafana: grafanaStatus(),
    python: '3.12.7',
    platform: 'Демо в браузере',
  })],

  ['GET', /^\/api\/settings$/, () => json({
    server: { host: '127.0.0.1', port: 8000, open_browser: true },
    database: {
      host: '127.0.0.1', port: 5432, name: 'plc', user: 'plc',
      password: '••••', admin_user: 'postgres', admin_password: '••••',
      readonly_user: 'grafana_ro', readonly_password: '••••',
      retention_days: 90, configured: true,
    },
    grafana: {
      enabled: true, version: '11.6.0', port: 3000, admin_user: 'admin',
      admin_password: '••••', external_url: '', external_token: '',
      url: 'http://127.0.0.1:3000', managed: true,
    },
  })],

  ['GET', /^\/api\/devices$/, () => json(
    DEVICES.map((d) => ({ ...d, runtime: runtimeStatus().devices[0] })))],

  ['GET', /^\/api\/devices\/(\d+)$/, (m) => {
    const device = DEVICES.find((d) => d.device_id === Number(m[1]));
    return device ? json(device) : json({ detail: 'устройство не найдено' }, 404);
  }],

  ['GET', /^\/api\/(?:tags|devices\/\d+\/tags)$/, () => json(TAGS.map(publicTag))],

  ['GET', /^\/api\/latest$/, () => json(TAGS.map(latestRow))],

  ['GET', /^\/api\/series$/, (m, url) => {
    const ids = (url.searchParams.get('tag_ids') || '')
      .split(',').filter(Boolean).map(Number);
    const minutes = Number(url.searchParams.get('minutes') || 30);
    const maxPoints = Math.min(Number(url.searchParams.get('max_points') || 600), 900);
    const end = now();
    const start = end - minutes * 60;
    const step = (end - start) / maxPoints;
    const series = {};
    for (const id of ids) {
      const tag = byId.get(id);
      if (!tag) { series[id] = []; continue; }
      const points = [];
      for (let t = start; t <= end; t += step) {
        const v = valueAt(tag, t);
        points.push([t, v, v, v, 0]);
      }
      series[id] = points;
    }
    return json({
      from: new Date(start * 1000).toISOString(),
      to: new Date(end * 1000).toISOString(),
      series,
    });
  }],

  ['GET', /^\/api\/twin\/nodes$/, () => json(
    NODES.map((n) => ({ ...n, description: null, sort_order: 0, meta: {},
      path: nodePath(n),
      tag_count: TAGS.filter((t) => t.node_id === n.node_id).length })))],

  ['GET', /^\/api\/twin\/overview$/, () => json(twinOverview())],

  ['GET', /^\/api\/system\/stats$/, () => json({
    rows: 1_284_930 + Math.floor((now() - START) * 30),
    first_ts: new Date((START - 86400 * 6) * 1000).toISOString(),
    last_ts: new Date(now() * 1000).toISOString(),
    table_size: '148 MB',
    tags: TAGS.length,
    devices: DEVICES.length,
    retention_days: 90,
  })],

  ['GET', /^\/api\/runtime\/plan$/, () => {
    const blocks = MACHINES.map((_, i) => ({
      device: 'plc-line1',
      register: 'holding',
      address: 100 + i * 20,
      count: 14,
      poll_interval_ms: 1000,
      tags: TAGS.filter((t) => t._machine === i).map((t) => t.name),
    }));
    return json({ active: blocks, preview: blocks });
  }],

  ['GET', /^\/api\/grafana\/status$/, () => json(grafanaStatus())],

  ['GET', /^\/api\/grafana\/dashboards$/, () => json({
    dashboards: grafana.running ? [
      { uid: 'plc-overview', title: 'Цифровой двойник — обзор производства',
        url: '#', path: '', folder: 'PLC', tags: ['plc2grafana'],
        embed_url: 'img/grafana-overview.png' },
      { uid: 'plc-dev-1', title: 'plc-line1 — обзор устройства',
        url: '#', path: '', folder: 'PLC', tags: ['plc2grafana'],
        embed_url: 'img/grafana-device.png' },
    ] : [],
    files: ['plc-device-1.json', 'plc-node-2.json', 'plc-overview.json'],
    url: 'http://127.0.0.1:3000',
  })],

  ['POST', /^\/api\/grafana\/install$/, () => {
    grafana.installed = true;
    return json({ started: true });
  }],
  ['POST', /^\/api\/grafana\/start$/, () => {
    grafana.running = true;
    return json(grafanaStatus());
  }],
  ['POST', /^\/api\/grafana\/stop$/, () => {
    grafana.running = false;
    return json(grafanaStatus());
  }],
  ['POST', /^\/api\/grafana\/dashboards$/, () => json({
    ok: true,
    dashboards: [
      { file: 'plc-device-1.json', title: 'plc-line1 — обзор устройства', uid: 'plc-dev-1' },
      { file: 'plc-node-2.json', title: 'Линия розлива №1 — линия', uid: 'plc-node-2' },
      { file: 'plc-overview.json', title: 'Цифровой двойник — обзор производства',
        uid: 'plc-overview' },
    ],
    dir: 'runtime/grafana-dashboards',
  })],

  // --- мастер подключения --------------------------------------------------
  ['POST', /^\/api\/discovery\/test$/, async (m, url, options) => {
    const body = JSON.parse(options.body || '{}');
    await sleep(700);
    return json({
      ok: true,
      endpoint: `${body.host}:${body.port}`,
      unit_id: body.unit_id,
      latency_ms: 12.4,
      registers: {
        holding: { supported: true, max_address: 999, error: null },
        input: { supported: true, max_address: 999, error: null },
        coil: { supported: true, max_address: 999, error: null },
        discrete: { supported: true, max_address: 999, error: null },
      },
      error: null,
    });
  }],

  ['POST', /^\/api\/discovery\/scan$/, async (m, url, options) => {
    const body = JSON.parse(options.body || '{}');
    await sleep(1400);
    return json(fakeScan(body));
  }],

  // --- изменения ------------------------------------------------------------
  ['POST', /^\/api\/twin\/nodes$/, (m, url, options) => {
    const body = JSON.parse(options.body || '{}');
    const node = {
      node_id: Math.max(...NODES.map((n) => n.node_id)) + 1,
      parent_id: body.parent_id || null,
      kind: body.kind || 'machine',
      name: body.name,
      description: body.description || null,
      sort_order: 0,
      meta: {},
    };
    NODES.push(node);
    return json(node, 201);
  }],

  ['PATCH', /^\/api\/twin\/nodes\/(\d+)$/, (m, url, options) => {
    const node = NODES.find((n) => n.node_id === Number(m[1]));
    if (!node) return json({ detail: 'узел не найден' }, 404);
    Object.assign(node, JSON.parse(options.body || '{}'));
    return json(node);
  }],

  ['DELETE', /^\/api\/twin\/nodes\/(\d+)$/, (m) => {
    const index = NODES.findIndex((n) => n.node_id === Number(m[1]));
    if (index >= 0) NODES.splice(index, 1);
    return json({ ok: true });
  }],

  ['POST', /^\/api\/twin\/nodes\/(\d+)\/tags$/, (m, url, options) => {
    const target = Number(m[1]) || null;
    const { tag_ids: ids = [] } = JSON.parse(options.body || '{}');
    for (const id of ids) {
      const tag = byId.get(id);
      if (tag) tag.node_id = target;
    }
    return json({ ok: true, tags: ids.length, node_id: target });
  }],

  ['PATCH', /^\/api\/tags\/(\d+)$/, (m, url, options) => {
    const tag = byId.get(Number(m[1]));
    if (!tag) return json({ detail: 'тег не найден' }, 404);
    Object.assign(tag, JSON.parse(options.body || '{}'));
    return json(publicTag(tag));
  }],

  ['PATCH', /^\/api\/devices\/(\d+)$/, (m, url, options) => {
    const device = DEVICES.find((d) => d.device_id === Number(m[1]));
    if (!device) return json({ detail: 'устройство не найдено' }, 404);
    Object.assign(device, JSON.parse(options.body || '{}'));
    return json(device);
  }],

  ['POST', /^\/api\/runtime\/reload$/, () => json({
    devices: DEVICES.length, tags: TAGS.length, error: null })],
];

const DEMO_WRITE_BLOCKED = {
  detail: 'Это витрина: изменения не сохраняются. Запустите приложение локально — '
        + 'см. ссылку на репозиторий внизу слева.',
};

function publicTag(tag) {
  const { _machine, _key, ...rest } = tag;
  return rest;
}

function runtimeStatus() {
  return {
    running: true,
    started_at: START,
    last_error: null,
    devices: [{
      device: 'plc-line1',
      host: '192.168.10.24',
      port: 502,
      unit_id: 1,
      connected: true,
      polls: Math.floor((now() - START) * 3) + 12840,
      poll_errors: 2,
      decode_errors: 0,
      samples_written: Math.floor((now() - START) * 26) + 384220,
      tags: TAGS.length,
      blocks: MACHINES.length,
      seconds_since_success: 0.3,
    }],
    storage: {
      db_connected: true,
      pending_rows: 12,
      rows_written: Math.floor((now() - START) * 26) + 384220,
      rows_dropped: 0,
      flush_errors: 0,
    },
    live_subscribers: 1,
  };
}

function grafanaStatus() {
  return {
    managed: true,
    enabled: true,
    installed: grafana.installed,
    version: '11.6.0',
    install_dir: 'runtime/grafana/grafana-v11.6.0',
    url: 'http://127.0.0.1:3000',
    port: 3000,
    admin_user: 'admin',
    running: grafana.running,
    pid: grafana.running ? 10336 : null,
    started_at: grafana.running ? START : null,
    exit_code: null,
    download: { active: false, percent: grafana.installed ? 100 : 0,
      downloaded: 0, total: 0, stage: grafana.installed ? 'готово' : '', error: null },
    log_tail: [],
    dashboard_dir: 'runtime/grafana-dashboards',
    healthy: grafana.running,
    demo: true,
  };
}

function twinOverview() {
  const rows = TAGS.map(latestRow);
  const byNode = new Map();
  for (const row of rows) {
    if (!byNode.has(row.node_id)) byNode.set(row.node_id, []);
    byNode.get(row.node_id).push(row);
  }

  const build = (node) => {
    const own = byNode.get(node.node_id) || [];
    const children = NODES.filter((n) => n.parent_id === node.node_id).map(build);
    const all = [...own, ...children.flatMap((c) => c._all)];
    const alarms = all.filter((t) => t.role === 'alarm' && t.value > 0);
    return {
      node_id: node.node_id,
      parent_id: node.parent_id,
      kind: node.kind,
      name: node.name,
      description: node.description || null,
      meta: {},
      children,
      tag_count: all.length,
      own_tags: own,
      alarm_count: alarms.length,
      alarms: alarms.map((t) => t.name),
      bad_quality: 0,
      state: alarms.length ? 'alarm' : all.length ? 'ok' : 'empty',
      _all: all,
    };
  };

  const strip = (list) => list.forEach((n) => { delete n._all; strip(n.children); });
  const tree = NODES.filter((n) => !n.parent_id).map(build);
  strip(tree);
  return {
    tree,
    unassigned: { tag_count: 0, tags: [] },
    devices: DEVICES.map((d) => ({ device_id: d.device_id, name: d.name,
      node_id: d.node_id, enabled: d.enabled })),
  };
}

/** Правдоподобный результат сканирования: карта одной машины линии. */
function fakeScan(body) {
  const start = Number(body.start) || 0;
  const count = Math.min(Number(body.count) || 24, 40);
  const samples = Number(body.samples) || 4;
  const t = now();
  const at = (offset) => valueAt(TAGS.find((x) => x._machine === 0
    && x._key === offset), t);
  const fmt = (v, n = 4) => Number(v).toPrecision(n).replace(/\.?0+$/, '');
  const seq = (fn) => Array.from({ length: samples },
    (_, k) => fmt(fn(t + k * 0.8)));

  const machine = (offset) => TAGS.find((x) => x._machine === 0 && x._key === offset);
  const candidates = [
    { offset: 0, datatype: 'float32', name: 'hr_%a', reason: 'похоже на float32 ABCD',
      confidence: 0.95, key: 'temp', unit: '°C', span: 2 },
    { offset: 2, datatype: 'float32', name: 'hr_%a', reason: 'похоже на float32 ABCD',
      confidence: 0.95, key: 'press', unit: 'bar', span: 2 },
    { offset: 4, datatype: 'float32', name: 'hr_%a', reason: 'похоже на float32 ABCD',
      confidence: 0.95, key: 'speed', unit: 'м/мин', span: 2 },
    { offset: 6, datatype: 'uint32', name: 'hr_%a', reason: 'растущий 32-битный счётчик',
      confidence: 0.95, key: 'count', unit: 'шт', span: 2, role: 'counter' },
    { offset: 8, datatype: 'uint16', name: 'hr_%a',
      reason: 'слово состояния: меняются отдельные биты',
      confidence: 0.8, key: 'run', span: 1, role: 'state', bits: [0, 1, 3, 5] },
    { offset: 10, datatype: 'float32', name: 'hr_%a', reason: 'похоже на float32 ABCD',
      confidence: 0.95, key: 'current', unit: 'A', span: 2 },
    { offset: 12, datatype: 'float32', name: 'hr_%a', reason: 'похоже на float32 ABCD',
      confidence: 0.95, key: 'vibro', unit: 'мм/с', span: 2 },
  ];

  const out = [];
  let i = 0;
  while (i < count) {
    const address = start + i;
    const spec = candidates.find((c) => c.offset === i);
    if (spec) {
      const tag = machine(spec.key);
      out.push({
        address,
        register: body.register || 'holding',
        datatype: spec.datatype,
        word_order: 'big',
        byte_order: 'big',
        length: null,
        name: `hr_${address}`,
        description: null,
        role: spec.role || 'value',
        unit: undefined,
        preview: spec.key === 'run'
          ? seq(() => 3)
          : seq((tt) => valueAt(tag, tt)),
        changing: true,
        confidence: spec.confidence,
        selected: true,
        reason: spec.reason,
        bits: spec.bits || null,
      });
      i += spec.span;
      continue;
    }
    out.push({
      address,
      register: body.register || 'holding',
      datatype: 'uint16',
      word_order: 'big',
      byte_order: 'big',
      length: null,
      name: `hr_${address}`,
      description: null,
      role: 'value',
      preview: Array.from({ length: samples }, () => '0'),
      changing: false,
      confidence: 0.3,
      selected: false,
      reason: '16-битное целое (по умолчанию); всё время ноль',
      bits: null,
    });
    i += 1;
  }
  return {
    register: body.register || 'holding',
    start,
    count,
    samples,
    unreadable: [],
    error: null,
    candidates: out,
    raw: [],
  };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- подмена fetch ----------------------------------------------------------
const realFetch = window.fetch.bind(window);

window.fetch = async function demoFetch(input, options = {}) {
  const raw = typeof input === 'string' ? input : input.url;
  if (!raw.includes('/api/')) return realFetch(input, options);

  const url = new URL(raw, location.origin);
  const method = (options.method || 'GET').toUpperCase();
  const path = url.pathname.replace(/^.*(\/api\/)/, '$1');

  for (const [verb, pattern, handler] of ROUTES) {
    if (verb !== method) continue;
    const match = pattern.exec(path);
    if (match) return handler(match, url, options);
  }
  // Всё остальное, что меняет состояние, честно говорим, что не сохраняем
  if (method !== 'GET') return json(DEMO_WRITE_BLOCKED, 400);
  return json({ detail: `демо: маршрут ${path} не реализован` }, 404);
};

// --- подмена WebSocket ------------------------------------------------------
class DemoSocket {
  constructor() {
    this.readyState = 1;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;

    setTimeout(() => {
      if (this.onopen) this.onopen({});
      this.send({ type: 'snapshot', items: TAGS.map(liveItem) });
    }, 60);

    this.timer = setInterval(() => {
      // Пишем только то, что заметно изменилось, — как делает мёртвая зона
      const items = TAGS.filter((t) => t.datatype === 'bool' || Math.random() < 0.75)
        .map(liveItem);
      this.send({ type: 'values', items });
    }, 500);
  }

  send(message) {
    if (this.onmessage) this.onmessage({ data: JSON.stringify(message) });
  }

  close() {
    clearInterval(this.timer);
    this.readyState = 3;
    if (this.onclose) this.onclose({});
  }
}

function liveItem(tag) {
  const t = now();
  return {
    tag_id: tag.tag_id,
    ts: new Date(t * 1000).toISOString(),
    value: valueAt(tag, t),
    text: null,
    quality: 0,
    quality_name: 'GOOD',
  };
}

const RealWebSocket = window.WebSocket;
window.WebSocket = function DemoWebSocket(url) {
  if (String(url).includes('/api/live')) return new DemoSocket();
  return new RealWebSocket(url);
};

export const demo = { tags: TAGS, nodes: NODES, devices: DEVICES, valueAt };
