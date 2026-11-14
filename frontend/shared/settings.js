/* Language settings and shared request/error handling. */
let settingsReturn = 'home';
let retryAction = null;
window.healthState = null;

async function apiFetch(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (error instanceof TypeError) error.code = 'network_error';
    throw error;
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
  } else if (error?.code === 'session_creation_failed') {
    message = t('Could not start guided play. Please try again.', '开局失败，请重试。');
  } else if (error?.code === 'stream_timeout') {
    message = t('The request took too long. Please try again.', '等待超时，请重试。');
  } else if (error?.status === 429) {
    message = error.code === 'budget_exceeded'
      ? t('Today’s model budget has been reached.', '今天的模型用量已达到上限。')
      : t('Too many requests. Please wait a moment.', '请求太频繁，请稍后再试。');
  } else if (error?.code === 'network_error') {
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
  return window.healthState;
}

function openSettings(addHistory = true) {
  if (document.body.dataset.screen !== 'settings') settingsReturn = document.body.dataset.screen || 'home';
  showScreen('settings', addHistory);
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
  $('settings-language').addEventListener('click', () => saveAppState(settingsReturn));
  async function initialize() {
    try {
      await refreshHealth();
      await restoreApp();
    } catch (error) { showAppError(error, initialize); }
  }
  await initialize();
});
