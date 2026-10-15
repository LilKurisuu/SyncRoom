'use strict';
const $ = id => document.getElementById(id);
let ws, identity, state, mediaId, applying = false, connected = false, serverOffset = 0;
const notify = message => { $('notice').textContent = message; $('notice').classList.add('active'); };
function send(data) { if(ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data)); else notify('Connection lost. Reload to reconnect.'); }
function host() { return state?.host === identity; }
function roomAction(type) { send({type, code:$('code').value}); }
$('create').onclick = () => roomAction('create');
$('join').onclick = () => roomAction('join');
$('leave').onclick = () => send({type:'leave'});
$('libraries').onclick = () => send({type:'browse'});
$('copy').onclick = async () => { try { await navigator.clipboard.writeText(location.origin+'/?room='+state.code); notify('Invite copied.'); } catch { notify('Share this room code: '+state.code); } };
$('resume').onclick = () => { if(state?.playing) $('video').play().catch(()=>notify('Playback failed. Check the video format and Plex connection.')); else notify('Playback is paused by the host.'); };
function render(s) {
 state=s; serverOffset=s.serverTime-Date.now()/1000; $('roomInfo').hidden=false; $('roomCode').textContent=s.code;
 $('create').disabled=$('join').disabled=true;
 $('role').textContent=host()?'YOU ARE THE HOST':'FOLLOWING THE HOST';
 $('title').textContent=s.media?.title || 'Nothing queued yet';
 $('members').replaceChildren(...s.members.map(m=>{const li=document.createElement('li');li.textContent=m.name+(m.id===s.host?' · host':'');return li;}));
 $('video').controls=host();
 if(s.media && s.media.id!==mediaId && connected) send({type:'stream'});
 sync();
}
function sync(){
 const v=$('video'); if(!state?.media || !v.src || v.readyState<1) return;
 applying=true;
 const target=state.position+(state.playing?Math.max(0,Date.now()/1000+serverOffset-state.updated):0);
 if(Math.abs(v.currentTime-target)>1.2) v.currentTime=Math.min(target,Number.isFinite(v.duration)?v.duration:target);
 if(state.playing) v.play().catch(()=>notify('Click Enable playback to allow playback in this browser.')); else v.pause();
 setTimeout(()=>{applying=false;},200);
}
for(const event of ['play','pause','seeked']) $('video').addEventListener(event,()=>{ if(host()&&!applying&&state?.media&&mediaId===state.media.id) send({type:'playback',position:$('video').currentTime,playing:!$('video').paused}); });
$('video').addEventListener('loadedmetadata',sync);
$('video').addEventListener('error',()=>notify('This media could not play. Check Plex access, HTTPS compatibility and browser codec support.'));
setInterval(()=>{if(host()&&state?.media&&!$('video').paused&&!applying) send({type:'playback',position:$('video').currentTime,playing:true}); else if(!host()) sync();},3000);
ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
ws.onopen=()=>{$('connection').textContent='● Connected';};
ws.onclose=()=>{clearInterval(authTimer); $('connection').textContent='○ Offline';$('video').pause();notify('Room connection closed. Reload to reconnect.');};
ws.onmessage=e=>{
 const data=JSON.parse(e.data);
 if(data.type==='hello') identity=data.id;
 if(data.type==='auth_ready') send({type:'auth_resume'});
 handleAuth(data);
 if(data.type==='error') notify(data.message);
 if(data.type==='state') render(data);
 if(data.type==='connected') { connected=true; $('plexStatus').textContent=data.name ? 'Connected to '+data.name : 'Plex connected';$('signOut').hidden=false; send({type:'browse'}); if(state?.media) send({type:'stream'}); }
 if(data.type==='stream' && data.id===state?.media?.id) { mediaId=data.id;$('empty').hidden=true;applying=true;$('video').src=data.url; }
 if(data.type==='left') {state=null;mediaId=null;$('video').pause();$('video').removeAttribute('src');$('video').load();$('empty').hidden=false;$('roomInfo').hidden=true;$('create').disabled=$('join').disabled=!signedIn;$('title').textContent='Nothing queued yet';$('role').textContent='WAITING FOR A ROOM';}
 if(data.type==='library') $('library').replaceChildren(...data.items.map(item=>{const b=document.createElement('button');b.className='tile';b.textContent=item.title;const small=document.createElement('small');small.textContent=item.kind||'Library';b.append(small);b.onclick=()=>{if(!data.section)send({type:'browse',section:item.id});else if(item.kind==='movie'||item.kind==='episode'){if(host())send({type:'select',id:item.id});else notify('Ask the host to select a movie.');}else notify('Episode browsing is not available yet. Choose a movie library.');};return b;}));
};
$('code').value=new URLSearchParams(location.search).get('room')||'';

