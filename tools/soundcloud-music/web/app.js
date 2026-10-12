'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="session-token"]').content;
let state = {tracks: [], job: {active: false}}, selected = new Set(), lastRender = '', firstState = true, failureCount = 0, listLimit = 60, settingsAutoSet = false;
const statusNames = {ready: 'Готов к переносу', pending: 'Готов к переносу', idle: 'Готов к переносу', imported: '✓ В «Музыке»', downloaded: '↓ Скачан', downloading: 'Скачиваем…', importing: 'Добавляем…', error: 'Ошибка · повторить', unavailable: 'Недоступен', skipped: 'Пропущен'};
function notice(message, error = false) { $('notice').textContent = message; $('notice').hidden = !message; $('notice').classList.toggle('error', error); }
async function api(path, body) {
  const result = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'Authorization': 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': 'application/json'})}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  let data;
  try { data = await result.json(); } catch { throw new Error('Не удалось прочитать ответ приложения.'); }
  if (!result.ok || data.ok === false || (data.error && !data.tracks)) throw new Error(data.error || 'Не удалось выполнить действие.');
  return data;
}
function duration(seconds) { if (!Number.isFinite(Number(seconds)) || !seconds) return '—'; const s = Math.round(Number(seconds)); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); }
function eligible(track) { return !['imported', 'unavailable', 'skipped'].includes(track.status); }
function filteredTracks() {
  const query = $('search').value.trim().toLocaleLowerCase('ru');
  const filter = $('filter').value;
  const tracks = state.tracks.filter(t => (!query || (t.title + ' ' + t.artist).toLocaleLowerCase('ru').includes(query)) && (filter === 'all' || (filter === 'done' ? t.status === 'imported' : filter === 'error' ? t.status === 'error' : filter === 'unavailable' ? t.status === 'unavailable' : t.status !== 'imported')));
  const sort = $('sort').value;
  if (sort !== 'recent') tracks.sort((a,b) => String(a[sort] || '').localeCompare(String(b[sort] || ''), 'ru'));
  return tracks;
}
function refreshActions() {
  const chosen = state.tracks.filter(t => selected.has(String(t.id)));
  const totalSeconds = chosen.reduce((sum, t) => sum + (Number(t.duration) || 0), 0);
  $('selection-count').textContent = chosen.length ? `Выбрано треков: ${chosen.length}` : 'Выбери треки для переноса';
  $('selection-meta').textContent = chosen.length ? `${Math.round(totalSeconds / 60)} мин музыки · ${$('import').checked ? 'в плейлист «' + ($('playlist').value.trim() || 'Из SoundCloud') + '»' : 'сохранить на Mac'}` : 'Файлы останутся на твоём Mac';
  if (state.job.active && state.job.kind === 'transfer') {
    $('selection-count').textContent = 'Переносим выбранные треки';
    $('selection-meta').textContent = 'В плейлист «' + (state.playlist || $('playlist').value.trim() || 'Из SoundCloud') + '»';
  }
  $('clear').hidden = !chosen.length || state.job.active;
  $('transfer').disabled = !chosen.length || state.job.active || !state.dependencies?.yt_dlp || !state.dependencies?.ffmpeg;
  $('transfer').replaceChildren(document.createTextNode($('import').checked ? 'Скачать и добавить ' : 'Скачать выбранные '), Object.assign(document.createElement('span'), {textContent: '→'}));
  $('transfer').hidden = Boolean(state.job.active);
  $('cancel').hidden = !state.job.active;
  $('load').disabled = Boolean(state.job.active);
  $('empty-load').disabled = Boolean(state.job.active);
  $('profile').disabled = Boolean(state.job.active);
  $('playlist').disabled = Boolean(state.job.active);
  $('import').disabled = Boolean(state.job.active);
  $('cookies').disabled = Boolean(state.job.active);
  $('artwork').hidden = !state.tracks.some(t => t.status === 'imported' && t.artwork && !t.music_artwork);
  $('artwork').disabled = Boolean(state.job.active);
  $('cookies-status').textContent = state.cookies ? 'Cookies SoundCloud подключены' : 'Вход не подключён';
  const visibleEligible = filteredTracks().filter(eligible);
  const count = visibleEligible.filter(t => selected.has(String(t.id))).length;
  $('select-all').checked = visibleEligible.length > 0 && count === visibleEligible.length;
  $('select-all').indeterminate = count > 0 && count < visibleEligible.length;
  $('select-all').disabled = !visibleEligible.length || Boolean(state.job.active);
}
function renderTracks() {
  const tracks = filteredTracks();
  const signature = JSON.stringify([tracks, [...selected], !!state.job.active, listLimit]);
  if (signature === lastRender) { refreshActions(); return; }
  lastRender = signature;
  const fragment = document.createDocumentFragment();
  for (const track of tracks.slice(0, listLimit)) {
    const id = String(track.id);
    const row = document.createElement('div'); row.className = 'track-row' + (selected.has(id) ? ' selected' : '');
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(id); checkbox.disabled = !eligible(track) || !!state.job.active; checkbox.setAttribute('aria-label', 'Выбрать ' + track.title);
    const toggle = () => { if (checkbox.disabled) return; selected.has(id) ? selected.delete(id) : selected.add(id); renderTracks(); };
    checkbox.addEventListener('change', toggle);
    row.addEventListener('click', event => { if (event.target.closest('a,input')) return; toggle(); });
    row.append(checkbox);
    const cover = document.createElement('div'); cover.className = 'cover cover-placeholder'; cover.textContent = '♫';
    if (track.artwork && /^https:\/\//.test(track.artwork)) { const img = document.createElement('img'); img.className = 'cover'; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer'; img.src = track.artwork; img.addEventListener('error', () => img.replaceWith(cover)); row.append(img); } else row.append(cover);
    const info = document.createElement('div'); info.style.minWidth = '0';
    const title = document.createElement('a'); title.className = 'track-title'; title.textContent = track.title || 'Без названия'; title.title = track.title || 'Без названия';
    if (/^https:\/\/soundcloud\.com\//.test(track.url || '')) { title.href = track.url; title.target = '_blank'; title.rel = 'noreferrer noopener'; }
    const artist = document.createElement('div'); artist.className = 'track-artist'; artist.textContent = (track.artist || 'SoundCloud') + (track.music_artwork ? ' · с обложкой' : ''); info.append(title, artist); row.append(info);
    const time = document.createElement('span'); time.className = 'duration'; time.textContent = duration(track.duration); row.append(time);
    const status = document.createElement('span'); status.className = 'status ' + (Object.hasOwn(statusNames, track.status) ? track.status : 'ready'); status.textContent = statusNames[track.status] || 'Готов к переносу'; status.title = track.music_artwork ? 'В «Музыке» с обложкой' : status.textContent; row.append(status);
    if (track.error) { const error = document.createElement('div'); error.className = 'track-error'; error.textContent = track.error; row.append(error); }
    if (track.artwork_error) { const warning = document.createElement('div'); warning.className = 'track-error'; warning.textContent = 'Обложка: ' + track.artwork_error; row.append(warning); }
    fragment.append(row);
  }
  $('track-list').replaceChildren(fragment);
  $('track-count').textContent = state.tracks.length;
  $('visible-count').textContent = tracks.length ? `Выбрать все найденные · ${tracks.length}` : 'Выбрать все';
  $('show-more').hidden = tracks.length <= listLimit;
  $('show-more').textContent = `Показать ещё · осталось ${Math.max(0, tracks.length - listLimit)}`;
  $('empty').hidden = tracks.length > 0;
  const emptyTitle = $('empty').querySelector('h3'), emptyText = $('empty').querySelector('p');
  if (state.job.active && state.job.kind === 'loading') { emptyTitle.textContent = 'Читаем сохранённые треки'; emptyText.textContent = 'Большая коллекция может загружаться несколько минут.'; }
  else if (state.tracks.length) { emptyTitle.textContent = 'Треки не найдены'; emptyText.textContent = 'Попробуй другой поиск или сбрось фильтр.'; }
  else { emptyTitle.textContent = 'Здесь будут твои песни'; emptyText.textContent = 'Вставь ссылку на свой профиль и нажми «Загрузить список», чтобы увидеть Likes.'; }
  $('empty-load').hidden = state.tracks.length > 0 || !!state.job.active;
  $('collection-description').textContent = state.tracks.length ? (state.profile_name ? state.profile_name.replace(/\s*\(Likes\)$/, '') + ' · только те песни, которые хочешь взять с собой.' : 'Только те песни, которые ты хочешь взять с собой.') : 'Загрузи список и выбери, что взять с собой.';
  refreshActions();
}
function renderJob() {
  const job = state.job;
  const completedList = !job.active && job.kind === 'loading' && (job.message || '').startsWith('Найдено треков:');
  $('job-panel').hidden = !job.active && (!job.message || completedList || job.message === 'Готово к работе');
  const current = state.tracks.find(t => String(t.id) === String(job.current));
  $('job-message').textContent = job.active && job.kind === 'transfer' && current?.status === 'imported' && job.message === 'Начинаем…' ? 'Добавляем обложку: ' + current.title : job.message || (job.kind === 'loading' ? 'Читаем коллекцию…' : 'Переносим выбранные треки…');
  $('job-count').textContent = job.total ? `${job.completed || 0} / ${job.total}` : '';
  $('job-progress').max = job.total || 1;
  if (job.active && !job.total) $('job-progress').removeAttribute('value'); else $('job-progress').value = job.completed || 0;
}
async function poll() {
  try {
    state = await api('state'); state.tracks ||= []; state.job ||= {active: false}; failureCount = 0;
    $('connection').textContent = 'На твоём Mac'; $('connection').classList.remove('offline');
    if (firstState) { if (state.profile) $('profile').value = state.profile; if (state.playlist) $('playlist').value = state.playlist; firstState = false; }
    if (state.tracks.length && !settingsAutoSet) { $('connection-settings').open = !matchMedia('(max-width:760px)').matches; settingsAutoSet = true; }
    const ids = new Set(state.tracks.filter(eligible).map(t => String(t.id))); selected = new Set([...selected].filter(id => ids.has(id)));
    renderTracks(); renderJob();
    if (state.dependencies && !state.dependencies.ffmpeg) notice('Не найден ffmpeg. Установи его по инструкции в папке инструмента и перезапусти приложение.', true);
    else if (state.dependencies && !state.dependencies.yt_dlp) notice('Запусти файл «SoundCloud в Музыку.command»: он подготовит компоненты SoundCloud.', true);
  } catch(error) {
    failureCount++; $('connection').textContent = 'Нет связи с приложением'; $('connection').classList.add('offline');
    if (failureCount > 2) { notice('Приложение остановлено. Открой «SoundCloud в Музыку.command», чтобы продолжить.', true); $('transfer').disabled = true; $('load').disabled = true; }
  } finally { setTimeout(poll, state.job.active ? 1000 : 2500); }
}
async function load() {
  if (state.job.active) return;
  if (!$('profile-form').reportValidity()) return;
  $('load').disabled = true; notice('');
  try { await api('load', {url: $('profile').value.trim()}); selected.clear(); state.job = {active: true, kind: 'loading', message: 'Читаем коллекцию SoundCloud…'}; renderTracks(); renderJob(); }
  catch(error) { notice(error.message, true); refreshActions(); }
}
$('profile-form').addEventListener('submit', event => {event.preventDefault(); load();});
$('empty-load').addEventListener('click', load);
for (const id of ['search','filter','sort']) $(id).addEventListener(id === 'search' ? 'input' : 'change', () => { listLimit = 60; renderTracks(); });
$('show-more').addEventListener('click', () => { listLimit += 60; renderTracks(); });
$('select-all').addEventListener('change', () => {const check = $('select-all').checked; for (const t of filteredTracks().filter(eligible)) check ? selected.add(String(t.id)) : selected.delete(String(t.id)); renderTracks();});
$('clear').addEventListener('click', () => {selected.clear(); renderTracks();});
$('playlist').addEventListener('input', refreshActions); $('import').addEventListener('change', refreshActions);
$('transfer').addEventListener('click', async () => {
  if (!selected.size || state.job.active) return;
  $('transfer').disabled = true; notice('');
  try { await api('transfer', {ids: [...selected], playlist: $('playlist').value.trim() || 'Из SoundCloud', import: $('import').checked}); state.job = {active: true, kind: 'transfer', total: selected.size, completed: 0, message: 'Готовим выбранные треки…'}; renderTracks(); renderJob(); }
  catch(error) { notice(error.message, true); refreshActions(); }
});
$('cancel').addEventListener('click', async () => { $('cancel').disabled = true; try { await api('cancel', {}); notice('Останавливаем перенос. Уже добавленные песни останутся в медиатеке.'); } catch(error) { notice(error.message, true); } finally { $('cancel').disabled = false; } });
$('reveal').addEventListener('click', async () => {try {await api('reveal', {});} catch(error) {notice(error.message, true);}});
$('artwork').addEventListener('click', async () => {
  const ids = state.tracks.filter(t => t.status === 'imported' && t.artwork && !t.music_artwork).map(t => String(t.id));
  if (!ids.length || state.job.active) return;
  $('artwork').disabled = true; notice('');
  try { await api('transfer', {ids, playlist: $('playlist').value.trim() || 'Из SoundCloud', import: true}); state.job = {active: true, kind: 'transfer', total: ids.length, completed: 0, message: 'Добавляем обложки…'}; renderTracks(); renderJob(); }
  catch(error) { notice(error.message, true); refreshActions(); }
});
$('cookies').addEventListener('click', async () => { $('cookies').disabled = true; try { await api('choose-cookies', {}); notice('Если файл cookies выбран, загрузи список ещё раз.'); } catch(error) {notice(error.message, true);} finally {$('cookies').disabled = false;} });
matchMedia('(max-width:760px)').addEventListener('change', event => { $('connection-settings').open = !event.matches || !state.tracks.length; });
poll();
