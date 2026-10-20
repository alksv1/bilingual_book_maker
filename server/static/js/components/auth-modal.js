/**
 * Password Authentication Modal Component.
 */

import { api } from '../api.js';
import { toast } from './toast.js';
import { bus, state, setAuthState } from '../state.js';

export function initAuthModal() {
  const modalEl = document.getElementById('auth-modal');
  const formEl = document.getElementById('auth-form');
  const passwordInput = document.getElementById('auth-password');
  const submitBtn = document.getElementById('auth-submit-btn');
  const authStatusIndicator = document.getElementById('auth-status-indicator');

  function open() {
    if (modalEl) {
      modalEl.classList.add('active');
      if (passwordInput) {
        passwordInput.value = '';
        setTimeout(() => passwordInput.focus(), 150);
      }
    }
  }

  function close() {
    if (modalEl) {
      modalEl.classList.remove('active');
    }
  }

  // Update navbar indicator
  function updateIndicator() {
    if (!authStatusIndicator) return;
    if (state.auth.protected_mode) {
      authStatusIndicator.style.display = 'inline-flex';
      if (state.auth.authenticated) {
        authStatusIndicator.className = 'auth-badge';
        authStatusIndicator.innerHTML = '<span class="dot"></span><span>已鉴权</span>';
      } else {
        authStatusIndicator.className = 'auth-badge unauthenticated';
        authStatusIndicator.innerHTML = '<span class="dot"></span><span>未登录</span>';
      }
    } else {
      authStatusIndicator.style.display = 'none';
    }
  }

  bus.on('auth:changed', updateIndicator);
  bus.on('auth:required', () => {
    if (state.auth.protected_mode && !state.auth.authenticated) {
      open();
    }
  });

  if (formEl) {
    formEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      const password = passwordInput.value.trim();
      if (!password) {
        toast.error('请输入访问密码');
        return;
      }

      submitBtn.disabled = true;
      submitBtn.innerText = '登录中...';

      try {
        await api.auth.login(password);
        toast.success('登录成功');
        close();
        bus.emit('auth:success');
      } catch (err) {
        toast.error(err.message || '密码验证失败');
        passwordInput.select();
      } finally {
        submitBtn.disabled = false;
        submitBtn.innerText = '确认进入';
      }
    });
  }

  // Logout button handler
  const logoutBtn = document.getElementById('btn-logout');
  if (logoutBtn) {
    logoutBtn.addEventListener('click', async () => {
      if (confirm('确定要退出当前登录会话吗？')) {
        try {
          await api.auth.logout();
          toast.info('已安全登出');
          open();
        } catch (err) {
          toast.error('登出失败: ' + err.message);
        }
      }
    });
  }

  return { open, close };
}
