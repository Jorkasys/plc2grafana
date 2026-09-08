// Мастер подключения устройства: адрес → проверка связи → сканирование
// карты регистров → правка предложенных тегов → сохранение.

import {
  el, field, input, select, get, post, put, toast, fail, spinner, emptyState,
} from '../core.js';

export const title = 'Подключение устройства';
export const subtitle = 'Введите адрес контроллера — остальное приложение предложит само';

const REGISTER_LABELS = {
  holding: 'Holding (FC3, 4xxxx)',
  input: 'Input (FC4, 3xxxx)',
  coil: 'Coil (FC1, 0xxxx)',
  discrete: 'Discrete (FC2, 1xxxx)',
};

const DATATYPES = ['bool', 'int16', 'uint16', 'int32', 'uint32', 'int64', 'uint64',
  'float32', 'float64', 'string'];

export async function mount(view, shell) {
  const state = {
    step: 1,
    conn: { host: '', port: 502, unit_id: 1, timeout_s: 3 },
    probe: null,
    scan: null,
    candidates: [],
    devices: [],
    nodes: [],
  };

  shell.setTitle(title, subtitle);
  shell.setActions([]);

  try {
    [state.devices, state.nodes] = await Promise.all([
      get('/devices').catch(() => []),
      get('/twin/nodes').catch(() => []),
    ]);
  } catch { /* база может быть ещё не настроена */ }

  render();

  function render() {
    view.replaceChildren(steps(), body());
  }

  function steps() {
    const items = [
      [1, 'Адрес и связь'],
      [2, 'Сканирование'],
      [3, 'Теги'],
      [4, 'Сохранение'],
    ];
    return el('div', { class: 'steps' }, items.map(([n, label]) => el('div', {
      class: `step ${state.step === n ? 'active' : ''} ${state.step > n ? 'done' : ''}`,
    }, el('span', { class: 'n' }, state.step > n ? '✓' : n), label)));
  }

  function body() {
    if (state.step === 1) return stepConnect();
    if (state.step === 2) return stepScan();
    if (state.step === 3) return stepTags();
    return stepSave();
  }

  // ------------------------------------------------------------ шаг 1: связь
  function stepConnect() {
    const host = input({ value: state.conn.host, placeholder: '192.168.0.10', class: 'mono' });
    const port = input({ value: state.conn.port, type: 'number', class: 'mono' });
    const unit = input({ value: state.conn.unit_id, type: 'number', min: '0', max: '247',
      class: 'mono' });
    const timeout = input({ value: state.conn.timeout_s, type: 'number', step: '0.5',
      min: '0.5', class: 'mono' });
    const out = el('div');
    const button = el('button', { class: 'primary', onclick: run }, 'Проверить связь');

    host.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });

    async function run() {
      state.conn = {
        host: host.value.trim(),
        port: Number(port.value) || 502,
        unit_id: Number(unit.value) || 0,
        timeout_s: Number(timeout.value) || 3,
      };
      if (!state.conn.host) { toast('Укажите IP-адрес', 'warn'); return; }
      button.disabled = true;
      out.replaceChildren(spinner('Подключаемся и определяем карту регистров…'));
      try {
        const probe = await post('/discovery/test', state.conn);
        state.probe = probe;
        out.replaceChildren(probeReport(probe));
      } catch (err) {
        out.replaceChildren(el('div', { class: 'note bad' }, err.message));
      } finally {
        button.disabled = false;
      }
    }

    return el('div', { class: 'card', style: 'max-width:900px' },
      el('h2', {}, 'Адрес устройства'),
      el('p', { class: 'hint' },
        'Контроллер, шлюз Profinet/Profibus → Modbus TCP, счётчик, частотник — '
        + 'подойдёт любое устройство с Modbus TCP. Приложение проверит связь и само '
        + 'определит, какие типы регистров оно отдаёт и до какого адреса.'),
      el('div', { class: 'form-grid' },
        field('IP-адрес или имя хоста', host),
        field('Порт', port, 'стандартный — 502'),
        field('Unit / Slave ID', unit, 'частые значения: 1, 0, 255'),
        field('Таймаут, с', timeout)),
      el('div', { class: 'row', style: 'margin-top:16px' }, button),
      out);
  }

  function probeReport(probe) {
    if (!probe.ok) {
      return el('div', {},
        el('div', { class: 'note bad' }, probe.error || 'устройство не отвечает'),
        el('div', { class: 'note' },
          el('b', {}, 'Что проверить: '),
          el('ul', { style: 'margin:6px 0 0;padding-left:18px' },
            el('li', {}, 'доступен ли адрес: ', el('code', {},
              `Test-NetConnection ${state.conn.host} -Port ${state.conn.port}`)),
            el('li', {}, 'включён ли на устройстве Modbus TCP (у S7 — блок MB_SERVER)'),
            el('li', {}, 'верный ли unit id — попробуйте 1, 0 и 255'),
            el('li', {}, 'не блокирует ли брандмауэр исходящее соединение'))));
    }

    const rows = Object.entries(probe.registers).map(([key, value]) => el('tr', {},
      el('td', {}, REGISTER_LABELS[key] || key),
      el('td', {}, value.supported
        ? el('span', { class: 'q q-0' }, 'отвечает')
        : el('span', { class: 'q q-1' }, 'нет')),
      el('td', { class: 'num' }, value.supported && value.max_address !== null
        ? `0 … ${value.max_address}` : '—'),
      el('td', { class: 'muted', style: 'font-size:12px' },
        value.supported ? '' : (value.error || '').slice(0, 90))));

    return el('div', {},
      el('div', { class: 'note ok' },
        `✓ Связь есть: ${probe.endpoint}, unit ${probe.unit_id}`
        + (probe.latency_ms !== null ? `, отклик ${probe.latency_ms} мс` : '')),
      el('div', { class: 'table-wrap', style: 'margin-top:12px' },
        el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, 'Тип регистров'), el('th', {}, 'Доступность'),
            el('th', { class: 'num' }, 'Диапазон адресов'), el('th', {}, ''))),
          el('tbody', {}, rows))),
      el('div', { class: 'row', style: 'margin-top:14px' },
        el('button', { class: 'primary', onclick: () => { state.step = 2; render(); } },
          'Дальше: сканировать регистры →')));
  }

  // -------------------------------------------------------- шаг 2: сканирование
  function stepScan() {
    const supported = Object.entries(state.probe?.registers || {})
      .filter(([, v]) => v.supported).map(([k]) => k);
    const options = (supported.length ? supported : Object.keys(REGISTER_LABELS))
      .map((k) => [k, REGISTER_LABELS[k]]);

    const register = select(options, options[0][0], { onchange: syncMax });
    const start = input({ value: 0, type: 'number', min: '0', class: 'mono' });
    const count = input({ value: 64, type: 'number', min: '1', max: '500', class: 'mono' });
    const samples = input({ value: 4, type: 'number', min: '1', max: '10', class: 'mono' });
    const maxHint = el('span', { class: 'muted', style: 'font-size:12px' });
    const out = el('div');
    const button = el('button', { class: 'primary', onclick: run }, 'Сканировать');

    function syncMax() {
      const info = state.probe?.registers?.[register.value];
      maxHint.textContent = info && info.max_address !== null
        ? `устройство отвечает до адреса ${info.max_address}` : '';
      if (info && info.max_address !== null) {
        count.value = Math.min(Number(count.value) || 64, info.max_address + 1);
      }
    }
    syncMax();

    async function run() {
      button.disabled = true;
      const passes = Number(samples.value) || 3;
      out.replaceChildren(spinner(
        `Читаем регистры ${passes} раза с паузой — так видно, что меняется…`));
      try {
        const payload = {
          ...state.conn,
          register: register.value,
          start: Number(start.value) || 0,
          count: Number(count.value) || 64,
          samples: passes,
          delay_s: 0.8,
        };
        const scan = await post('/discovery/scan', payload);
        state.scan = scan;
        state.candidates = scan.candidates.map((c, i) => ({
          ...c, _key: `${c.register}:${c.address}:${i}`,
        }));
        out.replaceChildren(scanSummary(scan));
      } catch (err) {
        out.replaceChildren(el('div', { class: 'note bad' }, err.message));
      } finally {
        button.disabled = false;
      }
    }

    return el('div', { class: 'card', style: 'max-width:900px' },
      el('h2', {}, 'Сканирование карты регистров'),
      el('p', { class: 'hint' },
        'Приложение прочитает выбранный участок несколько раз подряд и по тому, '
        + 'как меняются значения, предложит типы данных: float32 с нужным порядком слов, '
        + '32-битные счётчики, слова состояния, текст. Пустые области будут отмечены '
        + 'как незанятые.'),
      el('div', { class: 'form-grid' },
        field('Тип регистров', register),
        field('Начальный адрес', start, 'адрес протокола, с нуля'),
        field('Сколько регистров', count),
        field('Проходов чтения', samples, 'больше проходов — точнее типы')),
      el('div', { class: 'row', style: 'margin-top:6px' }, maxHint),
      el('div', { class: 'row', style: 'margin-top:14px' },
        el('button', { class: 'ghost', onclick: () => { state.step = 1; render(); } }, '← Назад'),
        button),
      out);
  }

  function scanSummary(scan) {
    const selected = state.candidates.filter((c) => c.selected).length;
    return el('div', {},
      el('div', { class: scan.candidates.length ? 'note ok' : 'note warn' },
        `Прочитано ${scan.count} регистров, распознано ${scan.candidates.length} тегов, `
        + `отмечено к добавлению ${selected}.`
        + (scan.unreadable.length ? ` Недоступно адресов: ${scan.unreadable.length}.` : '')),
      el('div', { class: 'row', style: 'margin-top:12px' },
        el('button', { class: 'primary', onclick: () => { state.step = 3; render(); } },
          'Дальше: посмотреть теги →')));
  }

  // ------------------------------------------------------------- шаг 3: теги
  function stepTags() {
    if (!state.candidates.length) {
      return el('div', { class: 'card' },
        emptyState('Ничего не найдено', 'Попробуйте другой тип регистров или диапазон адресов',
          el('button', { onclick: () => { state.step = 2; render(); } }, '← К сканированию')));
    }

    const tbody = el('tbody');
    const counter = el('span', { class: 'muted' });

    function refreshCounter() {
      const n = state.candidates.filter((c) => c.selected).length;
      counter.textContent = `отмечено ${n} из ${state.candidates.length}`;
    }

    state.candidates.forEach((c) => tbody.append(candidateRow(c, refreshCounter)));
    refreshCounter();

    return el('div', {},
      el('div', { class: 'card' },
        el('h2', {}, 'Предложенные теги'),
        el('p', { class: 'hint' },
          'Проверьте типы, задайте понятные имена и единицы измерения — они попадут '
          + 'в подписи графиков Grafana. Колонка «Значения» показывает, что реально '
          + 'прочиталось за проходы сканирования.'),
        el('div', { class: 'row' },
          el('button', { class: 'small', onclick: () => setAll(true) }, 'Отметить все'),
          el('button', { class: 'small', onclick: () => setAll(false) }, 'Снять все'),
          el('button', { class: 'small', onclick: () => setAll(null) }, 'Только изменяющиеся'),
          el('span', { class: 'spacer' }), counter),
        el('div', { class: 'table-wrap', style: 'margin-top:12px;max-height:56vh' },
          el('table', {},
            el('thead', {}, el('tr', {},
              el('th', { style: 'width:34px' }, ''),
              el('th', { class: 'num' }, 'Адрес'),
              el('th', {}, 'Имя тега'),
              el('th', {}, 'Тип'),
              el('th', {}, 'Порядок слов'),
              el('th', {}, 'Ед.'),
              el('th', {}, 'Описание'),
              el('th', {}, 'Значения'),
              el('th', {}, 'Распознано'))),
            tbody))),
      el('div', { class: 'row', style: 'margin-top:14px' },
        el('button', { class: 'ghost', onclick: () => { state.step = 2; render(); } },
          '← К сканированию'),
        el('button', { class: 'primary', onclick: () => { state.step = 4; render(); } },
          'Дальше: сохранить →')));

    function setAll(mode) {
      for (const c of state.candidates) {
        c.selected = mode === null ? !!c.changing : mode;
      }
      render();
    }
  }

  function candidateRow(c, onToggle) {
    const check = el('input', { type: 'checkbox', checked: c.selected,
      onchange: (e) => { c.selected = e.target.checked; onToggle(); } });
    const name = input({ value: c.name, class: 'mono',
      oninput: (e) => { c.name = e.target.value; } });
    const type = select(DATATYPES, c.datatype, { class: 'mono',
      onchange: (e) => { c.datatype = e.target.value; } });
    const order = select([['big', 'ABCD (big)'], ['little', 'CDAB (little)']],
      c.word_order || 'big', { onchange: (e) => { c.word_order = e.target.value; } });
    const unit = input({ value: c.unit || '', placeholder: '°C',
      oninput: (e) => { c.unit = e.target.value; } });
    const desc = input({ value: c.description || '', placeholder: 'Температура реактора',
      oninput: (e) => { c.description = e.target.value; } });

    // Адрес можно поправить руками: если у устройства в карте есть пустые
    // ячейки, автоопределение иногда сдвигает границу 32-битного значения.
    const address = input({ value: c.address, type: 'number', min: '0', class: 'mono',
      style: 'width:80px', oninput: (e) => { c.address = Number(e.target.value); } });

    const isWord = c.register === 'holding' || c.register === 'input';
    return el('tr', {},
      el('td', {}, check),
      el('td', { class: 'num' }, address),
      el('td', { style: 'min-width:170px' }, name),
      el('td', { style: 'min-width:110px' }, type),
      el('td', { style: 'min-width:130px' }, isWord ? order : el('span', { class: 'muted' }, '—')),
      el('td', { style: 'min-width:74px' }, unit),
      el('td', { style: 'min-width:180px' }, desc),
      el('td', { class: 'scan-preview' }, (c.preview || []).join('  →  ')),
      el('td', { style: 'min-width:260px' },
        el('div', { class: 'row tight', style: 'flex-wrap:nowrap' },
          el('span', { class: 'conf', title: `уверенность ${Math.round(c.confidence * 100)}%` },
            el('i', { style: `width:${Math.round(c.confidence * 100)}%` })),
          el('span', { class: 'muted', style: 'font-size:11.5px' }, c.reason))));
  }

  // -------------------------------------------------------- шаг 4: сохранение
  function stepSave() {
    const chosen = state.candidates.filter((c) => c.selected);
    const suggested = `plc-${state.conn.host.split('.').pop() || '01'}`;
    const deviceOptions = [['', '— создать новое устройство —'],
      ...state.devices.map((d) => [String(d.device_id), d.name])];
    const nodeOptions = [['', '— не привязывать —'],
      ...state.nodes.map((n) => [String(n.node_id), n.path || n.name])];

    const target = select(deviceOptions, '', { onchange: () => toggle() });
    const name = input({ value: suggested, class: 'mono' });
    const description = input({ placeholder: 'Линия розлива, узел дозирования' });
    const node = select(nodeOptions, '');
    const interval = input({ value: 1000, type: 'number', min: '50', class: 'mono' });
    const deadband = input({ value: 0, type: 'number', step: '0.1', class: 'mono' });
    const newBox = el('div', { class: 'form-grid' },
      field('Имя устройства', name, 'попадёт в подписи Grafana'),
      field('Описание', description),
      field('Узел цифрового двойника', node, 'машина или линия'));
    const out = el('div');
    const button = el('button', { class: 'primary', onclick: save }, 'Сохранить и запустить опрос');

    function toggle() { newBox.style.display = target.value ? 'none' : ''; }

    function payloadTags() {
      return chosen.map((c) => ({
        name: c.name,
        register: c.register,
        address: c.address,
        datatype: c.datatype,
        description: c.description || null,
        unit: c.unit || null,
        length: c.length ?? null,
        word_order: c.word_order || 'big',
        byte_order: c.byte_order || 'big',
        poll_interval_ms: Number(interval.value) || 1000,
        deadband: Number(deadband.value) || 0,
        role: c.role || 'value',
        node_id: node.value ? Number(node.value) : null,
        enabled: true,
      }));
    }

    async function save() {
      if (!chosen.length) { toast('Не отмечено ни одного тега', 'warn'); return; }
      button.disabled = true;
      out.replaceChildren(spinner('Сохраняем и перезапускаем опрос…'));
      try {
        let deviceId;
        if (target.value) {
          deviceId = Number(target.value);
          await post(`/devices/${deviceId}/tags`, { tags: payloadTags() });
        } else {
          const device = await post('/devices', {
            name: name.value.trim(),
            description: description.value.trim() || null,
            host: state.conn.host,
            port: state.conn.port,
            unit_id: state.conn.unit_id,
            timeout_s: state.conn.timeout_s,
            transport: 'tcp',
            node_id: node.value ? Number(node.value) : null,
            enabled: true,
          });
          deviceId = device.device_id;
          await put(`/devices/${deviceId}/tags`, { tags: payloadTags() });
        }
        toast(`Добавлено тегов: ${chosen.length}`, 'ok');
        await shell.refreshStatus();
        out.replaceChildren(el('div', { class: 'note ok' },
          el('div', {}, '✓ Устройство опрашивается, значения пишутся в базу.'),
          el('div', { class: 'row', style: 'margin-top:10px' },
            el('button', { class: 'primary',
              onclick: () => { location.hash = '#/monitor'; } }, 'Открыть мониторинг'),
            el('button', { onclick: async () => {
              try {
                await post('/grafana/dashboards');
                toast('Дашборды Grafana сгенерированы', 'ok');
                location.hash = '#/grafana';
              } catch (err) { fail(err); }
            } }, 'Сгенерировать дашборды Grafana'))));
      } catch (err) {
        out.replaceChildren(el('div', { class: 'note bad' }, err.message));
      } finally {
        button.disabled = false;
      }
    }

    return el('div', { class: 'card', style: 'max-width:900px' },
      el('h2', {}, 'Сохранение'),
      el('p', { class: 'hint' },
        `К добавлению отмечено тегов: ${chosen.length}. `
        + 'Опрос запустится сразу после сохранения — перезагружать приложение не нужно.'),
      el('div', { class: 'form-grid' },
        field('Куда добавить', target),
        field('Период опроса, мс', interval, 'общий для всех тегов'),
        field('Мёртвая зона', deadband, 'не писать изменения меньше этой величины')),
      el('div', { style: 'margin-top:12px' }, newBox),
      el('div', { class: 'row', style: 'margin-top:16px' },
        el('button', { class: 'ghost', onclick: () => { state.step = 3; render(); } },
          '← К тегам'),
        button),
      out);
  }

  return () => {};
}
