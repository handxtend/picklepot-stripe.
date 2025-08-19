/* PiCo Pickle Pot — working app with Start/End time + configurable Pot Share % + admin UI refresh + auto-load registrations + admin controls + per-entry Hold/Move/Resend + rotating banners + Stripe join + per-event payment method toggles + SUCCESS BANNER */

const SITE_ADMIN_PASS = 'Jesus7';
function isSiteAdmin(){ return localStorage.getItem('site_admin') === '1'; }
function setSiteAdmin(on){ on?localStorage.setItem('site_admin','1'):localStorage.removeItem('site_admin'); }

const $  = (s,el=document)=>el.querySelector(s);
const $$ = (s,el=document)=>[...el.querySelectorAll(s)];
const dollars = n => '$' + Number(n||0).toFixed(2);

// === Stripe backend base (Render Flask app) ===
const API_BASE = "https://picklepot-stripe.onrender.com";

/* ---------- Admin UI ---------- */
function refreshAdminUI(){
  const on = isSiteAdmin();
  $$('.admin-only').forEach(el => { el.style.display = on ? '' : 'none'; });
  const btnLogin  = $('#site-admin-toggle');
  const btnLogout = $('#site-admin-logout');
  const status    = $('#site-admin-status');
  if (btnLogin)  btnLogin.style.display  = on ? 'none' : '';
  if (btnLogout) btnLogout.style.display = on ? '' : 'none';
  if (status)    status.textContent      = on ? 'Admin mode ON' : 'Admin mode OFF';

  if (CURRENT_DETAIL_POT) renderRegistrations(LAST_DETAIL_ENTRIES);
}

/* ---------- SELECT OPTIONS ---------- */
const NAME_OPTIONS = ["GPC April (AL)","GPC September League (SL)","PiCoSO (55+)","BOTP","Other"];
const EVENTS = ["Mixed Doubles","Coed Doubles","Men's Doubles","Women's Doubles","Full Singles (Men)","Full Singles (Women)","Skinny Singles (Coed)","Other"];
const SKILLS = ["Any","2.5 - 3.0","3.25+","Other"];
const LOCATIONS = ["VR Parks& Rec. 646 Industrial Blvd. Villa Rica GA 30180","Other"];
const SKILL_ORDER={ "Any":0, "2.5 - 3.0":1, "3.25+":2 };
const skillRank = s => SKILL_ORDER[s] ?? 0;

/* ---------- Helpers ---------- */
function fillSelect(id, items){
  const el = typeof id==='string'?document.getElementById(id):id;
  el.innerHTML = items.map(v=>`<option>${v}</option>`).join('');
}
function toggleOther(selectEl, wrapEl){ if(!selectEl||!wrapEl) return; wrapEl.style.display = (selectEl.value==='Other')?'':'none'; }
function getSelectValue(selectEl, otherInputEl){ return selectEl.value==='Other'?(otherInputEl?.value||'').trim():selectEl.value; }
function setSelectOrOther(selectEl, wrap, input, val, list){
  if(list.includes(val)){ selectEl.value=val; wrap.style.display='none'; input.value=''; }
  else { selectEl.value='Other'; wrap.style.display=''; input.value=val||''; }
}
function escapeHtml(s){
  return String(s||'').replace(/[&<>"'`=\/]/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','/':'&#47;','`':'&#96;','=':'&#61;'
  }[c]));
}

/* ---------- FIREBASE ---------- */
const db = firebase.firestore();

/* ---------- UI bootstrap ---------- */
document.addEventListener('DOMContentLoaded', () => {
  fillSelect('c-name-select', NAME_OPTIONS);
  fillSelect('c-event', EVENTS);
  fillSelect('c-skill', SKILLS);
  fillSelect('c-location-select', LOCATIONS);
  fillSelect('j-skill', SKILLS);

  // Other toggles (create)
  toggleOther($('#c-name-select'), $('#c-name-other-wrap'));
  $('#c-name-select').addEventListener('change', ()=>toggleOther($('#c-name-select'), $('#c-name-other-wrap')));
  toggleOther($('#c-organizer'), $('#c-org-other-wrap'));
  $('#c-organizer').addEventListener('change', ()=>toggleOther($('#c-organizer'), $('#c-org-other-wrap')));
  toggleOther($('#c-event'), $('#c-event-other-wrap'));
  $('#c-event').addEventListener('change', ()=>toggleOther($('#c-event'), $('#c-event-other-wrap')));
  toggleOther($('#c-skill'), $('#c-skill-other-wrap'));
  $('#c-skill').addEventListener('change', ()=>toggleOther($('#c-skill'), $('#c-skill-other-wrap')));
  toggleOther($('#c-location-select'), $('#c-location-other-wrap'));
  $('#c-location-select').addEventListener('change', ()=>toggleOther($('#c-location-select'), $('#c-location-other-wrap')));

  attachActivePotsListener();

  $('#j-refresh').addEventListener('click', ()=>{ attachActivePotsListener(); onJoinPotChange(); });
  $('#j-pot-select').addEventListener('change', onJoinPotChange);
  $('#j-skill').addEventListener('change', evaluateJoinEligibility);
  $('#j-mtype').addEventListener('change', ()=>{ updateJoinCost(); evaluateJoinEligibility(); });

  $('#j-paytype').addEventListener('change', ()=>{ updateJoinCost(); updatePaymentNotes(); });

  $('#btn-create').addEventListener('click', createPot);
  $('#btn-join').addEventListener('click', joinPot);

  const loadBtn = $('#btn-load');
  if (loadBtn) { loadBtn.disabled = false; loadBtn.addEventListener('click', onLoadPotClicked); }
  const potIdInput = $('#v-pot');
  if (potIdInput) {
    potIdInput.addEventListener('keydown', (e)=>{
      if(e.key === 'Enter'){ e.preventDefault(); onLoadPotClicked(); }
    });
  }
  $('#j-pot-select').addEventListener('change', ()=>{
    const sel = $('#j-pot-select').value;
    if(sel && potIdInput){ potIdInput.value = sel; }
  });

  // Admin header buttons
  $('#site-admin-toggle').addEventListener('click', ()=>{
    const v = prompt('Admin password?');
    if(v===SITE_ADMIN_PASS){ setSiteAdmin(true); refreshAdminUI(); alert('Admin mode enabled.'); }
    else alert('Incorrect password.');
  });
  $('#site-admin-logout').addEventListener('click', ()=>{
    setSiteAdmin(false); refreshAdminUI(); alert('Admin mode disabled.');
  });

  // Admin buttons in Pot Detail
  $('#btn-admin-login')?.addEventListener('click', ()=>{
    const v = prompt('Admin password?');
    if(v===SITE_ADMIN_PASS){ setSiteAdmin(true); refreshAdminUI(); alert('Admin mode enabled.'); }
    else alert('Incorrect password.');
  });
  $('#btn-edit')?.addEventListener('click', enterPotEditMode);
  $('#btn-cancel-edit')?.addEventListener('click', ()=>{ $('#pot-edit-form').style.display='none'; });
  $('#btn-save-pot')?.addEventListener('click', savePotEdits);
  $('#btn-hold')?.addEventListener('click', ()=>updatePotStatus('hold'));
  $('#btn-resume')?.addEventListener('click', ()=>updatePotStatus('open'));
  $('#btn-delete')?.addEventListener('click', deleteCurrentPot);
  $('#btn-admin-grant')?.addEventListener('click', grantThisDeviceAdmin);
  $('#btn-admin-revoke')?.addEventListener('click', revokeThisDeviceAdmin);

  // Per-entry actions delegated
  const tbody = document.querySelector('#adminTable tbody');
  if (tbody){
    tbody.addEventListener('change', async (e)=>{
      const t = e.target;
      if (t && t.matches('input[type="checkbox"][data-act="paid"]')) {
        if(!requireAdmin()) { t.checked = !t.checked; return; }
        const entryId = t.getAttribute('data-id');
        try{
          await db.collection('pots').doc(CURRENT_DETAIL_POT.id)
            .collection('entries').doc(entryId).update({ paid: t.checked });
        }catch(err){
          console.error(err); alert('Failed to update paid status.'); t.checked = !t.checked;
        }
      }
    });
    tbody.addEventListener('click', async (e)=>{
      const btn = e.target.closest('button[data-act]');
      if(!btn) return;
      if(!requireAdmin()) return;
      const act = btn.getAttribute('data-act');
      const entryId = btn.getAttribute('data-id');
      if (act === 'remove'){
        const ok = confirm('Remove this registration?'); if(!ok) return;
        try{
          await db.collection('pots').doc(CURRENT_DETAIL_POT.id)
            .collection('entries').doc(entryId).delete();
        }catch(err){ console.error(err); alert('Failed to remove registration.'); }
        return;
      }
      if (act === 'hold'){
        const next = btn.getAttribute('data-next');
        try{
          await db.collection('pots').doc(CURRENT_DETAIL_POT.id)
            .collection('entries').doc(entryId).update({ status: next });
        }catch(err){ console.error(err); alert('Failed to update status.'); }
        return;
      }
      if (act === 'move'){ openMoveDialog(entryId); return; }
      if (act === 'resend'){ resendConfirmation(entryId); return; }
    });
  }

  refreshAdminUI();
  // NEW: show success banner if returning from Stripe
  checkStripeReturn();
});

