// Живой мониторинг: графики аналоговых тегов и таблица текущих значений.

import {
  el, select, get, spinner, emptyState, num, ago, displayValue, qualityCell, fail,
} from '../core.js';
import { live } from '../live.js';
import { TimeChart } from '../chart.js';

export const title = 'Мониторинг';
export const subtitle = 'Значения в реальном времени, без Grafana';

const RANGES = [[5, '5 минут'], [30, '30 минут'], [60, '1 час'], [360, '6 часов'],
  [1440, 'сутки']];

export async function mount(view, shell) {
  shell.setTitle(title, subtitle);

  const state = {
    deviceId: '',
    minutes: 30,
    tags: [],
    devices: [],
    charts: [],
  };

  const deviceSelect = select([['', 'Все устройства']], '', { onchange: (e) => {
    state.deviceId = e.target.value;
    build();
  } });
  const rangeSelect = select(RANGES, 30, { onchange: (e) => {
    state.minutes = Number(e.target.value);
    build();
  } });

  shell.setActions([
    el('label', { class: 'row tight', style: 'font-size:12.5px;color:var(--text-dim)' },
      'Устройство', deviceSelect),
    el('label', { class: 'row tight', style: 'font-size:12.5px;color:var(--text-dim)' },
      'Период', rangeSelect),
  ]);

  const charts = el('div', { class: 'grid cols-2' });
  const table = el('div');
  view.replaceChildren(spinner());

  let unsubscribe = null;
  let ticker = null;

  try {
    state.devices = await get('/devices');
    deviceSelect.replaceChildren(
      el('option', { value: '' }, 'Все устройства'),
      ...state.devices.map((d) => el('option', { value: String(d.device_id) }, d.name)));
    await build();
  } catch (err) {
    view.replaceChildren(el('div', { class: 'note bad' }, err.message));
  }

  async function build() {
    destroyCharts();
    view.replaceChildren(spinner());
    try {
      const query = state.deviceId ? `?device_id=${state.deviceId}` : '';
      state.tags = await get(`/tags${query}`);
      const active = state.tags.filter((t) => t.enabled);
      if (!active.length) {
        view.replaceChildren(el('div', { class: 'card' }, emptyState(
          'Нет тегов для показа',
          'Подключите устройство и отсканируйте его регистры',
          el('button', { class: 'primary', onclick: () => { location.hash = '#/connect'; } },
            'Подключить устройство'))));
        return;
      }

      view.replaceChildren(charts, el('div', { style: 'margin-top:16px' }, table));
      charts.replaceChildren();

      const analog = active.filter((t) => t.datatype !== 'bool' && t.datatype !== 'string');
      for (const [name, group] of groupTags(analog)) {
        const chart = new TimeChart(charts, group, {
          title: name, minutes: state.minutes, height: 220,
        });
        state.charts.push(chart);
        chart.load().then(() => chart.startLive()).catch(fail);
      }

      const bools = active.filter((t) => t.datatype === 'bool');
      if (bools.length) {
        const chart = new TimeChart(charts, bools.slice(0, 10), {
          title: 'Дискретные сигналы', minutes: state.minutes, height: 200, stepped: true,
        });
        state.charts.push(chart);
        chart.load().then(() => chart.startLive()).catch(fail);
      }

      buildTable(active);
    } catch (err) {
      view.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  function buildTable(tags) {
    const rows = new Map();
    const tbody = el('tbody');
    for (const tag of tags) {
      const value = el('td', { class: 'num' });
      const quality = el('td', {});
      const age = el('td', { class: 'num muted' });
      rows.set(tag.tag_id, { tag, value, quality, age });
      tbody.append(el('tr', {},
        el('td', { class: 'mono' }, tag.device),
        el('td', { class: 'mono' }, tag.name),
        el('td', { class: 'muted' }, tag.description || ''),
        value,
        el('td', { class: 'muted' }, tag.unit || ''),
        quality,
        age));
    }

    table.replaceChildren(
      el('div', { class: 'row', style: 'margin-bottom:8px' },
        el('h3', { style: 'margin:0;color:var(--text-dim)' }, 'Текущие значения'),
        el('span', { class: 'spacer' }),
        el('span', { class: 'muted', style: 'font-size:12px' },
          `тегов: ${tags.length}`)),
      el('div', { class: 'table-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Устройство'), el('th', {}, 'Тег'), el('th', {}, 'Описание'),
            el('th', { class: 'num' }, 'Значение'), el('th', {}, 'Ед.'),
            el('th', {}, 'Качество'), el('th', { class: 'num' }, 'Обновлено'))),
          tbody)));

    const paint = () => {
      const now = Date.now();
      for (const { tag, value, quality, age } of rows.values()) {
        const item = live.value(tag.tag_id);
        if (!item) { value.textContent = '—'; continue; }
        value.textContent = displayValue({
          ...tag, value: item.value, value_text: item.text,
        });
        quality.replaceChildren(qualityCell(item.quality));
        age.textContent = ago((now - Date.parse(item.ts)) / 1000);
      }
    };

    if (unsubscribe) unsubscribe();
    unsubscribe = live.subscribe(paint);
    if (ticker) clearInterval(ticker);
    ticker = setInterval(paint, 1000);
    paint();

    // Первое наполнение — из базы: живой поток начнётся со следующей записи
    get(`/latest${state.deviceId ? `?device_id=${state.deviceId}` : ''}`)
      .then((items) => {
        for (const item of items) {
          const row = rows.get(item.tag_id);
          if (!row || live.value(item.tag_id)) continue;
          row.value.textContent = displayValue(item);
          row.quality.replaceChildren(qualityCell(item.quality));
          row.age.textContent = ago(item.age_s);
        }
      })
      .catch(() => {});
  }

  function groupTags(tags) {
    const groups = new Map();
    for (const tag of tags) {
      const key = tag.group_name
        || (tag.unit ? `Сигналы, ${tag.unit}` : 'Сигналы без единиц');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(tag);
    }
    // Больше восьми линий на графике читать невозможно
    const result = [];
    for (const [name, items] of [...groups].sort((a, b) => a[0].localeCompare(b[0]))) {
      for (let i = 0; i < items.length; i += 8) {
        const chunk = items.slice(i, i + 8);
        result.push([items.length > 8 ? `${name} (${i / 8 + 1})` : name, chunk]);
      }
    }
    return result;
  }

  function destroyCharts() {
    for (const chart of state.charts) chart.destroy();
    state.charts = [];
  }

  return () => {
    destroyCharts();
    if (unsubscribe) unsubscribe();
    if (ticker) clearInterval(ticker);
  };
}
