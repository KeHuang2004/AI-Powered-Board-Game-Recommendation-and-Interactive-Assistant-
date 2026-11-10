/* Guided play: POST-based SSE, persisted sessions, and deterministic tools. */
let playSession = null;
let playMeta = null;
let playBusy = false;
let timerInterval = null;

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

function appendTool(event) {
  const names = {roll_dice: t('Dice roll', '掷骰'), update_score: t('Score update', '更新分数'),
    set_players: t('Players updated', '更新玩家'), advance_step: t('Next step', '推进步骤'),
    get_game: t('Game information', '游戏信息'), lookup_rulebook: t('Rulebook lookup', '查询规则书'),
    web_search: t('Web search', '联网搜索'), fetch_page: t('Read webpage', '读取网页')};
  const item = element('div', '', 'tool-event');
  const result = event.result || {};
  let detail = result.error || '';
  if (event.name === 'roll_dice' && result.values) detail = result.spec + ' · ' + result.values.join(', ');
  if (event.name === 'update_score' && result.scores) {
    detail = Object.entries(result.scores).map(([name, score]) => name + ': ' + score).join(' · ');
  }
  if (event.name === 'set_players' && result.players) detail = result.players.join(' · ');
  if (event.name === 'advance_step' && result.current) detail = result.current.text;
  if (event.name === 'get_game' && result.game) detail = result.game.name;
  if (event.name === 'lookup_rulebook' && result.passages) detail = result.passages.map(part => part.text).join('\n\n');
  if (event.name === 'web_search' && Array.isArray(result)) detail = result.map(part => part.title || part.url).join('\n');
  if (event.name === 'fetch_page' && result.url) detail = result.title || result.url;
  item.append(element('strong', names[event.name] || t('Game tool', '游戏工具')),
    element('span', detail, 'tool-event-result'));
  $('chat-log').append(item);
}

function setPlayBusy(value) {
  playBusy = value;
  for (const id of ['play-next', 'play-unclear', 'play-qa', 'roll-dice', 'score-player', 'score-delta']) {
    $(id).disabled = value;
  }
  $('chat-form').querySelector('button').disabled = value;
  $('score-form').querySelector('button').disabled = value;
  $('phase-nav').querySelectorAll('button').forEach(button => { button.disabled = value; });
}

async function streamPost(url, body, onEvent) {
  const response = await apiFetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)});
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
    const chunk = await reader.read();
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

function requirementNotice() {
  const notice = $('play-requirement');
  notice.hidden = false;
  notice.replaceChildren(document.createTextNode(t('A configured and verified model is needed to start guided play. ',
    '开始带玩前需要配置并验证模型。')));
  if (window.healthState?.mode === 'local') {
    const button = element('button', t('Open settings', '前往设置'), 'text-button');
    button.type = 'button';
    button.addEventListener('click', () => openSettings());
    notice.append(button);
  }
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
  playSession = null;
  $('chat-log').replaceChildren();
  $('play-game-name').textContent = activeGame.game.name;
  $('current-step').textContent = t('Preparing your game…', '正在准备这一局…');
  showScreen('play');
  setPlayBusy(true);
  try {
    await loadPlayMeta(language);
    await streamPost('/api/sessions', body, (event, data) => {
      if (event === 'status') $('play-status').textContent = data.stage === 'cache_hit'
        ? t('Ready from a saved plan', '已读取现有计划') : data.stage === 'ready'
          ? t('Ready to play', '准备就绪') : t('Preparing your game…', '正在准备这一局…');
      if (event === 'session') {
        playSession = data;
        sessionStorage.setItem('game-night-session', data.session_id);
        renderPlay();
      }
    });
    if (!playSession) throw new Error('Session was not created');
    $('play-status').textContent = '';
  } catch (error) {
    if (window.needsLogin?.()) { showScreen('login'); return showAppError(error); }
    showScreen('detail');
    if (error.code === 'llm_not_configured') requirementNotice();
    else showAppError(error, () => createPlay(names));
  } finally { setPlayBusy(false); }
}

async function loadPlayMeta(lang) {
  const response = await apiFetch('/api/meta?lang=' + encodeURIComponent(lang));
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
  $('dice-result').textContent = lastRoll ? lastRoll.spec + ': ' + lastRoll.values.join(', ') : '';
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
    if (error.code === 'llm_not_configured') {
      showAppError(error, () => sendPlayMessage(message));
      if (window.healthState?.mode === 'local') $('app-error-text').textContent = t('Configure and verify your model in Settings.', '请先在设置中配置并验证模型。');
    } else showAppError(error, () => sendPlayMessage(message));
  } finally { setPlayBusy(false); }
}

function timerKey() { return 'game-night-timer-' + playSession.session_id; }
function formatTime(seconds) {
  return String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
}
function renderTimer(end) {
  const remaining = Math.max(0, Math.ceil((end - Date.now()) / 1000));
  $('timer-output').textContent = formatTime(remaining);
  if (!remaining) { clearInterval(timerInterval); timerInterval = null; }
}
function restoreTimer() {
  clearInterval(timerInterval);
  timerInterval = null;
  let data = null;
  try { data = JSON.parse(sessionStorage.getItem(timerKey()) || 'null'); } catch { /* start fresh */ }
  if (data?.end) {
    $('timer-seconds').value = data.duration;
    renderTimer(data.end);
    if (data.end > Date.now()) timerInterval = setInterval(() => renderTimer(data.end), 250);
  } else $('timer-output').textContent = formatTime(Number($('timer-seconds').value) || 60);
}

$('start-play').addEventListener('click', requestPlay);
$('player-form').addEventListener('submit', event => {
  event.preventDefault();
  const names = $('player-names').value.split(/[,，\n]/).map(name => name.trim()).filter(Boolean);
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
$('open-tools').addEventListener('click', () => $('tool-drawer').classList.add('open'));
$('close-tools').addEventListener('click', () => $('tool-drawer').classList.remove('open'));
$('timer-start').addEventListener('click', () => {
  if (!playSession) return;
  const duration = Math.min(86400, Math.max(1, Number($('timer-seconds').value) || 60));
  const end = Date.now() + duration * 1000;
  sessionStorage.setItem(timerKey(), JSON.stringify({duration, end}));
  clearInterval(timerInterval);
  renderTimer(end);
  timerInterval = setInterval(() => renderTimer(end), 250);
});
$('timer-reset').addEventListener('click', () => {
  if (!playSession) return;
  sessionStorage.removeItem(timerKey());
  restoreTimer();
});
