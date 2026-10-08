// Управление локальной Grafana: установка, запуск, генерация дашбордов
// и просмотр их прямо здесь, во встроенном окне.

import {
  el, select, get, post, toast, fail, spinner, emptyState, bytes,
} from '../core.js';

export const title = 'Grafana';
export const subtitle = 'Установка, запуск и автоматические дашборды';

export async function mount(view, shell) {
  shell.setTitle(title, subtitle);

  let status = null;
  let dashboards = [];
  let current = localStorage.getItem('plc.dashboard') || '';
  let poll = null;

  shell.setActions([
    el('button', { onclick: refresh }, 'Обновить'),
    el('button', { class: 'primary', onclick: generate }, 'Сгенерировать дашборды'),
  ]);

  view.replaceChildren(spinner());
  await refresh();
  poll = setInterval(quietRefresh, 4000);

  // Адрес Grafana для браузера. Приложение может работать на виртуалке, а
  // открываться с другого компьютера: «127.0.0.1» там означал бы localhost
  // самого пользователя, поэтому берём хост, по которому открыт интерфейс.
  function grafanaBase() {
    if (status.browser_url) return status.browser_url;
    return `${location.protocol}//${location.hostname}:${status.port}`;
  }

  // Встроенной Grafana мы управляем сами, внешнюю (docker) — только видим
  function grafanaUp() {
    return status.running || (!status.managed && status.healthy);
  }

  async function refresh() {
    try {
      status = await get('/grafana/status');
      dashboards = grafanaUp() ? (await get('/grafana/dashboards')).dashboards : [];
      draw();
    } catch (err) {
      view.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  async function quietRefresh() {
    try {
      const next = await get('/grafana/status');
      const changed = !status
        || next.running !== status.running
        || next.healthy !== status.healthy
        || next.installed !== status.installed
        || next.download.active !== status.download.active
        || next.download.percent !== status.download.percent;
      status = next;
      if (changed) {
        if (grafanaUp() && !dashboards.length) {
          dashboards = (await get('/grafana/dashboards')).dashboards;
        }
        draw();
      }
    } catch { /* тихо */ }
  }

  function draw() {
    const parts = [statusCard()];
    if (status.download.active) parts.push(downloadCard());
    if (grafanaUp()) parts.push(viewerCard());
    else parts.push(helpCard());
    view.replaceChildren(...parts);
  }

  function statusCard() {
    const chips = status.managed ? [
      ['Установлена', status.installed ? 'да' : 'нет', status.installed ? 'ok' : 'warn'],
      ['Процесс', status.running ? `запущен (pid ${status.pid})` : 'остановлен',
        status.running ? 'ok' : 'warn'],
      ['Версия', status.version, ''],
      ['Адрес', grafanaBase(), ''],
    ] : [
      ['Режим', 'внешняя Grafana', ''],
      ['Связь', status.healthy ? 'есть' : 'нет', status.healthy ? 'ok' : 'bad'],
      ['Адрес', grafanaBase(), ''],
    ];

    const buttons = [];
    if (!status.managed) {
      if (status.healthy) {
        buttons.push(el('a', { class: 'btn', href: grafanaBase(), target: '_blank',
          rel: 'noopener' }, 'Открыть в новой вкладке ↗'));
      }
    } else if (!status.installed) {
      buttons.push(el('button', { class: 'primary', onclick: install },
        'Установить Grafana'));
    } else if (!status.running) {
      buttons.push(el('button', { class: 'primary', onclick: start }, 'Запустить'));
    } else {
      buttons.push(el('button', { onclick: stop }, 'Остановить'));
      // В демо настоящей Grafana нет — ссылка на localhost:3000 вводила бы в
      // заблуждение
      if (!window.PLC_DEMO) {
        buttons.push(el('a', { class: 'btn', href: grafanaBase(), target: '_blank',
          rel: 'noopener' }, 'Открыть в новой вкладке ↗'));
      }
    }
    if (status.managed && status.installed) {
      buttons.push(el('button', { class: 'ghost small', onclick: () => install(true) },
        'Переустановить'));
    }

    return el('div', { class: 'card' },
      el('div', { class: 'row' },
        el('h2', { style: 'margin:0' },
          status.managed ? 'Локальная Grafana' : 'Внешняя Grafana'),
        el('span', { class: 'spacer' }),
        ...buttons),
      el('p', { class: 'hint', style: 'margin-top:10px' }, status.managed
        ? 'Приложение скачивает portable-сборку Grafana в папку проекта и запускает её '
          + 'как дочерний процесс — в систему ничего не устанавливается. Датасорс '
          + 'PostgreSQL и дашборды настраиваются автоматически, вход не требуется.'
        : 'Grafana работает отдельно (например, соседним docker-контейнером). '
          + 'Приложение готовит для неё датасорс и дашборды через provisioning.'),
      el('div', { class: 'row' },
        ...chips.map(([k, v, kind]) => el('span', { class: `pill ${kind}` }, `${k}: ${v}`))),
      status.log_tail && status.log_tail.length && !status.running
        ? el('details', { style: 'margin-top:12px' },
          el('summary', { class: 'muted', style: 'cursor:pointer;font-size:12.5px' },
            'Журнал Grafana'),
          el('pre', { class: 'mono', style: 'font-size:11px;max-height:220px;overflow:auto;'
            + 'background:var(--bg);padding:10px;border-radius:6px;margin-top:8px' },
          status.log_tail.join('\n')))
        : null,
      status.download.error
        ? el('div', { class: 'note bad' }, `Ошибка установки: ${status.download.error}`)
        : null);
  }

  function downloadCard() {
    const d = status.download;
    return el('div', { class: 'card' },
      el('h3', {}, `Установка: ${d.stage}`),
      el('div', { class: 'progress' }, el('div', { style: `width:${d.percent}%` })),
      el('div', { class: 'muted', style: 'margin-top:8px;font-size:12.5px' },
        d.total ? `${bytes(d.downloaded)} из ${bytes(d.total)} (${d.percent}%)`
          : bytes(d.downloaded)));
  }

  function helpCard() {
    if (!status.managed) {
      return el('div', { class: 'card' }, emptyState(
        'Grafana не отвечает',
        `Приложение не может достучаться до ${status.url}. Проверьте, что контейнер `
        + 'или служба Grafana запущены.'));
    }
    return el('div', { class: 'card' }, emptyState(
      status.installed ? 'Grafana не запущена' : 'Grafana ещё не установлена',
      status.installed
        ? 'Нажмите «Запустить» — приложение поднимет её на порту '
          + `${status.port} и настроит датасорс само`
        : 'Понадобится интернет только на время скачивания (около 250 МБ). '
          + 'Дальше всё работает локально.'));
  }

  function viewerCard() {
    if (!dashboards.length) {
      return el('div', { class: 'card' }, emptyState(
        'Дашбордов пока нет',
        'Нажмите «Сгенерировать дашборды» — приложение соберёт их по вашим тегам',
        el('button', { class: 'primary', onclick: generate }, 'Сгенерировать дашборды')));
    }

    if (!dashboards.some((d) => d.uid === current)) current = dashboards[0].uid;
    const chosen = dashboards.find((d) => d.uid === current);
    const picker = select(dashboards.map((d) => [d.uid, d.title]), current, {
      onchange: (e) => {
        current = e.target.value;
        localStorage.setItem('plc.dashboard', current);
        draw();
      },
    });

    // В демо на GitHub Pages настоящей Grafana нет — показываем снимок
    // того же дашборда, чтобы было видно, что именно генерирует приложение.
    const isImage = !chosen.embed_path && /\.(png|jpe?g|webp)$/i.test(chosen.embed_url);
    const frame = isImage
      ? el('img', { src: chosen.embed_url, alt: chosen.title,
        style: 'width:100%;margin-top:12px;border:1px solid var(--line);'
             + 'border-radius:var(--radius)' })
      : el('iframe', { class: 'gf-frame', src: grafanaBase() + chosen.embed_path,
        style: 'margin-top:12px', title: chosen.title });

    return el('div', { class: 'card' },
      el('div', { class: 'row' },
        el('h3', { style: 'margin:0' }, 'Дашборд'),
        picker,
        el('span', { class: 'spacer' }),
        isImage ? null
          : el('a', { class: 'btn small', href: grafanaBase() + chosen.path,
            target: '_blank',
            rel: 'noopener' }, 'Открыть отдельно ↗')),
      isImage
        ? el('div', { class: 'note warn' },
          'Демо-режим: снимок дашборда, который приложение создаёт само. '
          + 'Запустите проект локально — Grafana поднимется и покажет живые данные.')
        : null,
      frame);
  }

  // -------------------------------------------------------------- действия
  async function install(force = false) {
    try {
      await post(`/grafana/install${force === true ? '?force=true' : ''}`);
      toast('Скачивание началось', 'ok');
      await quietRefresh();
    } catch (err) { fail(err); }
  }

  async function start() {
    const note = toast('Запускаем Grafana…', 'info', 60000);
    try {
      await post('/grafana/start');
      note.remove();
      toast('Grafana запущена', 'ok');
      await refresh();
    } catch (err) {
      note.remove();
      fail(err);
      await refresh();
    }
  }

  async function stop() {
    try {
      await post('/grafana/stop');
      toast('Grafana остановлена', 'ok');
      await refresh();
    } catch (err) { fail(err); }
  }

  async function generate() {
    try {
      const result = await post('/grafana/dashboards');
      toast(`Сгенерировано дашбордов: ${result.dashboards.length}`, 'ok');
      // Провайдер Grafana перечитывает папку раз в 10 секунд
      setTimeout(refresh, 2500);
    } catch (err) { fail(err); }
  }

  return () => { if (poll) clearInterval(poll); };
}