let authTimer, authPopup, authServers = [], signedIn = false;
function stopAuth() {
 clearInterval(authTimer); authTimer=null;
 $('authPending').hidden=true; $('authLink').removeAttribute('href');
 $('plexSignIn').disabled=false;
 if(authPopup && !authPopup.closed) authPopup.close();
 authPopup=null;
}
$('plexSignIn').onclick=()=>{
 if(ws?.readyState!==WebSocket.OPEN) return notify('Connect to SyncRoom first.');
 stopAuth();
 authPopup=window.open('about:blank','syncroom-plex','width=700,height=750');
 if(authPopup) authPopup.opener=null;
 $('plexSignIn').disabled=true;
 send({type:'auth_start',remember:$('remember').checked});
};
$('cancelAuth').onclick=()=>{stopAuth();send({type:'auth_cancel'});};
$('signOut').onclick=()=>send({type:'auth_signout'});
function endpoints(){
 const server=authServers.find(s=>s.id===$('plexServer').value);
 const automatic=document.createElement('option');automatic.value='';automatic.textContent='Automatic';
 $('plexEndpoint').replaceChildren(automatic,...(server?.connections||[]).map(c=>{const option=document.createElement('option');option.value=c.uri;option.textContent=(c.relay?'Relay':c.local?'Local network':'Remote')+' · '+c.uri;return option;}));
 $('useServer').disabled=!server;
}
$('plexServer').onchange=endpoints;
$('useServer').onclick=()=>send({type:'auth_select',id:$('plexServer').value,uri:$('plexEndpoint').value});
function handleAuth(data){
 if(data.type==='auth_cancelled') stopAuth();
 if(data.type==='auth_started'){
  $('authPending').hidden=false;$('authLink').href=data.url;
  if(authPopup && !authPopup.closed) authPopup.location=data.url;
  authTimer=setInterval(()=>send({type:'auth_check'}),2200);
 }
 if(data.type==='auth_signed_in'){
  stopAuth();connected=false;mediaId=null;$('video').pause();$('video').removeAttribute('src');$('video').load();$('empty').hidden=false;
  $('account').hidden=false;$('signOut').hidden=false;$('plexSignIn').hidden=true;$('remember').closest('label').hidden=true;
  signedIn=true;$('roomActions').hidden=false;
  $('create').disabled=$('join').disabled=!!state;
  $('accountName').textContent='Watching as '+data.name;
  $('plexStatus').textContent=data.remembered?'Sign-in remembered on this browser.':'Signed in for this visit.';
  authServers=data.servers;
  $('plexServer').replaceChildren(...authServers.map(s=>{const option=document.createElement('option');option.value=s.id;option.textContent=s.name;return option;}));
  if(data.selected && authServers.some(s=>s.id===data.selected)) $('plexServer').value=data.selected;
  endpoints();
  if(!authServers.length) notify('No Plex servers are available to this account. Check your Plex sharing access.');
  if((data.selected && authServers.some(s=>s.id===data.selected)) || authServers.length===1){
   if(data.uri && [...$('plexEndpoint').options].some(o=>o.value===data.uri)) $('plexEndpoint').value=data.uri;
   $('useServer').click();
  }
 }
 if(data.type==='auth_error'){
  stopAuth();connected=false;mediaId=null;$('video').pause();$('video').removeAttribute('src');$('video').load();$('empty').hidden=false;
  signedIn=false;$('roomActions').hidden=true;$('create').disabled=$('join').disabled=true;
  $('account').hidden=true;$('plexSignIn').hidden=false;$('remember').closest('label').hidden=false;
  $('plexStatus').textContent='Please sign in again.';notify(data.message);
 }
 if(data.type==='auth_signed_out'){stopAuth();location.reload();}
 if(data.type==='error' && authTimer){stopAuth();notify(data.message);}
 if(data.type==='error' && $('plexSignIn').disabled){stopAuth();notify(data.message);}
}
