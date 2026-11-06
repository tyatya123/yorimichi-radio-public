"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const music = $("music-audio");
  const voiceAudio = $("talk-audio");
  const synth = window.speechSynthesis;
  const labels = {weather: "天気のお話", news: "ニュース", music: "曲のお話", chat: "ひと息の雑談"};
  const sessionKey = "yorimichi-radio-session";
  const sessionMessage = "接続情報を確認できません。start.cmd（起動用ファイル）から、この画面を開き直してください。";
  let sessionToken = "";
  let sessionNoticeShown = false;
  const S = {
    settings: {}, tracks: [], selected: null, current: null, next: null,
    running: false, paused: false, stage: "idle", generation: 0,
    prefetch: null, prefetchInFlight: null, prefetchQueued: null,
    pendingTalk: null, preview: false, history: [], musicStarting: false,
    lastTalk: null, voices: [], utterance: null, toastTimer: null,
    youtube: null, youtubeReady: null, youtubeGeneration: -1,
    youtubeStarted: false, importing: false, dragCount: 0, status: null,
    saveVolumeTimer: null, youtubeVisibilityPaused: false,
    warmupKey: null, warmupInFlight: null, warmupPendingKey: null,
  };

  const escapeHTML = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
  const titleOf = track => track?.title || "タイトル未設定";
  const artistOf = track => track?.artist || (track?.source === "youtube" ? "YouTube" : "マイミュージック");
  const fmtTime = seconds => Number.isFinite(seconds) && seconds >= 0 ? `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}` : "0:00";
  const active = token => token === S.generation && S.running;
  const tagsOf = track => Array.isArray(track?.tags) ? track.tags.join(", ") : String(track?.tags || "");

  function restoreSession() {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    const supplied = fragment.get("session");
    if (fragment.has("session")) {
      // Remove the bootstrap secret before any requests, links, or UI updates.
      history.replaceState(null, "", location.pathname + location.search);
    }
    try {
      if (supplied && /^[A-Za-z0-9_-]{43}$/.test(supplied)) {
        sessionToken = supplied;
        sessionStorage.setItem(sessionKey, supplied);
      } else if (fragment.has("session")) {
        sessionStorage.removeItem(sessionKey);
      } else {
        const saved = sessionStorage.getItem(sessionKey) || "";
        if (/^[A-Za-z0-9_-]{43}$/.test(saved)) sessionToken = saved;
        else sessionStorage.removeItem(sessionKey);
      }
    } catch (_) {
      // A browser that disables storage can still use the current launch tab.
      if (supplied && /^[A-Za-z0-9_-]{43}$/.test(supplied)) sessionToken = supplied;
    }
  }

  function sessionError() {
    const error = new Error(sessionMessage);
    error.name = "SessionError";
    return error;
  }

  function authorizationHeaders() {
    if (!sessionToken) throw sessionError();
    return {"Authorization": `Bearer ${sessionToken}`};
  }

  function rejectSession() {
    sessionToken = "";
    try { sessionStorage.removeItem(sessionKey); } catch (_) { /* Storage may be disabled. */ }
    stopPlayback();
    $("activity-line").textContent = sessionMessage;
    if (!sessionNoticeShown) { sessionNoticeShown = true; toast(sessionMessage); }
    return sessionError();
  }

  async function api(path, data, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), options.timeout || 150000);
    try {
      const request = {method: data === undefined ? "GET" : "POST", credentials: "omit", signal: controller.signal, headers: authorizationHeaders()};
      if (data !== undefined) {
        Object.assign(request.headers, {"Content-Type": "application/json", "X-Requested-With": "YorimichiRadio"});
        request.body = JSON.stringify(data);
      }
      const response = await fetch(path, request);
      if (response.status === 401 || response.status === 403) throw rejectSession();
      let body;
      try { body = await response.json(); } catch { throw new Error("サーバーからの応答を読み取れませんでした。"); }
      if (!response.ok || body.error) throw new Error(typeof body.error === "string" ? body.error : body.message || `通信エラー (${response.status})`);
      return body;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("処理に時間がかかっています。接続先と起動状態を確認してください。");
      if (error instanceof TypeError) throw new Error("アプリに接続できません。起動用ファイルからアプリを起動し直してください。");
      throw error;
    } finally { clearTimeout(timeout); }
  }

  function toast(message, error = false) {
    clearTimeout(S.toastTimer);
    $("toast").textContent = message;
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    S.toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 9000 : 5000);
  }

  function guard(fn) {
    return async event => {
      try { await fn(event); } catch (error) { toast(error.message || "処理に失敗しました。", true); }
    };
  }

  async function busy(button, fn, message) {
    const original = button.textContent;
    button.disabled = true;
    if (message) button.textContent = message;
    try { return await fn(); }
    finally { button.disabled = false; button.textContent = original; }
  }

  function renderTracks() {
    const query = $("search").value.trim().toLocaleLowerCase();
    const visible = S.tracks.filter(t => [t.title, t.artist, tagsOf(t)].join(" ").toLocaleLowerCase().includes(query));
    $("track-count").textContent = S.tracks.length;
    $("nav-count").textContent = S.tracks.length;
    $("drop-zone").hidden = S.tracks.length > 0;
    $("library-summary").textContent = S.tracks.length ? `${S.tracks.length}曲 · ${S.settings.shuffle ? "シャッフル再生" : "リスト順に再生"}` : "0曲 · あなたの選曲を待っています";
    $("clear-library").disabled = !S.tracks.length;
    if (S.tracks.length && !visible.length) {
      $("track-list").innerHTML = '<p class="no-results">一致する曲が見つかりませんでした。</p>';
      return;
    }
    $("track-list").innerHTML = visible.map(track => {
      const isCurrent = S.current?.id === track.id;
      const index = S.tracks.findIndex(t => t.id === track.id);
      const isYoutube = track.source === "youtube";
      return `<div class="track-row${isCurrent ? " current" : ""}" role="listitem" data-id="${escapeHTML(track.id)}"><span class="track-number">${isCurrent && S.running ? (S.paused ? "Ⅱ" : "♪") : String(index + 1).padStart(2, "0")}</span><span class="track-cover${isYoutube ? " youtube" : ""}" aria-hidden="true">${isYoutube ? "▷" : "♫"}</span><button class="track-main" data-action="play" aria-label="${escapeHTML(titleOf(track))}を再生"><strong>${escapeHTML(titleOf(track))}</strong><small>${escapeHTML(artistOf(track))}${tagsOf(track) ? " · " + escapeHTML(tagsOf(track)) : ""}</small></button><span class="track-type">${isYoutube ? "YOUTUBE" : escapeHTML(track.extension?.replace(".", "").toUpperCase() || "AUDIO")}</span><button class="row-action" data-action="edit" title="曲情報を編集" aria-label="${escapeHTML(titleOf(track))}の曲情報を編集">✎</button><button class="row-action remove" data-action="remove" title="一覧から削除" aria-label="${escapeHTML(titleOf(track))}を一覧から削除">×</button></div>`;
    }).join("");
  }

  function renderPlayer() {
    const track = S.current || S.selected;
    const isTalk = S.stage === "talk";
    const waiting = S.stage === "preparing";
    $("now-title").textContent = isTalk ? (S.lastTalk?.label || "DJのおしゃべり") : track ? titleOf(track) : "あなたの選曲で、はじめよう。";
    $("now-artist").textContent = isTalk ? (S.preview ? "トークの試聴" : `${titleOf(track)} のあとに`) : track ? artistOf(track) : "音楽ファイルを追加して、再生ボタンを押してください。";
    $("track-kicker").textContent = isTalk ? "NOW TALKING" : waiting ? "PREPARING YOUR NEXT TALK" : "NOW PLAYING";
    $("source-tag").textContent = isTalk ? (S.lastTalk?.engine === "voicevox" ? "VOICEVOX" : "BROWSER VOICE") : track ? (track.source === "youtube" ? "YOUTUBE" : "LOCAL MUSIC") : "MY STATION";
    $("on-air").className = `on-air${S.running && !S.paused ? isTalk || waiting ? " talking" : " live" : ""}`;
    $("on-air").innerHTML = `<i></i> ${S.paused ? "PAUSED" : isTalk ? "DJ ON AIR" : waiting ? "PREPARING" : S.running ? "ON AIR" : "READY TO PLAY"}`;
    $("play-icon").textContent = S.running && !S.paused ? "Ⅱ" : "▶";
    $("play-label").textContent = S.running ? (S.paused ? "再開する" : "一時停止") : "再生する";
    $("play").setAttribute("aria-label", $("play-label").textContent);
    $("vinyl").classList.toggle("playing", S.running && !S.paused && S.stage === "music");
    $("next-title").textContent = S.next ? titleOf(S.next) : S.tracks.length ? "再生すると、次の曲が決まります。" : "曲を追加すると、ここに次の曲が表示されます。";
    $("shuffle").setAttribute("aria-pressed", String(!!S.settings.shuffle));
    const speaker = S.status?.voicevox?.speakers?.find(v => Number(v.id) === Number(S.settings.speaker_id));
    $("voice-credit").textContent = speaker ? `VOICEVOX：${speaker.name}` : Number(S.settings.speaker_id ?? 3) === 3 ? "VOICEVOX：ずんだもん" : `VOICEVOX（話者ID：${S.settings.speaker_id}）`;
    if (S.paused) $("activity-line").textContent = "ひと休み中。再開すると、続きから流れます。";
    else if (waiting) $("activity-line").textContent = "次のおしゃべりを準備しています…";
    else if (isTalk) $("activity-line").textContent = S.preview ? "トークを試聴しています。" : "DJのおしゃべりが終わると、次の曲が流れます。";
    else if (S.running) $("activity-line").textContent = S.prefetch?.result?.talk && !S.prefetch.invalidated ? "次のおしゃべりは準備できています。音楽をゆっくりどうぞ。" : "音楽を楽しむあいだに、次のおしゃべりを準備します。";
    else $("activity-line").textContent = "音楽 → トーク → 音楽。あとはラジオにおまかせ。";
    renderTracks();
  }

  function chooseNext(track) {
    if (!S.tracks.length) return null;
    const candidates = S.tracks.filter(t => t.id !== track?.id);
    if (!candidates.length) return S.tracks[0];
    if (S.settings.shuffle) return candidates[Math.floor(Math.random() * candidates.length)];
    const index = S.tracks.findIndex(t => t.id === track?.id);
    return S.tracks[(index + 1) % S.tracks.length];
  }

  function haltMedia() {
    S.musicStarting = false;
    music.onended = null; music.onerror = null;
    voiceAudio.onended = null; voiceAudio.onerror = null;
    music.pause(); voiceAudio.pause();
    if (synth) synth.cancel();
    if (S.utterance) { S.utterance.onend = null; S.utterance.onerror = null; }
    S.utterance = null;
    S.pendingTalk = null;
    if (S.youtube?.pauseVideo) {
      try { S.youtube.pauseVideo(); } catch (_) { /* Player may be initializing. */ }
    }
    $("youtube-resume").hidden = true;
  }

  function stopPlayback(announce = false) {
    S.generation++;
    S.running = false; S.paused = false; S.stage = "idle"; S.preview = false;
    discardPrefetch();
    S.prefetch = null; S.next = null;
    haltMedia();
    updateProgress(0, 0);
    renderPlayer();
    if (announce) toast("ラジオを停止しました。");
  }

  function displayTalk(talk) {
    S.lastTalk = talk;
    $("talk-category").textContent = talk.label || labels[talk.category] || "DJ TALK";
    const generation = ["ollama", "llm", "local_llm"].includes(talk.generation_engine) ? "ローカルLLM" : "定型";
    $("talk-engine").textContent = `${generation} · ${talk.engine === "voicevox" ? "VOICEVOX" : "ブラウザ音声"}`;
    $("talk-engine").title = talk.model || talk.generation_model || "";
    $("talk-text").textContent = talk.text || "";
    $("talk-sources").replaceChildren();
    for (const source of talk.sources || []) {
      try {
        const url = new URL(source.url);
        if (!["https:", "http:"].includes(url.protocol)) continue;
        const a = document.createElement("a");
        a.href = url.href; a.target = "_blank"; a.rel = "noopener noreferrer";
        a.textContent = `出典：${source.title || url.hostname}${source.published_at ? " · " + source.published_at : ""}`;
        $("talk-sources").append(a);
      } catch (_) { /* Ignore malformed source links. */ }
    }
    S.history.unshift(talk);
    S.history = S.history.slice(0, 20);
    $("history-count").textContent = S.history.length;
    $("talk-history").innerHTML = S.history.map(t => `<li><small>${escapeHTML(t.label || labels[t.category] || "トーク")} · ${["ollama", "llm", "local_llm"].includes(t.generation_engine) ? "ローカルLLM" : "定型"} · ${new Date(t.created_at || Date.now()).toLocaleTimeString("ja-JP", {hour: "2-digit", minute: "2-digit"})}</small><p>${escapeHTML(t.text)}</p></li>`).join("");
    $("speech-notice").hidden = !talk.warning;
    if (talk.warning) $("speech-notice").textContent = talk.warning;
  }

  async function prepareTalk(track, nextTrack, category) {
    return await api("/api/talk/prepare", {track: track || {}, next_track: nextTrack || {}, ...(category ? {category} : {})});
  }

  async function playbackURL(path, token) {
    const result = await api("/api/playback-url", {path});
    if (!active(token)) return null;
    const url = new URL(result.url, location.origin);
    if (url.origin !== location.origin || url.pathname !== path || url.username || url.password || url.hash || !/^\d+$/.test(url.searchParams.get("expires") || "") || !/^[A-Za-z0-9_-]{43}$/.test(url.searchParams.get("signature") || "")) {
      throw new Error("再生用の接続情報を確認できません。起動用ファイルから開き直してください。");
    }
    return url.pathname + url.search;
  }

  function settlePrefetch(job, result) {
    if (job.settled) return;
    job.settled = true; job.result = result; job.resolve(result);
  }

  function discardPrefetch() {
    if (S.prefetch) S.prefetch.invalidated = true;
    if (S.prefetchQueued) {
      S.prefetchQueued.invalidated = true;
      settlePrefetch(S.prefetchQueued, {cancelled: true});
      S.prefetchQueued = null;
    }
    // Keep the in-flight slot occupied until the HTTP request actually finishes.
    // Aborting fetch cannot stop backend inference and would allow overlaps.
  }

  function pumpPrefetch() {
    if (S.prefetchInFlight || !S.prefetchQueued) return;
    const job = S.prefetchQueued;
    S.prefetchQueued = null;
    const eligible = active(job.token) && !job.invalidated && (job.preview ? S.preview && S.stage === "preparing" : S.stage === "music");
    if (!eligible) { settlePrefetch(job, {cancelled: true}); return; }
    S.prefetchInFlight = job;
    prepareTalk(job.track, job.nextTrack, job.category)
      .then(talk => settlePrefetch(job, {talk}), error => settlePrefetch(job, {error}))
      .finally(() => {
        S.prefetchInFlight = null;
        if (active(job.token) && S.prefetch === job && S.stage === "music") renderPlayer();
        pumpPrefetch();
      });
  }

  function queuePrefetch(track, nextTrack, category, preview = false) {
    if (S.prefetch?.token === S.generation && !S.prefetch.invalidated && S.prefetch.preview === preview && S.prefetch.category === category) return S.prefetch;
    if (S.prefetchQueued) settlePrefetch(S.prefetchQueued, {cancelled: true});
    const job = {
      token: S.generation, track: {...track}, nextTrack: {...nextTrack}, category,
      preview, invalidated: false, settled: false, result: null,
    };
    job.promise = new Promise(resolve => { job.resolve = resolve; });
    S.prefetch = job; S.prefetchQueued = job;
    pumpPrefetch();
    return job;
  }

  function refreshPrefetch() {
    // Queue and metadata edits must not launch repeated LLM inference in the
    // same song. Use a generic, short fallback at the boundary instead.
    if (S.running && S.stage === "music") discardPrefetch();
  }

  async function startMusic(track) {
    if (!track) { toast("先に音楽ファイルを追加してください。"); return; }
    const token = ++S.generation;
    discardPrefetch();
    haltMedia();
    S.current = track; S.selected = track; S.next = chooseNext(track);
    S.running = true; S.paused = false; S.preview = false; S.stage = "music";
    S.musicStarting = true;
    S.youtubeVisibilityPaused = false;
    updateProgress(0, 0); renderPlayer();
    queuePrefetch(track, S.next);
    if (track.source === "youtube") {
      try {
        const player = await ensureYouTube();
        if (!active(token)) return;
        S.musicStarting = false;
        if (document.hidden) { S.paused = true; S.youtubeVisibilityPaused = true; renderPlayer(); }
        S.youtubeGeneration = token; S.youtubeStarted = false;
        player.setVolume(Math.round((S.settings.music_volume ?? 0.8) * 100));
        if (S.paused) player.cueVideoById(track.video_id);
        else player.loadVideoById(track.video_id);
        $("youtube-panel").scrollIntoView({behavior: "smooth", block: "nearest"});
      } catch (error) { if (active(token)) playbackFailure(error.message); }
      return;
    }
    let signedURL;
    try { signedURL = await playbackURL(`/api/media/${encodeURIComponent(track.id)}`, token); }
    catch (error) { if (active(token)) playbackFailure(error.message); return; }
    if (!signedURL || !active(token)) return;
    S.musicStarting = false;
    music.src = signedURL;
    music.volume = S.settings.music_volume ?? 0.8;
    music.onended = () => { if (active(token) && S.stage === "music") void playAfterMusic(token); };
    music.onerror = () => { if (active(token)) playbackFailure("この曲を再生できませんでした。ファイルの場所や形式を確認するか、次の曲に進んでください。"); };
    if (S.paused) return;
    try { await music.play(); }
    catch (error) {
      if (!active(token)) return;
      if (error.name === "NotAllowedError") { S.paused = true; renderPlayer(); toast("ブラウザが自動再生を止めました。「再開する」を押してください。"); }
      else if (error.name !== "AbortError") playbackFailure("音楽を再生できませんでした。ファイルと音声出力を確認してください。");
    }
  }

  function playbackFailure(message) {
    stopPlayback();
    $("activity-line").textContent = message;
    toast(message, true);
  }

  async function playAfterMusic(token) {
    if (!active(token)) return;
    S.stage = "preparing"; updateProgress(0, 0); renderPlayer();
    const job = S.prefetch;
    let result = job && !job.invalidated ? await Promise.race([
      job.promise,
      new Promise(resolve => setTimeout(() => resolve({late: true}), 1000)),
    ]) : {cancelled: true};
    if (!active(token)) return;
    if (result?.late || result?.cancelled || result?.error || !result?.talk?.text || job?.invalidated) {
      discardPrefetch();
      try {
        const talk = await api("/api/talk/fallback", {track: S.current || {}, next_track: {}}, {timeout: 5000});
        result = {talk};
      } catch (error) { result = {error}; }
    }
    if (!active(token)) return;
    if (result?.error || !result?.talk?.text) {
      toast(`トークを準備できなかったため、次の曲に進みます。${result?.error?.message || ""}`);
      if (S.paused) { S.pendingTalk = {skip: true}; return; }
      return await finishTalk(token);
    }
    if (S.paused) { S.pendingTalk = result.talk; return; }
    await playPreparedTalk(result.talk, token);
  }

  async function finishTalk(token) {
    if (!active(token)) return;
    if (S.preview) { stopPlayback(); return; }
    const next = S.tracks.find(t => t.id === S.next?.id) || chooseNext(S.current);
    if (next) await startMusic(next); else stopPlayback();
  }

  function japaneseVoice() {
    const voices = synth?.getVoices() || [];
    const named = voices.find(v => v.name === S.settings.browser_voice && /^ja(?:[-_]|$)/i.test(v.lang));
    return named || voices.find(v => /^ja(?:[-_]|$)/i.test(v.lang)) || null;
  }

  async function speakInBrowser(talk, token) {
    if (!synth) {
      playbackFailure("このブラウザは読み上げに対応していません。VOICEVOXを起動してから接続を再確認してください。");
      return;
    }
    let voice = japaneseVoice();
    if (!voice) {
      await new Promise(resolve => setTimeout(resolve, 600));
      if (!active(token)) return;
      voice = japaneseVoice();
    }
    if (!voice) {
      playbackFailure("日本語の読み上げ音声が見つかりません。VOICEVOXを起動するか、Windowsの日本語音声を追加してください。台本は画面で確認できます。");
      $("speech-notice").hidden = false;
      $("speech-notice").textContent = "日本語音声がありません。Windowsの「設定 → 時刻と言語 → 音声認識」で日本語の音声を追加するか、VOICEVOXを起動してください。";
      return;
    }
    if (S.paused) { S.pendingTalk = {...talk, audio_url: null, engine: "browser"}; return; }
    const utterance = new SpeechSynthesisUtterance(talk.text);
    utterance.lang = "ja-JP"; utterance.voice = voice;
    utterance.rate = S.settings.speech_rate || 1;
    utterance.volume = S.settings.talk_volume ?? 0.85;
    S.utterance = utterance;
    utterance.onend = () => { if (active(token)) void finishTalk(token); };
    utterance.onerror = event => {
      if (!active(token) || ["canceled", "interrupted"].includes(event.error)) return;
      playbackFailure("ブラウザの読み上げに失敗しました。音声設定を確認するか、VOICEVOXをご利用ください。");
    };
    synth.speak(utterance);
  }

  async function playPreparedTalk(talk, token, displayed = false) {
    if (!active(token)) return;
    if (S.paused) { S.pendingTalk = talk; return; }
    let signedAudio = null;
    if (talk.audio_url && /^\/api\/audio\/[a-zA-Z0-9_-]+\.wav$/.test(talk.audio_url)) {
      S.stage = "preparing"; renderPlayer();
      try { signedAudio = await playbackURL(talk.audio_url, token); }
      catch (error) { if (active(token)) playbackFailure(error.message); return; }
      if (!signedAudio || !active(token)) return;
      if (S.paused) { S.pendingTalk = talk; return; }
    }
    if (!displayed) displayTalk(talk);
    S.stage = "talk"; renderPlayer();
    if (signedAudio) {
      voiceAudio.src = signedAudio;
      voiceAudio.volume = S.settings.talk_volume ?? 0.85;
      voiceAudio.onended = () => { if (active(token)) void finishTalk(token); };
      voiceAudio.onerror = () => {
        if (!active(token)) return;
        voiceAudio.onended = null; voiceAudio.onerror = null;
        toast("合成音声を再生できなかったため、ブラウザ読み上げに切り替えます。");
        void speakInBrowser(talk, token);
      };
      try { await voiceAudio.play(); }
      catch (error) {
        if (!active(token)) return;
        if (error.name === "NotAllowedError") { S.paused = true; renderPlayer(); toast("「再開する」を押すとトークが流れます。"); }
        else if (error.name !== "AbortError") await speakInBrowser(talk, token);
      }
    } else await speakInBrowser(talk, token);
  }

  async function togglePlayback() {
    if (!S.running) { await startMusic(S.selected || S.current || S.tracks[0]); return; }
    if (!S.paused) {
      S.paused = true;
      music.pause(); voiceAudio.pause();
      if (synth?.speaking) synth.pause();
      if (S.youtube && S.current?.source === "youtube") S.youtube.pauseVideo();
      renderPlayer(); return;
    }
    S.paused = false; renderPlayer();
    const token = S.generation;
    if (S.pendingTalk) {
      const pending = S.pendingTalk; S.pendingTalk = null;
      if (pending.skip) await finishTalk(token); else await playPreparedTalk(pending, token);
    } else if (S.stage === "music") {
      if (S.musicStarting) return;
      if (S.current?.source === "youtube") {
        if (document.hidden) { S.paused = true; renderPlayer(); return; }
        const player = await ensureYouTube();
        if (active(token)) { S.youtubeGeneration = token; player.playVideo(); }
      } else {
        try { await music.play(); } catch (error) { if (active(token)) { S.paused = true; renderPlayer(); toast("再生できませんでした。もう一度再開するか、次の曲に進んでください。", true); } }
      }
    } else if (S.stage === "talk") {
      if (S.utterance && synth) {
        if (synth.paused) synth.resume();
        else if (!synth.speaking) await speakInBrowser(S.lastTalk, token);
      } else {
        try { await voiceAudio.play(); } catch (_) { if (active(token)) { S.paused = true; renderPlayer(); toast("トークを再開できませんでした。音声の接続を確認してください。", true); } }
      }
    }
  }

  async function previewTalk(category) {
    const token = ++S.generation;
    discardPrefetch();
    haltMedia(); S.running = true; S.paused = false; S.preview = true;
    S.stage = "preparing"; S.prefetch = null; S.next = null;
    updateProgress(0, 0); renderPlayer();
    try {
      const result = await queuePrefetch(S.current || S.tracks[0] || {}, {}, category, true).promise;
      if (!active(token)) return;
      if (result.error) throw result.error;
      if (!result.talk?.text) throw new Error("トークを準備できませんでした。もう一度お試しください。");
      const talk = result.talk;
      if (S.paused) S.pendingTalk = talk; else await playPreparedTalk(talk, token);
    } catch (error) { if (active(token)) playbackFailure(error.message); }
  }

  function updateProgress(position, duration) {
    const valid = Number.isFinite(duration) && duration > 0;
    $("seek").disabled = !valid || S.stage !== "music";
    $("seek").value = valid ? Math.min(1000, Math.max(0, position / duration * 1000)) : 0;
    $("elapsed").textContent = fmtTime(position);
    $("duration").textContent = fmtTime(duration);
  }

  function refreshVoices() {
    S.voices = synth?.getVoices().filter(v => /^ja(?:[-_]|$)/i.test(v.lang)) || [];
    const select = $("browser-voice");
    const previous = select.value || S.settings.browser_voice || "";
    select.replaceChildren(new Option("日本語の声を自動選択", ""));
    S.voices.forEach(v => select.add(new Option(`${v.name}${v.localService ? "（端末内）" : ""}`, v.name)));
    select.value = previous;
    if (!select.value) select.value = "";
  }

  function warmFallbackAudio() {
    if (!S.status?.voicevox?.ok) return;
    const key = JSON.stringify([S.settings.voicevox_url, S.settings.speaker_id, S.settings.speech_rate]);
    if (key === S.warmupKey || key === S.warmupInFlight) return;
    S.warmupPendingKey = key;
    if (S.warmupInFlight) return;
    S.warmupPendingKey = null;
    S.warmupInFlight = key;
    // This endpoint never invokes the LLM. Warm the short VOICEVOX fallback
    // before a song ends, including after a speaker or speed change.
    void api("/api/talk/fallback", {track: {}, next_track: {}}, {timeout: 20000})
      .then(talk => { if (talk.engine === "voicevox") S.warmupKey = key; })
      .catch(() => { /* A failed warmup must not interrupt listening. */ })
      .finally(() => {
        S.warmupInFlight = null;
        if (S.warmupPendingKey) {
          S.warmupPendingKey = null;
          warmFallbackAudio();
        }
      });
  }

  async function refreshStatus(showToast = false) {
    $("connection-refresh").disabled = true;
    try {
      const status = await api("/api/status", undefined, {timeout: 18000}); S.status = status;
      const connected = !!status.voicevox?.ok;
      $("voice-dot").className = `status-dot${connected ? " online" : ""}`;
      $("voice-status").textContent = connected ? "VOICEVOX 接続済み" : "ブラウザの読み上げを使用";
      const llmConnected = S.settings.llm_enabled && status.ollama?.ok;
      $("llm-dot").className = `status-dot${llmConnected ? " online" : ""}`;
      $("llm-status").textContent = llmConnected ? "ローカルLLM 接続済み" : S.settings.llm_enabled ? "LLM未接続 · 定型トーク" : "定型トークモード";
      const select = $("speaker-select");
      const previous = select.value || String(S.settings.speaker_id ?? 3);
      if (status.voicevox?.speakers?.length) {
        select.replaceChildren();
        status.voicevox.speakers.forEach(v => select.add(new Option(`${v.name} · ${v.style}（ID: ${v.id}）`, String(v.id))));
        if (![...select.options].some(o => o.value === previous)) select.add(new Option(`設定中の話者（ID: ${previous}）`, previous));
        select.value = previous;
      }
      $("model-list").replaceChildren(...(status.ollama?.models || []).map(v => new Option(typeof v === "string" ? v : v.name, typeof v === "string" ? v : v.name)));
      renderPlayer();
      if (connected) warmFallbackAudio();
      if (showToast) toast(connected ? "VOICEVOXに接続しました。" : "VOICEVOXは未接続です。ブラウザの日本語音声で動作します。");
    } catch (error) {
      $("voice-status").textContent = "接続を確認できません";
      $("llm-status").textContent = "定型トークモード";
      if (showToast) toast(error.message, true);
    } finally { $("connection-refresh").disabled = false; }
  }

  async function refreshState() {
    const state = await api("/api/state");
    S.settings = state.settings || {}; S.tracks = state.tracks || [];
    $("version").textContent = state.version ? `v${state.version}` : "";
    $("volume").value = Math.round((S.settings.music_volume ?? 0.8) * 100);
    S.current = S.tracks.find(t => t.id === S.current?.id) || S.current;
    S.selected = S.tracks.find(t => t.id === S.selected?.id) || S.tracks[0] || null;
    renderPlayer();
  }

  function fillSettings() {
    const form = $("settings-form");
    ["city", "timezone", "latitude", "longitude", "talk_seconds", "voicevox_url", "speech_rate", "ollama_url", "ollama_model", "persona"].forEach(name => { if (form.elements[name]) form.elements[name].value = S.settings[name] ?? ""; });
    form.elements.llm_enabled.checked = !!S.settings.llm_enabled;
    form.elements.talk_volume.value = Math.round((S.settings.talk_volume ?? 0.85) * 100);
    form.elements.music_volume.value = Math.round((S.settings.music_volume ?? 0.8) * 100);
    form.elements.news_feeds.value = (S.settings.news_feeds || []).join("\n");
    ["weather", "news", "music", "chat"].forEach(topic => { form.elements[`weight_${topic}`].value = S.settings.topic_weights?.[topic] ?? 25; });
    const speaker = String(S.settings.speaker_id ?? 3);
    if (![...$("speaker-select").options].some(o => o.value === speaker)) $("speaker-select").add(new Option(`話者 ID: ${speaker}`, speaker));
    $("speaker-select").value = speaker;
    refreshVoices();
    const preset = [...$("city-preset").options].find(o => o.value.startsWith(`${S.settings.city}|`));
    $("city-preset").value = preset?.value || "custom";
    $("settings-message").textContent = "";
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const topicWeights = Object.fromEntries(["weather", "news", "music", "chat"].map(t => [t, Number(form.elements[`weight_${t}`].value)]));
    if (!Object.values(topicWeights).some(n => n > 0)) { $("settings-message").textContent = "トークの割合を1つ以上、1以上にしてください。"; return; }
    const data = {topic_weights: topicWeights};
    ["city", "timezone", "voicevox_url", "ollama_url", "ollama_model", "persona", "browser_voice"].forEach(n => { data[n] = form.elements[n].value.trim(); });
    ["latitude", "longitude", "talk_seconds", "speaker_id", "speech_rate"].forEach(n => { data[n] = Number(form.elements[n].value); });
    data.talk_volume = Number(form.elements.talk_volume.value) / 100;
    data.music_volume = Number(form.elements.music_volume.value) / 100;
    data.llm_enabled = form.elements.llm_enabled.checked;
    data.news_feeds = form.elements.news_feeds.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    if (data.llm_enabled && !data.ollama_model) { $("settings-message").textContent = "Ollamaで使うモデル名を入力してください。"; return; }
    await busy(form.querySelector('[type="submit"]'), async () => {
      try {
        const result = await api("/api/settings", data); S.settings = result.settings;
        music.volume = S.settings.music_volume ?? 0.8;
        voiceAudio.volume = S.settings.talk_volume ?? 0.85;
        $("volume").value = Math.round((S.settings.music_volume ?? 0.8) * 100);
        if (S.youtube?.setVolume) S.youtube.setVolume(Math.round((S.settings.music_volume ?? 0.8) * 100));
        $("settings-dialog").close(); renderPlayer(); toast("設定を保存しました。これから準備するトークに反映します。");
        void refreshStatus();
      } catch (error) { $("settings-message").textContent = error.message; }
    }, "保存中…");
  }

  function showWarnings(result, fallback) {
    const warnings = result.warnings || [];
    toast(warnings.length ? `${fallback} ${warnings.slice(0, 2).join(" / ")}` : fallback);
  }

  async function uploadFiles(files) {
    if (!files.length) return;
    let added = 0; const errors = [];
    await busy($("add-files"), async () => {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        $("add-files").textContent = `追加中 ${i + 1}/${files.length}`;
        if (!/\.(mp3|wav|ogg|m4a|flac|aac)$/i.test(file.name)) { errors.push(`${file.name}: 対応外の形式`); continue; }
        try {
          const response = await fetch(`/api/library/upload?name=${encodeURIComponent(file.name)}`, {method: "POST", credentials: "omit", headers: {...authorizationHeaders(), "Content-Type": "application/octet-stream", "X-Requested-With": "YorimichiRadio"}, body: file});
          if (response.status === 401 || response.status === 403) throw rejectSession();
          const result = await response.json();
          if (!response.ok || result.error) throw new Error(result.error || "追加できませんでした");
          added++;
        } catch (error) { errors.push(`${file.name}: ${error.message}`); }
      }
      await refreshState();
      toast(`${added}曲を追加しました。${errors.length ? ` ${errors.length}件を追加できませんでした：${errors[0]}` : ""}`, added === 0 && errors.length > 0);
    });
    $("file-input").value = "";
  }

  function editTrack(track) {
    const form = $("edit-form");
    ["id", "title", "artist", "comment"].forEach(name => { form.elements[name].value = track[name] || ""; });
    form.elements.tags.value = tagsOf(track);
    $("edit-message").textContent = "";
    $("edit-dialog").showModal();
  }

  async function ensureYouTube() {
    $("youtube-panel").hidden = false;
    if (S.youtubeReady) return S.youtubeReady;
    S.youtubeReady = new Promise((resolve, reject) => {
      let settled = false;
      const timeout = setTimeout(() => {
        if (settled) return;
        settled = true; S.youtubeReady = null;
        reject(new Error("YouTubeプレーヤーを読み込めませんでした。インターネット接続を確認してください。"));
      }, 20000);
      function createPlayer() {
        if (settled) return;
        try {
          S.youtube = new YT.Player("youtube-player", {
            width: "640", height: "360", host: "https://www.youtube.com",
            playerVars: {playsinline: 1, origin: location.origin, autoplay: 0, controls: 1},
            events: {
              onReady: event => { if (settled) return; clearTimeout(timeout); settled = true; resolve(event.target); },
              onStateChange: onYouTubeState,
              onError: event => {
                if (!S.running || S.current?.source !== "youtube" || S.stage !== "music") return;
                const messages = {100: "この動画は削除済みか非公開です。", 101: "この動画は埋め込み再生が許可されていません。", 150: "この動画は埋め込み再生が許可されていません。", 153: "YouTubeがこの再生画面を確認できませんでした。通常のブラウザでアプリを開き直してください。"};
                playbackFailure((messages[event.data] || "YouTube動画を再生できませんでした。") + " 別の曲を選ぶか、次の曲に進んでください。");
              },
              onAutoplayBlocked: () => {
                if (!S.running || S.current?.source !== "youtube") return;
                S.paused = true; renderPlayer(); $("youtube-resume").hidden = false;
                toast("YouTubeの自動再生が止まりました。プレーヤーの再生ボタンか「再開する」を押してください。");
              },
            },
          });
        } catch (error) { clearTimeout(timeout); settled = true; S.youtubeReady = null; reject(error); }
      }
      if (window.YT?.Player) createPlayer();
      else {
        window.onYouTubeIframeAPIReady = createPlayer;
        if (!document.getElementById("youtube-api-script")) {
          const script = document.createElement("script"); script.id = "youtube-api-script"; script.src = "https://www.youtube.com/iframe_api";
          script.onerror = () => { if (!settled) { clearTimeout(timeout); settled = true; S.youtubeReady = null; script.remove(); reject(new Error("YouTubeへ接続できませんでした。")); } };
          document.head.append(script);
        }
      }
    });
    return S.youtubeReady;
  }

  function onYouTubeState(event) {
    if (S.importing) return;
    if (event.target && event.target !== S.youtube) {
      if (event.data === 1) { try { event.target.pauseVideo(); } catch (_) {} }
      return;
    }
    if (!S.running || S.current?.source !== "youtube" || S.stage !== "music" || S.youtubeGeneration !== S.generation) {
      if (event.data === 1) { try { S.youtube.pauseVideo(); } catch (_) {} }
      return;
    }
    const videoId = S.youtube.getVideoData?.().video_id;
    if (videoId && videoId !== S.current.video_id) {
      if (event.data === 1) { try { S.youtube.pauseVideo(); } catch (_) {} }
      return;
    }
    if (event.data === 1) {
      if (document.hidden) { S.youtube.pauseVideo(); S.paused = true; renderPlayer(); return; }
      S.youtubeStarted = true; S.paused = false; $("youtube-resume").hidden = true; renderPlayer();
    } else if (event.data === 2 && S.youtubeStarted && !S.paused) {
      S.paused = true; renderPlayer();
    } else if (event.data === 0 && S.youtubeStarted && !S.paused) {
      void playAfterMusic(S.generation);
    }
  }

  function parseYouTubeURL(value) {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    if (url.protocol !== "https:" || url.username || url.password) throw new Error("https:// で始まるYouTubeのURLを入力してください。");
    if (!["www.youtube.com", "youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"].includes(host)) throw new Error("YouTubeの動画または再生リストのURLを入力してください。");
    const list = url.searchParams.get("list");
    const video = host === "youtu.be" ? url.pathname.split("/").filter(Boolean)[0] : url.searchParams.get("v") || url.pathname.match(/^\/(?:embed|shorts)\/([\w-]{11})/)?.[1];
    if (!list && !video) throw new Error("URLから動画IDや再生リストIDを読み取れませんでした。");
    if (list && !/^[A-Za-z0-9_-]{2,150}$/.test(list)) throw new Error("再生リストIDの形式が正しくありません。");
    if (!list && !/^[A-Za-z0-9_-]{11}$/.test(video)) throw new Error("動画IDの形式が正しくありません。");
    const canonical = list ? `https://www.youtube.com/playlist?list=${encodeURIComponent(list)}` : `https://www.youtube.com/watch?v=${encodeURIComponent(video)}`;
    return {list, video, url: canonical};
  }

  async function importYouTube(event) {
    event.preventDefault();
    const value = $("youtube-url").value.trim();
    const parsed = parseYouTubeURL(value);
    await busy($("youtube-submit"), async () => {
      $("youtube-message").textContent = "YouTubeを確認しています…";
      try {
        let ids;
        if (parsed.list) {
          // Cue only: importing a list never starts unsolicited playback.
          stopPlayback(); S.importing = true;
          // A fresh player prevents getPlaylist() returning the previous list
          // while the new cuePlaylist() request is still loading.
          if (S.youtube) {
            S.youtube.destroy(); S.youtube = null; S.youtubeReady = null;
            const target = document.createElement("div"); target.id = "youtube-player";
            $("youtube-panel").insertBefore(target, $("youtube-panel").querySelector(".muted"));
          }
          const player = await ensureYouTube();
          player.cuePlaylist({list: parsed.list, listType: "playlist", index: 0});
          for (let i = 0; i < 40; i++) {
            await new Promise(resolve => setTimeout(resolve, 250));
            const values = player.getPlaylist?.();
            if (Array.isArray(values) && values.length) { ids = values.slice(0, 500); break; }
          }
          if (!ids?.length) throw new Error("再生リストを取得できませんでした。公開状態や埋め込みの制限を確認してください。動画URLから1曲ずつ追加することもできます。");
        }
        const result = await api("/api/youtube/import", {url: parsed.url, ...(ids ? {video_ids: ids} : {})});
        await refreshState(); $("youtube-dialog").close();
        showWarnings(result, `${result.added || 0}曲を追加しました。曲名は曲情報から編集できます。`);
      } catch (error) { $("youtube-message").textContent = error.message; }
      finally { S.importing = false; }
    }, "確認中…");
  }

  function setupEvents() {
    $("play").addEventListener("click", guard(togglePlayback));
    $("stop").addEventListener("click", () => stopPlayback(true));
    $("next").addEventListener("click", guard(async () => {
      const next = S.tracks.find(t => t.id === S.next?.id) || chooseNext(S.current || S.selected);
      await startMusic(next);
    }));
    $("shuffle").addEventListener("click", guard(async () => {
      const result = await api("/api/settings", {shuffle: !S.settings.shuffle}); S.settings = result.settings;
      if (!S.running || S.stage === "music") S.next = chooseNext(S.current || S.selected);
      refreshPrefetch(); renderPlayer();
      toast(S.settings.shuffle ? "シャッフルをオンにしました。" : "リストの順番で再生します。");
    }));
    $("volume").addEventListener("input", () => {
      S.settings.music_volume = Number($("volume").value) / 100;
      music.volume = S.settings.music_volume;
      if (S.youtube?.setVolume) S.youtube.setVolume(Number($("volume").value));
      clearTimeout(S.saveVolumeTimer);
      S.saveVolumeTimer = setTimeout(() => { api("/api/settings", {music_volume: S.settings.music_volume}).catch(error => toast(error.message, true)); }, 350);
    });
    $("seek").addEventListener("input", () => {
      if (S.stage !== "music") return;
      const value = Number($("seek").value) / 1000;
      if (S.current?.source === "youtube") { const duration = S.youtube?.getDuration?.(); if (duration) S.youtube.seekTo(duration * value, true); }
      else if (Number.isFinite(music.duration)) music.currentTime = music.duration * value;
    });
    music.addEventListener("timeupdate", () => { if (S.stage === "music" && S.current?.source !== "youtube") updateProgress(music.currentTime, music.duration); });
    music.addEventListener("loadedmetadata", () => { if (S.stage === "music" && S.current?.source !== "youtube") updateProgress(music.currentTime, music.duration); });
    $("preview-talk").addEventListener("click", guard(() => previewTalk()));
    document.querySelectorAll("[data-preview]").forEach(button => button.addEventListener("click", guard(() => previewTalk(button.dataset.preview))));
    $("search").addEventListener("input", renderTracks);
    $("add-files").addEventListener("click", () => $("file-input").click());
    $("file-input").addEventListener("change", guard(event => uploadFiles([...event.target.files])));
    $("add-demo").addEventListener("click", guard(() => busy($("add-demo"), async () => {
      const result = await api("/api/library/demo", {}); await refreshState(); showWarnings(result, `${result.added || 0}曲のデモを追加しました。再生ボタンで試せます。`);
    }, "追加中…")));
    $("add-folder").addEventListener("click", () => { $("folder-message").textContent = ""; $("folder-dialog").showModal(); });
    $("pick-folder").addEventListener("click", guard(() => busy($("pick-folder"), async () => {
      $("folder-message").textContent = "フォルダの選択画面が別ウィンドウに開きます。";
      const result = await api("/api/library/pick-folder", {}); if (result.path) $("folder-path").value = result.path;
      $("folder-message").textContent = "";
    }, "選択中…")));
    $("folder-form").addEventListener("submit", guard(async event => {
      event.preventDefault();
      await busy(event.currentTarget.querySelector('[type="submit"]'), async () => {
        try {
          const result = await api("/api/library/folder", {path: $("folder-path").value.trim()});
          await refreshState(); $("folder-dialog").close(); showWarnings(result, `${result.added || 0}曲を追加しました。`);
        } catch (error) { $("folder-message").textContent = error.message; }
      }, "読み込み中…");
    }));
    $("track-list").addEventListener("click", guard(async event => {
      const button = event.target.closest("[data-action]"); if (!button) return;
      const id = button.closest("[data-id]")?.dataset.id;
      const track = S.tracks.find(t => t.id === id); if (!track) return;
      if (button.dataset.action === "play") await startMusic(track);
      else if (button.dataset.action === "edit") editTrack(track);
      else if (button.dataset.action === "remove") {
        await api("/api/tracks/remove", {id});
        if (S.current?.id === id) { stopPlayback(); S.current = null; }
        await refreshState();
        if (S.next?.id === id) { S.next = chooseNext(S.current); refreshPrefetch(); renderPlayer(); }
        toast("曲を一覧から削除しました。");
      }
    }));
    $("clear-library").addEventListener("click", guard(async () => {
      if (!S.tracks.length) return;
      if (!confirm("曲一覧を空にしますか？ 元の音楽ファイルは削除されません。")) return;
      await api("/api/library/clear", {}); stopPlayback(); S.current = null; S.selected = null; await refreshState(); toast("曲一覧を空にしました。");
    }));
    $("edit-form").addEventListener("submit", guard(async event => {
      event.preventDefault(); const form = event.currentTarget;
      await busy(form.querySelector('[type="submit"]'), async () => {
        try {
          const data = Object.fromEntries(["id", "title", "artist", "tags", "comment"].map(n => [n, form.elements[n].value.trim()]));
          const result = await api("/api/tracks/update", data);
          S.tracks = S.tracks.map(t => t.id === result.track.id ? result.track : t);
          if (S.current?.id === result.track.id) S.current = result.track;
          if (S.selected?.id === result.track.id) S.selected = result.track;
          if (S.next?.id === result.track.id) S.next = result.track;
          if (S.current?.id === result.track.id || S.next?.id === result.track.id) refreshPrefetch();
          $("edit-dialog").close(); renderPlayer(); toast("曲の情報を保存しました。");
        } catch (error) { $("edit-message").textContent = error.message; }
      }, "保存中…");
    }));
    $("settings-open").addEventListener("click", () => { fillSettings(); $("settings-dialog").showModal(); });
    $("settings-form").addEventListener("submit", guard(saveSettings));
    $("city-preset").addEventListener("change", event => {
      if (event.target.value === "custom") return;
      const [city, latitude, longitude] = event.target.value.split("|");
      const form = $("settings-form"); form.elements.city.value = city; form.elements.latitude.value = latitude; form.elements.longitude.value = longitude; form.elements.timezone.value = "Asia/Tokyo";
    });
    $("connection-refresh").addEventListener("click", () => void refreshStatus(true));
    document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => $(button.dataset.close).close()));
    document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => { if (event.target === dialog) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); } }));
    $("youtube-open").addEventListener("click", () => { $("youtube-message").textContent = ""; $("youtube-dialog").showModal(); });
    $("youtube-form").addEventListener("submit", guard(importYouTube));
    $("youtube-resume").addEventListener("click", guard(async () => { if (S.paused) await togglePlayback(); else S.youtube?.playVideo(); }));
    document.addEventListener("visibilitychange", () => {
      if (document.hidden && S.current?.source === "youtube" && S.running && S.stage === "music" && !S.paused) {
        S.paused = true; S.youtubeVisibilityPaused = true; S.youtube?.pauseVideo(); renderPlayer();
      } else if (!document.hidden && S.youtubeVisibilityPaused) {
        S.youtubeVisibilityPaused = false; toast("YouTubeは画面を非表示にしたため一時停止しました。「再開する」で続けられます。");
      }
    });
    const dropSurface = $("library");
    ["dragenter", "dragover", "dragleave", "drop"].forEach(type => dropSurface.addEventListener(type, event => { event.preventDefault(); event.stopPropagation(); }));
    dropSurface.addEventListener("dragenter", () => { S.dragCount++; dropSurface.classList.add("drag-over"); });
    dropSurface.addEventListener("dragleave", () => { if (--S.dragCount <= 0) { S.dragCount = 0; dropSurface.classList.remove("drag-over"); } });
    dropSurface.addEventListener("drop", guard(async event => { S.dragCount = 0; dropSurface.classList.remove("drag-over"); await uploadFiles([...event.dataTransfer.files]); }));
    window.addEventListener("dragover", event => event.preventDefault());
    window.addEventListener("drop", event => event.preventDefault());
    window.addEventListener("pagehide", () => { haltMedia(); });
    if (synth) { refreshVoices(); synth.addEventListener("voiceschanged", refreshVoices); }
  }

  async function init() {
    restoreSession();
    setupEvents();
    function updateClock() {
      const now = new Date();
      $("date-label").textContent = new Intl.DateTimeFormat("ja-JP", {month: "long", day: "numeric", weekday: "short"}).format(now);
      $("clock-label").textContent = new Intl.DateTimeFormat("ja-JP", {hour: "2-digit", minute: "2-digit", hour12: false}).format(now);
    }
    updateClock(); setInterval(updateClock, 15000);
    setInterval(() => {
      if (S.running && S.stage === "music" && S.current?.source === "youtube" && S.youtube?.getCurrentTime) updateProgress(S.youtube.getCurrentTime(), S.youtube.getDuration());
    }, 500);
    try { await refreshState(); void refreshStatus(); }
    catch (error) { toast(error.message, true); $("activity-line").textContent = error.message; }
  }

  void init();
})();
