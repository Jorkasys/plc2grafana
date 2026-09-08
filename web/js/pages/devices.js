// Список устройств, их состояние и редактор тегов.

import {
  el, field, input, select, get, patch, del, post, toast, fail, spinner,
  emptyState, confirmDialog, modal, num, ago, displayValue, qualityCell,
} from '../core.js';
import { live } from '../live.js';

export const title = 'Устройства';
export const subtitle = 'Что опрашиваем и как это работает';

const DATATYPES = ['bool', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
  'float32', 'float64', 'string'];
const REGISTERS = ['holding', 'input', 'coil', 'discrete'];
const ROLES = [['value', 'значение'], ['state', 'состояние'], ['alarm', 'авария'],
  ['counter', 'счётчик'], ['setpoint', 'уставка']];

export async function mount(view, shell) {
  shell.setTitle(title, subtitle);
  shell.setActions([
    el('button', { class: 'primary', onclick: () => { location.hash = '#/connect'; } },
      '+ Подключить устройство'),
    el('button', { onclick: reload }, 'Обновить'),
  ]);

  let timer = null;
  await reload();
  timer = setInterval(refreshStatusOnly, 3000);

  async function reload() {
    view.replaceChildren(spinner());
    try {
      const [devices, nodes] = await Promise.all([get('/devices'), get('/twin/nodes')]);
      if (!devices.length) {
        view.replaceChildren(el('div', { class: 'card' }, emptyState(
          'Устройств пока нет',
          'Подключите контроллер по IP — приложение само найдёт его теги',
          el('button', { class: 'primary', onclick: () => { location.hash = '#/connect'; } },
            'Подключить устройство'))));
        return;
      }
      view.replaceChildren(el('div', { class: 'grid cols-2' },
        ...devices.map((d) => deviceCard(d, nodes))));
    } catch (err) {
      view.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  async function refreshStatusOnly() {
    try {
      const devices = await get('/devices');
      for (const device of devices) {
        const node = view.querySelector(`[data-runtime="${device.device_id}"]`);
        if (node) node.replaceChildren(runtimeLine(device));
      }
    } catch { /* тихо: страница обновится при следующем заходе */ }
  }

  function deviceCard(device, nodes) {
    const rt = device.runtime;
    const state = !device.enabled ? 'bad' : rt && rt.connected ? 'ok' : 'warn';
    return el('div', { class: 'card' },
      el('div', { class: 'row' },
        el('h2', { style: 'margin:0' }, device.name),
        el('span', { class: `pill ${state}` },
          !device.enabled ? 'выключено' : rt && rt.connected ? 'на связи' : 'нет связи'),
        el('span', { class: 'spacer' }),
        el('span', { class: 'muted mono', style: 'font-size:12px' },
          device.transport === 'rtu' ? device.serial_port
            : `${device.host}:${device.port} · unit ${device.unit_id}`)),
      device.description ? el('p', { class: 'hint', style: 'margin:6px 0 0' },
        device.description) : null,
      el('div', { class: 'row', style: 'margin-top:10px', dataset: { runtime: device.device_id } },
        runtimeLine(device)),
      el('div', { class: 'row', style: 'margin-top:14px' },
        el('button', { class: 'small', onclick: () => openTags(device) },
          `Теги (${device.tag_count})`),
        el('button', { class: 'small', onclick: () => openSettings(device, nodes) },
          'Настройки'),
        el('button', { class: 'small', onclick: () => toggleEnabled(device) },
          device.enabled ? 'Остановить' : 'Запустить'),
        el('span', { class: 'spacer' }),
        el('button', { class: 'small danger', onclick: () => removeDevice(device) },
          'Удалить')));
  }

  function runtimeLine(device) {
    const rt = device.runtime;
    if (!rt) {
      return el('span', { class: 'muted', style: 'font-size:12.5px' },
        device.enabled ? 'опрос не запущен (нет включённых тегов)' : 'опрос остановлен');
    }
    const stats = [
      ['тегов', rt.tags],
      ['запросов', rt.blocks],
      ['опросов', num(rt.polls, 0)],
      ['ошибок', num(rt.poll_errors, 0)],
      ['записано', num(rt.samples_written, 0)],
      ['ответ', rt.seconds_since_success === null ? '—' : ago(rt.seconds_since_success)],
    ];
    return el('div', { class: 'row tight', style: 'font-size:12px' },
      ...stats.map(([k, v]) => el('span', { class: 'tag-chip' }, `${k}: ${v}`)));
  }

  async function toggleEnabled(device) {
    try {
      await patch(`/devices/${device.device_id}`, { enabled: !device.enabled });
      toast(device.enabled ? 'Опрос остановлен' : 'Опрос запущен', 'ok');
      await reload();
    } catch (err) { fail(err); }
  }

  async function removeDevice(device) {
    const ok = await confirmDialog('Удалить устройство?',
      `Будут удалены устройство «${device.name}», его теги и вся история значений. `
      + 'Это действие необратимо.');
    if (!ok) return;
    try {
      await del(`/devices/${device.device_id}`);
      toast('Устройство удалено', 'ok');
      await reload();
    } catch (err) { fail(err); }
  }

  // ------------------------------------------------------------- настройки
  function openSettings(device, nodes) {
    const f = {
      name: input({ value: device.name, class: 'mono' }),
      description: input({ value: device.description || '' }),
      host: input({ value: device.host || '', class: 'mono' }),
      port: input({ value: device.port, type: 'number', class: 'mono' }),
      unit_id: input({ value: device.unit_id, type: 'number', class: 'mono' }),
      timeout_s: input({ value: device.timeout_s, type: 'number', step: '0.5', class: 'mono' }),
      retries: input({ value: device.retries, type: 'number', class: 'mono' }),
      inter_request_delay_ms: input({ value: device.inter_request_delay_ms, type: 'number',
        class: 'mono' }),
      block_max_registers: input({ value: device.block_max_registers, type: 'number',
        class: 'mono' }),
      block_max_gap: input({ value: device.block_max_gap, type: 'number', class: 'mono' }),
      node_id: select([['', '— не привязано —'],
        ...nodes.map((n) => [String(n.node_id), n.path || n.name])],
      device.node_id ? String(device.node_id) : ''),
    };

    let handle;
    const body = el('div', {},
      el('div', { class: 'form-grid' },
        field('Имя', f.name),
        field('Описание', f.description),
        field('Узел двойника', f.node_id)),
      el('div', { class: 'form-grid', style: 'margin-top:12px' },
        field('Хост', f.host),
        field('Порт', f.port),
        field('Unit ID', f.unit_id),
        field('Таймаут, с', f.timeout_s),
        field('Повторов', f.retries),
        field('Пауза между запросами, мс', f.inter_request_delay_ms,
          'помогает слабым шлюзам'),
        field('Регистров в запросе', f.block_max_registers, '≤ 125'),
        field('Допустимый разрыв', f.block_max_gap, 'склейка соседних тегов')),
      el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:16px' },
        el('button', { class: 'ghost', onclick: () => handle.close(null) }, 'Отмена'),
        el('button', { class: 'primary', onclick: save }, 'Сохранить')));

    handle = modal(`Настройки — ${device.name}`, body);

    async function save() {
      try {
        await patch(`/devices/${device.device_id}`, {
          name: f.name.value.trim(),
          description: f.description.value.trim() || null,
          host: f.host.value.trim(),
          port: Number(f.port.value),
          unit_id: Number(f.unit_id.value),
          timeout_s: Number(f.timeout_s.value),
          retries: Number(f.retries.value),
          inter_request_delay_ms: Number(f.inter_request_delay_ms.value),
          block_max_registers: Number(f.block_max_registers.value),
          block_max_gap: Number(f.block_max_gap.value),
          node_id: f.node_id.value ? Number(f.node_id.value) : null,
        });
        toast('Сохранено', 'ok');
        handle.close(true);
        await reload();
      } catch (err) { fail(err); }
    }
  }

  // ----------------------------------------------------------------- теги
  async function openTags(device) {
    const container = el('div', {}, spinner());
    const handle = modal(`Теги — ${device.name}`, container, { wide: true });
    let nodes = [];
    try {
      const [tags, twin] = await Promise.all([
        get(`/devices/${device.device_id}/tags`), get('/twin/nodes')]);
      nodes = twin;
      container.replaceChildren(tagsTable(device, tags, nodes, handle));
    } catch (err) {
      container.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  function tagsTable(device, tags, nodes, handle) {
    const tbody = el('tbody');
    let painters = [];
    const redraw = () => {
      painters = [];
      tbody.replaceChildren(...tags.map((t) => {
        const { row, paint } = tagRow(t, nodes, () => {
          tags.splice(tags.indexOf(t), 1);
          redraw();
        });
        painters.push(paint);
        return row;
      }));
    };
    redraw();

    // Одна подписка на всю таблицу — отписываемся при закрытии окна
    const off = live.subscribe(() => painters.forEach((paint) => paint()));
    handle.done.then(off);

    return el('div', {},
      el('p', { class: 'hint' },
        'Правки применяются сразу: опрос перезапускается автоматически. '
        + 'Роль тега влияет на то, как он попадёт в дашборды Grafana — '
        + '«авария» рисуется красным и считается в сводке.'),
      el('div', { class: 'table-wrap', style: 'max-height:60vh' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Имя'), el('th', {}, 'Регистр'), el('th', { class: 'num' }, 'Адрес'),
            el('th', {}, 'Тип'), el('th', {}, 'Ед.'), el('th', { class: 'num' }, 'Масштаб'),
            el('th', { class: 'num' }, 'Период, мс'), el('th', {}, 'Роль'),
            el('th', {}, 'Узел'), el('th', {}, 'Текущее'), el('th', {}, ''))),
          tbody)),
      el('div', { class: 'row', style: 'justify-content:space-between;margin-top:14px' },
        el('button', { onclick: () => { handle.close(null); location.hash = '#/connect'; } },
          '+ Досканировать регистры'),
        el('button', { class: 'primary', onclick: () => handle.close(true) }, 'Готово')));
  }

  function tagRow(tag, nodes, onRemoved) {
    const cell = el('td', {});
    const update = async (payload) => {
      try {
        await patch(`/tags/${tag.tag_id}`, payload);
        Object.assign(tag, payload);
      } catch (err) { fail(err); }
    };

    const nodeSelect = select([['', '—'], ...nodes.map((n) => [String(n.node_id),
      n.path || n.name])], tag.node_id ? String(tag.node_id) : '', {
      onchange: (e) => update({ node_id: e.target.value ? Number(e.target.value) : null }),
    });

    const row = el('tr', {},
      el('td', {}, input({ value: tag.name, class: 'mono',
        onchange: (e) => update({ name: e.target.value.trim() }) })),
      el('td', {}, select(REGISTERS, tag.register,
        { onchange: (e) => update({ register: e.target.value }) })),
      el('td', { class: 'num' }, input({ value: tag.address, type: 'number', class: 'mono',
        style: 'width:80px', onchange: (e) => update({ address: Number(e.target.value) }) })),
      el('td', {}, select(DATATYPES, tag.datatype,
        { onchange: (e) => update({ datatype: e.target.value }) })),
      el('td', {}, input({ value: tag.unit || '', style: 'width:70px',
        onchange: (e) => update({ unit: e.target.value || null }) })),
      el('td', { class: 'num' }, input({ value: tag.scale, type: 'number', step: 'any',
        class: 'mono', style: 'width:90px',
        onchange: (e) => update({ scale: Number(e.target.value) }) })),
      el('td', { class: 'num' }, input({ value: tag.poll_interval_ms, type: 'number',
        class: 'mono', style: 'width:80px',
        onchange: (e) => update({ poll_interval_ms: Number(e.target.value) }) })),
      el('td', {}, select(ROLES, tag.role,
        { onchange: (e) => update({ role: e.target.value }) })),
      el('td', {}, nodeSelect),
      cell,
      el('td', {}, el('button', { class: 'small danger', onclick: async () => {
        if (!await confirmDialog('Удалить тег?',
          `Тег «${tag.name}» и его история будут удалены.`)) return;
        try {
          await del(`/tags/${tag.tag_id}`);
          onRemoved();
          toast('Тег удалён', 'ok');
        } catch (err) { fail(err); }
      } }, '×')));

    const paint = () => {
      const item = live.value(tag.tag_id);
      cell.replaceChildren(item
        ? el('div', { class: 'row tight' },
          el('span', { class: 'mono' },
            displayValue({ ...tag, value: item.value, value_text: item.text })),
          qualityCell(item.quality))
        : el('span', { class: 'muted' }, '—'));
    };
    paint();
    return { row, paint };
  }

  return () => { if (timer) clearInterval(timer); };
}
