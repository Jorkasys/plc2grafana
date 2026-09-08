// Мастер первичной настройки: подключение к PostgreSQL и установка схемы.

import { el, field, input, post, toast, fail, spinner } from '../core.js';

export const title = 'Первичная настройка';
export const subtitle = 'Где приложение будет хранить историю тегов';

export async function mount(view, shell) {
  const form = {
    host: input({ value: '127.0.0.1', class: 'mono' }),
    port: input({ value: '5432', type: 'number', class: 'mono' }),
    name: input({ value: 'plc', class: 'mono' }),
    admin_user: input({ value: 'postgres', class: 'mono' }),
    admin_password: input({ type: 'password', placeholder: 'пароль суперпользователя' }),
    user: input({ value: 'plc', class: 'mono' }),
    retention_days: input({ value: '90', type: 'number', min: '0' }),
  };

  const result = el('div');
  const testBtn = el('button', { onclick: onTest }, 'Проверить подключение');
  const initBtn = el('button', { class: 'primary', onclick: onInit, disabled: true },
    'Создать базу и запустить');

  function values() {
    return {
      host: form.host.value.trim() || '127.0.0.1',
      port: Number(form.port.value) || 5432,
      name: form.name.value.trim() || 'plc',
      admin_user: form.admin_user.value.trim() || 'postgres',
      admin_password: form.admin_password.value,
      admin_db: 'postgres',
      user: form.user.value.trim() || 'plc',
      retention_days: Number(form.retention_days.value) || 0,
    };
  }

  async function onTest() {
    result.replaceChildren(spinner('Проверяем…'));
    testBtn.disabled = true;
    try {
      const data = await post('/setup/test-db', values());
      if (!data.ok) {
        initBtn.disabled = true;
        result.replaceChildren(el('div', { class: 'note bad' }, data.error));
        return;
      }
      initBtn.disabled = false;
      result.replaceChildren(el('div', { class: 'note ok' },
        el('div', {}, '✓ PostgreSQL отвечает'),
        el('div', { class: 'muted', style: 'margin-top:6px;font-size:12px' },
          String(data.version).split(',')[0]),
        el('ul', { style: 'margin:8px 0 0;padding-left:18px;font-size:12.5px' },
          el('li', {}, data.database_exists
            ? `база «${values().name}» уже существует — схема будет обновлена`
            : `база «${values().name}» будет создана`),
          el('li', {}, data.can_create_db
            ? 'прав достаточно для создания базы и ролей'
            : 'у пользователя нет прав CREATE DATABASE — укажите суперпользователя'),
          el('li', {}, data.timescaledb_available
            ? 'TimescaleDB доступен — включим гипертаблицу, сжатие и агрегаты'
            : 'TimescaleDB не установлен — работаем на обычном PostgreSQL'))));
    } catch (err) {
      initBtn.disabled = true;
      result.replaceChildren(el('div', { class: 'note bad' }, err.message));
    } finally {
      testBtn.disabled = false;
    }
  }

  async function onInit() {
    initBtn.disabled = true;
    testBtn.disabled = true;
    result.replaceChildren(spinner('Создаём базу, роли и схему…'));
    try {
      const data = await post('/setup/init-db', values());
      result.replaceChildren(el('div', { class: 'note ok' },
        el('div', {}, '✓ Готово. Можно подключать устройство.'),
        el('ul', { style: 'margin:8px 0 0;padding-left:18px;font-size:12.5px' },
          ...data.steps.map((s) => el('li', {}, s)))));
      toast('База данных готова', 'ok');
      await shell.refreshStatus();
      setTimeout(() => { location.hash = '#/connect'; }, 900);
    } catch (err) {
      result.replaceChildren(el('div', { class: 'note bad' }, err.message));
      fail(err);
    } finally {
      initBtn.disabled = false;
      testBtn.disabled = false;
    }
  }

  view.replaceChildren(
    el('div', { class: 'card', style: 'max-width:860px' },
      el('h2', {}, 'Подключение к PostgreSQL'),
      el('p', { class: 'hint' },
        'История значений хранится в PostgreSQL — из неё же читает Grafana. '
        + 'Укажите учётную запись с правом создавать базы и роли: приложение один раз '
        + 'создаст базу, роль для записи и отдельную роль только на чтение для Grafana. '
        + 'Если установлен TimescaleDB, он будет включён автоматически.'),
      el('div', { class: 'form-grid' },
        field('Адрес сервера', form.host),
        field('Порт', form.port),
        field('Имя базы', form.name, 'будет создана, если её нет')),
      el('div', { class: 'form-grid', style: 'margin-top:12px' },
        field('Администратор PostgreSQL', form.admin_user, 'обычно postgres'),
        field('Пароль администратора', form.admin_password),
        field('Пользователь приложения', form.user, 'создаётся автоматически'),
        field('Хранить сырые данные, дней', form.retention_days, '0 — не удалять')),
      el('div', { class: 'row', style: 'margin-top:16px' }, testBtn, initBtn),
      result));

  shell.setTitle(title, subtitle);
  shell.setActions([]);
  return () => {};
}
