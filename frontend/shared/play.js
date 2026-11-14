/* Guided play: POST-based SSE, persisted sessions, and deterministic tools. */
let playSession = null;
let playMeta = null;
let playBusy = false;
let playPreparation = null;
let timerInterval = null;

const playPreparationStages = {
  details: [1, t('Loading game details…', '正在载入游戏资料…')],
  planning: [2, t('Reviewing the rules and planning your first round…', '正在整理规则和首轮带玩步骤…')],
  searching: [2, t('Searching online for game rules…', '正在联网查找游戏规则…')],
  reading: [2, t('Reading rule sources…', '正在阅读规则资料…')],
  checking: [2, t('Checking the steps for your first round…', '正在核对首轮带玩步骤…')],
  cache_hit: [2, t('Loading your saved guide…', '正在读取已保存的带玩计划…')],
  starting: [3, t('Setting up your play session…', '正在创建带玩会话…')],
};

function sourceText(tag) {
  return {rulebook: t('Rulebook', '规则书'), dataset: t('Dataset', '数据集'),
    general: t('General knowledge', '通用知识'), web: t('Web', '网络')}[tag] || tag;
}

function renderTaggedText(container, text) {
  container.replaceChildren();
  const pieces = String(text || '').split(/(\[\[(?:rulebook|dataset|general|web)\]\])/g);
  for (const piece of pieces) {
    const match = piece.match(/^\[\[(rulebook|dataset|general|web)\]\]$/);
    if (match) container.append(element('span', sourceText(match[1]), 'source-tag source-' + match[1]));
    else container.append(document.createTextNode(piece));
  }
}

function renderSources(item, sources) {
  item.querySelector('.chat-sources')?.remove();
  const list = element('div', '', 'chat-sources');
  for (const source of sources || []) {
    try {
      const url = new URL(source.url);
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) continue;
      const link = element('a', source.title || url.href);
      link.href = url.href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      list.append(link);
    } catch { /* Invalid links are not rendered. */ }
  }
  if (list.childElementCount) item.append(list);
}

function appendChat(role, text, sources = []) {
  const item = element('div', '', 'chat-item chat-' + role);
  const label = element('strong', role === 'user' ? t('You', '你们') : t('Guide', '带玩助手'), 'chat-label');
  const content = element('div', '', 'chat-text');
  renderTaggedText(content, text);
  item.append(label, content);
  renderSources(item, sources);
  $('chat-log').append(item);
  item.scrollIntoView({block: 'nearest'});
  return content;
}

function formatDiceRoll(roll) {
  const sides = roll.spec.split('d')[1];
  const count = roll.values.length;
  const values = roll.values.join(t(', ', '、'));
  const dice = count === 1 ? `${sides}-sided die` : `${count} ${sides}-sided dice`;
  const chineseDice = count === 1 ? `${sides} 面骰` : `${count} 个 ${sides} 面骰`;
  return t(`Rolled ${values} (${dice})`, `掷出 ${values} 点（${chineseDice}）`);
}

function appendTool(event) {
  const names = {roll_dice: t('Dice roll', '掷骰'), update_score: t('Score update', '更新分数'),
    reset_scores: t('All scores reset', '全部分数已重置'),
    set_players: t('Players updated', '更新玩家'), advance_step: t('Next step', '推进步骤'),
    get_game: t('Game information', '游戏信息'), lookup_rulebook: t('Rulebook lookup', '查询规则书'),
    web_search: t('Web search', '联网搜索'), fetch_page: t('Read webpage', '读取网页')};
  const item = element('div', '', 'tool-event');
  const result = event.result || {};
  const rolled = event.name === 'roll_dice' && result.values;
  let detail = result.error || '';
  if (event.name === 'update_score' && result.scores) {
    detail = Object.entries(result.scores).map(([name, score]) => name + ': ' + score).join(' · ');
  }
  if (event.name === 'set_players' && result.players) detail = result.players.join(' · ');
  if (event.name === 'advance_step' && result.current) detail = result.current.text;
  if (event.name === 'get_game' && result.game) detail = result.game.name;
  if (event.name === 'lookup_rulebook' && result.passages) detail = result.passages.map(part => part.text).join('\n\n');
  if (event.name === 'web_search' && Array.isArray(result)) detail = result.map(part => part.title || part.url).join('\n');
  if (event.name === 'fetch_page' && result.url) detail = result.title || result.url;
  const title = rolled ? formatDiceRoll(result) : names[event.name] || t('Game tool', '游戏工具');
  item.append(element('strong', title));
  if (!rolled) item.append(element('span', detail, 'tool-event-result'));
  $('chat-log').append(item);
}

