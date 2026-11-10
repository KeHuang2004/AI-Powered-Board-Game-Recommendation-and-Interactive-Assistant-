/* Local settings, server login, and shared request/error handling. */
const TOKEN_KEY = 'game-night-token';
let settingsReturn = 'home';
let settingsDirty = false;
let retryAction = null;
window.healthState = null;

function authToken() {
  try {
    const value = JSON.parse(sessionStorage.getItem(TOKEN_KEY) || 'null');
    if (value && Date.parse(value.expires_at) > Date.now()) return value.token;
  } catch { /* invalid or expired token */ }
  sessionStorage.removeItem(TOKEN_KEY);
  return '';
}

window.needsLogin = () => window.healthState?.mode === 'server' && !authToken();

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = authToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);
  const response = await fetch(url, {...options, headers});
  if (response.status === 401 && window.healthState?.mode === 'server' && url !== '/api/login') {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.setItem('game-night-login-return', document.body.dataset.screen || 'home');
    showScreen('login');
  }
  return response;
}

async function responseError(response) {
  let data = {};
  try { data = await response.json(); } catch { /* HTTP response was not JSON */ }
  const error = new Error(data.error?.message || data.detail || 'Request failed');
  error.status = response.status;
  error.code = data.error?.code || '';
  return error;
}

function showAppError(error, retry) {
  if (error?.name === 'AbortError') return;
  let message = t('Something went wrong. Please try again.', '暂时出了点问题，请重试。');
  if (error?.userMessage) {
    message = error.userMessage;
  } else if (error?.status === 429) {
    message = error.code === 'budget_exceeded'
      ? t('Today’s model budget has been reached.', '今天的模型用量已达到上限。')
      : t('Too many requests. Please wait a moment.', '请求太频繁，请稍后再试。');
  } else if (error?.status === 401) {
    message = t('Please sign in again.', '请重新登录。');
  } else if (!error?.status) {
    message = t('Could not connect. Check your connection and try again.', '无法连接，请检查网络后重试。');
  }
  $('app-error-text').textContent = message;
  retryAction = retry || null;
  $('app-error-retry').hidden = !retryAction;
  $('app-error').hidden = false;
}

async function refreshHealth() {
  const response = await apiFetch('/health');
  if (!response.ok) throw await responseError(response);
  window.healthState = await response.json();
  $('header-settings').hidden = window.healthState.mode !== 'local';
  return window.healthState;
}

async function openSettings(addHistory = true, returnTo = null) {
  if (window.healthState?.mode !== 'local') return;
  if (document.body.dataset.screen !== 'settings') settingsReturn = returnTo || document.body.dataset.screen || 'home';
  showScreen('settings', addHistory);
  $('settings-key').value = '';
  $('settings-status').textContent = '';
  try {
    const response = await apiFetch('/api/settings');
    if (!response.ok) throw await responseError(response);
    const settings = await response.json();
    $('settings-provider').value = settings.provider;
    $('settings-base').value = settings.base_url || '';
    $('settings-model').value = settings.model || '';
    $('settings-key').placeholder = settings.api_key_set ? t('Key saved', '已有密钥') : '';
    $('settings-search').checked = Boolean(settings.search_enabled);
    settingsDirty = false;
  } catch (error) { showAppError(error, () => openSettings(false)); }
}

async function saveSettings() {
  const provider = $('settings-provider').value;
  const key = $('settings-key').value;
  if (!key && !['mock', 'catalog'].includes(provider)) {
    $('settings-status').textContent = t('Enter the API key before saving.', '保存前请输入 API Key。');
    $('settings-key').focus();
    return false;
  }
  const body = {provider, base_url: $('settings-base').value.trim(), model: $('settings-model').value.trim(), api_key: key};
  $('settings-key').value = '';
  const response = await apiFetch('/api/settings', {method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});
  if (!response.ok) throw await responseError(response);
  const saved = await response.json();
  settingsDirty = false;
  $('settings-key').placeholder = saved.api_key_set ? t('Key saved', '已有密钥') : '';
  $('settings-status').textContent = t('Saved. Test the connection to use guided play.', '已保存，请测试连接后使用带玩。');
  await refreshHealth();
  return true;
}

document.addEventListener('DOMContentLoaded', async () => {
  $('app-error-close').addEventListener('click', () => { $('app-error').hidden = true; });
  $('app-error-retry').addEventListener('click', () => {
    const action = retryAction;
    $('app-error').hidden = true;
    if (action) action();
  });
  $('header-settings').addEventListener('click', () => openSettings());
  $('settings-back').addEventListener('click', () => restoreScreen(settingsReturn));
  $('settings-search').addEventListener('change', async event => {
    const input = event.currentTarget;
    const desired = input.checked;
    input.disabled = true;
    try {
      const response = await apiFetch('/api/settings/search', {method: 'PUT',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({search_enabled: desired})});
      if (!response.ok) throw await responseError(response);
      if (window.healthState) window.healthState.search_enabled = desired;
      $('settings-status').textContent = desired
        ? t('Web search enabled.', '联网搜索已开启。') : t('Web search disabled.', '联网搜索已关闭。');
    } catch (error) {
      input.checked = !desired;
      showAppError(error, () => { input.checked = desired; input.dispatchEvent(new Event('change')); });
    } finally { input.disabled = false; }
  });
  for (const id of ['settings-provider', 'settings-base', 'settings-model', 'settings-key']) {
    $(id).addEventListener('input', () => { settingsDirty = true; });
    $(id).addEventListener('change', () => { settingsDirty = true; });
  }
  $('settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    try { await saveSettings(); } catch (error) {
      showAppError(error, () => {
        $('settings-status').textContent = t('Re-enter your key, then save again.', '请重新输入密钥，再次保存。');
        $('settings-key').focus();
      });
    }
  });
  $('settings-test').addEventListener('click', async () => {
    try {
      if (settingsDirty && !(await saveSettings())) return;
      const response = await apiFetch('/api/settings/test', {method: 'POST'});
      if (!response.ok) throw await responseError(response);
      const result = await response.json();
      $('settings-status').textContent = result.ok
        ? t('Connection verified. You can start guided play.', '连接测试通过，可以开始带玩。')
        : t('Connection test failed. Check the settings and try again.', '连接测试未通过，请检查设置后重试。');
      await refreshHealth();
    } catch (error) { showAppError(error, () => $('settings-test').click()); }
  });
  $('login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const body = {username: $('login-username').value.trim(), password: $('login-password').value};
    $('login-password').value = '';
    try {
      const response = await apiFetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)});
      if (!response.ok) throw await responseError(response);
      sessionStorage.setItem(TOKEN_KEY, JSON.stringify(await response.json()));
      const target = sessionStorage.getItem('game-night-login-return') || 'home';
      sessionStorage.removeItem('game-night-login-return');
      await restoreScreen(target);
    } catch (error) { showAppError(error, () => $('login-password').focus()); }
  });

  async function initialize() {
    try {
      const health = await refreshHealth();
      const saved = loadAppState();
      if (needsLogin()) {
        const target = saved.screen || 'home';
        if (target !== 'login') sessionStorage.setItem('game-night-login-return', target);
        showScreen('login', false);
      } else if (health.mode === 'local' && health.provider_setting !== 'catalog' && !health.llm_configured) {
        await openSettings(false, saved.screen && saved.screen !== 'settings' ? saved.screen : 'home');
      } else {
        await restoreApp();
      }
    } catch (error) { showAppError(error, initialize); }
  }
  await initialize();
});
