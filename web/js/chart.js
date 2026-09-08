// Обёртка над uPlot: история из БД + дописывание живых значений.

import { el, get } from './core.js';
import { live } from './live.js';

const PALETTE = [
  '#4ade80', '#60a5fa', '#fbbf24', '#f87171', '#c084fc',
  '#22d3ee', '#fb923c', '#a3e635', '#f472b6', '#94a3b8',
];

export class TimeChart {
  /**
   * @param {HTMLElement} host     куда рисовать
   * @param {Array} tags           [{tag_id, name, unit, datatype}]
   * @param {Object} options       {minutes, title, height, live}
   */
  constructor(host, tags, options = {}) {
    this.host = host;
    this.tags = tags;
    this.minutes = options.minutes ?? 30;
    this.height = options.height ?? 240;
    this.title = options.title || '';
    this.stepped = options.stepped ?? tags.every((t) => t.datatype === 'bool');
    this.plot = null;
    this.data = [[], ...tags.map(() => [])];
    this.timer = null;
    this.unsubscribe = null;
    this.destroyed = false;

    this.head = el('div', { class: 'chart-head' },
      el('span', { class: 'chart-title' }, this.title),
      el('span', { class: 'chart-sub' }, this.subtitle()));
    this.body = el('div', {});
    this.card = el('div', { class: 'chart-card' }, this.head, this.body);
    host.append(this.card);

    this.onResize = () => this.resize();
    window.addEventListener('resize', this.onResize);
  }

  subtitle() {
    const units = [...new Set(this.tags.map((t) => t.unit).filter(Boolean))];
    return `${this.tags.length} тег(ов)${units.length ? ` · ${units.join(', ')}` : ''}`;
  }

  async load() {
    const ids = this.tags.map((t) => t.tag_id).join(',');
    if (!ids) return;
    const response = await get(`/series?tag_ids=${ids}&minutes=${this.minutes}&max_points=1200`);
    const series = response.series || {};

    const times = new Set();
    for (const tag of this.tags) {
      for (const point of series[tag.tag_id] || []) times.add(point[0]);
    }
    const xs = [...times].sort((a, b) => a - b);
    const index = new Map(xs.map((x, i) => [x, i]));
    const columns = this.tags.map(() => new Array(xs.length).fill(null));

    this.tags.forEach((tag, i) => {
      for (const [ts, avg] of (series[tag.tag_id] || []).map((p) => [p[0], p[1]])) {
        columns[i][index.get(ts)] = avg;
      }
    });

    this.data = [xs, ...columns];
    this.render();
  }

  render() {
    if (this.destroyed) return;
    const width = Math.max(this.body.clientWidth || this.host.clientWidth || 600, 260);
    const opts = {
      width,
      height: this.height,
      padding: [10, 12, 0, 0],
      cursor: { drag: { x: true, y: false } },
      legend: { live: true },
      scales: { x: { time: true } },
      axes: [
        { stroke: '#6b7889', grid: { stroke: '#1f2836', width: 1 },
          ticks: { stroke: '#26303f' }, font: '11px ui-monospace, monospace' },
        { stroke: '#6b7889', grid: { stroke: '#1f2836', width: 1 },
          ticks: { stroke: '#26303f' }, font: '11px ui-monospace, monospace',
          size: 58 },
      ],
      series: [
        { label: 'время' },
        ...this.tags.map((tag, i) => ({
          label: tag.unit ? `${tag.name}, ${tag.unit}` : tag.name,
          stroke: PALETTE[i % PALETTE.length],
          width: 1.8,
          spanGaps: true,
          points: { show: false },
          paths: this.stepped
            ? uPlot.paths.stepped({ align: 1 })
            : undefined,
          value: (_self, raw) => (raw === null || raw === undefined
            ? '—'
            : Number(raw).toLocaleString('ru-RU', { maximumFractionDigits: 3 })),
        })),
      ],
    };

    if (this.plot) this.plot.destroy();
    this.body.innerHTML = '';
    this.plot = new uPlot(opts, this.data, this.body);
  }

  resize() {
    if (!this.plot || this.destroyed) return;
    const width = Math.max(this.body.clientWidth || 600, 260);
    this.plot.setSize({ width, height: this.height });
  }

  /** Раз в секунду дописываем текущее значение из живого потока. */
  startLive(intervalMs = 1000) {
    if (this.timer) return;
    this.timer = setInterval(() => this.appendNow(), intervalMs);
  }

  appendNow() {
    if (!this.plot || this.destroyed) return;
    const now = Date.now() / 1000;
    let changed = false;
    this.data[0].push(now);
    this.tags.forEach((tag, i) => {
      const item = live.value(tag.tag_id);
      const column = this.data[i + 1];
      let value = null;
      if (item && item.quality === 0 && item.value !== null) value = item.value;
      else if (column.length) value = column[column.length - 1];
      column.push(value);
      if (value !== null) changed = true;
    });

    const cutoff = now - this.minutes * 60;
    while (this.data[0].length > 2 && this.data[0][0] < cutoff) {
      for (const column of this.data) column.shift();
    }
    if (changed || this.data[0].length < 3) this.plot.setData(this.data);
  }

  destroy() {
    this.destroyed = true;
    if (this.timer) clearInterval(this.timer);
    if (this.unsubscribe) this.unsubscribe();
    window.removeEventListener('resize', this.onResize);
    if (this.plot) this.plot.destroy();
    this.card.remove();
  }
}
