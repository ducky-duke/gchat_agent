"""Embedded JavaScript sources injected into the call page by meet_call_browser.

The RTCPeerConnection hook + getStats/DOM probes that power join / hang-up
detection and the in-browser audio tap. Split out of meet_call_browser for size."""

# Read the live participant roster from the Meet call frame. [data-participant-id]
# renders one element per participant tile; the 'X joined' toast names the joiner.
# Empirically (diag_call_join.py, 2026-06-18) BOTH flip the instant the callee
# answers: tile count 1→2 and the toast appears at the same poll. connectionState is
# NOT a join signal (Meet is SFU-based → it reads 'connected' once WE join the
# server, before any remote), so this DOM roster is the real-time join detector.
_JOIN_PROBE = r"""
(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch(e){ return 0; } };
  // Force a synchronous layout each poll. A throttled/backgrounded renderer can leave the
  // participant tile uncommitted/unlaid-out (tiles read 0 forever → roster-collapse hang-up
  // never fires); touching offsetHeight pokes the renderer (this is what the live diagnostic
  // was incidentally doing when it read tiles=1 where the plain poll read 0).
  try { void (document.body && document.body.offsetHeight); } catch(e){}
  let joined = null;
  try {
    const bt = (document.body ? document.body.innerText : '') || '';
    const m = bt.match(/([^\n]{1,40}?)\s+joined\b/i);
    if (m) joined = m[1].trim().slice(0, 60);
  } catch(e){}
  return {count: q('[data-participant-id]'), joined: joined};
})()
"""


# Live STRUCTURE probe — diagnostic only (--diag-structure). The user confirmed the call
# UI + video render on screen yet [data-participant-id] reads 0 and the audio tap captures
# silence, so the roster selector has DRIFTED and the live media lives somewhere the tap
# isn't looking. This dumps, per frame, counts for many candidate tile selectors + the live
# <video>/<audio> elements + visibilityState, so one observed call reveals the right selector
# and where the live media actually is. Heavy (scans attributes) → diagnostic runs only.
_STRUCTURE_PROBE = r"""
(() => {
  const q = (s) => { try { return document.querySelectorAll(s).length; } catch(e){ return -1; } };
  let playingVids = -1, liveAud = -1, partAttr = -1;
  try { playingVids = [...document.querySelectorAll('video')]
          .filter(v => !v.paused && v.readyState >= 2).length; } catch(e){}
  try {
    let n = 0;
    document.querySelectorAll('audio,video').forEach(el => {
      const s = el.srcObject;
      if (s && s.getAudioTracks) n += s.getAudioTracks().filter(t => t.readyState === 'live').length;
    });
    liveAud = n;
  } catch(e){}
  try {  // any element carrying a 'participant'-ish attribute → hints the current selector
    let n = 0;
    document.querySelectorAll('div,span,c-wiz').forEach(e => {
      for (const a of e.attributes) { if (/participant|allocation|device-id/i.test(a.name)) { n++; break; } }
    });
    partAttr = n;
  } catch(e){}
  return {
    url: location.href.slice(0, 70), vis: document.visibilityState,
    videos: q('video'), playingVids: playingVids, audios: q('audio'), liveAud: liveAud,
    partId: q('[data-participant-id]'), allocIdx: q('[data-allocation-index]'),
    selfName: q('[data-self-name]'), reqId: q('[data-requested-participant-id]'),
    listitem: q('[role=listitem]'), partAttr: partAttr,
  };
})()
"""