function setPlayBusy(value) {
  playBusy = value;
  for (const id of [
    'play-next', 'play-unclear', 'play-qa', 'roll-dice', 'score-player', 'score-delta', 'reset-scores',
  ]) {
    $(id).disabled = value;
  }
  $('chat-form').querySelector('button').disabled = value;
  $('score-form').querySelector('button[type="submit"]').disabled = value;
  $('phase-nav').querySelectorAll('button').forEach(button => { button.disabled = value; });
}

function setPlayPreparing(preparing) {
  $('play-preparing').hidden = !preparing;
  $('play-layout').hidden = preparing;
  $('open-tools').hidden = preparing;
}

function updatePlayPreparation(stage) {
  const current = playPreparationStages[stage === 'ready' ? 'starting' : stage];
  if (!current) return;
  const [step, message] = current;
  const status = $('play-preparing-status');
  if (status.textContent !== message) status.textContent = message;
  $('play-preparing-progress').dataset.step = step;
}

function cancelPlayPreparation() {
  playPreparation?.abort();
}

async function streamPost(url, body, onEvent, signal) {
  const response = await apiFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body), signal});
  if (!response.ok) throw await responseError(response);
  if (!response.body) throw new Error('Streaming unavailable');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let doneEvent = false;
  let streamError = null;
  const dispatch = block => {
    let name = 'message';
    const data = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) name = line.slice(6).trim();
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
    }
    if (!data.length) return;
    const value = JSON.parse(data.join('\n'));
    if (name === 'error') {
      streamError = new Error(value.message || 'Stream failed');
      streamError.code = value.code || '';
    }
    if (name === 'done') doneEvent = true;
    onEvent(name, value);
  };
  while (true) {
    let chunk;
    try {
      chunk = await reader.read();
    } catch (error) {
      if (error instanceof TypeError) error.code = 'network_error';
      throw error;
    }
    buffer += decoder.decode(chunk.value || new Uint8Array(), {stream: !chunk.done});
    buffer = buffer.replace(/\r\n/g, '\n');
    let split;
    while ((split = buffer.indexOf('\n\n')) !== -1) {
      dispatch(buffer.slice(0, split));
      buffer = buffer.slice(split + 2);
    }
    if (chunk.done) break;
  }
  if (buffer.trim()) dispatch(buffer);
  if (streamError) throw streamError;
  if (!doneEvent) throw new Error('Stream ended before completion');
}

function modelRequiredMessage() {
  if (window.healthState?.mode === 'local') {
    return t('Configure the model in the local .env file and restart the app.',
      '请在本地 .env 文件中配置模型后重启服务。');
  }
  return t('Guided play requires a configured model.', '带玩需要先配置模型。');
}

function requirementNotice() {
  const notice = $('play-requirement');
  notice.hidden = false;
  notice.textContent = modelRequiredMessage();
}

async function requestPlay() {
  try {
    const health = await refreshHealth();
    if (!health.llm_configured || !health.verified) return requirementNotice();
    $('play-requirement').hidden = true;
    $('player-form').hidden = false;
    $('player-names').focus();
  } catch (error) { showAppError(error, requestPlay); }
}

