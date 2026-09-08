// Оболочка: навигация, шапка, индикаторы состояния и маршрутизация страниц.

import { el, clear, get, fail } from './js/core.js';
import { live } from './js/live.js';

import * as setup from './js/pages/setup.js';
import * as connect from './js/pages/connect.js';
import * as devices from './js/pages/devices.js';
import * as monitor from './js/pages/monitor.js';
import * as twin from './js/pages/twin.js';
import * as grafana from './js/pages/grafana.js';
import * as system from './js/pages/settings.js';

const PAGES = {
  connect: { module: connect, label: 'Подключение', icon: '⊕' },
  devices: { module: devices, label: 'Устройства', icon: '▤' },
  monitor: { module: monitor, label: 'Мониторинг', icon: '∿' },
  twin: { module: twin, label: 'Цифровой двойник', icon: '⌗' },
  grafana: { module: grafana, label: 'Grafana', icon: '◱' },
  system: { module: system, label: 'Система', icon: '⚙' },
  setup: { module: setup, label: 'Настройка БД', icon: '⛁', hidden: true },
};

const view = document.getElementById('view');
const nav = document.getElementById('nav');
const titleNode = document.getElementById('page-title');
const subNode = document.getElementById('page-sub');
const actions = document.getElementById('topbar-actions');

const shell = {
  status: null,
  setTitle(title, sub = '') {
    titleNode.textContent = title;
    subNode.textContent = sub;
  },
  setActions(nodes) {
    clear(actions);
    for (const node of nodes || []) actions.append(node);
  },
  async refreshStatus() {
    try {
      shell.status = await get('/status');
      paintStatus(shell.status);
      return shell.status;
    } catch (err) {
      paintStatus(null);
      return null;
    }
  },
};

let cleanup = null;
let currentRoute = null;

function buildNav() {
  clear(nav);
  for (const [key, page] of Object.entries(PAGES)) {
    if (page.hidden) continue;
    nav.append(el('a', { href: `#/${key}`, dataset: { route: key } },
      el('span', { style: 'width:16px;display:inline-block;text-align:center' }, page.icon),
      page.label,
      el('span', { class: 'badge', dataset: { badge: key } })));
  }
}

function paintNav(route) {
  for (const link of nav.querySelectorAll('a')) {
    link.classList.toggle('active', link.dataset.route === route);
  }
}

function paintStatus(status) {
  const db = document.getElementById('pill-db');
  const poll = document.getElementById('pill-poll');
  const gf = document.getElementById('pill-grafana');

  if (!status) {
    for (const pill of [db, poll, gf]) pill.className = 'pill bad';
    db.textContent = 'нет связи с приложением';
    poll.textContent = '';
    gf.textContent = '';
    return;
  }

  db.className = `pill ${status.database.connected ? 'ok' : 'bad'}`;
  db.textContent = status.database.connected
    ? `БД${status.database.timescaledb ? ' · TS' : ''}`
    : 'БД не настроена';

  const rt = status.runtime;
  const connected = rt.devices.filter((d) => d.connected).length;
  poll.className = `pill ${rt.devices.length === 0 ? '' : connected ? 'ok' : 'warn'}`;
  poll.textContent = rt.devices.length
    ? `опрос ${connected}/${rt.devices.length}` : 'опрос: нет устройств';

  gf.className = `pill ${status.grafana.running ? 'ok' : status.grafana.installed
    ? 'warn' : ''}`;
  gf.textContent = status.grafana.running ? 'Grafana работает'
    : status.grafana.installed ? 'Grafana остановлена' : 'Grafana не установлена';

  const badge = nav.querySelector('[data-badge="devices"]');
  if (badge) badge.textContent = rt.devices.length ? String(rt.devices.length) : '';
}

async function route() {
  const raw = (location.hash || '').replace(/^#\/?/, '') || 'monitor';
  const key = PAGES[raw] ? raw : 'monitor';

  await shell.refreshStatus();
  // Пока нет базы, всё остальное бессмысленно — ведём в мастер настройки
  const needSetup = !shell.status || !shell.status.database.connected;
  const target = needSetup && key !== 'setup' ? 'setup' : key;
  if (needSetup && key !== 'setup') {
    location.hash = '#/setup';
    return;
  }

  if (currentRoute === target && target !== 'setup') return;
  currentRoute = target;

  if (cleanup) { try { cleanup(); } catch (err) { console.error(err); } cleanup = null; }
  paintNav(target);
  clear(view);

  const page = PAGES[target].module;
  shell.setTitle(page.title || '', page.subtitle || '');
  shell.setActions([]);
  try {
    cleanup = await page.mount(view, shell);
  } catch (err) {
    fail(err);
    view.replaceChildren(el('div', { class: 'note bad' }, String(err.message || err)));
  }
}

// --- демо-режим ------------------------------------------------------------
// Интерфейс умеет работать без сервера: витрина на GitHub Pages и просто
// «пощёлкать, ничего не устанавливая». Подменять fetch нужно до первого
// запроса, поэтому проверка идёт раньше всего остального.
async function demoRequested() {
  const params = new URLSearchParams(location.search);
  if (params.has('demo')) return params.get('demo') !== '0';
  const local = ['localhost', '127.0.0.1', '::1', ''].includes(location.hostname);
  if (local) return false;
  try {
    const response = await fetch('/api/status', { cache: 'no-store' });
    return !response.ok;
  } catch {
    return true;
  }
}

/** Адрес репозитория выводим из адреса Pages: user.github.io/repo → github.com/user/repo. */
function repoUrl() {
  const user = /^([\w-]+)\.github\.io$/.exec(location.hostname);
  const repo = location.pathname.split('/').filter(Boolean)[0];
  if (user && repo) return `https://github.com/${user[1]}/${repo}`;
  if (user) return `https://github.com/${user[1]}`;
  return 'https://github.com/';
}

function markDemo() {
  window.PLC_DEMO = true;
  document.title = `Демо · ${document.title}`;
  document.querySelector('.sidebar-foot').prepend(
    el('div', { class: 'demo-note' },
      el('b', {}, 'Демо-режим'),
      el('span', {}, 'Данные генерируются в браузере, сервера нет. '
        + 'Изменения не сохраняются.'),
      el('a', { href: repoUrl(), target: '_blank', rel: 'noopener' },
        'Исходники и запуск →')));
}

async function start() {
  buildNav();
  if (await demoRequested()) {
    await import('./js/demo.js');
    markDemo();
  }
  window.addEventListener('hashchange', () => { currentRoute = null; route(); });
  live.connect();
  route();
  setInterval(() => shell.refreshStatus(), 5000);
}

start();
