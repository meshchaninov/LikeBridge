'use strict';
const $ = (q, root = document) => root.querySelector(q);
const $$ = (q, root = document) => [...root.querySelectorAll(q)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names = {yandex:'Яндекс Музыка',spotify:'Spotify',apple:'Apple Music'};
const symbols = {yandex:'Я',spotify:'≋',apple:'♫'};
const statuses = {pending:'В очереди',retry:'Повторим позже',review:'Нужно проверить',baseline:'Старый лайк',sent:'Заявка отправлена',existing:'Уже в DroppedNeedle',duplicate:'Повтор альбома',skipped:'Пропущено'};
let csrf = '', currentView = 'overview', currentPage = 0, state = {}, formFilled = false, toastTimer;
function toast(message) {const t = $('#toast');t.textContent = message;t.style.display = 'block';clearTimeout(toastTimer);toastTimer = setTimeout(() => t.style.display = 'none', 6500);}
async function api(path, data, method = 'POST') {
  const options = {method:data === undefined ? 'GET' : method, headers:{'X-CSRF-Token':csrf}};
  if (data !== undefined) {options.headers['Content-Type'] = 'application/json';options.body = JSON.stringify(data);}
  const r = await fetch(path, options);const body = await r.json();
  if (!r.ok) {if (r.status === 401) showLogin(false);throw new Error(body.error || 'Не удалось выполнить действие');}
  return body;
}
async function action(button, fn) {
  const text = button.textContent;button.disabled = true;button.textContent = 'Подождите…';
  try {await fn();} catch (error) {toast(error.message || 'Ошибка соединения');}
  finally {button.disabled = false;button.textContent = text;}
}
function date(value) {return value ? new Date(value * 1000).toLocaleString('ru-RU', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : 'Ещё не запускалась';}
function likedDate(value) {
  if (!value) return 'Дата лайка недоступна';
  return 'Лайк: ' + new Date(value * 1000).toLocaleString('ru-RU', {
    day:'numeric',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'
  });
}
function showLogin(setup) {$('#login').hidden = false;$('#shell').hidden = true;$('#setup-label').hidden = !setup;}
function showApp() {$('#login').hidden = true;$('#shell').hidden = false;$('#login-password').value = '';}
async function showView(view) {
  currentView = view;$$('.view').forEach(x => x.hidden = x.id !== `view-${view}`);
  $$('.nav').forEach(x => x.classList.toggle('active', x.dataset.view === view));
  if (view === 'queue') await loadTracks();
}
function providerCard(provider, account, detail = false) {
  const connected = account && account.enabled;
  return `<article class="provider-card"><div class="provider-top"><span class="provider-symbol ${provider}">${symbols[provider]}</span><div><h3>${names[provider]}</h3><small>${connected ? `${account.count || 0} лайков · ${date(account.last_sync)}` : provider === 'apple' ? 'Любимые песни из плейлиста' : 'Твои понравившиеся треки'}</small></div></div><p><span class="tag ${connected ? 'connected' : ''}">${connected ? 'Подключён' : 'Не подключён'}</span></p>${account?.error ? `<p class="error-text">${esc(account.error)}</p>` : ''}<button data-connect="${provider}">${connected ? 'Настроить подключение ↗' : 'Подключить ↗'}</button>${detail && connected ? `<button class="text" data-disconnect="${provider}">Отключить аккаунт</button>` : ''}</article>`;
}
function fillForm(form, data) {
  for (const [key, value] of Object.entries(data || {})) {
    const field = form.elements.namedItem(key);if (!field) continue;
    if (field.type === 'checkbox') field.checked = Boolean(value);else field.value = value ?? '';
  }
}
async function refresh() {
  if ($('#shell').hidden) return;
  state = await api('/api/state');
  const c = state.counts;$('#stat-total').textContent = Object.values(c).reduce((a,b) => a+b,0);
  $('#stat-sent').textContent = c.sent || 0;$('#stat-review').textContent = c.review || 0;
  $('#queue-badge').textContent = (c.review || 0) + (c.pending || 0) + (c.retry || 0);
  $('#stat-status').textContent = state.running ? 'Собираем…' : state.accounts.some(a => a.enabled) ? 'На связи' : 'Ожидание';
  $('#stat-time').textContent = state.running ? 'Лайки → альбомы → DroppedNeedle' : date(state.last_run || Math.max(0,...state.accounts.map(a => a.last_sync || 0)));
  $('#sync').disabled = state.running;
  for (const [selector, detail] of [['#overview-providers', false],['#connection-providers',true]]) {
    $(selector).innerHTML = Object.keys(names).map(p => providerCard(p, state.accounts.find(a => a.provider === p), detail)).join('');
  }
  $('#events').innerHTML = state.events.length ? state.events.map(e => `<div class="event"><span>${esc(e.message)}</span><time>${date(e.at)}</time></div>`).join('') : '<div class="empty">Пока здесь тихо.<br>Подключи музыкальный аккаунт — и начнём собирать коллекцию.</div>';
  $('#dn-status').textContent = state.destination_connected ? 'Подключён' : 'Не подключён';
  $('#dn-status').classList.toggle('connected', state.destination_connected);
  if (!formFilled) {fillForm($('#destination-form'), state.destination);fillForm($('#settings-form'), state.settings);formFilled = true;}
  if (currentView === 'queue') await loadTracks();
}
function artworkURL(value, mbid) {
  if (value) {
    try {
      const url = new URL(value);
      const host = url.hostname;
      const allowed = ['i.scdn.co','avatars.yandex.net','avatars.mds.yandex.net','avatars.mds.yandex.com','coverartarchive.org'];
      if (url.protocol === 'https:' && !url.username && !url.password &&
          (allowed.includes(host) || host.endsWith('.mzstatic.com') || host.endsWith('.archive.org'))) return url.href;
    } catch (_) { /* Missing/invalid artwork falls back to a known release group. */ }
  }
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(mbid || '')
    ? `https://coverartarchive.org/release-group/${encodeURIComponent(mbid)}/front-250` : '';
}
function cover(value, mbid, small = false) {
  const url = artworkURL(value, mbid);
  return `<span class="album-cover${small ? ' album-cover-small' : ''}" aria-hidden="true"><span>♫</span>${url ? `<img src="${esc(url)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">` : ''}</span>`;
}
// Broken or unavailable artwork leaves the music-note placeholder, without inline handlers.
document.addEventListener('error', event => {
  if (event.target instanceof HTMLImageElement && event.target.closest('.album-cover')) event.target.remove();
}, true);
let activePreview = null;
let previewGeneration = 0;
const previewAudio = $('#preview-audio');
function previewButtons() {
  $$('[data-preview]').forEach(button => {
    const active = activePreview?.id === button.dataset.preview;
    button.disabled = Boolean(active && activePreview.loading);
    button.setAttribute('aria-label', (active && !previewAudio.paused ? 'Приостановить фрагмент: ' : 'Прослушать фрагмент: ') + button.dataset.previewName);
    button.textContent = active && activePreview.loading ? 'Ищем фрагмент…' : active && !previewAudio.paused ? 'Ⅱ Пауза' : '▶ ' + (button.dataset.previewLabel || 'Фрагмент');
  });
}
function sourceURL(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password &&
      ['music.yandex.ru','music.yandex.com','open.spotify.com','music.apple.com'].includes(url.hostname) ? url.href : '';
  } catch (_) { return ''; }
}
function listenLinks(track) {
  const performer = track.track_artist || track.artist;
  const query = encodeURIComponent(`${performer} ${track.title}`);
  const source = sourceURL(track.source_url);
  return `<div class="track-listen"><button class="secondary" data-preview="${track.id}" data-preview-name="${esc(performer + ' — ' + track.title)}" aria-label="Прослушать фрагмент: ${esc(performer)} — ${esc(track.title)}">▶ Фрагмент</button><a class="listen-link" href="https://www.youtube.com/results?search_query=${query}" target="_blank" rel="noopener noreferrer" aria-label="Найти ${esc(track.title)} на YouTube">YouTube ↗</a>${source ? `<a class="listen-link" href="${esc(source)}" target="_blank" rel="noopener noreferrer">${names[track.provider]} ↗</a>` : ''}</div>`;
}
function stopPreview() {
  previewGeneration++;activePreview = null;previewAudio.pause();previewAudio.removeAttribute('src');previewAudio.load();
  $('#preview-player').hidden = true;document.body.classList.remove('has-preview');previewButtons();
}
async function playPreview(button) {
  const id = button.dataset.preview;
  if (activePreview?.loading) {toast('Подожди, пока найдётся текущий фрагмент');return;}
  if (activePreview?.id === id && previewAudio.getAttribute('src')) {
    if (!previewAudio.paused) previewAudio.pause();
    else {
      if (previewAudio.currentTime >= 30) previewAudio.currentTime = 0;
      try {await previewAudio.play();} catch (_) {toast('Нажми ▶ в плеере, чтобы начать прослушивание');}
    }
    previewButtons();return;
  }
  previewAudio.pause();previewAudio.removeAttribute('src');previewAudio.load();
  const generation = ++previewGeneration;activePreview = {id,loading:true};previewButtons();
  try {
    const candidate = button.dataset.previewCandidate;
    const trackId = button.closest('[data-track]').dataset.track;
    const path = candidate ? `/api/tracks/${trackId}/candidates/${encodeURIComponent(candidate)}/preview` : `/api/tracks/${trackId}/preview`;
    const result = await api(path);
    if(generation !== previewGeneration) return;
    if (!result.available) {stopPreview();toast(result.message);return;}
    activePreview = {id,loading:false};
    $('#preview-title').textContent = `${result.artist} — ${result.title}`;
    const provider = {deezer:'Deezer',itunes:'iTunes'}[result.provider] || result.provider;
    $('#preview-credit').textContent = `${result.album_title ? result.album_title + ' · ' : ''}Фрагмент до 30 с · предоставлен ${provider}`;
    $('#preview-youtube').href = 'https://www.youtube.com/results?search_query=' + encodeURIComponent(`${result.artist} ${result.title}`);
    $('#preview-player').hidden = false;document.body.classList.add('has-preview');
    previewAudio.src = result.preview_url;
    try {await previewAudio.play();} catch (_) {toast('Фрагмент готов. Нажми ▶ в плеере');}
  } catch (error) {if(generation === previewGeneration){stopPreview();toast(error.message);}}
  finally {if (generation === previewGeneration && activePreview) activePreview.loading = false;previewButtons();}
}
previewAudio.addEventListener('timeupdate', () => {if (previewAudio.currentTime >= 30 && !previewAudio.paused) previewAudio.pause();});
previewAudio.addEventListener('play', () => {if(previewAudio.currentTime >= 30)previewAudio.currentTime=0;previewButtons();});
previewAudio.addEventListener('pause', previewButtons);
previewAudio.addEventListener('ended', previewButtons);
previewAudio.addEventListener('error', () => {if(previewAudio.getAttribute('src')){stopPreview();toast('Фрагмент недоступен. Попробуй YouTube или исходный сервис.');}});
$('#preview-close').onclick = stopPreview;
function requestChoices(track) {
  const kind = track.request_kind || 'album';
  const queued = ['pending','retry','sent','existing','duplicate'].includes(track.status);
  return `<div class="request-choices"><span>Запросить:</span><button class="secondary" data-request-kind="track" ${kind === 'track' && queued ? 'disabled' : ''}>Только трек</button><button class="secondary" data-request-kind="album" ${kind === 'album' && queued ? 'disabled' : ''}>Весь альбом</button><small>Выбрано: ${kind === 'track' ? 'один трек' : 'весь альбом'}</small></div>`;
}
function candidateHTML(candidate, track) {
  const kind = track.request_kind || 'album';
  return `<div class="candidate">${cover(candidate.cover_url || candidate.album_thumb_url, kind === 'track' ? candidate.release_group_mbid : candidate.musicbrainz_id, true)}<div class="candidate-info">${esc(candidate.title)}<small>${esc(candidate.artist)} ${candidate.year ? '· '+esc(candidate.year) : ''} · ${Math.round(candidate.confidence * 100)}% совпадения ${kind === 'track' ? '· '+esc(candidate.album_title || 'Одиночный трек') : candidate.type_info ? '· '+esc(candidate.type_info) : ''}</small></div><div class="candidate-actions"><button class="secondary" data-preview="${track.id}:${esc(candidate.musicbrainz_id)}" data-preview-candidate="${esc(candidate.musicbrainz_id)}" data-preview-label="Пример" data-preview-name="${esc(candidate.artist + ' — ' + candidate.title)}" aria-label="Прослушать пример: ${esc(candidate.artist)} — ${esc(candidate.title)}">▶ Пример</button><a class="listen-link" href="https://www.youtube.com/results?search_query=${encodeURIComponent(candidate.artist + ' ' + candidate.title)}" target="_blank" rel="noopener noreferrer" aria-label="Найти вариант ${esc(candidate.title)} на YouTube">YouTube ↗</a><button class="secondary" data-approve="${esc(candidate.musicbrainz_id)}">${kind === 'track' ? 'Этот трек' : 'Выбрать'} ↗</button></div></div>`;
}
async function loadTracks() {
  const result = await api(`/api/tracks?status=${encodeURIComponent($('#status-filter').value)}&page=${currentPage}`);
  $('#tracks').innerHTML = result.items.length ? result.items.map(t => `<article class="panel track" data-track="${t.id}"><div class="track-heading">${cover(t.cover_url, t.request_kind === 'track' ? t.candidates.find(c=>c.musicbrainz_id===t.mbid)?.release_group_mbid : t.mbid)}<div class="track-info"><h3>${esc(t.request_kind === 'track' ? t.title : t.album)}</h3><p class="muted">${esc(t.request_kind === 'track' ? (t.track_artist || t.artist) : t.artist)} ${t.year ? '· '+esc(t.year) : ''}</p><p class="muted">${t.request_kind === 'track' ? 'Альбом: '+esc(t.album) : '♡ '+esc(t.title)} · ${names[t.provider] || esc(t.provider)}</p><p class="muted liked-date">${esc(likedDate(t.liked_at))}</p></div><span class="tag ${esc(t.status)}">${statuses[t.status] || esc(t.status)}</span></div>${listenLinks(t)}${requestChoices(t)}${t.error ? `<p class="error-text">${esc(t.error)}</p>` : ''}${t.status === 'review' ? `<div class="candidates">${t.candidates.map(c => candidateHTML(c, t)).join('')}</div><form class="search-row"><label>${t.request_kind === 'track' ? 'Поиск трека в MusicBrainz (исполнитель и название)' : 'Поиск другого альбома'}<input name="query" value="${esc(t.request_kind === 'track' ? (t.track_artist || t.artist) + ' ' + t.title : t.artist + ' ' + t.album)}" required></label><button type="submit" class="secondary">Найти</button></form>` : ''}<div class="track-actions">${['sent','existing','duplicate'].includes(t.status) ? '' : '<button class="secondary" data-track-action="retry">'+(t.status === 'baseline' ? 'Обработать этот лайк' : 'Повторить поиск')+'</button><button class="text" data-track-action="skip">Пропустить</button>'}${t.mbid && ['retry','review'].includes(t.status) ? '<button class="text" data-track-action="allow_resend">Разрешить повторную заявку</button>' : ''}${t.mbid ? `<a class="text" href="https://musicbrainz.org/${t.request_kind === 'track' ? 'recording' : 'release-group'}/${encodeURIComponent(t.mbid)}" target="_blank" rel="noopener noreferrer">MusicBrainz ↗</a>` : ''}</div></article>`).join('') : '<div class="panel empty">Здесь пока нет треков.<br>Подключи аккаунт или выбери другой фильтр.</div>';
  $('#page-info').textContent = `${result.total} треков · страница ${currentPage + 1}`;
  $('#prev').disabled = currentPage === 0;$('#next').disabled = (currentPage + 1) * 30 >= result.total;
  $('#process-baseline').disabled = !state.counts?.baseline;
  previewButtons();
}
function dialog(title, body) {$('#dialog-title').textContent = title;$('#dialog-body').innerHTML = body;$('#connect-dialog').showModal();}
function connection(provider) {
  if (provider === 'yandex') {
    dialog('Яндекс Музыка', `<p>Для чтения лайков нужен токен Яндекс Музыки. Это не пароль от Яндекса. Токен хранится зашифрованным на твоём сервере.</p><p><a href="https://yandex-music.readthedocs.io/en/main/token.html" target="_blank" rel="noopener noreferrer">Как получить токен ↗</a></p><form id="yandex-form"><label>Токен Яндекс Музыки<input name="token" type="password" autocomplete="off" required></label><p>Первый запуск: ${state.settings.first_sync === 'baseline' ? 'старые лайки сохранятся для просмотра, новые пойдут в очередь.' : 'все лайки будут обработаны порциями.'}</p><button class="primary" type="submit">Подключить и проверить</button></form>`);
    $('#yandex-form').onsubmit = e => {e.preventDefault();action($('button',e.target), async () => {await api('/api/accounts/yandex', Object.fromEntries(new FormData(e.target)));$('#connect-dialog').close();toast('Яндекс Музыка подключена');await refresh();});};
  } else if (provider === 'spotify') {
    dialog('Spotify', `<p>Создай приложение в <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener noreferrer">Spotify Developer Dashboard ↗</a>. В development mode владелец приложения должен иметь Premium, а твой аккаунт — быть в списке разрешённых пользователей.</p><label>Redirect URI — скопируй в настройки приложения<input id="spotify-callback" readonly value="${esc(state.callback_url)}"></label><button class="secondary" id="copy-callback">Скопировать адрес</button><form id="spotify-form"><label>Client ID<input name="client_id" value="${esc(state.spotify_client_id)}" autocomplete="off" required></label><p>Запрашиваем только чтение библиотеки. Пароль Spotify вводится на сайте Spotify.</p><button class="primary" type="submit">Войти через Spotify ↗</button></form>`);
    $('#copy-callback').onclick = () => action($('#copy-callback'), async () => {await navigator.clipboard.writeText(state.callback_url);toast('Адрес скопирован');});
    $('#spotify-form').onsubmit = e => {e.preventDefault();action($('button',e.target), async () => {const data = await api('/api/accounts/spotify/start', Object.fromEntries(new FormData(e.target)));window.location.assign(data.url);});};
  } else {
    dialog('Apple Music', `<p>Для MusicKit нужен developer token из Apple Developer Program. После входа выбери плейлист «Любимые песни» — название зависит от языка аккаунта.</p><p><a href="https://developer.apple.com/documentation/applemusicapi/generating-developer-tokens" target="_blank" rel="noopener noreferrer">Как создать developer token ↗</a></p><form id="apple-form"><label>Developer token<input name="developer_token" type="password" autocomplete="off" required></label><button type="button" class="secondary" id="apple-authorize">Войти через Apple Music ↗</button><details><summary>У меня уже есть Music User Token</summary><label>Music User Token<input name="user_token" type="password" autocomplete="off"></label></details><button type="button" id="apple-list" class="secondary">Получить плейлисты</button><label>Плейлист с любимыми треками<select name="playlist_id" required><option value="">Сначала получи плейлисты</option></select></label><button class="primary" type="submit">Подключить выбранный плейлист</button></form>`);
    const form = $('#apple-form');
    $('#apple-authorize').onclick = () => action($('#apple-authorize'), async () => {
      const token = form.elements.developer_token.value.trim();if (!token) throw new Error('Введи developer token');
      if (!window.MusicKit) await new Promise((resolve,reject) => {const s = document.createElement('script');s.src='https://js-cdn.music.apple.com/musickit/v3/musickit.js';s.onload=resolve;s.onerror=() => reject(new Error('Не удалось загрузить MusicKit'));document.head.appendChild(s);});
      await window.MusicKit.configure({developerToken:token,app:{name:'LikeBridge',build:'0.1.1'}});
      form.elements.user_token.value = await window.MusicKit.getInstance().authorize();toast('Вход выполнен. Получи плейлисты и выбери любимые песни.');
    });
    $('#apple-list').onclick = () => action($('#apple-list'), async () => {const data = await api('/api/accounts/apple/playlists', Object.fromEntries(new FormData(form)));form.elements.playlist_id.innerHTML = data.playlists.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');toast(`Найдено плейлистов: ${data.playlists.length}`);});
    form.onsubmit = e => {e.preventDefault();action($('button[type=submit]', form), async () => {await api('/api/accounts/apple',Object.fromEntries(new FormData(form)));$('#connect-dialog').close();toast('Apple Music подключена');await refresh();});};
  }
}
document.addEventListener('click', event => {
  const button = event.target.closest('button');if (!button) return;
  if (button.dataset.view) showView(button.dataset.view).catch(e => toast(e.message));
  if (button.dataset.connect) connection(button.dataset.connect);
  if (button.dataset.disconnect && confirm(`Отключить ${names[button.dataset.disconnect]}? История и файлы сохранятся.`)) action(button, async () => {await api(`/api/accounts/${button.dataset.disconnect}/disconnect`,{});toast('Аккаунт отключён');await refresh();});
  if (button.dataset.preview) {playPreview(button);return;}
  const track = button.closest('[data-track]');
  if (track && button.dataset.requestKind) {
    action(button, async () => {
      const kind = button.dataset.requestKind;
      await api(`/api/tracks/${track.dataset.track}/request`, {request_kind:kind});
      await refresh();toast(kind === 'track' ? 'Одиночный трек добавлен в очередь' : 'Альбом добавлен в очередь');
    });return;
  }
  if (track && (button.dataset.trackAction || button.dataset.approve)) action(button, async () => {
    const operation = button.dataset.approve ? 'approve' : button.dataset.trackAction;
    if(operation === 'allow_resend' && !confirm('Сначала проверь Requests в DroppedNeedle. Если заявки там нет, разрешить повторную отправку?')) return;
    await api(`/api/tracks/${track.dataset.track}/${operation}`, button.dataset.approve ? {mbid:button.dataset.approve} : {});await refresh();toast(operation === 'approve' ? 'Выбор обработан — проверь статус заявки' : 'Очередь обновлена');
  });
});
document.addEventListener('submit', event => {
  if (!event.target.matches('.search-row')) return;event.preventDefault();const form = event.target;
  action($('button',form), async () => {await api(`/api/tracks/${form.closest('[data-track]').dataset.track}/search`,Object.fromEntries(new FormData(form)));await loadTracks();});
});
$('#login-form').onsubmit = e => {e.preventDefault();action($('button',e.target), async () => {try {const data = await api('/api/login',{password:$('#login-password').value,setup_token:$('#setup-token').value});csrf=data.csrf;showApp();await refresh();}catch(error){$('#login-error').textContent=error.message;}});};
$('#logout').onclick = () => action($('#logout'), async () => {stopPreview();await api('/api/logout',{});const data=await api('/api/session');csrf=data.csrf;showLogin(false);});
$('#sync').onclick = () => action($('#sync'), async () => {const data=await api('/api/sync',{});toast(data.message);setTimeout(() => refresh().catch(e=>toast(e.message)),600);});
$('#settings-form').onsubmit = e => {e.preventDefault();action($('button[type=submit]',e.target), async () => {const f=e.target;const result=await api('/api/settings',{interval:Number(f.elements.interval.value),batch_size:Number(f.elements.batch_size.value),first_sync:f.elements.first_sync.value,request_kind:f.elements.request_kind.value,match_threshold:Number(f.elements.match_threshold.value),fallback_track:f.elements.fallback_track.checked,automatic:f.elements.automatic.checked});toast(result.requeued ? `Настройки сохранены. Вернули в обработку: ${result.requeued}` : 'Настройки сохранены');await refresh();});};
$('#destination-form').onsubmit = e => {e.preventDefault();action($('button[type=submit]',e.target), async () => {const data = await api('/api/destination',Object.fromEntries(new FormData(e.target)));e.target.elements.password.value='';e.target.elements.token.value='';toast(`DroppedNeedle подключён${data.role ? ' · роль '+data.role : ''}`);await refresh();});};
$('#password-form').onsubmit = e => {e.preventDefault();action($('button',e.target), async () => {await api('/api/password',Object.fromEntries(new FormData(e.target)));e.target.reset();toast('Пароль изменён');});};
$('#status-filter').onchange = () => {currentPage=0;loadTracks().catch(e=>toast(e.message));};
$('#prev').onclick = () => {currentPage=Math.max(0,currentPage-1);loadTracks().catch(e=>toast(e.message));};
$('#next').onclick = () => {currentPage++;loadTracks().catch(e=>toast(e.message));};
$('#process-baseline').onclick = () => {if(confirm('Добавить все старые лайки в очередь? Альбомы будут обрабатываться порциями по настройкам.'))action($('#process-baseline'),async()=>{const data=await api('/api/baseline/process',{});toast(`В очередь добавлено: ${data.count}`);await refresh();});};
$('#connect-dialog').addEventListener('close',()=>{$('#dialog-body').replaceChildren();});
async function init(){try{const data=await api('/api/session');csrf=data.csrf;if(data.authenticated){showApp();await refresh();if(new URLSearchParams(location.search).get('connection')==='spotify')toast('Spotify подключён');}else showLogin(data.setup_required);}catch(error){toast(error.message);}}
init();setInterval(()=>{if(!document.hidden && !$('#shell').hidden && !$('#connect-dialog').open)refresh().catch(e=>toast(e.message));},15000);
