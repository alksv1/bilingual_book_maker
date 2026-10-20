/**
 * Real-time SSE Terminal Log Modal Component.
 */

import { LogStreamer } from '../sse.js';
import { toast } from './toast.js';

export function initLogModal() {
  const modalEl = document.getElementById('log-modal');
  const closeBtn = document.getElementById('log-modal-close');
  const taskTitleEl = document.getElementById('log-task-title');
  const terminalBody = document.getElementById('log-terminal-body');
  const autoscrollCheckbox = document.getElementById('log-autoscroll');
  const clearLogsBtn = document.getElementById('log-clear-btn');
  const copyLogsBtn = document.getElementById('log-copy-btn');

  let currentStreamer = null;
  let logHistory = [];

  function open(task) {
    if (!modalEl) return;
    taskTitleEl.innerText = `${task.filename} (${task.id})`;
    terminalBody.innerHTML = '';
    logHistory = [];
    modalEl.classList.add('active');

    currentStreamer = new LogStreamer(task.id, {
      onOpen: () => {
        appendLine({ time: new Date().toLocaleTimeString(), line: '>>> [SSE] 已连接后台实时日志流...' });
      },
      onMessage: (msg) => {
        appendLine(msg);
      },
      onError: (err) => {
        appendLine({ time: new Date().toLocaleTimeString(), line: '>>> [SSE] 日志流连接已断开或任务已结束。' });
      },
    });

    currentStreamer.connect();
  }

  function close() {
    if (currentStreamer) {
      currentStreamer.close();
      currentStreamer = null;
    }
    if (modalEl) {
      modalEl.classList.remove('active');
    }
  }

  function appendLine(msg) {
    const timeStr = msg.time || '';
    const lineStr = msg.line || '';
    logHistory.push(`[${timeStr}] ${lineStr}`);

    const lineEl = document.createElement('div');
    lineEl.className = 'terminal-line';
    lineEl.innerHTML = `
      <span class="terminal-time">[${escapeHtml(timeStr)}]</span>
      <span class="terminal-text">${escapeHtml(lineStr)}</span>
    `;

    terminalBody.appendChild(lineEl);

    if (autoscrollCheckbox && autoscrollCheckbox.checked) {
      terminalBody.scrollTop = terminalBody.scrollHeight;
    }
  }

  if (closeBtn) closeBtn.addEventListener('click', close);

  if (clearLogsBtn) {
    clearLogsBtn.addEventListener('click', () => {
      terminalBody.innerHTML = '';
      logHistory = [];
    });
  }

  if (copyLogsBtn) {
    copyLogsBtn.addEventListener('click', () => {
      const fullText = logHistory.join('\n');
      navigator.clipboard.writeText(fullText)
        .then(() => toast.success('日志已复制到剪贴板'))
        .catch(() => toast.error('复制失败，请手动选择'));
    });
  }

  // Close when clicking modal backdrop
  if (modalEl) {
    modalEl.addEventListener('click', (e) => {
      if (e.target === modalEl) close();
    });
  }

  return { open, close };
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
