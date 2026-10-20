/**
 * Main Web Application Entrypoint for Bilingual Book Maker.
 */

import { api } from './api.js';
import { toast } from './components/toast.js';
import { bus, state, setAuthState, setTasks, setGeneralSettings, setProviders, setActiveTab } from './state.js';
import { initAuthModal } from './components/auth-modal.js';
import { initUploader } from './components/uploader.js';
import { initTaskBoard } from './components/task-board.js';
import { initLogModal } from './components/log-modal.js';
import { initSettings } from './components/settings.js';

let pollTimer = null;

async function checkAuth() {
  try {
    const authStatus = await api.auth.getStatus();
    setAuthState(authStatus);
    if (authStatus.protected_mode && !authStatus.authenticated) {
      bus.emit('auth:required');
    }
  } catch (err) {
    console.warn('Failed to check auth status:', err);
  }
}

async function fetchTasks() {
  try {
    const data = await api.tasks.list(state.tasks.filter || 'all');
    setTasks(data.items || [], data.total || 0);

    // Schedule next polling
    scheduleNextPoll(data.items || []);
  } catch (err) {
    console.warn('Failed to fetch tasks:', err);
    scheduleNextPoll([]);
  }
}

function scheduleNextPoll(items) {
  if (pollTimer) clearTimeout(pollTimer);

  // If there are running or pending tasks, poll more frequently (2.5s)
  const hasActiveTasks = items.some(t => t.status === 'running' || t.status === 'pending');
  const delay = hasActiveTasks ? 2500 : 8000;

  pollTimer = setTimeout(() => {
    fetchTasks();
  }, delay);
}

async function fetchSettings() {
  try {
    const [general, providers] = await Promise.all([
      api.settings.getGeneral(),
      api.settings.getProviders(),
    ]);
    if (general) setGeneralSettings(general);
    if (providers) setProviders(providers);
  } catch (err) {
    console.warn('Failed to fetch settings:', err);
  }
}

function initNavigation() {
  const tabButtons = document.querySelectorAll('.nav-item-btn[data-tab]');
  const panels = {
    tasks: document.getElementById('panel-tasks'),
    settings: document.getElementById('panel-settings'),
  };

  tabButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.getAttribute('data-tab');
      tabButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      Object.entries(panels).forEach(([key, panel]) => {
        if (!panel) return;
        if (key === tab) {
          panel.classList.add('active');
        } else {
          panel.classList.remove('active');
        }
      });

      setActiveTab(tab);

      if (tab === 'tasks') {
        fetchTasks();
      } else if (tab === 'settings') {
        fetchSettings();
      }
    });
  });
}

// Bootstrap application on DOM ready
document.addEventListener('DOMContentLoaded', async () => {
  // Initialize subcomponents
  const authModal = initAuthModal();
  const logModal = initLogModal();
  initUploader();
  initTaskBoard(logModal);
  initSettings();
  initNavigation();

  // Listen for manual refresh events
  bus.on('tasks:refreshRequired', () => fetchTasks());
  bus.on('settings:refreshProviders', async () => {
    try {
      const providers = await api.settings.getProviders();
      setProviders(providers);
    } catch (err) {
      console.warn(err);
    }
  });

  bus.on('auth:success', () => {
    fetchTasks();
    fetchSettings();
  });

  // Initial startup flow
  await checkAuth();
  await Promise.all([
    fetchTasks(),
    fetchSettings(),
  ]);
});
