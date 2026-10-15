'use strict';
const $ = id => document.getElementById(id);
let ws, identity, state = null, signedIn = false, connected = false, connectedMachine;
let mediaKey, pendingMedia, serverOffset = 0, suppressUntil = 0, autoplayBlocked = false;
let authTimer, authPopup, authServers = [], reconnectTimer, reconnectAttempt = 0;
let pendingRoom = null, intentionalClose = false, noticeTimer;
let requestedRoom = new URLSearchParams(location.search).get('room')?.trim().toUpperCase() || '';
$('code').value = requestedRoom;

function notify(message) {
  $('notice').textContent = message;
  $('notice').classList.add('active');
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => $('notice').classList.remove('active'), 6500);
}
const online = () => ws?.readyState === WebSocket.OPEN;
const host = () => !!state && state.host === state.self;
function send(data) {
  if (!online()) return false;
  ws.send(JSON.stringify(data));
  return true;
}
function controls() {
  const ready = signedIn && online() && !pendingRoom;
  $('roomActions').hidden = !signedIn || !!state;
  $('accountHint').textContent = state ? 'Your Plex identity is how friends see you in this room.' : 'Sign in, then create a room or join your friends.';
  $('create').disabled = $('join').disabled = !ready;
  $('create').textContent = pendingRoom === 'create' ? 'Creating room…' : 'Create a room ↗';
  $('join').textContent = pendingRoom === 'join' ? 'Joining…' : 'Join';
  $('leave').disabled = !online() || pendingRoom === 'leave';
  $('copy').disabled = !state;
  $('video').controls = online() && host();
}
function roomAction(type) {
  if (pendingRoom || !signedIn || !online() || state) return;
  const code = $('code').value.replace(/\s/g, '').toUpperCase();
  if (type === 'join' && !/^[0-9A-F]{8}$/.test(code)) return notify('Enter the 8-character room code.');
  pendingRoom = type;
  controls();
  send({type, code});
}
$('create').onclick = () => roomAction('create');
$('joinForm').onsubmit = event => { event.preventDefault(); roomAction('join'); };
$('leave').onclick = () => {
  if (!state || pendingRoom || !online()) return;
  pendingRoom = 'leave'; controls(); send({type:'leave'});
};
$('libraries').onclick = () => { if (!send({type:'browse'})) notify('Reconnecting. Try again in a moment.'); };
$('copy').onclick = async () => {
  if (!state) return;
  const invite = new URL(location.href); invite.search = ''; invite.searchParams.set('room', state.code);
  try { await navigator.clipboard.writeText(invite.href); notify('Room invite copied.'); }
  catch { notify('Share this room code: ' + state.code); }
};
function resetVideo() {
  suppressUntil = performance.now() + 600;
  mediaKey = pendingMedia = null;
  autoplayBlocked = false;
  $('video').pause(); $('video').removeAttribute('src'); $('video').load();
  $('empty').hidden = false; $('resume').hidden = true;
  $('emptyTitle').textContent = 'Your evening starts here.';
  $('emptyDescription').textContent = state ? 'The host can choose a movie from the library.' : 'Sign in, create a room or join your friends.';
}
function clearRoom() {
  state = null; pendingRoom = null; requestedRoom = '';
  resetVideo(); document.body.classList.remove('in-room');
  $('roomInfo').hidden = $('roomHeader').hidden = true;
  $('members').replaceChildren();
  $('title').textContent = 'Nothing queued yet'; $('role').textContent = 'WAITING FOR A ROOM';
  document.title = 'SyncRoom · Nocturne';
  const url = new URL(location.href); url.searchParams.delete('room'); history.replaceState(null, '', url);
  controls();
}
function render(s) {
  const entering = !state;
  state = s; pendingRoom = null; reconnectAttempt = 0;
  serverOffset = s.serverTime - Date.now()/1000;
  document.body.classList.add('in-room');
  if (entering) window.scrollTo({top:0, left:0});
  $('roomHeader').hidden = $('roomInfo').hidden = false;
  $('roomCode').textContent = s.code;
  $('role').textContent = host() ? 'YOU HOST THIS ROOM' : 'FOLLOWING THE HOST';
  $('title').textContent = s.media?.title || 'Choose something to watch';
  document.title = s.code + ' · SyncRoom';
  const url = new URL(location.href); url.searchParams.set('room', s.code); history.replaceState(null, '', url);
  const totalTabs = s.members.reduce((sum, member) => sum + member.tabs, 0);
  $('memberCount').textContent = s.members.length;
  $('peopleSummary').textContent = s.members.length + (s.members.length === 1 ? ' person' : ' people') + ' · ' + totalTabs + (totalTabs === 1 ? ' active tab' : ' active tabs');
  $('members').replaceChildren(...s.members.map(member => {
    const row = document.createElement('li'); row.className = 'person';
    const avatar = document.createElement('span'); avatar.className = 'avatar';
    avatar.textContent = member.name.trim().split(/\s+/).map(part => [...part][0] || '').slice(0,2).join('').toUpperCase() || '?';
    const details = document.createElement('div'); details.className = 'person-details';
    const name = document.createElement('div'); name.className = 'person-name'; name.textContent = member.name;
    if (member.id === s.self) { const you = document.createElement('span'); you.className = 'you-label'; you.textContent = 'You'; name.append(you); }
    const status = document.createElement('div'); status.className = 'person-status';
    status.textContent = member.online ? 'Online' + (member.tabs > 1 ? ' · ' + member.tabs + ' tabs' : '') : 'Reconnecting…';
    details.append(name, status);
    row.append(avatar, details);
    if (member.id === s.host) { const badge = document.createElement('span'); badge.className = 'host-badge'; badge.textContent = 'HOST'; row.append(badge); }
    row.classList.toggle('reconnecting', !member.online);
    return row;
  }));
  $('roomStatus').textContent = '● In room';
  if (!s.media) {
    $('emptyTitle').textContent = 'Your room is ready.';
    $('emptyDescription').textContent = host() ? 'Choose a movie below. Invite your friends to watch with you.' : 'Waiting for the host to choose a movie.';
  }
  controls(); requestStream(); sync();
}
function requestStream() {
  if (!state?.media || !connected || !online()) return;
  const key = state.media.machine + ':' + state.media.id;
  if (connectedMachine && connectedMachine !== state.media.machine) {
    if (pendingMedia !== key) notify('Choose the host’s Plex server under Your Plex server.');
    pendingMedia = key; return;
  }
  if (mediaKey === key || pendingMedia === key) return;
  resetVideo(); pendingMedia = key;
  $('emptyTitle').textContent = 'Loading ' + state.media.title + '…';
  $('emptyDescription').textContent = 'Preparing your stream. Each viewer uses their own Plex access.';
  send({type:'stream'});
}
function sync() {
  const video = $('video');
  if (!online() || !state?.media || !video.src || video.readyState < 1) return;
  const expectedKey = state.media.machine + ':' + state.media.id;
  if (mediaKey !== expectedKey) return;
  const target = state.position + (state.playing ? Math.max(0, Date.now()/1000 + serverOffset - state.updated) : 0);
  suppressUntil = performance.now() + 400;
  if (!video.seeking && Math.abs(video.currentTime - target) > 1.2) video.currentTime = Math.min(target, Number.isFinite(video.duration) ? video.duration : target);
  if (state.playing && video.paused && !autoplayBlocked) video.play().catch(() => {
    autoplayBlocked = true; $('resume').hidden = false;
  });
  if (!state.playing && !video.paused) video.pause();
  if (!state.playing) { autoplayBlocked = false; $('resume').hidden = true; }
}
function publishPlayback() {
  if (online() && host() && state?.media && performance.now() > suppressUntil && mediaKey === state.media.machine + ':' + state.media.id) {
    send({type:'playback', position:$('video').currentTime, playing:!$('video').paused});
  }
}
for (const event of ['play','pause','seeked']) $('video').addEventListener(event, publishPlayback);
$('video').addEventListener('loadedmetadata', sync);
$('video').addEventListener('error', () => { if ($('video').getAttribute('src')) notify('This file could not play. Check Plex access and browser codec support.'); });
$('resume').onclick = () => {
  if (!state?.playing) return;
  autoplayBlocked = false; suppressUntil = performance.now() + 400;
  $('video').play().then(() => { $('resume').hidden = true; }).catch(() => notify('Playback failed. Check the file format and Plex connection.'));
};
setInterval(() => {
  if (host() && !$('video').paused) publishPlayback(); else if (!host()) sync();
}, 3000);

