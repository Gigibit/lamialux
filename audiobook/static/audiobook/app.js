(() => {
  const config = window.lamialuxPageConfig || {};
  const page = config.page || 'book';
  const browserSessionInput = document.getElementById('browser-session-id');
  const playButton = document.getElementById('play-media');
  const stopButton = document.getElementById('stop-media');
  const connectButton = document.getElementById('connect-stream');
  const statusEl = document.getElementById('stream-status');
  const animationCanvas = document.getElementById('theia-canvas');
  const animationOverlay = document.getElementById('theia-canvas-overlay');
  const video = document.getElementById('whep-player');
  const coquiPlayer = document.getElementById('coqui-player');
  const musicPlayer = document.getElementById('music-player');
  const sessionLabel = document.getElementById('stream-session-label');
  const items = Array.from(document.querySelectorAll('.chunk-list li'));
  const musicTrack = config.musicTrack || null;
  const narrativeModeProvider = String(config.narrativeModeProvider || '').trim().toUpperCase();
  const narrativeSourceVideo = document.getElementById('narrative-source-video');
  const sourceVideoUrl = String(config.sourceVideoUrl || '').trim();
  const usesSourceNarration = narrativeModeProvider === 'YOUTUBE_SEARCH';
  const PROMPT_UPDATE_INTERVAL_MS = 5000;
  const logger = window.logger && typeof window.logger.error === 'function' ? window.logger : console;

  if (!playButton || !stopButton || !connectButton || !statusEl || !animationCanvas || !animationOverlay || !video || !sessionLabel) {
    return;
  }

  const createBrowserSessionId = () => ((window.crypto && typeof window.crypto.randomUUID === 'function')
    ? window.crypto.randomUUID()
    : `session-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const storedBrowserSessionId = window.localStorage.getItem('lamialux.browserSessionId');
  const browserSessionId = storedBrowserSessionId || createBrowserSessionId();
  if (!storedBrowserSessionId) {
    window.localStorage.setItem('lamialux.browserSessionId', browserSessionId);
  }
  if (browserSessionInput) {
    browserSessionInput.value = browserSessionId;
  }

  const streamSession = { sessionId: browserSessionId, whipUrl: '', whepUrl: '', outputVideoUrl: '' };
  const rtcConfig = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] };
  let publisherPc = null;
  let viewerPc = null;
  let whepResourceUrl = null;
  let compositeStream = null;
  let connectPromise = null;
  let streamReady = false;
  let audioContext = null;
  let destination = null;
  let gainNode = null;
  let sourceNode = null;
  let promptUpdateTimerId = null;
  let playbackToken = 0;
  let currentIndex = 0;
  let cancelled = false;

  const sentencePool = items
    .flatMap((item) => (item.dataset.text || '').match(/[^.!?]+[.!?]+|[^.!?]+$/g) || [])
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence.length > 20);

  const setStatus = (message) => { statusEl.textContent = message; };
  const setOverlay = (title, text) => {
    if (!animationOverlay) {
      logger.error('Unable to update the Theia canvas overlay because the overlay element is missing.');
      return;
    }
    animationOverlay.innerHTML = '';
    const titleEl = document.createElement('strong');
    titleEl.textContent = title;
    const body = document.createElement('span');
    body.textContent = text;
    animationOverlay.append(titleEl, body);
  };
  const chooseRandomPromptSentence = () => sentencePool.length
    ? sentencePool[Math.floor(Math.random() * sentencePool.length)]
    : '';
  const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));
  const isLikelyDirectMediaUrl = (url) => /\.(mp4|m4v|mov|webm|m3u8|mp3|m4a|ogg|wav)(\?|#|$)/i.test(url);
  const isUnsupportedNarrativeSourceUrl = (url) => {
    if (!url) {
      return false;
    }
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase();
      if (host.includes('youtube.com') || host.includes('youtu.be')) {
        return true;
      }
    } catch (error) {
      logger.error('Narrative source URL parsing failed.', { error, sourceVideoUrl: url });
      return true;
    }
    return !isLikelyDirectMediaUrl(url);
  };
  const summarizeResponse = async (response) => {
    const body = await response.text();
    return {
      status: response.status,
      statusText: response.statusText,
      url: response.url,
      location: response.headers.get('location'),
      livepeerPlaybackUrl: response.headers.get('livepeer-playback-url'),
      body,
      bodyPreview: body.slice(0, 500),
    };
  };

  const stopPromptUpdates = () => {
    if (promptUpdateTimerId) {
      window.clearInterval(promptUpdateTimerId);
      promptUpdateTimerId = null;
    }
  };

  const pushPromptUpdate = async (prompt) => {
    if (!prompt || !streamSession.sessionId) {
      return;
    }
    try {
      const response = await fetch(`/streams/${encodeURIComponent(streamSession.sessionId)}/prompt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt }),
      });
      if (!response.ok) {
        logger.error('Prompt update returned a non-2xx response.', { sessionId: streamSession.sessionId, status: response.status });
      }
    } catch (error) {
      logger.error('Prompt update failed in the browser.', error);
    }
  };

  const startPromptUpdates = () => {
    stopPromptUpdates();
    if (page !== 'book' || !sentencePool.length) {
      return;
    }
    promptUpdateTimerId = window.setInterval(() => {
      const prompt = chooseRandomPromptSentence();
      if (prompt) {
        void pushPromptUpdate(prompt);
      }
    }, PROMPT_UPDATE_INTERVAL_MS);
  };

  const logPeerConnectionState = (label, pc, extra = {}) => {
    logger.log(`${label}:`, {
      connectionState: pc.connectionState,
      iceConnectionState: pc.iceConnectionState,
      iceGatheringState: pc.iceGatheringState,
      signalingState: pc.signalingState,
      ...extra,
    });
  };

  const stopMediaElement = async (element) => {
    if (!element) {
      return;
    }
    try {
      element.pause();
      element.currentTime = 0;
    } catch (error) {
      logger.error('Media element stop failed.', error);
    }
  };

  const ensureAudioGraph = () => {
    if (!audioContext) {
      audioContext = new window.AudioContext();
      destination = audioContext.createMediaStreamDestination();
      gainNode = audioContext.createGain();
      gainNode.gain.value = 1;
      gainNode.connect(destination);
    }
    return { audioContext, destination, gainNode };
  };

  const disconnectSourceNode = () => {
    if (sourceNode) {
      try {
        sourceNode.disconnect();
      } catch (error) {
        logger.error('Audio source disconnect failed.', error);
      }
      sourceNode = null;
    }
  };

  const bindAudioElementToGraph = (element) => {
    const graph = ensureAudioGraph();
    disconnectSourceNode();
    sourceNode = graph.audioContext.createMediaElementSource(element);
    sourceNode.connect(graph.gainNode);
    graph.gainNode.connect(graph.audioContext.destination);
    return graph.destination.stream;
  };

  const prepareCompositeStream = async () => {
    setOverlay('LamiaLux live canvas', page === 'music' ? 'Visual music canvas ready for WHIP publishing.' : 'Theia canvas capture ready for WHIP publishing.');
    const canvasStream = animationCanvas.captureStream(30);
    let audioStream = null;
    if (page === 'music' && musicPlayer) {
      audioStream = bindAudioElementToGraph(musicPlayer);
    } else if (page === 'book' && usesSourceNarration && narrativeSourceVideo) {
      audioStream = bindAudioElementToGraph(narrativeSourceVideo);
    } else if (page === 'book' && coquiPlayer) {
      audioStream = bindAudioElementToGraph(coquiPlayer);
    }
    compositeStream = new MediaStream([...canvasStream.getVideoTracks(), ...(audioStream ? audioStream.getAudioTracks() : [])]);
    return compositeStream;
  };

  const prepareNarrativeSourceVideo = () => {
    if (!usesSourceNarration || !narrativeSourceVideo || !sourceVideoUrl) {
      return;
    }
    if (isUnsupportedNarrativeSourceUrl(sourceVideoUrl)) {
      logger.error('Unsupported narrative source URL detected; direct browser media playback is required.', {
        narrativeModeProvider,
        sourceVideoUrl,
      });
      setStatus('Source narration URL is not a direct media file. Use a direct MP4/WebM URL.');
      setOverlay(
        'LamiaLux source narration',
        'The selected source is a web page URL (for example YouTube watch) and cannot be played as <video>.',
      );
      return;
    }
    narrativeSourceVideo.src = sourceVideoUrl;
  };

  const refreshStreamSessionAfterWhip = async () => {
    const response = await fetch('/streams/match', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sessionId: browserSessionId }),
    });
    if (!response.ok) {
      logger.error('Stream match request returned a non-2xx response.', { status: response.status });
      throw new Error(`Stream match failed with status ${response.status}`);
    }
    const payload = await response.json();
    streamSession.sessionId = payload.sessionId;
    streamSession.whipUrl = payload.whipUrl;
    streamSession.whepUrl = payload.whepUrl;
    streamSession.outputVideoUrl = payload.outputVideoUrl || '';
    sessionLabel.textContent = `Matched browser session ${payload.sessionId}.`;
  };

  const validateStreamSession = () => {
    if (!streamSession.sessionId || !streamSession.whipUrl) {
      const error = new Error('Missing Livepeer session metadata required for WHIP publishing.');
      logger.error('Livepeer session metadata is incomplete:', { browserSessionId, normalizedStreamSession: streamSession });
      throw error;
    }
  };

  const publishWhip = async () => {
    publisherPc = new RTCPeerConnection(rtcConfig);
    publisherPc.addEventListener('connectionstatechange', () => {
      logPeerConnectionState('WHIP publisher', publisherPc, { sessionId: streamSession.sessionId });
      setStatus(`WHIP publishing connection: ${publisherPc.connectionState}.`);
    });
    publisherPc.addEventListener('iceconnectionstatechange', () => {
      logPeerConnectionState('WHIP publisher ICE', publisherPc, { sessionId: streamSession.sessionId });
    });

    const stream = compositeStream || await prepareCompositeStream();
    stream.getTracks().forEach((track) => publisherPc.addTrack(track, stream));
    const offer = await publisherPc.createOffer();
    await publisherPc.setLocalDescription(offer);
    const response = await fetch(streamSession.whipUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/sdp' },
      body: offer.sdp,
    });
    const responseSummary = await summarizeResponse(response);
    if (!response.ok) {
      logger.error('WHIP publish request returned a non-2xx response.', responseSummary);
      throw new Error(`WHIP connection failed with status ${responseSummary.status}`);
    }
    if (!responseSummary.body) {
      logger.error('WHIP publish response was missing the SDP answer body.', responseSummary);
      throw new Error('WHIP connection failed because the SDP answer was empty.');
    }
    const answer = { type: 'answer', sdp: responseSummary.body };
    try {
      await publisherPc.setRemoteDescription(answer);
    } catch (error) {
      logger.error('Failed to apply WHIP SDP answer as the remote description.', {
        error,
        responseSummary,
      });
      throw error;
    }
  };

  const connectWhep = async () => {
    viewerPc = new RTCPeerConnection(rtcConfig);
    viewerPc.addEventListener('track', (event) => {
      const [remoteStream] = event.streams;
      if (remoteStream) {
        video.srcObject = remoteStream;
      }
    });
    viewerPc.addEventListener('connectionstatechange', () => {
      logPeerConnectionState('WHEP viewer', viewerPc, { sessionId: streamSession.sessionId, whepResourceUrl });
    });
    viewerPc.addEventListener('iceconnectionstatechange', () => {
      logPeerConnectionState('WHEP viewer ICE', viewerPc, { sessionId: streamSession.sessionId, whepResourceUrl });
    });

    const offer = await viewerPc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    await viewerPc.setLocalDescription(offer);

    let lastError = null;
    for (let attempt = 1; attempt <= 4; attempt += 1) {
      const response = await fetch(streamSession.whepUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: offer.sdp,
      });
      const responseSummary = await summarizeResponse(response);
      if (response.ok) {
        whepResourceUrl = responseSummary.location;
        if (!responseSummary.body) {
          logger.error('WHEP playback response was missing the SDP answer body.', responseSummary);
          throw new Error('WHEP connection failed because the SDP answer was empty.');
        }
        try {
          await viewerPc.setRemoteDescription({ type: 'answer', sdp: responseSummary.body });
        } catch (error) {
          logger.error('Failed to apply WHEP SDP answer as the remote description.', {
            error,
            responseSummary,
          });
          throw error;
        }
        return;
      }
      lastError = new Error(`WHEP connection failed with status ${responseSummary.status}`);
      logger.error('WHEP playback request returned a non-2xx response; retrying with backoff.', responseSummary);
      const pollIntervalMs = attempt * 1000;
      setStatus(`WHIP connected. Waiting for WHEP (attempt ${attempt}, retry in ${Math.round(pollIntervalMs / 1000)}s)...`);
      await sleep(pollIntervalMs);
    }
    logger.error('WHEP playback failed after exhausting the configured backoff retries.', lastError);
    throw lastError;
  };

  const ensureStreamingReady = async () => {
    if (streamReady) {
      return;
    }
    if (!connectPromise) {
      connectPromise = (async () => {
        setStatus('Connecting WHIP/WHEP...');
        await refreshStreamSessionAfterWhip();
        validateStreamSession();
        await prepareCompositeStream();
        await publishWhip();
        setStatus('Canvas feed published through WHIP. Waiting for WHEP playback...');
        await connectWhep();
        streamReady = true;
        setStatus('WHIP/WHEP connected.');
      })().catch((error) => {
        logger.error('Streaming setup failed.', error);
        setStatus('Streaming setup failed.');
        throw error;
      }).finally(() => {
        connectPromise = null;
      });
    }
    await connectPromise;
  };

  const highlightChunk = (index) => {
    items.forEach((item, itemIndex) => item.classList.toggle('active', itemIndex === index));
  };

  const playBook = async () => {
    if (!config.hasPreparedStream) {
      setStatus('Search and prepare a PDF before connecting WHIP/WHEP.');
      return;
    }
    cancelled = false;
    playbackToken += 1;
    const localToken = playbackToken;
    await ensureStreamingReady();
    startPromptUpdates();

    if (usesSourceNarration) {
      if (!narrativeSourceVideo || !sourceVideoUrl) {
        logger.error('Source narration provider is enabled but no narrative source video is available.', {
          narrativeModeProvider,
          hasNarrativeSourceVideoElement: Boolean(narrativeSourceVideo),
          sourceVideoUrl,
        });
        setStatus('Narrative source video is missing for this provider.');
        return;
      }
      if (isUnsupportedNarrativeSourceUrl(sourceVideoUrl)) {
        logger.error('Source narration playback blocked because the URL is not a direct media resource.', {
          narrativeModeProvider,
          sourceVideoUrl,
        });
        setStatus('Source narration URL is unsupported. Provide a direct MP4/WebM media URL.');
        return;
      }
      setStatus('Playing source narration video...');
      setOverlay('LamiaLux source narration', 'Playback is using the upstream source video audio.');
      try {
        await narrativeSourceVideo.play();
      } catch (error) {
        logger.error('Source narration video playback failed to start.', {
          error,
          narrativeModeProvider,
          sourceVideoUrl: narrativeSourceVideo.src,
        });
        setStatus('Source narration playback failed to start.');
        throw error;
      }
      return;
    }

    for (let index = currentIndex; index < items.length; index += 1) {
      if (cancelled || localToken !== playbackToken) {
        break;
      }
      currentIndex = index;
      highlightChunk(index);
      setStatus(`Reading chunk ${index + 1} of ${items.length} with Coqui TTS...`);
      setOverlay('LamiaLux live PDF reading', items[index].dataset.text || '');
      coquiPlayer.src = `/tts/coqui?sessionId=${encodeURIComponent(browserSessionId)}&chunkIndex=${index + 1}`;
      try {
        await coquiPlayer.play();
      } catch (error) {
        logger.error('Coqui audio playback failed to start.', { error, chunkIndex: index + 1 });
        setStatus('Coqui TTS playback failed to start.');
        throw error;
      }
      await new Promise((resolve, reject) => {
        const cleanup = () => {
          coquiPlayer.removeEventListener('ended', onEnded);
          coquiPlayer.removeEventListener('error', onError);
        };
        const onEnded = () => { cleanup(); resolve(); };
        const onError = () => {
          cleanup();
          reject(new Error('The Coqui TTS audio element reported an error.'));
        };
        coquiPlayer.addEventListener('ended', onEnded, { once: true });
        coquiPlayer.addEventListener('error', onError, { once: true });
      }).catch((error) => {
        logger.error('Coqui TTS audio playback failed mid-chunk.', { error, chunkIndex: index + 1 });
        setStatus('Coqui TTS failed on the current chunk.');
        throw error;
      });
    }
  };

  const playMusic = async () => {
    if (!musicTrack) {
      setStatus('Search and prepare a Spotify track before playback.');
      return;
    }
    if (!musicTrack.preview_url) {
      logger.error('Spotify track playback requested without a preview URL.', { musicTrack });
      setStatus('Spotify preview is unavailable for this track. Open it on Spotify instead.');
      return;
    }
    await ensureStreamingReady();
    setOverlay('LamiaLux visual music', `${musicTrack.title} · ${musicTrack.artist}`);
    musicPlayer.src = musicTrack.preview_url;
    musicPlayer.loop = true;
    try {
      await musicPlayer.play();
      await pushPromptUpdate(`${musicTrack.title} by ${musicTrack.artist}`);
      setStatus(`Playing ${musicTrack.title} by ${musicTrack.artist}.`);
    } catch (error) {
      logger.error('Spotify preview playback failed to start.', { error, musicTrack });
      setStatus('Spotify preview playback failed to start.');
      throw error;
    }
  };

  const stopEverything = async () => {
    cancelled = true;
    playbackToken += 1;
    currentIndex = 0;
    stopPromptUpdates();
    highlightChunk(-1);
    await stopMediaElement(coquiPlayer);
    await stopMediaElement(musicPlayer);
    await stopMediaElement(narrativeSourceVideo);
    setStatus('Stopped.');
    setOverlay('LamiaLux ready', page === 'music' ? 'Search a track to start visual music playback.' : 'Search a PDF to start narration playback.');
  };

  document.querySelectorAll('[data-nav-target]').forEach((link) => {
    link.addEventListener('click', (event) => {
      const href = link.getAttribute('href');
      const card = document.getElementById('page-flip-card');
      if (!href || !card || href === window.location.pathname) {
        return;
      }
      event.preventDefault();
      card.classList.add('is-flipping');
      window.setTimeout(() => {
        window.location.href = href;
      }, 320);
    });
  });

  connectButton.addEventListener('click', async () => {
    try {
      await ensureStreamingReady();
    } catch (error) {
      logger.error('Connect WHIP/WHEP action failed.', error);
    }
  });
  playButton.addEventListener('click', async () => {
    try {
      if (page === 'music') {
        await playMusic();
      } else {
        await playBook();
      }
    } catch (error) {
      logger.error('Playback action failed.', error);
    }
  });
  stopButton.addEventListener('click', () => { void stopEverything(); });

  prepareNarrativeSourceVideo();
  setOverlay('LamiaLux ready', page === 'music' ? 'Search a track to prepare the visual music canvas.' : 'Search a PDF to prepare the book canvas.');
})();