async function createPlay(names) {
  if (!activeGame || playBusy) return;
  const body = {game_id: activeGame.game.game_id, lang: language, players: answers.players, rid: latestResult?.rid || null};
  if (names?.length) body.player_names = names;
  const request = new AbortController();
  playPreparation = request;
  playSession = null;
  $('chat-log').replaceChildren();
  $('play-game-name').textContent = activeGame.game.name;
  updatePlayPreparation('details');
  setPlayPreparing(true);
  setPlayBusy(true);
  try {
    showScreen('play');
    await loadPlayMeta(language, request.signal);
    await streamPost('/api/sessions', body, (event, data) => {
      if (request.signal.aborted) return;
      if (event === 'status') updatePlayPreparation(data.stage);
      if (event === 'session') {
        playSession = data;
        sessionStorage.setItem('game-night-session', data.session_id);
        renderPlay();
      }
    }, request.signal);
    if (!playSession) {
      const error = new Error('Session was not created');
      error.code = 'session_creation_failed';
      throw error;
    }
  } catch (error) {
    if (request.signal.aborted) return;
    showScreen('detail');
    if (error.code === 'llm_not_configured') requirementNotice();
    else showAppError(error, () => createPlay(names));
  } finally {
    playPreparation = null;
    setPlayPreparing(false);
    setPlayBusy(false);
  }
}

async function loadPlayMeta(lang, signal) {
  const response = await apiFetch('/api/meta?lang=' + encodeURIComponent(lang), {signal});
  if (!response.ok) throw await responseError(response);
  playMeta = await response.json();
}

function renderOverview() {
  const game = playSession.game;
  $('play-game-name').textContent = game.name;
  $('quick-goal').textContent = playSession.plan?.goal || game.blurb || t('Not specified.', '暂无资料。');
  $('quick-core').textContent = game.core_rules || t('Not specified.', '暂无资料。');
  $('quick-detailed').textContent = game.detailed_rules || t('Not specified.', '暂无资料。');
  const items = (game.setup || '').split(/[\n；;]+/).map(item => item.trim()).filter(Boolean);
  const checkedKey = 'game-night-setup-' + playSession.session_id;
  let checked = [];
  try { checked = JSON.parse(sessionStorage.getItem(checkedKey) || '[]'); } catch { /* no saved checks */ }
  const list = $('quick-setup');
  list.replaceChildren();
  if (!items.length) list.textContent = t('Unavailable in this language.', '暂无资料。');
  items.forEach((text, index) => {
    const label = element('label', '', 'setup-check');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = checked.includes(index);
    input.addEventListener('change', () => {
      const values = [...list.querySelectorAll('input')].flatMap((node, position) => node.checked ? [position] : []);
      sessionStorage.setItem(checkedKey, JSON.stringify(values));
    });
    label.append(input, document.createTextNode(text));
    list.append(label);
  });
  $('quick-view').open = window.matchMedia('(min-width: 641px)').matches;
}

function renderProgress() {
  const nav = $('phase-nav');
  nav.replaceChildren();
  nav.hidden = playSession.state.mode === 't0';
  if (!nav.hidden) for (const phase of playMeta.phases) {
    const button = element('button', phase.label, 'phase-button');
    button.type = 'button';
    if (phase.id === playSession.state.phase) button.setAttribute('aria-current', 'step');
    button.addEventListener('click', () => jumpPhase(phase.id));
    nav.append(button);
  }
  const current = playSession.current;
  const panel = $('current-step');
  panel.replaceChildren();
  if (current) {
    panel.append(element('span', t('Current step', '当前步骤'), 'step-label'));
    const text = element('p', '', 'step-text');
    renderTaggedText(text, current.text + (current.src ? ' [[' + current.src + ']]' : ''));
    panel.append(text);
  } else {
    panel.append(element('p', t('Ask a question whenever you are ready.', '准备好后，随时可以提问。'), 'step-text'));
  }
}

function renderScores() {
  const state = playSession.state;
  $('score-list').replaceChildren(...state.players.map(name => {
    const row = element('div', '', 'score-row');
    row.append(element('span', name), element('strong', String(state.scores[name] ?? 0)));
    return row;
  }));
  const select = $('score-player');
  const selected = select.value;
  select.replaceChildren(...state.players.map(name => {
    const option = element('option', name);
    option.value = name;
    return option;
  }));
  if (state.players.includes(selected)) select.value = selected;
  const lastRoll = state.dice_log?.at(-1);
  $('dice-result').textContent = lastRoll ? formatDiceRoll(lastRoll) : '';
}