# WebRTC join signal — robust when the call tab is BACKGROUNDED. Switching to
# another tab throttles the page's timers + pauses requestAnimationFrame, so the
# DOM roster ('X joined' toast, sometimes the tile) can lag while you work
# elsewhere. The inbound `track` event fires at the media layer regardless of tab
# visibility (it's why Meet audio keeps playing when you tab away), so a growth in
# the remote-track count is the signal that survives backgrounding. Installed as a
# context init-script BEFORE the call iframe loads (wraps RTCPeerConnection to count
# inbound tracks). Proven live in diag_call_join.py: tracks went 3→5 the instant the
# callee answered (two video tracks added), alongside the DOM tile 1→2.
#
# DOUBLE DUTY: when window.__MCB_CAPTURE is set (by --capture-audio in webrtc mode),
# the same `track` handler also taps the inbound AUDIO track — it feeds it to a
# MediaRecorder and pushes base64 chunks onto window.__audioChunks, which the Python
# BrowserAudioTap drains. This captures the REMOTE voice (what the bot hears) cleanly
# at the media layer — NOT the OS output mix — and survives a backgrounded tab.
_WEBRTC_HOOK = r"""
(() => {
  try {
    if (window.__mcb_installed) return;
    window.__mcb_installed = true;
    window.__remoteTracks = 0;       // cumulative inbound tracks → JOIN signal (monotonic)
    window.__pcDead = 0;             // PC reached closed/failed → END signal
    window.__audioChunks = window.__audioChunks || [];
    window.__mcbDiag = window.__mcbDiag || {};
    window.__mcbGen = window.__mcbGen || 0;          // recorder generation (bumps on each (re)start)
    window.__mcbFrameId = window.__mcbFrameId ||      // disambiguates per-frame recorders
        ('f' + Math.floor(Math.random() * 1e9).toString(36));
    window.__mcbCaptureOwner = window.__mcbCaptureOwner || false;  // this frame owns a live recorder

    // Each chunk is tagged "<frameId>:<gen>|<base64>" so the Python drainer groups one
    // recorder's chunks into a single standalone segment and NEVER interleaves two
    // recorders. That kills the truncation bug: a RESTARTED recorder writes a fresh webm
    // header mid-file, so a naive append leaves ffmpeg decoding only the first segment
    // (~3s). Per-segment files are decoded independently then concatenated. gen is fixed
    // per recorder (captured in its closure), so chunks never straddle a generation.
    function pushBlob(blob, gen) {
      if (!blob || !blob.size) return;
      const key = window.__mcbFrameId + ':' + gen;
      const fr = new FileReader();
      fr.onload = () => {
        const s = '' + fr.result; const i = s.indexOf(',');
        window.__audioChunks.push(key + '|' + (i >= 0 ? s.slice(i + 1) : s));
      };
      fr.readAsDataURL(blob);
    }

    window.__mcbPCs = window.__mcbPCs || [];
    window.__mcbConnected = window.__mcbConnected || {};  // track.id -> true (already wired into the graph)

    // The CURRENTLY-live remote audio tracks: read fresh from BOTH the PeerConnections'
    // RECEIVERS and the playing <audio>/<video> elements' srcObject. The track objects
    // handed to `ontrack` die / get superseded as the SFU renegotiates (recording them
    // stopped the recorder <1s in; feeding them to WebAudio gave silence — same root
    // cause), and even getReceivers() can read 0-live mid-call, so we union both sources.
    window.__mcbLiveAudioTracks = function() {
      var out = []; var seen = {};
      function add(t){
        if (t && t.kind === 'audio' && t.readyState === 'live' && !seen[t.id]) { seen[t.id] = 1; out.push(t); }
      }
      (window.__mcbPCs || []).forEach(function(pc){
        try { pc.getReceivers().forEach(function(r){ add(r.track); }); } catch(e){}
      });
      try {
        document.querySelectorAll('audio,video').forEach(function(el){
          try { var s = el.srcObject; if (s && s.getAudioTracks) s.getAudioTracks().forEach(add); } catch(e){}
        });
      } catch(e){}
      return out;
    };

    // IMMORTAL capture graph: one AudioContext + MediaStreamDestination created once.
    // The recorder records the DESTINATION's stream — a synthetic LOCAL track that never
    // ends — so the recorder's lifetime is decoupled from the volatile remote tracks
    // (the bug that killed every prior attempt). Remote audio tracks are merely CONNECTED
    // as sources and reconnected as they churn; the recorder keeps running throughout.
    window.__mcbEnsureGraph = function() {
      if (window.__mcbCtx) return true;
      try {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return false;
        window.__mcbCtx = new AC();
        window.__mcbDest = window.__mcbCtx.createMediaStreamDestination();
      } catch(e) { window.__mcbErr = 'graph:' + e; return false; }
      return true;
    };

    // Idempotent: connect any not-yet-wired live remote audio track to the destination,
    // and start the immortal recorder once at least one source is connected. Python
    // re-pokes this every drain, so newly-unmuted / post-renegotiation tracks get wired
    // as they appear. A track is `muted` (no RTP) until audio flows — we still connect it
    // (the source pulls samples once it unmutes), but only START recording once we have a
    // connected source, so a call with no audio never writes a bogus silent WAV.
    window.__mcbStartRec = function() {
      try {
        if (!window.__MCB_CAPTURE || !window.MediaRecorder) return;
        if (!window.__mcbEnsureGraph()) return;
        try { if (window.__mcbCtx.state === 'suspended') window.__mcbCtx.resume(); } catch(e){}
        var live = window.__mcbLiveAudioTracks();
        window.__mcbDiag.recvLive = live.length;
        window.__mcbDiag.recvUnmuted = live.filter(function(t){ return !t.muted; }).length;
        window.__mcbSinks = window.__mcbSinks || {};  // track.id -> <audio> sink (kept alive)
        live.forEach(function(t){
          if (window.__mcbConnected[t.id]) return;
          try {
            // 🔑 ACTIVATE DECODING: a MediaStreamAudioSourceNode built from a REMOTE WebRTC
            // track outputs SILENCE unless the same track is also attached to a PLAYING media
            // element — Chromium decodes remote audio lazily, only when an element sinks it
            // (proven live 2026-06-19: recvUnmuted=1, recorder 'recording', yet ch=0 / -91dB
            // until this sink was added). muted keeps it off the speakers; we capture via the
            // WebAudio graph, not the element.
            // ONE shared MediaStream for BOTH the decode-activation sink and the WebAudio
            // source. Chromium activates lazy decode of a remote track PER-MediaStream: two
            // separate `new MediaStream([t])` wrappers meant the SINK's stream decoded but the
            // SOURCE's stream stayed silent (proven live 2026-06-19: ICE connected, recvUnmuted=1,
            // inbound RTP 3MB+, recorder 'recording' — yet the WAV was -91dB). Sharing the stream
            // is what feeds the decoded PCM into the capture graph.
            var ms = new MediaStream([t]);
            var sink = new Audio();
            sink.srcObject = ms;
            sink.muted = true;
            try { var pp = sink.play(); if (pp && pp.catch) pp.catch(function(){}); } catch(e){}
            window.__mcbSinks[t.id] = sink;  // retain ref so it isn't GC'd (would stop decode)

            // Capture path 1: tap the shared stream directly.
            var src = window.__mcbCtx.createMediaStreamSource(ms);
            src.connect(window.__mcbDest);
            // Capture path 2 (belt-and-suspenders): route the playing sink ELEMENT through the
            // graph. createMediaElementSource forces the element to decode and re-routes its audio
            // INTO the AudioContext (not the speakers — we connect only to __mcbDest), so even if
            // the raw-stream source above stays lazy, the element pipeline delivers PCM. Mixing
            // both into one destination is harmless (silence + audio = audio).
            try {
              var esrc = window.__mcbCtx.createMediaElementSource(sink);
              esrc.connect(window.__mcbDest);
            } catch(e) { /* element-source unsupported for this srcObject — path 1 still active */ }
            window.__mcbConnected[t.id] = true;
            window.__mcbDiag.connected = (window.__mcbDiag.connected || 0) + 1;
          } catch(e) { window.__mcbErr = 'connect:' + e; }
        });
        window.__mcbDiag.ctxState = window.__mcbCtx.state;
        if (window.__mcbRecorder && window.__mcbRecorder.state === 'recording') return;
        if (!Object.keys(window.__mcbConnected).length) return;  // nothing wired yet
        var mime = 'audio/webm;codecs=opus';
        try { if (!MediaRecorder.isTypeSupported(mime)) mime = 'audio/webm'; } catch(e){ mime = ''; }
        var rec = mime ? new MediaRecorder(window.__mcbDest.stream, {mimeType: mime})
                       : new MediaRecorder(window.__mcbDest.stream);
        window.__audioMime = mime;
        var myGen = ++window.__mcbGen;       // this recorder's generation (1, 2, …)
        window.__mcbCaptureOwner = true;     // this frame owns a recorder → Python drains it
        rec.ondataavailable = function(e){ pushBlob(e.data, myGen); };
        rec.onstart = function(){ window.__mcbDiag.started = (window.__mcbDiag.started||0)+1; };
        rec.onstop  = function(){ window.__mcbDiag.stopped = (window.__mcbDiag.stopped||0)+1; };
        rec.onerror = function(e){ window.__mcbErr = 'onerror:' + ((e&&e.error&&e.error.name)||e); };
        rec.start(1000);  // emit a chunk each second
        window.__mcbDiag.afterStart = rec.state;
        window.__mcbDiag.gen = myGen;
        window.__mcbRecorder = rec;
      } catch(e) { window.__mcbErr = 'startRec:' + e; }
    };

    // Full mid-call inventory — decisive when capture still finds nothing: shows per-PC
    // receiver track states and per-media-element srcObject track states + ctx state.
    window.__mcbInventory = function() {
      var inv = {ctx: (window.__mcbCtx ? window.__mcbCtx.state : null),
                 connected: Object.keys(window.__mcbConnected || {}).length, pcs: [], els: []};
      (window.__mcbPCs || []).forEach(function(pc){
        try {
          var recvs = pc.getReceivers().map(function(r){
            var t = r.track; return t ? {k: t.kind, rs: t.readyState, m: t.muted} : null; });
          // ics/igs reveal the ICE failure MODE: 'checking'→'failed' = no working
          // candidate pair (network/TURN/firewall); 'connected'/'completed' = media
          // DID connect (so a later 'closed' cs is a real hang-up, not a connect failure).
          inv.pcs.push({cs: pc.connectionState, ss: pc.signalingState,
                        ics: pc.iceConnectionState, igs: pc.iceGatheringState, recvs: recvs});
        } catch(e) { inv.pcs.push({err: '' + e}); }
      });
      try {
        document.querySelectorAll('audio,video').forEach(function(el){
          var s = el.srcObject;
          var ats = (s && s.getAudioTracks) ? s.getAudioTracks().map(function(t){
            return {rs: t.readyState, m: t.muted}; }) : [];
          if (ats.length || el.srcObject) inv.els.push({tag: el.tagName, paused: el.paused, ats: ats});
        });
      } catch(e){}
      return inv;
    };

    const O = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (!O) return;
    // Per-PC instrumentation, IDEMPOTENT. Shared by the constructor wrap AND the
    // prototype-method patch below, so a PC is caught no matter HOW it was minted.
    window.__mcbRegisterPC = function(pc) {
      try {
        if (!pc || pc.__mcbReg) return;
        pc.__mcbReg = true;
        window.__mcbPCs.push(pc);  // so __mcbLiveAudioTracks() can read its receivers
        // PC teardown is a reliable END signal: in a 1:1 huddle either side hanging
        // up ends the call → the call UI's PC closes. Survives a backgrounded tab.
        const markDead = () => { window.__pcDead = (window.__pcDead || 0) + 1; };
        pc.addEventListener('connectionstatechange', () => {
          const s = pc.connectionState;
          if (s === 'closed' || s === 'failed') markDead();
        });
        pc.addEventListener('iceconnectionstatechange', () => {
          const s = pc.iceConnectionState;
          if (s === 'closed' || s === 'failed') markDead();
        });
        pc.addEventListener('track', (ev) => {
          window.__remoteTracks++;  // cumulative → JOIN signal (monotonic)
          try {
            const tr = ev && ev.track;
            if (tr && tr.kind === 'audio' && window.__MCB_CAPTURE) {
              // The track is `muted` until RTP flows; start recording when audio is
              // actually arriving (unmute) so the recorder doesn't stop on an empty
              // stream. Python re-pokes __mcbStartRec each drain as the main driver;
              // this listener just reacts faster on the common path.
              tr.addEventListener('unmute', () => {
                window.__mcbDiag.unmuteSeen = (window.__mcbDiag.unmuteSeen || 0) + 1;
                window.__mcbStartRec();
              });
              window.__mcbStartRec();  // in case it's already unmuted
            }
          } catch(e) { window.__mcbErr = 'track:' + e; }
        });
      } catch(e) { window.__mcbErr = 'register:' + e; }
    };
    const W = function(...a) {
      const pc = new O(...a);
      window.__mcbRegisterPC(pc);
      return pc;
    };
    W.prototype = O.prototype;
    window.RTCPeerConnection = W;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = W;
    // 🔑 PROTOTYPE-LEVEL CAPTURE — the decisive fix for catching Meet's LIVE media PC.
    // Wrapping window.RTCPeerConnection only catches PCs built through THAT reference;
    // Google Meet captures the native constructor BEFORE our init-script runs (esp. over
    // CDP into a pre-loaded browser + the cross-origin meet OOPIF), so its real media PC
    // bypasses the wrap. Proven live (run 175205): __mcbPCs held only CLOSED ringback PCs,
    // recvLive=0 throughout, WAV silent — yet the callee confirmed the call connected, so
    // the caller WAS receiving remote audio on a PC the wrap never saw. Every genuine
    // RTCPeerConnection shares O.prototype, so patching these methods registers each
    // instance the first time it touches SDP/media — independent of the minting ctor.
    // Meet renegotiates over the call's life (ICE restarts / track churn), so even a PC
    // created before the patch is caught on its next setRemoteDescription.
    try {
      ['setRemoteDescription','setLocalDescription','addTrack','addTransceiver',
       'createOffer','createAnswer'].forEach(function(m){
        const orig = O.prototype[m];
        if (typeof orig !== 'function') return;
        O.prototype[m] = function() {
          try { window.__mcbRegisterPC(this); } catch(e){}
          return orig.apply(this, arguments);
        };
      });
    } catch(e) { window.__mcbErr = 'protopatch:' + e; }
  } catch(e) {}
})();
"""


