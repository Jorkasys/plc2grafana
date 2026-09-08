// Система: состояние базы, объём данных, срок хранения, план опроса.

import {
  el, field, input, get, patch, post, toast, fail, spinner, num, bytes,
} from '../core.js';

export const title = 'Система';
export const subtitle = 'Хранилище, срок хранения и план опроса';

export async function mount(view, shell) {
  shell.setTitle(title, subtitle);
  shell.setActions([el('button', { onclick: reload }, 'Обновить')]);

  await reload();

  async function reload() {
    view.replaceChildren(spinner());
    try {
      const [status, settings] = await Promise.all([get('/status'), get('/settings')]);
      const stats = status.database.connected
        ? await get('/system/stats').catch(() => null) : null;
      const plan = status.database.connected
        ? await get('/runtime/plan').catch(() => null) : null;
      view.replaceChildren(
        dbCard(status, stats),
        retentionCard(settings, stats),
        planCard(plan),
        aboutCard(status));
    } catch (err) {
      view.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  function dbCard(status, stats) {
    const db = status.database;
    return el('div', { class: 'card' },
      el('div', { class: 'row' },
        el('h2', { style: 'margin:0' }, 'Хранилище'),
        el('span', { class: `pill ${db.connected ? 'ok' : 'bad'}` },
          db.connected ? 'подключено' : 'нет связи'),
        el('span', { class: 'spacer' }),
        db.connected ? null : el('button', { class: 'small', onclick: reconnect },
          'Переподключиться')),
      db.error ? el('div', { class: 'note bad' }, db.error) : null,
      el('div', { class: 'grid cols-4', style: 'margin-top:12px' },
        stat('PostgreSQL', db.server_version || '—'),
        stat('TimescaleDB', db.timescaledb ? 'включён' : 'нет',
          db.timescaledb ? '' : 'работаем на обычном PostgreSQL'),
        stat('Строк значений', stats ? num(stats.rows, 0) : '—'),
        stat('Размер таблицы', stats ? stats.table_size : '—')),
      el('div', { class: 'muted mono', style: 'margin-top:12px;font-size:12px' }, db.dsn),
      stats && stats.first_ts
        ? el('div', { class: 'muted', style: 'margin-top:6px;font-size:12.5px' },
          `История: ${new Date(stats.first_ts).toLocaleString('ru-RU')} — `
          + `${new Date(stats.last_ts).toLocaleString('ru-RU')}`)
        : null);
  }

  function retentionCard(settings, stats) {
    const days = input({ value: settings.database.retention_days, type: 'number', min: '0',
      class: 'mono' });
    const port = input({ value: settings.grafana.port, type: 'number', class: 'mono' });
    return el('div', { class: 'card' },
      el('h2', {}, 'Настройки'),
      el('p', { class: 'hint' },
        'Срок хранения сырых значений. С TimescaleDB удаление делает политика '
        + 'самой базы, без него — приложение раз в час.'),
      el('div', { class: 'form-grid' },
        field('Хранить сырьё, дней', days, '0 — не удалять'),
        field('Порт Grafana', port, 'применится при следующем запуске')),
      el('div', { class: 'row', style: 'margin-top:14px' },
        el('button', { class: 'primary', onclick: async () => {
          try {
            await patch('/settings', {
              retention_days: Number(days.value) || 0,
              grafana_port: Number(port.value) || 3000,
            });
            toast('Сохранено', 'ok');
            await reload();
          } catch (err) { fail(err); }
        } }, 'Сохранить')));
  }

  function planCard(plan) {
    if (!plan) return null;
    const rows = plan.active.length ? plan.active : plan.preview;
    if (!rows.length) {
      return el('div', { class: 'card' },
        el('h2', {}, 'План опроса'),
        el('p', { class: 'hint' }, 'Пока нечего опрашивать — добавьте устройство.'));
    }
    return el('div', { class: 'card' },
      el('h2', {}, 'План опроса'),
      el('p', { class: 'hint' },
        'Соседние теги склеиваются в один Modbus-запрос: так десять тегов читаются '
        + 'двумя-тремя запросами, а не десятью.'),
      el('div', { class: 'table-wrap' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Устройство'), el('th', {}, 'Регистр'),
            el('th', { class: 'num' }, 'Адреса'), el('th', { class: 'num' }, 'Слов'),
            el('th', { class: 'num' }, 'Период, мс'), el('th', {}, 'Теги'))),
          el('tbody', {}, ...rows.map((b) => el('tr', {},
            el('td', { class: 'mono' }, b.device),
            el('td', {}, b.register),
            el('td', { class: 'num' }, `${b.address} … ${b.address + b.count - 1}`),
            el('td', { class: 'num' }, String(b.count)),
            el('td', { class: 'num' }, String(b.poll_interval_ms)),
            el('td', { class: 'muted', style: 'font-size:12px' },
              b.tags.join(', '))))))));
  }

  function aboutCard(status) {
    const rt = status.runtime;
    return el('div', { class: 'card' },
      el('h2', {}, 'Процесс'),
      el('div', { class: 'grid cols-4' },
        stat('Python', status.python),
        stat('Система', status.platform),
        stat('Буфер записи', num(rt.storage.pending_rows, 0), 'строк ждут записи'),
        stat('Записано строк', num(rt.storage.rows_written, 0),
          rt.storage.rows_dropped ? `потеряно ${rt.storage.rows_dropped}` : '')),
      rt.last_error ? el('div', { class: 'note bad' }, rt.last_error) : null);
  }

  function stat(label, value, sub) {
    return el('div', { class: 'stat' },
      el('div', { class: 'label' }, label),
      el('div', { class: 'value', style: 'font-size:18px' }, String(value)),
      sub ? el('div', { class: 'sub' }, sub) : null);
  }

  async function reconnect() {
    try {
      await post('/setup/reconnect');
      toast('Подключено', 'ok');
      await shell.refreshStatus();
      await reload();
    } catch (err) { fail(err); }
  }

  return () => {};
}