function renderPlay() {
  renderOverview();
  renderProgress();
  renderScores();
  $('chat-log').replaceChildren();
  for (const entry of playSession.history || []) {
    if (entry.role === 'tool') appendTool(entry);
    else if (entry.role === 'user' || entry.role === 'assistant') appendChat(entry.role, entry.text, entry.sources);
  }
  restoreTimer();
}

async function restorePlay() {
  const id = sessionStorage.getItem('game-night-session');
  if (!id) return false;
  try {
    const response = await apiFetch('/api/sessions/' + encodeURIComponent(id));
    if (!response.ok) throw await responseError(response);
    playSession = await response.json();
    await loadPlayMeta(playSession.state.lang);
    renderPlay();
    return true;
  } catch (error) {
    if (error.status === 404) sessionStorage.removeItem('game-night-session');
    showAppError(error, () => restoreScreen('play'));
    return false;
  }
}

async function updatePlay(path, body, retry) {
  if (!playSession || playBusy) return;
  setPlayBusy(true);
  try {
    const response = await apiFetch('/api/sessions/' + encodeURIComponent(playSession.session_id) + path,
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    playSession.state = result.state;
    if ('current' in result) playSession.current = result.current;
    renderProgress();
    renderScores();
    if (result.result) appendTool({name: path.split('/').at(-1), result: result.result});
  } catch (error) { showAppError(error, retry); }
  finally { setPlayBusy(false); }
}

function jumpPhase(phase) {
  return updatePlay('/jump', {phase}, () => jumpPhase(phase));
}

async function sendPlayMessage(text) {
  if (!playSession || playBusy || !text.trim()) return;
  const message = text.trim();
  appendChat('user', message);
  $('chat-input').value = '';
  let draft = null;
  let accumulated = '';
  setPlayBusy(true);
  try {
    await streamPost('/api/sessions/' + encodeURIComponent(playSession.session_id) + '/message', {text: message}, (event, data) => {
      if (event === 'delta') {
        accumulated += data.text || '';
        if (!draft) draft = appendChat('assistant', '');
        renderTaggedText(draft, accumulated);
      } else if (event === 'message') {
        if (!draft) draft = appendChat('assistant', '');
        renderTaggedText(draft, data.text);
        renderSources(draft.parentElement, data.sources);
      } else if (event === 'tool') appendTool(data);
      else if (event === 'state') {
        playSession.state = data;
        renderScores();
        renderProgress();
      }
    });
    // A tool may advance a step; reload its current text from the authoritative session.
    const response = await apiFetch('/api/sessions/' + encodeURIComponent(playSession.session_id));
    if (!response.ok) throw await responseError(response);
    const fresh = await response.json();
    playSession.state = fresh.state;
    playSession.current = fresh.current;
    renderScores();
    renderProgress();
  } catch (error) {
    if (error.code === 'llm_not_configured') error.userMessage = modelRequiredMessage();
    showAppError(error, () => sendPlayMessage(message));
  } finally { setPlayBusy(false); }
}

function timerKey() { return 'game-night-timer-' + playSession.session_id; }
function readSavedTimer() {
  try { return JSON.parse(sessionStorage.getItem(timerKey()) || 'null'); }
  catch { return null; }
}
function readTimerDuration(input) {
  return /^\d+$/.test(input.value) && input.validity.valid ? input.valueAsNumber : null;
}
function validateTimerDuration() {
  const input = $('timer-seconds');
  const duration = readTimerDuration(input);
  const invalid = duration === null;
  input.setAttribute('aria-invalid', String(invalid));
  const error = $('timer-error');
  const max = Number(input.max).toLocaleString(language);
  const message = invalid
    ? t(`Enter a whole number of seconds from ${input.min} to ${max}.`, `请输入 ${input.min} 到 ${max} 之间的整数秒。`)
    : '';
  if (error.textContent !== message) error.textContent = message;
  return duration;
}
function formatTime(seconds) {
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
}
function setTimerNotice(visible) {
  $('timer-notice-card').classList.toggle('is-visible', visible);
  $('timer-notice').textContent = visible ? t('Time’s up!', '时间到！') : '';
}
function renderTimer(timer) {
  const remaining = Math.max(0, Math.ceil((timer.end - Date.now()) / 1000));
  $('timer-output').textContent = formatTime(remaining);
  if (!remaining) {
    clearInterval(timerInterval);
    timerInterval = null;
    setTimerNotice(!timer.dismissed);
  }
  return remaining;
}
function restoreTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
  setTimerNotice(false);
  const input = $('timer-seconds');
  const timer = readSavedTimer();
  if (Number.isFinite(timer?.end)) {
    input.value = Math.ceil(timer.duration); // Older sessions could save fractional durations.
    if (renderTimer(timer) > 0) timerInterval = setInterval(() => renderTimer(timer), 250);
  } else {
    const duration = readTimerDuration(input) ?? Number(input.defaultValue);
    input.value = duration;
    $('timer-output').textContent = formatTime(duration);
  }
  validateTimerDuration();
}

