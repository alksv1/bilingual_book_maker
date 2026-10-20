/**
 * Unified API Client with 401 Interception and Request Queuing.
 */

import { bus, state, setAuthState } from './state.js';
import { toast } from './components/toast.js';

let isRefreshing = false;
let failedQueue = [];

function processQueue(error) {
  failedQueue.forEach(prom => {
    if (error) {
      prom.reject(error);
    } else {
      prom.resolve();
    }
  });
  failedQueue = [];
}

/**
 * Core fetch wrapper with 401 interception.
 */
async function request(url, options = {}) {
  options.credentials = 'include';
  options.headers = options.headers || {};

  try {
    const res = await fetch(url, options);

    if (res.status === 401) {
      // Avoid looping if auth/login itself failed with 401
      if (url.includes('/api/auth/login')) {
        const errorData = await res.json().catch(() => ({}));
        throw new Error(errorData.detail || '密码错误');
      }

      // Mark session as unauthenticated
      setAuthState({ authenticated: false });
      bus.emit('auth:required');

      // Enqueue the request to be retried after login
      return new Promise((resolve, reject) => {
        failedQueue.push({
          resolve: () => resolve(request(url, options)),
          reject: err => reject(err),
        });
      });
    }

    if (!res.ok) {
      const errJson = await res.json().catch(() => null);
      const message = errJson?.detail || `HTTP Error ${res.status}: ${res.statusText}`;
      const error = new Error(message);
      error.status = res.status;
      error.code = errJson?.code;
      throw error;
    }

    if (res.status === 204) {
      return null;
    }

    const contentType = res.headers.get('content-type') || '';
    if (contentType.includes('application/json')) {
      return await res.json();
    }
    return res;
  } catch (err) {
    throw err;
  }
}

export const api = {
  // Auth API
  auth: {
    async getStatus() {
      return request('/api/auth/status');
    },
    async login(password) {
      const result = await request('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      setAuthState({ authenticated: true });
      processQueue(null);
      return result;
    },
    async logout() {
      await request('/api/auth/logout', { method: 'POST' });
      setAuthState({ authenticated: false });
      bus.emit('auth:required');
    },
  },

  // Tasks API
  tasks: {
    async list(statusFilter = 'all', limit = 50, offset = 0) {
      const params = new URLSearchParams({
        status: statusFilter,
        limit: String(limit),
        offset: String(offset),
      });
      return request(`/api/tasks?${params.toString()}`);
    },

    async create(files, { targetLang, provider, prompt } = {}) {
      const formData = new FormData();
      for (const file of files) {
        formData.append('files', file);
      }
      if (targetLang) formData.append('target_lang', targetLang);
      if (provider) formData.append('provider', provider);
      if (prompt) formData.append('prompt', prompt);

      return request('/api/tasks', {
        method: 'POST',
        body: formData,
      });
    },

    async cancel(taskId) {
      return request(`/api/tasks/${taskId}/cancel`, {
        method: 'POST',
      });
    },

    async delete(taskId, deleteFile = true) {
      return request(`/api/tasks/${taskId}?delete_file=${deleteFile}`, {
        method: 'DELETE',
      });
    },

    getDownloadUrl(taskId) {
      return `/api/tasks/${taskId}/download`;
    },

    getLogStreamUrl(taskId) {
      return `/api/tasks/${taskId}/logs/stream`;
    },
  },

  // Settings API
  settings: {
    async getGeneral() {
      return request('/api/settings/general');
    },

    async updateGeneral(payload) {
      return request('/api/settings/general', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    },

    async getProviders() {
      return request('/api/settings/providers');
    },

    async updateProvider(name, payload) {
      return request(`/api/settings/providers/${name}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    },

    async testProvider(name, payload) {
      return request(`/api/settings/providers/${name}/test`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    },
  },
};