# Cumulative INBOUND-RTP bytes, summed over every call PeerConnection's getStats() — the
# MOST ROBUST hang-up signal for this embedded huddle. Everything DOM-based fails here (the
# roster tiles only render when the OS window is visible; the survey + call iframe never
# tear down), and the SFU keeps the PeerConnection `connected` and the receiver tracks
# `live` after the remote leaves — so _webrtc_pc_dead / _webrtc_live_audio never drop. But
# the one thing that genuinely STOPS when the remote leaves is the media itself: no more RTP
# arrives, so bytesReceived FLATLINES. The caller diffs this per poll; a sustained flatline
# after media was flowing = the remote is gone (hang-up). getStats() is async, so the
# expression is an async IIFE (Playwright awaits the returned promise).
# Bare async FUNCTION (not an IIFE expression): Playwright detects `async () => …`, CALLS it,
# and awaits the returned promise. An `(async()=>{})()` IIFE-as-expression is NOT reliably
# awaited by evaluate() — that mis-form made _webrtc_inbound_bytes return -1 every poll, so the
# flatline hang-up signal never engaged (the bug).
_INBOUND_BYTES_FN = r"""
async () => {
  try {
    const pcs = window.__mcbPCs || [];
    let total = 0;
    for (const pc of pcs) {
      try {
        const stats = await pc.getStats();
        stats.forEach(function(r){
          if (r && r.type === 'inbound-rtp') total += (r.bytesReceived || 0);
        });
      } catch(e){}
    }
    return total;
  } catch(e) { return -1; }
}
"""


