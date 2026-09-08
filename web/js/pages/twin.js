// Цифровой двойник: дерево «площадка → участок → линия → машина»
// с живой сводкой по каждому узлу.

import {
  el, field, input, select, get, post, patch, del, toast, fail, spinner,
  emptyState, modal, confirmDialog, num, displayValue,
} from '../core.js';
import { live } from '../live.js';

export const title = 'Цифровой двойник';
export const subtitle = 'Структура производства и состояние машин';

const KINDS = [['site', 'Площадка'], ['area', 'Участок'], ['line', 'Линия'],
  ['machine', 'Машина']];
const KIND_LABEL = Object.fromEntries(KINDS);

export async function mount(view, shell) {
  shell.setTitle(title, subtitle);
  shell.setActions([
    el('button', { class: 'primary', onclick: () => openNode(null) }, '+ Добавить узел'),
    el('button', { onclick: reload }, 'Обновить'),
  ]);

  let tree = null;
  let unsubscribe = null;
  let timer = null;

  await reload();
  timer = setInterval(softRefresh, 5000);

  async function reload() {
    view.replaceChildren(spinner());
    try {
      tree = await get('/twin/overview');
      draw();
    } catch (err) {
      view.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  async function softRefresh() {
    try {
      tree = await get('/twin/overview');
      draw();
    } catch { /* тихо */ }
  }

  function draw() {
    if (!tree.tree.length && !tree.unassigned.tag_count) {
      view.replaceChildren(el('div', { class: 'card' }, emptyState(
        'Двойник ещё не построен',
        'Создайте линию и машины, затем привяжите к ним теги — '
        + 'приложение соберёт по этой структуре дашборды Grafana',
        el('button', { class: 'primary', onclick: () => openNode(null) },
          'Создать первый узел'))));
      return;
    }

    const kpis = summary(tree.tree);
    view.replaceChildren(
      el('div', { class: 'grid cols-4', style: 'margin-bottom:16px' },
        stat('Узлов', kpis.nodes),
        stat('Тегов привязано', kpis.tags),
        stat('Активных аварий', kpis.alarms, kpis.alarms ? 'bad' : 'ok'),
        stat('Без привязки', tree.unassigned.tag_count,
          tree.unassigned.tag_count ? 'warn' : 'ok')),
      el('div', { class: 'twin-tree' }, ...tree.tree.map((n) => nodeCard(n))),
      tree.unassigned.tag_count ? unassignedCard() : null);
  }

  function summary(nodes) {
    let count = 0;
    const walk = (list) => {
      for (const node of list) {
        count += 1;
        walk(node.children);
      }
    };
    walk(nodes);
    // tag_count и alarm_count у корня уже учитывают всех потомков,
    // поэтому суммируем только по корням — иначе счёт задваивается
    const tags = nodes.reduce((acc, n) => acc + n.tag_count, 0);
    const alarms = nodes.reduce((acc, n) => acc + n.alarm_count, 0);
    return { nodes: count, alarms, tags };
  }

  function stat(label, value, kind) {
    return el('div', { class: 'stat' },
      el('div', { class: 'label' }, label),
      el('div', { class: 'value', style: kind === 'bad' ? 'color:var(--red)'
        : kind === 'warn' ? 'color:var(--amber)' : '' }, num(value, 0)));
  }

  function nodeCard(node) {
    const head = el('div', { class: 'twin-node-head' },
      el('span', { class: 'twin-kind' }, KIND_LABEL[node.kind] || node.kind),
      el('b', {}, node.name),
      node.alarm_count
        ? el('span', { class: 'pill bad' }, `аварий: ${node.alarm_count}`)
        : null,
      el('span', { class: 'muted', style: 'font-size:12px' },
        `${node.tag_count} тег(ов)`),
      el('span', { class: 'spacer' }),
      el('button', { class: 'small ghost', onclick: () => openNode(node.node_id) },
        'Изменить'),
      el('button', { class: 'small ghost', onclick: () => openNode(null, node.node_id) },
        '+ Дочерний'),
      el('button', { class: 'small ghost', onclick: () => assignTags(node) }, 'Теги'),
      el('button', { class: 'small danger ghost', onclick: () => removeNode(node) }, '×'));

    const body = el('div', { class: 'twin-body' });
    if (node.description) {
      body.append(el('div', { class: 'muted', style: 'font-size:12.5px' }, node.description));
    }
    if (node.own_tags.length) body.append(kpiGrid(node.own_tags));
    if (node.children.length) {
      body.append(el('div', { class: 'twin-children' },
        ...node.children.map((child) => nodeCard(child))));
    }

    return el('div', { class: `twin-node state-${node.state}` }, head, body);
  }

  function kpiGrid(tags) {
    const grid = el('div', { class: 'kpi-grid' });
    for (const tag of tags) {
      const value = el('div', { class: 'v' });
      const cell = el('div', { class: 'kpi' },
        el('div', { class: 'k', title: tag.description || tag.name },
          tag.description || tag.name),
        value);
      const paint = () => {
        const item = live.value(tag.tag_id);
        const current = item
          ? { ...tag, value: item.value, value_text: item.text, quality: item.quality }
          : tag;
        value.textContent = displayValue(current)
          + (tag.unit && current.value !== null && tag.datatype !== 'bool'
            ? ` ${tag.unit}` : '');
        cell.classList.toggle('alarm-on',
          tag.role === 'alarm' && (current.value || 0) > 0);
        cell.classList.toggle('on',
          tag.datatype === 'bool' && tag.role !== 'alarm' && (current.value || 0) > 0);
        cell.classList.toggle('off',
          tag.datatype === 'bool' && !((current.value || 0) > 0));
      };
      paint();
      grid.append(cell);
      grid._painters = grid._painters || [];
      grid._painters.push(paint);
    }
    if (unsubscribe) unsubscribe();
    unsubscribe = live.subscribe(() => {
      for (const g of view.querySelectorAll('.kpi-grid')) {
        (g._painters || []).forEach((p) => p());
      }
    });
    return grid;
  }

  function unassignedCard() {
    return el('div', { class: 'card', style: 'margin-top:16px' },
      el('h2', {}, 'Теги без привязки'),
      el('p', { class: 'hint' },
        `${tree.unassigned.tag_count} тег(ов) ещё не отнесены ни к одной машине. `
        + 'Привяжите их — и они появятся в дашбордах линии.'),
      el('div', { class: 'kpi-grid' },
        ...tree.unassigned.tags.slice(0, 24).map((tag) => el('div', { class: 'kpi' },
          el('div', { class: 'k' }, tag.name),
          el('div', { class: 'v' }, displayValue(tag))))));
  }

  // ------------------------------------------------------------- операции
  async function openNode(nodeId, parentId = null) {
    let node = null;
    let nodes = [];
    try { nodes = await get('/twin/nodes'); } catch (err) { fail(err); return; }
    if (nodeId) node = nodes.find((n) => n.node_id === nodeId) || null;

    const f = {
      kind: select(KINDS, node ? node.kind : (parentId ? 'machine' : 'line')),
      name: input({ value: node ? node.name : '' }),
      description: input({ value: node ? node.description || '' : '' }),
      parent_id: select([['', '— верхний уровень —'],
        ...nodes.filter((n) => n.node_id !== nodeId).map((n) => [String(n.node_id),
          n.path || n.name])],
      node ? (node.parent_id ? String(node.parent_id) : '')
        : (parentId ? String(parentId) : '')),
    };

    let handle;
    const body = el('div', {},
      el('div', { class: 'form-grid' },
        field('Тип узла', f.kind),
        field('Название', f.name, 'например: Линия розлива №2'),
        field('Родительский узел', f.parent_id)),
      el('div', { style: 'margin-top:12px' }, field('Описание', f.description)),
      el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:16px' },
        el('button', { class: 'ghost', onclick: () => handle.close(null) }, 'Отмена'),
        el('button', { class: 'primary', onclick: save }, 'Сохранить')));

    handle = modal(node ? `Узел — ${node.name}` : 'Новый узел', body);

    async function save() {
      const payload = {
        kind: f.kind.value,
        name: f.name.value.trim(),
        description: f.description.value.trim() || null,
        parent_id: f.parent_id.value ? Number(f.parent_id.value) : null,
      };
      if (!payload.name) { toast('Укажите название', 'warn'); return; }
      try {
        if (node) await patch(`/twin/nodes/${node.node_id}`, payload);
        else await post('/twin/nodes', payload);
        handle.close(true);
        toast('Сохранено', 'ok');
        await reload();
      } catch (err) { fail(err); }
    }
  }

  async function removeNode(node) {
    const ok = await confirmDialog('Удалить узел?',
      `Узел «${node.name}» и все вложенные узлы будут удалены. `
      + 'Теги при этом сохранятся, но потеряют привязку.');
    if (!ok) return;
    try {
      await del(`/twin/nodes/${node.node_id}`);
      toast('Узел удалён', 'ok');
      await reload();
    } catch (err) { fail(err); }
  }

  async function assignTags(node) {
    const container = el('div', {}, spinner());
    const handle = modal(`Теги узла — ${node.name}`, container, { wide: true });
    try {
      const tags = await get('/tags');
      const checks = new Map();
      const tbody = el('tbody');
      for (const tag of tags) {
        const box = el('input', { type: 'checkbox', checked: tag.node_id === node.node_id });
        checks.set(tag.tag_id, box);
        tbody.append(el('tr', {},
          el('td', {}, box),
          el('td', { class: 'mono' }, tag.device),
          el('td', { class: 'mono' }, tag.name),
          el('td', { class: 'muted' }, tag.description || ''),
          el('td', { class: 'muted' }, tag.unit || ''),
          el('td', { class: 'muted' }, tag.node_id
            ? (tag.node_id === node.node_id ? 'этот узел' : `узел #${tag.node_id}`) : '—')));
      }
      container.replaceChildren(
        el('p', { class: 'hint' },
          'Отмеченные теги будут привязаны к этому узлу. Снятая отметка отвязывает тег.'),
        el('div', { class: 'table-wrap', style: 'max-height:58vh' },
          el('table', {}, el('thead', {}, el('tr', {},
            el('th', {}, ''), el('th', {}, 'Устройство'), el('th', {}, 'Тег'),
            el('th', {}, 'Описание'), el('th', {}, 'Ед.'), el('th', {}, 'Сейчас'))), tbody)),
        el('div', { class: 'row', style: 'justify-content:flex-end;margin-top:14px' },
          el('button', { class: 'ghost', onclick: () => handle.close(null) }, 'Отмена'),
          el('button', { class: 'primary', onclick: save }, 'Применить')));

      async function save() {
        const attach = [];
        const detach = [];
        for (const tag of tags) {
          const checked = checks.get(tag.tag_id).checked;
          if (checked && tag.node_id !== node.node_id) attach.push(tag.tag_id);
          if (!checked && tag.node_id === node.node_id) detach.push(tag.tag_id);
        }
        try {
          if (attach.length) await post(`/twin/nodes/${node.node_id}/tags`,
            { tag_ids: attach });
          if (detach.length) await post('/twin/nodes/0/tags', { tag_ids: detach });
          handle.close(true);
          toast(`Привязано: ${attach.length}, отвязано: ${detach.length}`, 'ok');
          await reload();
        } catch (err) { fail(err); }
      }
    } catch (err) {
      container.replaceChildren(el('div', { class: 'note bad' }, err.message));
    }
  }

  return () => {
    if (unsubscribe) unsubscribe();
    if (timer) clearInterval(timer);
  };
}
