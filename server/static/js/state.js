/**
 * Event-driven reactive state store for Bilingual Book Maker UI.
 */

class EventEmitter {
  constructor() {
    this.events = {};
  }

  on(event, listener) {
    if (!this.events[event]) {
      this.events[event] = [];
    }
    this.events[event].push(listener);
    return () => this.off(event, listener);
  }

  off(event, listener) {
    if (!this.events[event]) return;
    this.events[event] = this.events[event].filter(l => l !== listener);
  }

  emit(event, data) {
    if (!this.events[event]) return;
    this.events[event].forEach(listener => listener(data));
  }
}

export const bus = new EventEmitter();

export const state = {
  auth: {
    protected_mode: false,
    authenticated: true,
  },
  tasks: {
    items: [],
    total: 0,
    filter: 'all', // all, pending, running, completed, failed
    loading: false,
  },
  settings: {
    general: {
      default_target_lang: 'zh-hans',
      bilingual_style: 'default',
      concurrency_limit: 1,
      max_retries: 3,
    },
    providers: {
      active_provider: 'openai',
      providers: {},
    },
    loading: false,
  },
  activeTab: 'tasks', // 'tasks' or 'settings'
};

export function setAuthState(auth) {
  state.auth = { ...state.auth, ...auth };
  bus.emit('auth:changed', state.auth);
}

export function setTasks(items, total) {
  state.tasks.items = items;
  state.tasks.total = total;
  bus.emit('tasks:updated', state.tasks);
}

export function setTaskFilter(filter) {
  state.tasks.filter = filter;
  bus.emit('tasks:filterChanged', filter);
}

export function setGeneralSettings(general) {
  state.settings.general = { ...state.settings.general, ...general };
  bus.emit('settings:generalUpdated', state.settings.general);
}

export function setProviders(providersData) {
  state.settings.providers = providersData;
  bus.emit('settings:providersUpdated', state.settings.providers);
}

export function setActiveTab(tab) {
  state.activeTab = tab;
  bus.emit('tab:changed', tab);
}