/* ---------- Utility: payment methods map ---------- */
function getPaymentMethods(p){
  const pm = p?.payment_methods || {};
  const has = v => v === true;
  return {
    stripe: has(pm.stripe) || false,
    zelle:  has(pm.zelle)  || (!!p?.pay_zelle),
    cashapp:has(pm.cashapp)|| (!!p?.pay_cashapp),
    onsite: has(pm.onsite) || (!!p?.pay_onsite)
  };
}

/* ---------- Create Pot ---------- */
async function createPot(){
  try{
    const uid = firebase.auth().currentUser?.uid;
    if(!uid){ alert('Auth not ready, please try again.'); return; }

    const name = getSelectValue($('#c-name-select'), $('#c-name-other')) || 'Sunday Round Robin';
    const organizer = ($('#c-organizer').value==='Other') ? ($('#c-org-other').value.trim()||'Other') : 'Pickleball Compete';
    const event = getSelectValue($('#c-event'), $('#c-event-other'));
    const skill = getSelectValue($('#c-skill'), $('#c-skill-other'));
    const location = getSelectValue($('#c-location-select'), $('#c-location-other'));
    const buyin_member = Number($('#c-buyin-m').value || 0);
    const buyin_guest  = Number($('#c-buyin-g').value || 0);
    const date = $('#c-date').value || '';
    const time = $('#c-time').value || '';
    const endTime = $('#c-end-time').value || '';

    const pot_share_pct = Math.max(0, Math.min(100, Number($('#c-pot-pct').value || 100)));

    let start_at = null, end_at = null;
    if(date && time){
      const startLocal = new Date(`${date}T${time}:00`);
      start_at = firebase.firestore.Timestamp.fromDate(startLocal);
      if(endTime){
        let endLocal = new Date(`${date}T${endTime}:00`);
        if(endLocal < startLocal){ endLocal = new Date(startLocal.getTime() + 2*60*60*1000); }
        end_at = firebase.firestore.Timestamp.fromDate(endLocal);
      }else{
        const endLocal = new Date(startLocal.getTime() + 2*60*60*1000);
        end_at = firebase.firestore.Timestamp.fromDate(endLocal);
      }
    }

    const allowStripe = ($('#c-allow-stripe')?.value||'no') === 'yes';
    const zelleInfo   = $('#c-pay-zelle')?.value || '';
    const cashInfo    = $('#c-pay-cashapp')?.value || '';
    const onsiteYes   = ($('#c-pay-onsite')?.value||'yes') === 'yes';

    const pot = {
      name, organizer, event, skill, location,
      buyin_member, buyin_guest,
      date, time,
      status:'open',
      ownerUid: uid,
      adminUids: [],
      created_at: firebase.firestore.FieldValue.serverTimestamp(),
      pay_zelle: zelleInfo,
      pay_cashapp: cashInfo,
      pay_onsite: onsiteYes,
      payment_methods: {
        stripe: allowStripe,
        zelle: !!zelleInfo,
        cashapp: !!cashInfo,
        onsite: onsiteYes
      },
      start_at, end_at,
      pot_share_pct
    };

    const docRef = await db.collection('pots').add(pot);
    localStorage.setItem(`owner_${docRef.id}`, '1');
    $('#create-result').innerHTML = `Created! Pot ID: <b>${docRef.id}</b>`;
  }catch(e){
    console.error(e);
    $('#create-result').textContent = 'Failed to create pot.';
  }
}

/* ---------- Active list / Totals ---------- */
let JOIN_POTS_CACHE = [];
let JOIN_POTS_SUB = null;
let CURRENT_JOIN_POT = null;
let JOIN_ENTRIES_UNSUB = null;
let DETAIL_ENTRIES_UNSUB = null;
let LAST_DETAIL_ENTRIES = [];
let CURRENT_DETAIL_POT = null;

