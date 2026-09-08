// Живой поток значений по WebSocket: одно соединение на всё приложение,
// автоматическое переподключение, подписчики получают пачки значений.

class LiveFeed {
  constructor() {
    this.socket = null;
    this.latest = new Map();      // tag_id -> {value, quality, ts, text}
    this.listeners = new Set();
    this.connected = false;
    this.retryDelay = 1000;
    this.closedByUs = false;
  }

  connect() {
    if (this.socket) return;
    this.closedByUs = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const socket = new WebSocket(`${proto}://${location.host}/api/live`);
    this.socket = socket;

    socket.onopen = () => {
      this.connected = true;
      this.retryDelay = 1000;
      this.emit({ type: 'state', connected: true });
    };

    socket.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.type === 'snapshot' || message.type === 'values') {
        for (const item of message.items || []) this.latest.set(item.tag_id, item);
        if (message.items && message.items.length) {
          this.emit({ type: message.type, items: message.items });
        }
      }
    };

    socket.onclose = () => {
      this.connected = false;
      this.socket = null;
      this.emit({ type: 'state', connected: false });
      if (!this.closedByUs) {
        setTimeout(() => this.connect(), this.retryDelay);
        this.retryDelay = Math.min(this.retryDelay * 1.7, 15000);
      }
    };

    socket.onerror = () => socket.close();
  }

  disconnect() {
    this.closedByUs = true;
    if (this.socket) this.socket.close();
    this.socket = null;
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(message) {
    for (const fn of [...this.listeners]) {
      try { fn(message); } catch (err) { console.error(err); }
    }
  }

  value(tagId) {
    return this.latest.get(tagId) || null;
  }
}

export const live = new LiveFeed();
