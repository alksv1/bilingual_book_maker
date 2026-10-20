/**
 * Task Board and Monitoring Component.
 */

import { api } from '../api.js';
import { toast } from './toast.js';
import { bus, state, setTaskFilter } from '../state.js';

export function initTaskBoard(logModal) {
  const taskListEl = document.getElementById('task-list');
  const emptyStateEl = document.getElementById('tasks-empty');
  const taskCountBadge = document.getElementById('tasks-count-badge');
  const filterTabs = document.querySelectorAll('.filter-tab');

  function getStatusLabel(status) {
    const map = {
      pending: '排队中',
      running: '翻译中',
      completed: '已完成',
      failed: '失败',
      cancelled: '已取消',
    };
    return map[status] || status;
  }

  function renderTasks() {
    const items = state.tasks.items || [];
    const filter = state.tasks.filter;

    const filtered = items.filter(task => {
      if (filter === 'all') return true;
      return task.status === filter;
    });

    if (taskCountBadge) {
      taskCountBadge.innerText = `${filtered.length} / ${items.length}`;
    }

    if (filtered.length === 0) {
      taskListEl.innerHTML = '';
      if (emptyStateEl) emptyStateEl.style.display = 'block';
      return;
    }

    if (emptyStateEl) emptyStateEl.style.display = 'none';

    taskListEl.innerHTML = filtered
      .map(task => {
        const percent = task.progress_total > 0
          ? Math.min(100, Math.round((task.progress_current / task.progress_total) * 100))
          : (task.status === 'completed' ? 100 : 0);

        const statusClass = task.status || 'pending';
        const isRunning = task.status === 'running';
        const isCompleted = task.status === 'completed';
        const isFailed = task.status === 'failed';
        const canCancel = task.status === 'pending' || task.status === 'running';

        return `
          <div class="task-card" data-id="${task.id}">
            <div class="task-header">
              <div class="task-title-group">
                <span class="file-type-badge">${escapeHtml(task.file_type || 'epub')}</span>
                <span class="task-filename" title="${task.filename}">${escapeHtml(task.filename)}</span>
                <span class="status-badge ${statusClass}">
                  ${isRunning ? '<span class="pulse-dot"></span>' : ''}
                  ${getStatusLabel(task.status)}
                </span>
              </div>
              <div class="task-actions">
                <button class="btn btn-secondary btn-sm btn-view-logs" data-id="${task.id}" title="查看实时日志">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="4 17 10 11 4 5"></polyline>
                    <line x1="12" y1="19" x2="20" y2="19"></line>
                  </svg>
                  日志
                </button>
                ${isCompleted ? `
                  <a href="${api.tasks.getDownloadUrl(task.id)}" class="btn btn-primary btn-sm" download title="下载双语电子书">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                      <polyline points="7 10 12 15 17 10"></polyline>
                      <line x1="12" y1="15" x2="12" y2="3"></line>
                    </svg>
                    下载
                  </a>
                ` : ''}
                ${canCancel ? `
                  <button class="btn btn-secondary btn-sm btn-cancel-task" data-id="${task.id}" title="取消当前任务">
                    取消
                  </button>
                ` : ''}
                <button class="btn btn-danger btn-sm btn-delete-task" data-id="${task.id}" title="删除任务记录">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
                  </svg>
                </button>
              </div>
            </div>

            <!-- Progress Bar -->
            <div class="progress-container">
              <div class="progress-header">
                <span>进度: ${percent}%</span>
                <span>${task.progress_current || 0} / ${task.progress_total || 0} 段落</span>
              </div>
              <div class="progress-track">
                <div class="progress-fill ${statusClass}" style="width: ${percent}%;"></div>
              </div>
            </div>

            ${task.error_message ? `
              <div class="task-error">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;">
                  <circle cx="12" cy="12" r="10"></circle>
                  <line x1="12" y1="8" x2="12" y2="12"></line>
                  <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                <span>${escapeHtml(task.error_message)}</span>
              </div>
            ` : ''}

            <div class="task-meta">
              <div class="task-meta-item">
                <span>语言:</span>
                <strong style="color:var(--text-secondary);">${escapeHtml(task.target_language || 'zh-hans')}</strong>
              </div>
              <div class="task-meta-item">
                <span>模型商:</span>
                <strong style="color:var(--text-secondary);">${escapeHtml(task.model_provider || '默认')}</strong>
              </div>
              <div class="task-meta-item">
                <span>创建时间:</span>
                <span>${task.created_at ? new Date(task.created_at).toLocaleString() : '-'}</span>
              </div>
              <div class="task-meta-item" style="margin-left:auto;">
                <span>ID:</span>
                <code>${task.id}</code>
              </div>
            </div>
          </div>
        `;
      })
      .join('');

    // Bind action listeners
    taskListEl.querySelectorAll('.btn-view-logs').forEach(btn => {
      btn.addEventListener('click', () => {
        const taskId = btn.getAttribute('data-id');
        const task = items.find(t => t.id === taskId);
        if (task && logModal) logModal.open(task);
      });
    });

    taskListEl.querySelectorAll('.btn-cancel-task').forEach(btn => {
      btn.addEventListener('click', async () => {
        const taskId = btn.getAttribute('data-id');
        if (confirm(`确定要取消任务 ${taskId} 吗？`)) {
          try {
            await api.tasks.cancel(taskId);
            toast.info(`任务 ${taskId} 已取消`);
            bus.emit('tasks:refreshRequired');
          } catch (err) {
            toast.error('取消任务失败: ' + err.message);
          }
        }
      });
    });

    taskListEl.querySelectorAll('.btn-delete-task').forEach(btn => {
      btn.addEventListener('click', async () => {
        const taskId = btn.getAttribute('data-id');
        if (confirm(`确定要删除任务 ${taskId} 及其产物吗？`)) {
          try {
            await api.tasks.delete(taskId, true);
            toast.success(`任务 ${taskId} 已删除`);
            bus.emit('tasks:refreshRequired');
          } catch (err) {
            toast.error('删除任务失败: ' + err.message);
          }
        }
      });
    });
  }

  // Filter tabs listeners
  filterTabs.forEach(tab => {
    tab.addEventListener('click', () => {
      filterTabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const filter = tab.getAttribute('data-filter') || 'all';
      setTaskFilter(filter);
      renderTasks();
    });
  });

  bus.on('tasks:updated', renderTasks);
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