# Cumulative OUTBOUND-RTP bytes (bytesSent), summed over every call PeerConnection — the
# mirror of _INBOUND_BYTES_FN for the media WE send (the bot mic → callee; e.g. Gemini's
# voice during an AI call). The caller uses this to tell "the callee went silent because
# they're LISTENING to us" (outbound still growing) apart from "the callee LEFT" (both
# directions flatline). Without it, a one-sided AI monologue reads as a hang-up after a
# few seconds of inbound silence — the bug where the call dropped mid-answer while the
# human was just listening. Same async-arrow form as the inbound probe so evaluate() awaits it.
_OUTBOUND_BYTES_FN = r"""
async () => {
  try {
    const pcs = window.__mcbPCs || [];
    let total = 0;
    for (const pc of pcs) {
      try {
        const stats = await pc.getStats();
        stats.forEach(function(r){
          if (r && r.type === 'outbound-rtp') total += (r.bytesSent || 0);
        });
      } catch(e){}
    }
    return total;
  } catch(e) { return -1; }
}
"""


# WHY does the caller's ICE never connect? getStats() candidate-pair / candidate diagnostics.
# For each call PeerConnection this reports the iceConnectionState plus, from getStats:
# the candidate-pair states (succeeded/failed/in-progress), whether any pair was nominated,
# and the TYPE mix of gathered local/remote candidates (host/srflx/relay). The decisive reads:
#   • ics 'checking' with NO succeeded pair  → connectivity failure (network/firewall/TURN).
#   • zero 'relay' candidates + srflx-only failing → no TURN fallback reachable.
#   • a 'succeeded'/nominated pair but cs later 'closed' → media DID connect (real hang-up).
_ICE_STATS_FN = r"""
async () => {
  const out = [];
  const pcs = window.__mcbPCs || [];
  for (const pc of pcs) {
    const e = {ics: pc.iceConnectionState, igs: pc.iceGatheringState,
               pairs: [], loc: {}, rem: {}, sel: null, inB: 0};
    try {
      const stats = await pc.getStats();
      const cands = {};
      stats.forEach(function(r){
        if (!r) return;
        if (r.type === 'inbound-rtp') e.inB += (r.bytesReceived || 0);
        else if (r.type === 'local-candidate' || r.type === 'remote-candidate') cands[r.id] = r;
        else if (r.type === 'candidate-pair') {
          e.pairs.push({st: r.state, nom: !!r.nominated, br: r.bytesReceived || 0});
          if (r.selected || r.nominated) e.sel = r;
        }
      });
      stats.forEach(function(r){
        if (r && r.type === 'local-candidate') e.loc[r.candidateType] = (e.loc[r.candidateType]||0)+1;
        if (r && r.type === 'remote-candidate') e.rem[r.candidateType] = (e.rem[r.candidateType]||0)+1;
      });
      if (e.sel) {
        var L = cands[e.sel.localCandidateId], R = cands[e.sel.remoteCandidateId];
        e.sel = {st: e.sel.state, l: L && L.candidateType, r: R && R.candidateType};
      }
    } catch(err) { e.err = '' + err; }
    out.push(e);
  }
  return out;
}
"""