function attachActivePotsListener(){
  const sel = $('#j-pot-select');
  if(JOIN_POTS_SUB){ try{JOIN_POTS_SUB();}catch(_){} JOIN_POTS_SUB=null; }
  sel.innerHTML = '';
  JOIN_POTS_CACHE = [];

  JOIN_POTS_SUB = db.collection('pots').where('status','==','open')
    .onSnapshot(snap=>{
      const now = Date.now();
      const pots = [];
      snap.forEach(d=>{
        const x = { id:d.id, ...d.data() };
        const endMs   = x.end_at?.toMillis ? x.end_at.toMillis() : null;
        if(endMs && endMs <= now) return;
        pots.push(x);
      });
      pots.sort((a,b)=>{
        const as = a.start_at?.toMillis?.() ?? 0;
        const bs = b.start_at?.toMillis?.() ?? 0;
        return as-bs;
      });

      JOIN_POTS_CACHE = pots;

      if(!pots.length){
        sel.innerHTML = `<option value="">No open pots</option>`;
        $('#btn-join').disabled = true;
        $('#j-pot-summary-brief').textContent = '—';
        $('#j-started-badge').style.display='none';
        updateBigTotals(0,0);
        return;
      }

      sel.innerHTML = pots.map(p=>{
        const label = [p.name||'Unnamed', p.event||'—', p.skill||'Any'].join(' • ');
        return `<option value="${p.id}">${label}</option>`;
      }).join('');

      if(sel.selectedIndex < 0) sel.selectedIndex = 0;

      const firstId = sel.value;
      if (firstId) { const potIdInput = $('#v-pot'); if(potIdInput) potIdInput.value = firstId; }

      onJoinPotChange();
    }, err=>{
      console.error('pots watch error', err);
      sel.innerHTML = `<option value="">Error loading pots</option>`;
    });
}

function onJoinPotChange(){
  const sel = $('#j-pot-select');
  CURRENT_JOIN_POT = JOIN_POTS_CACHE.find(p=>p.id === sel.value) || null;

  const brief = $('#j-pot-summary-brief');
  const startedBadge = $('#j-started-badge');
  const btn = $('#btn-join');

  const potIdInput = $('#v-pot');
  if (potIdInput && sel.value) potIdInput.value = sel.value;

  if(!CURRENT_JOIN_POT){
    brief.textContent = '—';
    startedBadge.style.display='none';
    btn.disabled = true;
    watchPotTotals(null);
    return;
  }

  const p = CURRENT_JOIN_POT;
  brief.textContent = [p.name||'Unnamed', p.event||'—', p.skill||'Any'].join(' • ');

  const now = Date.now();
  const startMs = p.start_at?.toMillis ? p.start_at.toMillis() : null;
  const endMs   = p.end_at?.toMillis   ? p.end_at.toMillis()   : null;
  const started = startMs && startMs <= now;
  const ended   = endMs && endMs <= now;

  startedBadge.style.display = started && !ended ? '' : 'none';
  btn.disabled = ended;

  updateJoinCost();
  evaluateJoinEligibility();
  updatePaymentOptions();
  updatePaymentNotes();
  watchPotTotals(p.id);

  autoLoadDetailFromSelection();
}

function autoLoadDetailFromSelection(){
  const selId = $('#j-pot-select')?.value;
  if(!selId) return;
  if($('#v-pot')) $('#v-pot').value = selId;
  onLoadPotClicked();
}

/* ---------- Totals ---------- */
function getPotSharePct(potId){
  const fromJoin = JOIN_POTS_CACHE.find(p=>p.id===potId);
  if (fromJoin && typeof fromJoin.pot_share_pct === 'number') return fromJoin.pot_share_pct;
  if (CURRENT_DETAIL_POT && CURRENT_DETAIL_POT.id===potId && typeof CURRENT_DETAIL_POT.pot_share_pct === 'number') return CURRENT_DETAIL_POT.pot_share_pct;
  return 50;
}

function watchPotTotals(potId){
  if(JOIN_ENTRIES_UNSUB){ try{JOIN_ENTRIES_UNSUB();}catch(_){} JOIN_ENTRIES_UNSUB=null; }
  const totalEl = $('#j-pot-total');
  if(!potId){ totalEl.style.display='none'; updateBigTotals(0,0); return; }

  JOIN_ENTRIES_UNSUB = db.collection('pots').doc(potId).collection('entries')
    .onSnapshot(snap=>{
      let totalAll=0, totalPaid=0, countAll=0, countPaid=0;

      snap.forEach(doc=>{
        const d = doc.data();
        const isActive = !d.status || d.status === 'active';
        if (!isActive) return;
        const amt = Number(d.applied_buyin || 0);
        if (amt > 0) {
          totalAll += amt;
          countAll++;
          if (d.paid) { totalPaid += amt; countPaid++; }
        }
      });

      totalEl.innerHTML =
        `Total Pot (All): <b>${dollars(totalAll)}</b> (${countAll} entr${countAll===1?'y':'ies'}) • ` +
        `Paid: <b>${dollars(totalPaid)}</b> (${countPaid} paid)`;
      totalEl.style.display='';

      const pct = getPotSharePct(potId) / 100;
      updateBigTotals(totalPaid*pct, totalAll*pct);
    }, err=>{
      console.error('entries watch failed', err);
      totalEl.textContent = 'Total Pot: (error loading)';
      totalEl.style.display='';
      updateBigTotals(0,0);
    });
}
function updateBigTotals(paidShare, totalShare){
  $('#j-big-paid-amt').textContent  = dollars(paidShare);
  $('#j-big-total-amt').textContent = dollars(totalShare);
}

/* ---------- Join helpers ---------- */
function updateJoinCost(){
  const p = CURRENT_JOIN_POT; if(!p) return;
  const mtype = $('#j-mtype').value;
  const amt = (mtype==='Member'? Number(p.buyin_member||0) : Number(p.buyin_guest||0));
  $('#j-cost').textContent = 'Cost: ' + dollars(amt);
}
function evaluateJoinEligibility(){
  const p=CURRENT_JOIN_POT; if(!p) return;
  const warn = $('#j-warn');
  const playerSkill = $('#j-skill').value;
  const allow = (p.skill==='Any') || ( ({"Any":0,"2.5 - 3.0":1,"3.25+":2}[playerSkill]??0) <= ({"Any":0,"2.5 - 3.0":1,"3.25+":2}[p.skill]??0) );
  warn.style.display = allow ? 'none' : 'block';
  warn.textContent = allow ? '' : 'Higher skill level cannot play down';
}

