/**
 * Settings & Provider Configuration Component.
 */

import { api } from '../api.js';
import { toast } from './toast.js';
import { bus, state, setGeneralSettings, setProviders } from '../state.js';

export function initSettings() {
  const generalForm = document.getElementById('general-settings-form');
  const targetLangSelect = document.getElementById('setting-target-lang');
  const bilingualStyleSelect = document.getElementById('setting-bilingual-style');
  const concurrencyInput = document.getElementById('setting-concurrency');
  const maxRetriesInput = document.getElementById('setting-retries');

  const providerListEl = document.getElementById('providers-list');
  const providerModalEl = document.getElementById('provider-modal');
  const providerModalClose = document.getElementById('provider-modal-close');
  const providerForm = document.getElementById('provider-form');
  const providerNameInput = document.getElementById('provider-name');
  const providerBaseUrlInput = document.getElementById('provider-base-url');
  const providerApiKeyInput = document.getElementById('provider-api-key');
  const providerModelsInput = document.getElementById('provider-models');
  const providerSetActiveCheck = document.getElementById('provider-set-active');

  const testProviderBtn = document.getElementById('btn-test-provider');
  const testResultEl = document.getElementById('provider-test-result');

  let editingProviderName = null;

  function populateGeneral() {
    const gen = state.settings.general || {};
    if (targetLangSelect) targetLangSelect.value = gen.default_target_lang || 'zh-hans';
    if (bilingualStyleSelect) bilingualStyleSelect.value = gen.bilingual_style || 'default';
    if (concurrencyInput) concurrencyInput.value = gen.concurrency_limit || 1;
    if (maxRetriesInput) maxRetriesInput.value = gen.max_retries || 3;
  }

  function renderProviders() {
    const data = state.settings.providers || {};
    const active = data.active_provider;
    const providers = data.providers || {};

    if (!providerListEl) return;

    if (Object.keys(providers).length === 0) {
      providerListEl.innerHTML = '<div style="color:var(--text-muted); padding:1rem 0;">暂无可用的服务商配置</div>';
      return;
    }

    providerListEl.innerHTML = Object.entries(providers)
      .map(([name, info]) => {
        const isActive = name === active;
        const models = (info.default_models || []).join(', ') || '未配置模型';
        const keyStatus = info.has_api_key
          ? `<span style="color:#10b981; font-size:0.75rem;">已配置 Key (${info.masked_api_key || '****'})</span>`
          : `<span style="color:#f59e0b; font-size:0.75rem;">未配置 API Key</span>`;

        return `
          <div class="provider-card ${isActive ? 'active' : ''}">
            <div class="provider-header">
              <div class="provider-name-badge">
                <span>${escapeHtml(name)}</span>
                ${isActive ? '<span class="active-pill">当前默认</span>' : ''}
              </div>
              <div style="display:flex; gap:0.5rem;">
                <button class="btn btn-secondary btn-sm btn-edit-provider" data-name="${name}">
                  编辑配置
                </button>
              </div>
            </div>
            <div style="font-size:0.8125rem; color:var(--text-secondary); display:flex; flex-direction:column; gap:0.25rem;">
              <div><strong>风格:</strong> ${escapeHtml(info.api_style || name)}</div>
              <div><strong>Base URL:</strong> ${escapeHtml(info.base_url || '默认官方端点')}</div>
              <div><strong>可用模型:</strong> <code>${escapeHtml(models)}</code></div>
              <div><strong>密钥状态:</strong> ${keyStatus}</div>
            </div>
          </div>
        `;
      })
      .join('');

    providerListEl.querySelectorAll('.btn-edit-provider').forEach(btn => {
      btn.addEventListener('click', () => {
        const name = btn.getAttribute('data-name');
        openProviderModal(name);
      });
    });
  }

  function openProviderModal(name) {
    editingProviderName = name;
    const data = state.settings.providers.providers || {};
    const info = data[name] || {};
    const isActive = name === state.settings.providers.active_provider;

    providerNameInput.value = name;
    providerBaseUrlInput.value = info.base_url || '';
    providerApiKeyInput.value = '';
    providerApiKeyInput.placeholder = info.has_api_key
      ? `已保存密钥: ${info.masked_api_key || '****'} (留空则不修改)`
      : '请输入 API Key';
    providerModelsInput.value = (info.default_models || []).join(', ');
    providerSetActiveCheck.checked = isActive;
    testResultEl.style.display = 'none';

    providerModalEl.classList.add('active');
  }

  function closeProviderModal() {
    editingProviderName = null;
    providerModalEl.classList.remove('active');
  }

  if (providerModalClose) {
    providerModalClose.addEventListener('click', closeProviderModal);
  }

  if (generalForm) {
    generalForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = {
        default_target_lang: targetLangSelect.value,
        bilingual_style: bilingualStyleSelect.value,
        concurrency_limit: parseInt(concurrencyInput.value, 10) || 1,
        max_retries: parseInt(maxRetriesInput.value, 10) || 3,
      };

      try {
        const updated = await api.settings.updateGeneral(payload);
        setGeneralSettings(updated);
        toast.success('全局翻译配置已保存');
      } catch (err) {
        toast.error('保存全局配置失败: ' + err.message);
      }
    });
  }

  if (providerForm) {
    providerForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!editingProviderName) return;

      const modelsStr = providerModelsInput.value.trim();
      const modelsList = modelsStr ? modelsStr.split(',').map(m => m.trim()).filter(Boolean) : [];

      const payload = {
        base_url: providerBaseUrlInput.value.trim() || null,
        api_key: providerApiKeyInput.value.trim() || undefined,
        default_models: modelsList.length > 0 ? modelsList : undefined,
        set_as_active: providerSetActiveCheck.checked,
      };

      try {
        await api.settings.updateProvider(editingProviderName, payload);
        toast.success(`服务商 ${editingProviderName} 配置已更新`);
        closeProviderModal();
        bus.emit('settings:refreshProviders');
      } catch (err) {
        toast.error('更新服务商失败: ' + err.message);
      }
    });
  }

  if (testProviderBtn) {
    testProviderBtn.addEventListener('click', async () => {
      if (!editingProviderName) return;

      testProviderBtn.disabled = true;
      testProviderBtn.innerText = '测试中...';
      testResultEl.style.display = 'block';
      testResultEl.style.color = 'var(--text-muted)';
      testResultEl.innerText = '正在发起连通性 Ping 测试...';

      const modelsStr = providerModelsInput.value.trim();
      const firstModel = modelsStr ? modelsStr.split(',')[0].trim() : null;

      const payload = {
        base_url: providerBaseUrlInput.value.trim() || null,
        api_key: providerApiKeyInput.value.trim() || null,
        model: firstModel,
      };

      try {
        const res = await api.settings.testProvider(editingProviderName, payload);
        if (res.success) {
          testResultEl.style.color = '#10b981';
          testResultEl.innerText = `✓ ${res.message || '测试通过，连接正常'}`;
        } else {
          testResultEl.style.color = '#ef4444';
          testResultEl.innerText = `✗ ${res.message || '连接测试失败'}`;
        }
      } catch (err) {
        testResultEl.style.color = '#ef4444';
        testResultEl.innerText = `✗ 测试请求异常: ${err.message}`;
      } finally {
        testProviderBtn.disabled = false;
        testProviderBtn.innerText = '测试连通性 (Ping)';
      }
    });
  }

  bus.on('settings:generalUpdated', populateGeneral);
  bus.on('settings:providersUpdated', renderProviders);

  populateGeneral();
  renderProviders();
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