# Gathers ON-SCREEN text + aria-labels (lowercased, capped) so we can match the survey copy
# regardless of which frame/document it renders in.
# ⚠️ VISIBILITY-AWARE (fixed 2026-06-19): this embedded Chat DM huddle does NOT remove the
# post-call "Rate the meeting …" survey from the DOM after it's dismissed — a stale survey
# from a PRIOR call lingers as HIDDEN nodes. A probe that scanned every [aria-label]
# regardless of visibility therefore reported the survey "always present", so the hang-up
# arm-after-absent guard could never arm and the script rode to the duration cap (the
# long-standing "hang-up never detected" bug). We now collect labels/text ONLY from elements
# that are actually rendered on screen, so a hidden leftover survey is ignored and a freshly
# shown survey (the real hang-up) registers.
_FEEDBACK_PROBE = r"""
(() => {
  try {
    function vis(el) {
      try {
        const r = el.getBoundingClientRect();
        if (r.width <= 1 || r.height <= 1) return false;
        const s = window.getComputedStyle(el);
        if (!s || s.visibility === 'hidden' || s.display === 'none') return false;
        if (parseFloat(s.opacity || '1') === 0) return false;
        return el.offsetParent !== null || s.position === 'fixed';
      } catch(e) { return false; }
    }
    const hay = [];
    try {
      document.querySelectorAll('[aria-label],[role="dialog"],[role="alertdialog"],button').forEach(function(el){
        if (!vis(el)) return;
        const a = (el.getAttribute && el.getAttribute('aria-label')) || '';
        if (a) hay.push(a);
        const t = (el.innerText || el.textContent || '');
        if (t) hay.push(t);
      });
    } catch(e){}
    return hay.join(' \n ').toLowerCase().slice(0, 30000);
  } catch(e) { return ''; }
})()
"""
