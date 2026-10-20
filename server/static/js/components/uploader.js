/**
 * Drag and Drop Batch Uploader Component.
 */

import { api } from '../api.js';
import { toast } from './toast.js';
import { bus, state } from '../state.js';

export function initUploader() {
  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('file-input');
  const queuedFilesList = document.getElementById('queued-files');
  const uploadOptions = document.getElementById('upload-options');
  const startUploadBtn = document.getElementById('btn-start-upload');
  const clearQueueBtn = document.getElementById('btn-clear-queue');

  const uploadTargetLang = document.getElementById('upload-target-lang');
  const uploadProvider = document.getElementById('upload-provider');
  const uploadPrompt = document.getElementById('upload-prompt');

  let queuedFiles = [];

  function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
  }

  function getFileExt(filename) {
    const parts = filename.split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : '';
  }

  function renderQueue() {
    if (queuedFiles.length === 0) {
      queuedFilesList.innerHTML = '';
      uploadOptions.style.display = 'none';
      return;
    }

    uploadOptions.style.display = 'block';
    queuedFilesList.innerHTML = queuedFiles
      .map(
        (file, idx) => `
        <div class="queued-file-item">
          <div class="file-info">
            <span class="file-type-badge">${getFileExt(file.name)}</span>
            <span class="file-name" title="${file.name}">${escapeHtml(file.name)}</span>
            <span class="file-size">(${formatBytes(file.size)})</span>
          </div>
          <button class="btn btn-secondary btn-sm remove-file-btn" data-index="${idx}" style="padding: 2px 8px;">
            &times;
          </button>
        </div>
      `
      )
      .join('');

    queuedFilesList.querySelectorAll('.remove-file-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const idx = parseInt(btn.getAttribute('data-index'), 10);
        queuedFiles.splice(idx, 1);
        renderQueue();
      });
    });
  }

  function addFiles(files) {
    const validExts = ['epub', 'pdf', 'txt', 'srt'];
    let rejectedCount = 0;

    for (const file of files) {
      const ext = getFileExt(file.name);
      if (validExts.includes(ext)) {
        queuedFiles.push(file);
      } else {
        rejectedCount++;
      }
    }

    if (rejectedCount > 0) {
      toast.error(`已过滤 ${rejectedCount} 个不支持的文件。仅支持 .epub, .pdf, .txt, .srt 格式。`);
    }

    renderQueue();
  }

  // Populate provider selector from settings
  function updateProviderOptions() {
    if (!uploadProvider) return;
    const currentVal = uploadProvider.value;
    uploadProvider.innerHTML = '<option value="">跟随全局配置 (默认)</option>';

    const providers = state.settings.providers.providers || {};
    Object.keys(providers).forEach(pName => {
      const opt = document.createElement('option');
      opt.value = pName;
      opt.textContent = `${pName} ${pName === state.settings.providers.active_provider ? '(当前活跃)' : ''}`;
      uploadProvider.appendChild(opt);
    });
    if (currentVal) uploadProvider.value = currentVal;
  }

  bus.on('settings:providersUpdated', updateProviderOptions);

  if (dropzone && fileInput) {
    dropzone.addEventListener('click', () => fileInput.click());

    dropzone.addEventListener('dragover', (e) => {
      e.preventDefault();
      dropzone.classList.add('dragover');
    });

    dropzone.addEventListener('dragleave', () => {
      dropzone.classList.remove('dragover');
    });

    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        addFiles(e.dataTransfer.files);
      }
    });

    fileInput.addEventListener('change', (e) => {
      if (e.target.files && e.target.files.length > 0) {
        addFiles(e.target.files);
        fileInput.value = ''; // Reset input
      }
    });
  }

  if (clearQueueBtn) {
    clearQueueBtn.addEventListener('click', () => {
      queuedFiles = [];
      renderQueue();
    });
  }

  if (startUploadBtn) {
    startUploadBtn.addEventListener('click', async () => {
      if (queuedFiles.length === 0) return;

      startUploadBtn.disabled = true;
      startUploadBtn.innerText = '正在上传...';

      try {
        const options = {
          targetLang: uploadTargetLang ? uploadTargetLang.value : null,
          provider: uploadProvider ? uploadProvider.value : null,
          prompt: uploadPrompt ? uploadPrompt.value.trim() : null,
        };

        const res = await api.tasks.create(queuedFiles, options);
        toast.success(`成功创建 ${res.length || queuedFiles.length} 个书籍翻译任务！`);
        queuedFiles = [];
        renderQueue();
        bus.emit('tasks:refreshRequired');
      } catch (err) {
        toast.error('上传创建任务失败: ' + err.message);
      } finally {
        startUploadBtn.disabled = false;
        startUploadBtn.innerText = '立即开始翻译';
      }
    });
  }
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
