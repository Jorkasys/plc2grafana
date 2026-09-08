// Мелкие помощники: DOM, запросы к API, форматирование, уведомления.
// Ни одной внешней зависимости — приложение должно работать без интернета.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key === 'value') node.value = value;
    else if (key === 'checked') node.checked = !!value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat(4)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

// --- API -------------------------------------------------------------------
export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload;
  }
}

export async function api(path, options = {}) {
  const opts = { headers: {}, ...options };
  if (opts.body !== undefined && typeof opts.body !== 'string') {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const response = await fetch(`/api${path}`, opts);
  const text = await response.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!response.ok) {
    const detail = (data && data.detail) || response.statusText || 'ошибка запроса';
    throw new ApiError(typeof detail === 'string' ? detail : JSON.stringify(detail),
                       response.status, data);
  }
  return data;
}

export const get = (path) => api(path);
export const post = (path, body) => api(path, { method: 'POST', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const del = (path) => api(path, { method: 'DELETE' });

// --- уведомления -----------------------------------------------------------
export function toast(message, kind = 'info', ms = 4200) {
  const root = document.getElementById('toasts');
  const node = el('div', { class: `toast ${kind}` }, message);
  root.append(node);
  setTimeout(() => {
    node.style.transition = 'opacity .25s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 260);
  }, ms);
  return node;
}

export function fail(error) {
  console.error(error);
  toast(error && error.message ? error.message : String(error), 'bad', 7000);
}

// --- модальное окно --------------------------------------------------------
export function modal(title, contentNode, { wide = false } = {}) {
  const root = document.getElementById('modal-root');
  let resolveFn;
  const done = new Promise((resolve) => { resolveFn = resolve; });

  const close = (value) => { backdrop.remove(); resolveFn(value); };
  const box = el('div', { class: 'modal', style: wide ? 'width:min(1000px,100%)' : '' },
    el('h2', {}, title), contentNode);
  const backdrop = el('div', { class: 'modal-backdrop', onclick: (e) => {
    if (e.target === backdrop) close(null);
  } }, box);

  document.addEventListener('keydown', function esc(e) {
    if (e.key === 'Escape') { document.removeEventListener('keydown', esc); close(null); }
  });
  root.append(backdrop);
  return { close, done, box };
}

export async function confirmDialog(title, message, { danger = true } = {}) {
  let handle;
  const body = el('div', {},
    el('p', { class: 'hint' }, message),
    el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:14px' },
      el('button', { class: 'ghost', onclick: () => handle.close(false) }, 'Отмена'),
      el('button', { class: danger ? 'danger' : 'primary', onclick: () => handle.close(true) },
        'Подтвердить')));
  handle = modal(title, body);
  return (await handle.done) === true;
}

// --- форматирование --------------------------------------------------------
export function num(value, decimals = 3) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e7)) return value.toExponential(2);
  const d = abs >= 1000 ? 1 : decimals;
  return Number(value).toLocaleString('ru-RU', {
    minimumFractionDigits: 0, maximumFractionDigits: d,
  });
}

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 1) return 'сейчас';
  if (seconds < 60) return `${Math.round(seconds)} с`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} мин`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} ч`;
  return `${Math.round(seconds / 86400)} дн`;
}

export function bytes(value) {
  if (!value) return '0 Б';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let i = 0;
  let v = value;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

export const QUALITY = {
  0: 'GOOD', 1: 'НЕТ СВЯЗИ', 2: 'ВНЕ ГРАНИЦ', 3: 'ОШИБКА ДЕКОДА', 4: 'УСТАРЕЛО',
};

export function qualityCell(quality) {
  if (quality === null || quality === undefined) {
    return el('span', { class: 'q q-none' }, 'нет данных');
  }
  return el('span', { class: `q q-${quality}` }, QUALITY[quality] || String(quality));
}

export function displayValue(row) {
  if (row.value_text !== null && row.value_text !== undefined && row.value_text !== '') {
    return row.value_text;
  }
  if (row.value === null || row.value === undefined) return '—';
  if (row.datatype === 'bool') return row.value > 0 ? 'ВКЛ' : 'выкл';
  return num(row.value);
}

// --- разное ----------------------------------------------------------------
export function debounce(fn, ms = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

export function field(labelText, input, hint) {
  return el('label', { class: 'field' }, labelText, input,
    hint ? el('span', { class: 'muted', style: 'font-size:11px' }, hint) : null);
}

export function input(attrs = {}) {
  return el('input', { autocomplete: 'off', ...attrs });
}

export function select(options, value, attrs = {}) {
  const node = el('select', attrs);
  for (const opt of options) {
    const [val, label] = Array.isArray(opt) ? opt : [opt, opt];
    node.append(el('option', { value: val, selected: String(val) === String(value) }, label));
  }
  node.value = value;
  return node;
}

export function spinner(text = 'Загрузка…') {
  return el('div', { class: 'row', style: 'color:var(--text-faint);padding:20px' },
    el('span', { class: 'spinner' }), text);
}

export function emptyState(title, text, action) {
  return el('div', { class: 'empty' }, el('h3', {}, title), el('p', {}, text), action || null);
}