function stopAuth() {
  clearInterval(authTimer); authTimer = null;
  $('authPending').hidden = true; $('authLink').removeAttribute('href'); $('plexSignIn').disabled = false;
  if (authPopup && !authPopup.closed) authPopup.close();
  authPopup = null;
}
$('plexSignIn').onclick = () => {
  if (!online()) return notify('Reconnecting. Try again in a moment.');
  stopAuth();
  authPopup = window.open('about:blank', 'syncroom-plex', 'width=700,height=750');
  if (authPopup) authPopup.opener = null;
  $('plexSignIn').disabled = true;
  send({type:'auth_start', remember:$('remember').checked});
};
$('cancelAuth').onclick = () => { stopAuth(); send({type:'auth_cancel'}); };
$('signOut').onclick = () => { intentionalClose = true; send({type:'auth_signout'}); };
function endpoints() {
  const server = authServers.find(s => s.id === $('plexServer').value);
  const automatic = document.createElement('option'); automatic.value = ''; automatic.textContent = 'Automatic';
  $('plexEndpoint').replaceChildren(automatic, ...(server?.connections || []).map(c => {
    const option = document.createElement('option'); option.value = c.uri;
    option.textContent = (c.relay ? 'Relay' : c.local ? 'Local network' : 'Remote') + ' · ' + c.uri;
    return option;
  }));
  $('useServer').disabled = !server;
}
$('plexServer').onchange = endpoints;
$('useServer').onclick = () => {
  if (!online() || $('useServer').disabled) return;
  connected = false; connectedMachine = null; resetVideo();
  $('useServer').disabled = true; $('plexStatus').textContent = 'Connecting to Plex server…';
  send({type:'auth_select', id:$('plexServer').value, uri:$('plexEndpoint').value});
};
function handleAuth(data) {
  if (data.type === 'auth_cancelled') stopAuth();
  if (data.type === 'auth_guest') {
    $('authLoading').hidden = true; $('plexSignIn').hidden = false; $('remember').closest('label').hidden = false;
    signedIn = false; if (state) clearRoom(); controls();
  }
  if (data.type === 'auth_started') {
    $('authPending').hidden = false; $('authLink').href = data.url;
    if (authPopup && !authPopup.closed) authPopup.location = data.url;
    authTimer = setInterval(() => send({type:'auth_check'}), 2200);
  }
  if (data.type === 'auth_signed_in') {
    stopAuth(); $('authLoading').hidden = true;
    signedIn = true; $('account').hidden = false; $('signOut').hidden = false;
    $('plexSignIn').hidden = true; $('remember').closest('label').hidden = true;
    $('accountName').textContent = 'Watching as ' + data.name;
    $('plexStatus').textContent = data.remembered ? 'Plex sign-in remembered.' : 'Signed in for this visit.';
    authServers = data.servers;
    $('plexServer').replaceChildren(...authServers.map(s => { const option = document.createElement('option'); option.value = s.id; option.textContent = s.name; return option; }));
    if (data.selected && authServers.some(s => s.id === data.selected)) $('plexServer').value = data.selected;
    endpoints(); controls();
    const target = requestedRoom || state?.code;
    if (target && !pendingRoom) { $('code').value = target; if (!state) roomAction('join'); else send({type:'join',code:target}); }
    if ((data.selected && authServers.some(s => s.id === data.selected)) || authServers.length === 1) {
      if (data.uri && [...$('plexEndpoint').options].some(o => o.value === data.uri)) $('plexEndpoint').value = data.uri;
      $('useServer').click();
    } else if (!authServers.length) notify('No Plex servers are available to this account.');
  }
  if (data.type === 'auth_error') {
    stopAuth(); signedIn = connected = false; resetVideo();
    $('authLoading').hidden = true; $('account').hidden = true; $('signOut').hidden = true;
    $('plexSignIn').hidden = false; $('remember').closest('label').hidden = false;
    $('plexStatus').textContent = 'Please sign in again.'; controls(); notify(data.message);
  }
  if (data.type === 'auth_signed_out') { intentionalClose = true; stopAuth(); location.reload(); }
}
function message(data) {
  if (data.type === 'hello') identity = data.id;
  if (data.type === 'auth_ready') send({type:'auth_resume'});
  handleAuth(data);
  if (data.type === 'error') {
    if (data.action === 'join' && state) clearRoom();
    if (['create','join','leave'].includes(data.action)) {
      pendingRoom = null; if (!state) requestedRoom = ''; controls();
      if (!state && data.action === 'join') {
        const url = new URL(location.href); url.searchParams.delete('room'); history.replaceState(null,'',url);
      }
    }
    if (data.action === 'stream') pendingMedia = null;
    if (data.action === 'auth_select') endpoints();
    if (data.action === 'auth_resume') { $('authLoading').hidden = true; $('plexSignIn').hidden = false; }
    if (['auth_start','auth_check'].includes(data.action)) stopAuth();
    notify(data.message);
  }
  if (data.type === 'state') render(data);
  if (data.type === 'connected') {
    connected = true; connectedMachine = data.machine; pendingMedia = null;
    $('plexStatus').textContent = data.name ? 'Connected to ' + data.name : 'Plex connected';
    endpoints(); send({type:'browse'}); requestStream();
  }
  if (data.type === 'stream' && state?.media && data.id === state.media.id && data.machine === state.media.machine) {
    mediaKey = data.machine + ':' + data.id; pendingMedia = null;
    $('empty').hidden = true; suppressUntil = performance.now() + 600; $('video').src = data.url;
  }
  if (data.type === 'left') clearRoom();
  if (data.type === 'library') $('library').replaceChildren(...data.items.map(item => {
    const button = document.createElement('button'); button.className = 'tile'; button.textContent = item.title;
    const small = document.createElement('small'); small.textContent = item.kind || 'Library'; button.append(small);
    button.onclick = () => {
      if (!data.section) send({type:'browse', section:item.id});
      else if (item.kind === 'movie' || item.kind === 'episode') {
        if (host()) send({type:'select',id:item.id}); else notify('The host chooses what to watch.');
      } else notify('Choose a movie library. Episode browsing is not available yet.');
    };
    return button;
  }));
}
async function connectSocket() {
  clearTimeout(reconnectTimer);
  if (reconnectAttempt > 0) {
    try { await fetch('/', {cache:'no-store', credentials:'same-origin'}); }
    catch { reconnectTimer = setTimeout(connectSocket, Math.min(1000 * 2 ** reconnectAttempt++, 8000)); return; }
  }
  const socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
  ws = socket;
  socket.onopen = () => { if (ws !== socket) return; $('connection').textContent = '● Connected'; controls(); };
  socket.onmessage = event => { if (ws === socket) message(JSON.parse(event.data)); };
  socket.onclose = () => {
    if (ws !== socket || intentionalClose) return;
    clearInterval(authTimer); pendingRoom = null; connected = false;
    suppressUntil = performance.now() + 600; $('video').pause();
    $('connection').textContent = '○ Reconnecting…'; $('roomStatus').textContent = '○ Reconnecting…'; controls();
    reconnectTimer = setTimeout(connectSocket, Math.min(1000 * 2 ** reconnectAttempt++, 8000));
  };
}
connectSocket();