$('start-play').addEventListener('click', requestPlay);
$('player-form').addEventListener('submit', event => {
  event.preventDefault();
  const names = $('player-names').value.split(/[,，、\n]/).map(name => name.trim()).filter(Boolean);
  if (names.length && (names.length !== answers.players || new Set(names).size !== names.length)) {
    $('play-requirement').hidden = false;
    $('play-requirement').textContent = t('Enter one different name for each player, or skip names.', '请为每位玩家填写不同的名字，或选择跳过。');
    return;
  }
  createPlay(names);
});
$('skip-names').addEventListener('click', () => createPlay([]));
$('play-back').addEventListener('click', () => activeGame ? openGame(activeGame.game, activeGame.index) : showScreen('results'));
$('play-next').addEventListener('click', () => {
  if (!playSession) return;
  if (playSession.state.mode === 't0') sendPlayMessage(playMeta.nudges.next_t0);
  else updatePlay('/next', {}, () => $('play-next').click());
});
$('play-unclear').addEventListener('click', () => sendPlayMessage(playMeta.nudges.didnt_get_it));
$('play-qa').addEventListener('click', () => {
  if (playSession?.state.mode === 't0') $('chat-input').focus();
  else jumpPhase('qa');
});
$('chat-form').addEventListener('submit', event => { event.preventDefault(); sendPlayMessage($('chat-input').value); });
$('roll-dice').addEventListener('click', () => updatePlay('/tools/roll_dice', {count: 1, sides: 6}, () => $('roll-dice').click()));
$('score-form').addEventListener('submit', event => {
  event.preventDefault();
  const body = {player: $('score-player').value, delta: Number($('score-delta').value)};
  updatePlay('/tools/update_score', body, () => updatePlay('/tools/update_score', body, null));
});
$('reset-scores').addEventListener('click', () => {
  updatePlay('/tools/reset_scores', {}, () => $('reset-scores').click());
});
$('open-tools').addEventListener('click', () => $('tool-drawer').classList.add('open'));
$('close-tools').addEventListener('click', () => $('tool-drawer').classList.remove('open'));
$('timer-seconds').addEventListener('input', validateTimerDuration);
$('timer-start').addEventListener('click', () => {
  if (!playSession) return;
  const duration = validateTimerDuration();
  if (duration === null) {
    $('timer-seconds').focus();
    return;
  }
  const end = Date.now() + duration * 1000;
  sessionStorage.setItem(timerKey(), JSON.stringify({duration, end}));
  restoreTimer();
});
$('timer-reset').addEventListener('click', () => {
  if (!playSession) return;
  sessionStorage.removeItem(timerKey());
  restoreTimer();
});
$('timer-notice-close').addEventListener('click', event => {
  if (!playSession) return;
  const timer = readSavedTimer();
  if (timer) sessionStorage.setItem(timerKey(), JSON.stringify({...timer, dismissed: true}));
  const wasFocused = document.activeElement === event.currentTarget;
  setTimerNotice(false);
  if (wasFocused) {
    const start = $('timer-start');
    const target = start.getClientRects().length ? start : $('open-tools');
    target.focus({preventScroll: true});
  }
});