/* Build payment options per event */
function updatePaymentOptions(){
  const p = CURRENT_JOIN_POT; if(!p) return;
  const pm = getPaymentMethods(p);
  const sel = $('#j-paytype');
  const opts = [];
  if (pm.stripe)  opts.push(`<option value="Stripe">Stripe (card)</option>`);
  if (pm.zelle)   opts.push(`<option value="Zelle">Zelle</option>`);
  if (pm.cashapp) opts.push(`<option value="CashApp">CashApp</option>`);
  if (pm.onsite)  opts.push(`<option value="Onsite">Onsite</option>`);
  sel.innerHTML = opts.join('') || `<option value="">No payment methods available</option>`;
}

/* Notes under payment select */
function updatePaymentNotes(){
  const p = CURRENT_JOIN_POT; const el = $('#j-pay-notes');
  if(!p){ el.style.display='none'; el.textContent=''; return; }
  const t = $('#j-paytype').value;
  const lines=[];
  if(t==='Stripe')  lines.push('Pay securely by card via Stripe Checkout.');
  if(t==='Zelle')   lines.push(p.pay_zelle ? `Zelle: ${p.pay_zelle}` : 'Zelle instructions not provided.');
  if(t==='CashApp') lines.push(p.pay_cashapp ? `CashApp: ${p.pay_cashapp}` : 'CashApp instructions not provided.');
  if(t==='Onsite')  lines.push(p.pay_onsite ? 'Onsite payment accepted at event check-in.' : 'Onsite payment is not enabled for this tournament.');
  el.innerHTML = lines.join('<br>'); el.style.display = lines.length ? '' : 'none';
}

/* ---------- Join (Stripe + others) ---------- */
async function joinPot(){
  const p = CURRENT_JOIN_POT; 
  const btn = $('#btn-join');
  const msg = $('#join-msg');

  function setBusy(on, text){
    if (!btn) return;
    btn.disabled = !!on;
    btn.textContent = on ? (text || 'Working…') : 'Join';
  }
  function fail(message){
    console.error('[JOIN] Error:', message);
    msg.textContent = message || 'Something went wrong.';
    setBusy(false);
  }

  if(!p){ msg.textContent='Select a pot to join.'; return; }

  const now=Date.now(), endMs=p.end_at?.toMillis?.();
  if((endMs && endMs<=now) || p.status==='closed'){
    msg.textContent='Registration is closed for this tournament.'; return;
  }

  const fname=$('#j-fname').value.trim();
  const lname=$('#j-lname').value.trim();
  const email=$('#j-email').value.trim();
  const playerSkill=$('#j-skill').value;
  const member_type=$('#j-mtype').value;
  const pay_type=$('#j-paytype').value;

  if(!fname){ msg.textContent='First name is required.'; return; }
  if(!pay_type){ msg.textContent='Choose a payment method.'; return; }

  const rank = s => ({"Any":0,"2.5 - 3.0":1,"3.25+":2}[s] ?? 0);
  if(p.skill!=='Any' && rank(playerSkill) > rank(p.skill)){
    msg.textContent='Selected skill is higher than pot skill — joining is not allowed.'; 
    return;
  }

  const name=[fname,lname].filter(Boolean).join(' ').trim();
  const applied_buyin=(member_type==='Member'? (p.buyin_member??0) : (p.buyin_guest??0));
  const emailLC = (email||'').toLowerCase(), nameLC = name.toLowerCase();

  try{
    setBusy(true, pay_type==='Stripe' ? 'Redirecting to Stripe…' : 'Joining…');
    msg.textContent = '';

    const entriesRef = db.collection('pots').doc(p.id).collection('entries');

    const dupEmail = emailLC ? await entriesRef.where('email_lc','==', emailLC).limit(1).get() : { empty:true };
    const dupName  = nameLC  ? await entriesRef.where('name_lc','==', nameLC).limit(1).get()  : { empty:true };
    if(!dupEmail.empty || !dupName.empty){ 
      return fail('Duplicate registration: this name or email already joined this event.');
    }

    const entry = {
      name, name_lc:nameLC, email, email_lc:emailLC,
      member_type, player_skill:playerSkill, pay_type,
      applied_buyin, paid:false, status:'active',
      created_at: firebase.firestore.FieldValue.serverTimestamp()
    };
    const docRef = await entriesRef.add(entry);
    const entryId = docRef.id;
    console.log('[JOIN] Entry created', { potId: p.id, entryId });

    if (pay_type === 'Stripe'){
      const pm = getPaymentMethods(p);
      if (!pm.stripe){
        return fail('Stripe is disabled for this event.');
      }

      const amount_cents = Math.round(Number(applied_buyin || 0) * 100);
      if (!Number.isFinite(amount_cents) || amount_cents < 50){
        return fail('Stripe requires a fee of at least $0.50.');
      }

      // Use HTTPS origin if page was opened as file://
      const origin =
        window.location.protocol === 'file:'
          ? 'https://pickleballcompete.com'
          : window.location.origin;

      const payload = {
        pot_id: p.id,
        entry_id: entryId,
        amount_cents,
        player_name: name || 'Player',
        player_email: email || undefined,
        success_url: origin + '/success.html',
        cancel_url:  origin + '/cancel.html',
        method: 'stripe'
      };

      console.log('[JOIN] Creating checkout session…', payload);

      let res, data;
      try{
        res = await fetch(`${API_BASE}/create-checkout-session`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      }catch(networkErr){
        return fail('Network error contacting payment server. Check your internet or CORS.');
      }

      try { data = await res.json(); }
      catch(parseErr){ return fail('Bad response from payment server.'); }

      if (!res.ok || !data?.url){
        const errMsg = data?.error || `Payment server error (${res.status}).`;
        return fail(errMsg);
      }

      // Keep IDs for success page UX
      sessionStorage.setItem('potId', p.id);
      sessionStorage.setItem('entryId', entryId);

      try { window.location.href = data.url; }
      catch { window.open(data.url, '_blank', 'noopener'); }
      return;
    }

    // Non-Stripe:
    setBusy(false);
    msg.textContent='Joined! Complete payment using the selected method.';
    updatePaymentNotes();
    try{ $('#j-fname').value=''; $('#j-lname').value=''; $('#j-email').value=''; }catch(_){}
  }catch(e){
    console.error('[JOIN] Unexpected failure:', e);
    fail('Join failed (check Firebase rules and your network).');
  }
}

/* ---------- Pot Detail loader + registrations subscription ---------- */
async function onLoadPotClicked(){
  let id = ($('#v-pot')?.value || '').trim();
  if(!id){ id = $('#j-pot-select')?.value || ''; }
  if(!id){ alert('Select an active tournament or enter a Pot ID.'); return; }

  const snap = await db.collection('pots').doc(id).get();
  if(!snap.exists){ alert('Pot not found'); return; }

  const pot = { id:snap.id, ...snap.data() };
  CURRENT_DETAIL_POT = pot;

  if($('#v-pot')) $('#v-pot').value = pot.id;

  $('#pot-info').style.display='';
  $('#pi-name').textContent = pot.name||'';
  $('#pi-event').textContent = pot.event||'';
  $('#pi-skill').textContent = pot.skill||'';
  $('#pi-when').textContent = [pot.date||'', pot.time||''].filter(Boolean).join(' ');
  const endLocal = pot.end_at?.toDate?.();
  $('#pi-when-end').textContent = endLocal ? ('Ends: '+ endLocal.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})) : '';
  $('#pi-location').textContent = pot.location||'';
  $('#pi-organizer').textContent = `Org: ${pot.organizer||''}`;
  $('#pi-status').textContent = `Status: ${pot.status||'open'}`;
  $('#pi-id').textContent = `ID: ${pot.id}`;

  subscribeDetailEntries(pot.id);
  if ($('#pot-edit-form')?.style.display === '') prefillEditForm(pot);
}

/* ---------- Registrations table ---------- */
function subscribeDetailEntries(potId){
  if(DETAIL_ENTRIES_UNSUB){ try{DETAIL_ENTRIES_UNSUB();}catch(_){} DETAIL_ENTRIES_UNSUB=null; }
  const tbody = document.querySelector('#adminTable tbody');
  if(!tbody){ return; }
  tbody.innerHTML = `<tr><td colspan="7" class="muted">Loading registrations…</td></tr>`;

  DETAIL_ENTRIES_UNSUB = db.collection('pots').doc(potId).collection('entries')
    .orderBy('created_at','asc')
    .onSnapshot(snap=>{
      LAST_DETAIL_ENTRIES = [];
      snap.forEach(doc=>{
        const d = doc.data();
        LAST_DETAIL_ENTRIES.push({ id: doc.id, ...d });
      });
      renderRegistrations(LAST_DETAIL_ENTRIES);
    }, err=>{
      console.error('registrations watch error', err);
      tbody.innerHTML = `<tr><td colspan="7" class="warn">Failed to load registrations.</td></tr>`;
    });
}

function renderRegistrations(entries){
  const tbody = document.querySelector('#adminTable tbody');
  if(!tbody) return;
  const showEmail = isSiteAdmin();
  const canAdmin  = isSiteAdmin();

  if(!entries || !entries.length){
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No registrations yet.</td></tr>`;
    return;
  }

  const html = entries.map(e=>{
    const name = e.name || '—';
    const email = showEmail ? (e.email || '—') : '';
    const type = e.member_type || '—';
    const buyin = dollars(e.applied_buyin || 0);
    const paidChecked = e.paid ? 'checked' : '';
    const status = (e.status || 'active').toLowerCase();
    const next = status==='hold' ? 'active' : 'hold';
    const holdLabel = status==='hold' ? 'Resume' : 'Hold';

    const actions = canAdmin
      ? `
        <label style="display:inline-flex;align-items:center;gap:6px">
          <input type="checkbox" data-act="paid" data-id="${e.id}" ${paidChecked}/> Paid
        </label>
        <button class="btn" data-act="hold" data-id="${e.id}" data-next="${next}" style="margin-left:6px">${holdLabel}</button>
        <button class="btn" data-act="move" data-id="${e.id}" style="margin-left:6px">Move</button>
        <button class="btn" data-act="resend" data-id="${e.id}" style="margin-left:6px">Resend</button>
        <button class="btn" data-act="remove" data-id="${e.id}" style="margin-left:6px">Remove</button>
      `
      : '—';

    return `
      <tr>
        <td>${escapeHtml(name)}</td>
        <td>${escapeHtml(email)}</td>
        <td>${escapeHtml(type)}</td>
        <td>${buyin}</td>
        <td>${e.paid ? 'Yes' : 'No'}</td>
        <td>${escapeHtml(status)}</td>
        <td>${actions}</td>
      </tr>`;
  }).join('');

  tbody.innerHTML = html;
}

/* ---------- Admin utilities ---------- */
function requireAdmin(){
  if(!isSiteAdmin()){ alert('Admin only. Use Admin Login.'); return false; }
  if(!CURRENT_DETAIL_POT){ alert('Load a pot first.'); return false; }
  return true;
}

function enterPotEditMode(){
  if(!requireAdmin()) return;
  fillSelect('f-name-select', NAME_OPTIONS);
  fillSelect('f-event', EVENTS);
  fillSelect('f-skill', SKILLS);
  fillSelect('f-location-select', LOCATIONS);
  prefillEditForm(CURRENT_DETAIL_POT);
  $('#pot-edit-form').style.display = '';
}

function prefillEditForm(pot){
  if(!pot) return;
  setSelectOrOther($('#f-name-select'), $('#f-name-other-wrap'), $('#f-name-other'), pot.name||'', NAME_OPTIONS);
  const orgSel = $('#f-organizer');
  if (orgSel){
    if (['Pickleball Compete','Other'].includes(pot.organizer)) {
      orgSel.value = pot.organizer;
      $('#wrap-organizer-other').style.display = (pot.organizer==='Other')? '' : 'none';
      if (pot.organizer==='Other') $('#f-organizer-other').value = '';
    } else {
      orgSel.value = 'Other';
      $('#wrap-organizer-other').style.display = '';
      $('#f-organizer-other').value = pot.organizer || '';
    }
  }
  setSelectOrOther($('#f-event'), $('#f-event-other-wrap'), $('#f-event-other'), pot.event||'', EVENTS);
  setSelectOrOther($('#f-skill'), $('#f-skill-other-wrap'), $('#f-skill-other'), pot.skill||'', SKILLS);
  $('#f-buyin-member').value = Number(pot.buyin_member||0);
  $('#f-buyin-guest').value  = Number(pot.buyin_guest||0);

  const pctVal = (typeof pot.pot_share_pct === 'number')
    ? pot.pot_share_pct
    : (typeof pot.potPercentage === 'number' ? pot.potPercentage : 100);
  const fPct = document.getElementById('f-pot-pct');
  if (fPct) fPct.value = pctVal;

  $('#f-date').value = pot.date || '';
  $('#f-time').value = pot.time || '';
  const endLocal = pot.end_at?.toDate?.();
  $('#f-end-time').value = endLocal ? endLocal.toTimeString().slice(0,5) : '';
  setSelectOrOther($('#f-location-select'), $('#f-location-other-wrap'), $('#f-location-other'), pot.location||'', LOCATIONS);

  const pm = getPaymentMethods(pot);
  $('#f-allow-stripe').value = pm.stripe ? 'yes' : 'no';
  $('#f-pay-zelle').value    = pot.pay_zelle || '';
  $('#f-pay-cashapp').value  = pot.pay_cashapp || '';
  $('#f-pay-onsite').value   = pm.onsite ? 'yes' : 'no';

  $('#f-status').value = pot.status || 'open';
}

async function savePotEdits(){
  if(!requireAdmin()) return;
  try{
    const ref = db.collection('pots').doc(CURRENT_DETAIL_POT.id);
    const name = getSelectValue($('#f-name-select'), $('#f-name-other')) || CURRENT_DETAIL_POT.name;
    const organizer = ($('#f-organizer').value==='Other') ? ($('#f-organizer-other').value.trim()||'Other') : $('#f-organizer').value;
    const event = getSelectValue($('#f-event'), $('#f-event-other')) || CURRENT_DETAIL_POT.event;
    const skill = getSelectValue($('#f-skill'), $('#f-skill-other')) || CURRENT_DETAIL_POT.skill;
    const buyin_member = Number($('#f-buyin-member').value || CURRENT_DETAIL_POT.buyin_member || 0);
    const buyin_guest  = Number($('#f-buyin-guest').value  || CURRENT_DETAIL_POT.buyin_guest  || 0);

    let pctRaw = Number(document.getElementById('f-pot-pct')?.value);
    if (!Number.isFinite(pctRaw)) {
      pctRaw = (CURRENT_DETAIL_POT.pot_share_pct ?? CURRENT_DETAIL_POT.potPercentage ?? 100);
    }
    const pot_share_pct = Math.max(0, Math.min(100, pctRaw));

    const date = $('#f-date').value || CURRENT_DETAIL_POT.date || '';
    const time = $('#f-time').value || CURRENT_DETAIL_POT.time || '';
    const endTime = $('#f-end-time').value || '';
    const location = getSelectValue($('#f-location-select'), $('#f-location-other')) || CURRENT_DETAIL_POT.location;

    let end_at = CURRENT_DETAIL_POT.end_at || null;
    if(date && (time || endTime)){
      const startLocal = time ? new Date(`${date}T${time}:00`) : (CURRENT_DETAIL_POT.start_at?.toDate?.() || null);
      if(endTime){
        let endLocal = new Date(`${date}T${endTime}:00`);
        if(startLocal && endLocal < startLocal){ endLocal = new Date(startLocal.getTime() + 2*60*60*1000); }
        end_at = firebase.firestore.Timestamp.fromDate(endLocal);
      }else{
        end_at = null;
      }
    }
    const status = $('#f-status').value || CURRENT_DETAIL_POT.status;

    const allowStripe = ($('#f-allow-stripe')?.value||'no') === 'yes';
    const zelleInfo   = $('#f-pay-zelle')?.value || '';
    const cashInfo    = $('#f-pay-cashapp')?.value || '';
    const onsiteYes   = ($('#f-pay-onsite')?.value||'yes') === 'yes';

    await ref.update({
      name, organizer, event, skill, buyin_member, buyin_guest,
      date, time, location, status, end_at, pot_share_pct,
      pay_zelle: zelleInfo,
      pay_cashapp: cashInfo,
      pay_onsite: onsiteYes,
      payment_methods: {
        stripe: allowStripe,
        zelle: !!zelleInfo,
        cashapp: !!cashInfo,
        onsite: onsiteYes
      }
    });
    $('#pot-edit-form').style.display = 'none';
    alert('Saved.');
    onLoadPotClicked();
  }catch(e){ console.error(e); alert('Failed to save changes.'); }
}

async function updatePotStatus(newStatus){
  if(!requireAdmin()) return;
  try{
    await db.collection('pots').doc(CURRENT_DETAIL_POT.id).update({ status: newStatus });
    alert(`Status updated to ${newStatus}.`);
    onLoadPotClicked();
  }catch(e){ console.error(e); alert('Failed to update status.'); }
}

async function deleteCurrentPot(){
  if(!requireAdmin()) return;
  const go = confirm('This deletes the pot document. Continue?');
  if(!go) return;
  try{
    await db.collection('pots').doc(CURRENT_DETAIL_POT.id).delete();
    alert('Pot deleted.');
    CURRENT_DETAIL_POT = null;
    $('#pot-info').style.display = 'none';
    attachActivePotsListener();
  }catch(e){ console.error(e); alert('Failed to delete pot.'); }
}

async function grantThisDeviceAdmin(){
  if(!requireAdmin()) return;
  try{
    const uid = firebase.auth().currentUser?.uid;
    if(!uid){ alert('No auth UID.'); return; }
    await db.collection('pots').doc(CURRENT_DETAIL_POT.id)
      .update({ adminUids: firebase.firestore.FieldValue.arrayUnion(uid) });
    alert('This device UID granted co-admin.');
  }catch(e){ console.error(e); alert('Failed to grant co-admin.'); }
}
async function revokeThisDeviceAdmin(){
  if(!requireAdmin()) return;
  try{
    const uid = firebase.auth().currentUser?.uid;
    if(!uid){ alert('No auth UID.'); return; }
    await db.collection('pots').doc(CURRENT_DETAIL_POT.id)
      .update({ adminUids: firebase.firestore.FieldValue.arrayRemove(uid) });
    alert('This device UID revoked.');
  }catch(e){ console.error(e); alert('Failed to revoke co-admin.'); }
}

/* ---------- Move & Resend (unchanged) ---------- */
function openMoveDialog(entryId){
  const currentId = CURRENT_DETAIL_POT?.id;
  const options = JOIN_POTS_CACHE
    .filter(p=>p.id!==currentId)
    .map(p=>`<option value="${p.id}">${escapeHtml([p.name,p.event,p.skill].filter(Boolean).join(' • '))}</option>`)
    .join('');
  const html = `
    <div id="move-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;z-index:9999">
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;max-width:520px;width:92%;padding:16px">
        <h3 style="margin:0 0 10px">Move Registration</h3>
        <label style="display:block;margin:6px 0">Target tournament</label>
        <select id="move-target" style="width:100%;padding:10px;border:1px solid #e5e7eb;border-radius:10px">${options||'<option value="">No other open tournaments</option>'}</select>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button id="move-cancel" class="btn">Cancel</button>
          <button id="move-confirm" class="btn primary">Move</button>
        </div>
      </div>
    </div>`;
  document.body.insertAdjacentHTML('beforeend', html);
  $('#move-cancel').onclick = ()=>{ $('#move-overlay')?.remove(); };
  $('#move-confirm').onclick = async ()=>{
    const toPotId = $('#move-target')?.value || '';
    if(!toPotId){ alert('Pick a target tournament.'); return; }
    await moveEntry(entryId, toPotId);
    $('#move-overlay')?.remove();
  };
}

async function moveEntry(entryId, toPotId){
  try{
    const fromPotId = CURRENT_DETAIL_POT.id;
    if(toPotId===fromPotId){ alert('Already in this tournament.'); return; }

    const entry = LAST_DETAIL_ENTRIES.find(e=>e.id===entryId);
    if(!entry){ alert('Entry not found.'); return; }

    const toRef = db.collection('pots').doc(toPotId).collection('entries');
    const emailLC = (entry.email||'').toLowerCase();
    const nameLC  = (entry.name||'').toLowerCase();
    const dupEmail = emailLC ? await toRef.where('email_lc','==', emailLC).limit(1).get() : { empty:true };
    const dupName  = nameLC  ? await toRef.where('name_lc','==', nameLC).limit(1).get()  : { empty:true };
    if(!dupEmail.empty || !dupName.empty){
      alert('Duplicate exists in the target tournament (same name or email).'); return;
    }

    const data = {...entry}; delete data.id;
    data.created_at = firebase.firestore.FieldValue.serverTimestamp();
    data.moved_from = fromPotId;
    data.moved_at   = firebase.firestore.FieldValue.serverTimestamp();

    await toRef.add(data);
    await db.collection('pots').doc(fromPotId).collection('entries').doc(entryId).delete();
    alert('Registration moved.');
  }catch(err){ console.error(err); alert('Failed to move registration.'); }
}

async function resendConfirmation(entryId){
  try{
    const entry = LAST_DETAIL_ENTRIES.find(e=>e.id===entryId);
    if(!entry){ alert('Entry not found.'); return; }
    if(!entry.email){ alert('No email on this registration.'); return; }
    const pot = CURRENT_DETAIL_POT;
    const subject = `Your registration for ${pot?.name||'PiCo Pickle Pot'}`;
    const text =
`Hi ${entry.name||'player'},

This is a confirmation for your registration in:
${pot?.name||''} • ${pot?.event||''} • ${pot?.skill||''}
Date/Time: ${[pot?.date||'', pot?.time||''].filter(Boolean).join(' ')}

Member Type: ${entry.member_type||'-'}
Buy-in: ${dollars(entry.applied_buyin||0)}
Paid: ${entry.paid ? 'Yes' : 'No'}

Thanks for playing!
PiCo Pickle Pot`;

    await db.collection('mail').add({
      to: [entry.email],
      message: { subject, text }
    });
    alert('Resend queued.');
  }catch(err){ console.error(err); alert('Failed to queue resend.'); }
}

/* ---------- Rotating Banners ---------- */
(function(){
  const ROTATE_MS = 20000;
  const FADE_MS = 1200;

  const TOP_BANNERS = [
    { src: 'ads/top_728x90_1.png', url: 'https://pickleballcompete.com' },
    { src: 'ads/top_728x90_2.png', url: 'https://pickleballcompete.com' },
    { src: 'ads/sponsor_728x90.png', url: 'https://pickleballcompete.com' }
  ];
  const BOTTOM_BANNERS = [
    { src: 'ads/bottom_300x250_1.png', url: '' },
    { src: 'ads/bottom_300x250_2.png', url: '' },
    { src: 'ads/sponsor_300x250.png', url: '' }
  ];

  function preload(banners){
    return Promise.all(
      banners.map(b => new Promise(resolve => {
        const img = new Image();
        img.onload = () => resolve(b);
        img.onerror = () => resolve(null);
        img.src = b.src;
      }))
    ).then(list => list.filter(Boolean));
  }

  function createImgEl(){
    const img = document.createElement('img');
    img.alt = 'Sponsor';
    img.style.maxWidth = '100%';
    img.style.height = 'auto';
    img.style.opacity = '0';
    img.style.transition = `opacity ${FADE_MS}ms ease-in-out`;
    return img;
  }

  function setupBanner(wrapperId, metaId){
    const wrap = document.getElementById(wrapperId);
    const meta = document.getElementById(metaId);
    if(!wrap) return null;
    wrap.style.display = '';
    if (meta) meta.style.display = '';

    const a = document.createElement('a');
    a.target = '_blank';
    a.rel = 'noopener';

    const img = createImgEl();
    a.appendChild(img);
    wrap.innerHTML = '';
    wrap.appendChild(a);

    return { img, link: a };
  }

  function startRotator(imgEl, linkEl, banners){
    if(!imgEl || !banners.length) return;
    let i = 0;
    const swap = () => {
      imgEl.style.opacity = '0';
      setTimeout(() => {
        const banner = banners[i % banners.length];
        imgEl.src = banner.src;
        if (banner.url) {
          linkEl.href = banner.url;
          linkEl.style.pointerEvents = 'auto';
          linkEl.style.cursor = 'pointer';
        } else {
          linkEl.href = '#';
          linkEl.style.pointerEvents = 'none';
          linkEl.style.cursor = 'default';
        }
        imgEl.style.opacity = '1';
        i++;
      }, FADE_MS);
    };
    swap();
    if (banners.length > 1) setInterval(swap, ROTATE_MS);
  }

  (async () => {
    const [topList, bottomList] = await Promise.all([preload(TOP_BANNERS), preload(BOTTOM_BANNERS)]);
    const top = setupBanner('ad-top', 'ad-top-meta');
    const bottom = setupBanner('ad-bottom', 'ad-bottom-meta');
    if (top) startRotator(top.img, top.link, topList);
    if (bottom) startRotator(bottom.img, bottom.link, bottomList);
  })();
})();

/* ---------- NEW: Stripe return success banner ---------- */
function checkStripeReturn(){
  const params = new URLSearchParams(location.search);
  const sessionId = params.get('session_id'); // present after successful Checkout
  const banner = $('#pay-banner');
  if (!banner) return;

  if (sessionId){
    // Show a friendly banner immediately
    banner.style.display = '';
    banner.textContent = 'Payment successful! Finalizing your registration… ✅';

    // Try to confirm against Firestore using saved IDs
    const potId = sessionStorage.getItem('potId');
    const entryId = sessionStorage.getItem('entryId');

    if (potId && entryId){
      // Live-listen for paid:true flip (webhook)
      db.collection('pots').doc(potId).collection('entries').doc(entryId)
        .onSnapshot(doc=>{
          const d = doc.data() || {};
          if (d.paid){
            const amt = (typeof d.paid_amount === 'number') ? (d.paid_amount/100) : (d.applied_buyin||0);
            banner.textContent = `Payment successful: ${dollars(amt)} received. Enjoy the event! 🎉`;
            // Auto-hide after a bit
            setTimeout(()=>{ try{ banner.style.display='none'; }catch{} }, 8000);
          } else {
            banner.textContent = 'Payment completed. Waiting for confirmation…';
          }
        }, _err=>{
          banner.textContent = 'Payment completed. (If status doesn’t update, refresh in a few seconds.)';
        });
    }

    // Clean the session_id from the URL for a nicer look
    if (history.replaceState){
      const cleanUrl = location.pathname + location.hash;
      history.replaceState(null, '', cleanUrl);
    }
  }
}

/* ========= Organizer Subscription & Auth (added) ========= */
(function(){
  // Show/hide create card if (signed-in && active subscription) OR site admin.
  let currentUser = null;
  let organizerActive = false;

  function show(el, on){ if(el) el.style.display = on ? '' : 'none'; }

  async function refreshCreateVisibility(){
    const card = document.getElementById('create-card');
    const isAdmin = isSiteAdmin();
    const canCreate = isAdmin || (currentUser && organizerActive);
    show(card, !!canCreate);
    refreshAdminUI();
  }

  async function readOrganizerFlag(uid){
    try{
      const doc = await db.collection('organizers').doc(uid).get();
      return !!doc.exists && !!doc.data()?.active;
    }catch(_){ return false; }
  }

  function setAuthUI(){
    const btnIn   = document.getElementById('btn-signin');
    const btnOut  = document.getElementById('btn-signout');
    const label   = document.getElementById('auth-user');
    if(currentUser){
      if(btnIn)  btnIn.style.display = 'none';
      if(btnOut) btnOut.style.display = '';
      if(label){ label.style.display=''; label.textContent = currentUser.email || '(signed in)'; }
    }else{
      if(btnIn)  btnIn.style.display = '';
      if(btnOut) btnOut.style.display = 'none';
      if(label){ label.style.display='none'; label.textContent = ''; }
    }
  }

  async function checkStripeReturn(){
    try{
      const url = new URL(window.location.href);
      const banner = document.getElementById('pay-banner');
      // Show a banner for joins (existing behavior) or organizer subscription
      const subSuccess = url.searchParams.get('sub') === 'success';
      const joinSuccess = url.pathname.endsWith('/success.html') || url.searchParams.get('join') === 'success';

      if (subSuccess && banner){
        banner.textContent = 'Organizer subscription confirmed. Please sign in to start creating and managing your Pots.';
        banner.style.display='';
        // Persist a hint so we don't need immediate webhook: mark local flag
        if (currentUser){
          try{
            await db.collection('organizers').doc(currentUser.uid).set({
              active: true,
              updated_at: firebase.firestore.FieldValue.serverTimestamp()
            }, { merge: true });
            organizerActive = true;
            refreshCreateVisibility();
          }catch(e){ console.warn('Failed to set organizer active flag', e); }
        }
        // Clean the URL
        url.searchParams.delete('sub');
        window.history.replaceState({}, '', url.toString());
      }else if (joinSuccess && banner){
        banner.textContent = 'Payment received — thanks for joining! Check your email for confirmation.';
        banner.style.display='';
      }
    }catch(e){ /* ignore */ }
  }

  // Public for join code fallback
  window.checkStripeReturn = checkStripeReturn;

  async function startOrganizerSubscription(){
    const btn = document.getElementById('btn-subscribe-organizer');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = 'Redirecting…';
    try{
      const origin =
        window.location.protocol === 'file:'
          ? 'https://pickleballcompete.com'
          : window.location.origin;
      const payload = {
        // attach UID if available (for webhooks to map)
        uid: firebase.auth().currentUser?.uid || null,
        success_url: origin + '?sub=success',
        cancel_url: origin + '?sub=cancel'
      };
      const res = await fetch(`${API_BASE}/create-organizer-subscription`, {
        method: 'POST',
        headers: { 'Content-Type':'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(()=>null);
      if (!res.ok || !data?.url) {
        alert(data?.error || 'Subscription service error.');
        btn.disabled = false; btn.textContent = 'Organizer Subscription';
        return;
      }
      try{ window.location.href = data.url; }
      catch{ window.open(data.url, '_blank', 'noopener'); }
    }catch(err){
      console.error(err);
      alert('Network error starting subscription.');
      btn.disabled = false; btn.textContent = 'Organizer Subscription';
    }
  }

  async function signIn(){
    try{
      const provider = new firebase.auth.GoogleAuthProvider();
      const cred = await firebase.auth().signInWithPopup(provider);
      // After sign-in, check organizer status
      currentUser = cred.user || null;
      organizerActive = currentUser ? await readOrganizerFlag(currentUser.uid) : false;
      setAuthUI();
      refreshCreateVisibility();
    }catch(e){
      console.warn('Sign-in cancelled or failed', e);
    }
  }
  async function signOut(){
    try{ await firebase.auth().signOut(); }
    catch(_){}
  }

  // Bind buttons after DOM ready
  document.addEventListener('DOMContentLoaded', ()=>{
    const btnSub = document.getElementById('btn-subscribe-organizer');
    if (btnSub) btnSub.addEventListener('click', startOrganizerSubscription);

    const btnIn  = document.getElementById('btn-signin');
    const btnOut = document.getElementById('btn-signout');
    if (btnIn)  btnIn.addEventListener('click', signIn);
    if (btnOut) btnOut.addEventListener('click', signOut);

    // Listen for auth state changes
    firebase.auth().onAuthStateChanged(async (user)=>{
      currentUser = user || null;
      organizerActive = currentUser ? await readOrganizerFlag(currentUser.uid) : false;
      setAuthUI();
      refreshCreateVisibility();
      checkStripeReturn();
    });
  });
})();
/* ======== end Organizer Subscription & Auth additions ======== */
