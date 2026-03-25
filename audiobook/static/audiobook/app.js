(() => {
  const config = window.lamialuxPageConfig || {};
  const page = config.page || 'book';
  const browserSessionInput = document.getElementById('browser-session-id');
  const playButton = document.getElementById('play-media');
  const stopButton = document.getElementById('stop-media');
  const fullscreenButton = document.getElementById('fullscreen-output');
  const statusEl = document.getElementById('stream-status');
  const animationCanvas = document.getElementById('theia-canvas');
  const animationOverlay = document.getElementById('theia-canvas-overlay');
  const theiaFormSvg = document.getElementById('theia-form-deeper');
  const video = document.getElementById('whep-player');
  const coquiPlayer = document.getElementById('coqui-player');
  const musicPlayer = document.getElementById('music-player');
  const sessionLabel = document.getElementById('stream-session-label');
  const items = Array.from(document.querySelectorAll('.chunk-list li'));
  const musicTrack = config.musicTrack || null;
  const narrativeModeProvider = String(config.narrativeModeProvider || '').trim().toUpperCase();
  const narrativeSourceVideo = document.getElementById('narrative-source-video');
  const sourceVideoUrl = String(config.sourceVideoUrl || '').trim();
  const isSourceNarrationProvider = narrativeModeProvider === 'YOUTUBE_SEARCH';
  const PROMPT_UPDATE_INTERVAL_MS = 5000;
  const STORY_PROMPT_DELTA_SECONDS = Number(config.updateStoryPromptDeltaSeconds || 10);
  const logger = window.logger && typeof window.logger.error === 'function' ? window.logger : console;

  if (!playButton || !stopButton || !statusEl || !animationCanvas || !animationOverlay || !video || !sessionLabel) {
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
  let promptUpdateTimerId = null;
  let storyPromptTimerId = null;
  let compositeCanvas = null;
  let compositeContext = null;
  let compositeFrameTimerId = null;
  let theiaSvgRefreshTimerId = null;
  let theiaSvgTexture = null;
  let theiaSvgTextureUrl = '';
  let theiaSvgAnimatorId = null;
  let lipAudioContext = null;
  let lipAudioAnalyser = null;
  let lipAudioSourceNode = null;
  let lipAudioData = null;
  let lipAudioElement = null;
  let playbackToken = 0;
  let currentIndex = 0;
  let cancelled = false;
  let youtubePlayer = null;
  let youtubePlayerReadyPromise = null;

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
  const waitForIceGatheringComplete = async (pc) => new Promise((resolve) => {
    if (!pc || pc.iceGatheringState === 'complete') {
      resolve();
      return;
    }
    const onIceGatheringStateChange = () => {
      if (pc.iceGatheringState === 'complete') {
        pc.removeEventListener('icegatheringstatechange', onIceGatheringStateChange);
        resolve();
      }
    };
    pc.addEventListener('icegatheringstatechange', onIceGatheringStateChange);
  });
  const parseRelativeWhepResourcePath = (locationHeader) => {
    if (!locationHeader) {
      return '';
    }
    try {
      return new URL(locationHeader, window.location.origin).pathname;
    } catch (error) {
      logger.error('WHEP location header parsing failed.', { error, locationHeader });
      return '';
    }
  };
  const buildTrickleIceSdpFragment = (candidate, ufrag, mid) => {
    if (!candidate || !candidate.candidate || !ufrag) {
      return '';
    }
    return [
      'a=ice-options:trickle',
      `a=ice-ufrag:${ufrag}`,
      `m=video 9 UDP/TLS/RTP/SAVPF 96`,
      `a=mid:${mid || '0'}`,
      `a=${candidate.candidate}`,
      '',
    ].join('\r\n');
  };
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
  const extractYouTubeVideoId = (url) => {
    if (!url) {
      return '';
    }
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase();
      if (host.includes('youtu.be')) {
        return parsed.pathname.replace('/', '').trim();
      }
      if (host.includes('youtube.com')) {
        if (parsed.pathname === '/watch') {
          return String(parsed.searchParams.get('v') || '').trim();
        }
        if (parsed.pathname.startsWith('/embed/')) {
          return parsed.pathname.replace('/embed/', '').trim();
        }
      }
    } catch (error) {
      logger.error('YouTube video ID extraction failed because the URL is invalid.', { error, sourceVideoUrl: url });
    }
    return '';
  };
  const loadYouTubeIframeApi = () => {
    if (window.YT && typeof window.YT.Player === 'function') {
      return Promise.resolve(window.YT);
    }
    if (!youtubePlayerReadyPromise) {
      youtubePlayerReadyPromise = new Promise((resolve, reject) => {
        const finishIfReady = () => {
          if (window.YT && typeof window.YT.Player === 'function') {
            resolve(window.YT);
            return true;
          }
          return false;
        };
        if (finishIfReady()) {
          return;
        }
        const previousReady = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = () => {
          if (typeof previousReady === 'function') {
            previousReady();
          }
          if (!finishIfReady()) {
            logger.error('YouTube Iframe API reported ready, but the Player constructor is unavailable.', { sourceVideoUrl });
            reject(new Error('YouTube player constructor is unavailable.'));
          }
        };
        const existingScript = document.getElementById('youtube-iframe-api');
        if (existingScript) {
          window.setTimeout(() => {
            if (!finishIfReady()) {
              logger.error('YouTube Iframe API script exists, but the API did not initialize in time.', { sourceVideoUrl });
              reject(new Error('YouTube Iframe API did not initialize in time.'));
            }
          }, 5000);
          return;
        }
        const script = document.createElement('script');
        script.id = 'youtube-iframe-api';
        script.src = 'https://www.youtube.com/iframe_api';
        script.async = true;
        script.onerror = (error) => {
          logger.error('YouTube Iframe API script failed to load.', { error, sourceVideoUrl });
          reject(new Error('YouTube Iframe API script failed to load.'));
        };
        document.head.appendChild(script);
      });
    }
    return youtubePlayerReadyPromise;
  };
  const ensureYouTubePlayer = async (videoId) => {
    if (!videoId) {
      const error = new Error('YouTube video ID is missing.');
      logger.error('Cannot initialize YouTube player without a video ID.', { sourceVideoUrl });
      throw error;
    }
    await loadYouTubeIframeApi();
    if (youtubePlayer && typeof youtubePlayer.loadVideoById === 'function') {
      youtubePlayer.loadVideoById(videoId);
      return youtubePlayer;
    }
    let host = document.getElementById('youtube-source-player');
    if (!host) {
      host = document.createElement('div');
      host.id = 'youtube-source-player';
      host.style.position = 'fixed';
      host.style.left = '-9999px';
      host.style.top = '0';
      host.style.width = '320px';
      host.style.height = '180px';
      host.style.opacity = '0';
      host.style.pointerEvents = 'none';
      document.body.appendChild(host);
    }
    if (!window.YT || typeof window.YT.Player !== 'function') {
      logger.error('YouTube player API is unavailable after loading the Iframe API.', { sourceVideoUrl, hasWindowYT: !!window.YT });
      throw new Error('YouTube player API is unavailable.');
    }
    youtubePlayer = await new Promise((resolve, reject) => {
      const player = new window.YT.Player('youtube-source-player', {
        width: '320',
        height: '180',
        videoId,
        playerVars: { autoplay: 1, controls: 0, modestbranding: 1, rel: 0 },
        events: {
          onReady: () => resolve(player),
          onError: (event) => {
            logger.error('YouTube embedded player reported an initialization error.', {
              sourceVideoUrl,
              eventData: event && typeof event.data !== 'undefined' ? event.data : null,
            });
            reject(new Error('YouTube embedded player failed to initialize.'));
          },
        },
      });
      window.setTimeout(() => {
        reject(new Error('Timed out while waiting for the YouTube embedded player to initialize.'));
      }, 8000);
    });
    return youtubePlayer;
  };
  const playYouTubeSourceNarration = async (url) => {
    const videoId = extractYouTubeVideoId(url);
    if (!videoId) {
      const error = new Error('YouTube watch URL does not contain a valid video ID.');
      logger.error('YouTube source playback failed because the URL did not contain a valid video ID.', { sourceVideoUrl: url });
      throw error;
    }
    const player = await ensureYouTubePlayer(videoId);
    if (typeof player.playVideo !== 'function') {
      const error = new Error('YouTube player is unavailable.');
      logger.error('YouTube source playback failed because the YouTube player API is unavailable.', { sourceVideoUrl: url });
      throw error;
    }
    player.playVideo();
  };
  const stopYouTubeSourceNarration = () => {
    if (youtubePlayer && typeof youtubePlayer.stopVideo === 'function') {
      youtubePlayer.stopVideo();
    }
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

  const getLipAudioIntensity = () => {
    if (!lipAudioAnalyser || !lipAudioData) {
      return 0;
    }
    lipAudioAnalyser.getByteFrequencyData(lipAudioData);
    let energy = 0;
    for (let index = 0; index < lipAudioData.length; index += 1) {
      energy += lipAudioData[index];
    }
    const averageEnergy = energy / (lipAudioData.length || 1);
    return Math.min(1, averageEnergy / 255);
  };

  const detachLipAudioInput = () => {
    if (lipAudioSourceNode) {
      try {
        lipAudioSourceNode.disconnect();
      } catch (error) {
        logger.error('Failed to disconnect the existing Theia mouth audio input node.', { error });
      }
    }
    lipAudioSourceNode = null;
    lipAudioElement = null;
  };

  const connectLipAudioInput = (mediaElement) => {
    if (!mediaElement) {
      detachLipAudioInput();
      return;
    }
    if (lipAudioElement === mediaElement && lipAudioAnalyser) {
      return;
    }
    try {
      if (!lipAudioContext) {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextClass) {
          logger.error('Theia mouth audio input cannot be attached because AudioContext is unavailable.');
          return;
        }
        lipAudioContext = new AudioContextClass();
      }
      if (!lipAudioAnalyser) {
        lipAudioAnalyser = lipAudioContext.createAnalyser();
        lipAudioAnalyser.fftSize = 512;
        lipAudioAnalyser.smoothingTimeConstant = 0.82;
        lipAudioData = new Uint8Array(lipAudioAnalyser.frequencyBinCount);
      }
      detachLipAudioInput();
      lipAudioSourceNode = lipAudioContext.createMediaElementSource(mediaElement);
      lipAudioSourceNode.connect(lipAudioAnalyser);
      lipAudioAnalyser.connect(lipAudioContext.destination);
      lipAudioElement = mediaElement;
      if (lipAudioContext.state === 'suspended') {
        void lipAudioContext.resume().catch((error) => {
          logger.error('Theia mouth audio input resume failed.', { error });
        });
      }
    } catch (error) {
      logger.error('Theia mouth audio input connection failed.', { error });
    }
  };

  const updateTheiaLips = (timestampMs) => {
    if (!theiaFormSvg) {
      return;
    }
    const mouthUpper = theiaFormSvg.querySelector('#mouthUpper');
    const mouthLower = theiaFormSvg.querySelector('#mouthLower');
    const mouthInside = theiaFormSvg.querySelector('#mouthInside');
    if (!mouthUpper || !mouthLower || !mouthInside) {
      logger.error('Theia form lip animation failed because one or more SVG mouth paths are missing.');
      return;
    }
    const oscillation = (Math.sin(timestampMs / 220) + 1) / 2;
    const audioIntensity = getLipAudioIntensity();
    const blend = Math.min(1, (oscillation * 0.55) + (audioIntensity * 0.9));
    const width = 18 + (blend * 7.5);
    const upperLift = 186 + (blend * 2.6);
    const lowerDrop = 191 + (blend * 8);
    const cornerLeft = 150 - width;
    const cornerRight = 150 + width;
    mouthUpper.setAttribute('d', `M${cornerLeft} 190 Q150 ${upperLift} ${cornerRight} 190 Q150 ${192 + (blend * 1.5)} ${cornerLeft} 190 Z`);
    mouthLower.setAttribute('d', `M${cornerLeft} 190 Q150 ${lowerDrop} ${cornerRight} 190 Q150 ${191 + (blend * 3.2)} ${cornerLeft} 190 Z`);
    mouthInside.setAttribute('d', `M${cornerLeft + 2} 190 Q150 ${189 + (blend * 5.8)} ${cornerRight - 2} 190 Q150 ${190 + (blend * 4.5)} ${cornerLeft + 2} 190 Z`);
  };

  const startTheiaSvgAnimator = () => {
    if (!theiaFormSvg) {
      return;
    }
    const animate = (timestampMs) => {
      updateTheiaLips(timestampMs);
      theiaSvgAnimatorId = window.requestAnimationFrame(animate);
    };
    if (!theiaSvgAnimatorId) {
      theiaSvgAnimatorId = window.requestAnimationFrame(animate);
    }
  };

  const refreshTheiaSvgTexture = () => {
    if (!theiaFormSvg) {
      return;
    }
    try {
      const serializedSvg = new XMLSerializer().serializeToString(theiaFormSvg);
      const blob = new Blob([serializedSvg], { type: 'image/svg+xml;charset=utf-8' });
      const nextUrl = URL.createObjectURL(blob);
      if (!theiaSvgTexture) {
        theiaSvgTexture = new Image();
      }
      theiaSvgTexture.onload = () => {
        if (theiaSvgTextureUrl) {
          URL.revokeObjectURL(theiaSvgTextureUrl);
        }
        theiaSvgTextureUrl = nextUrl;
      };
      theiaSvgTexture.onerror = () => {
        logger.error('Theia SVG texture refresh failed because the serialized SVG image could not be decoded.');
        URL.revokeObjectURL(nextUrl);
      };
      theiaSvgTexture.src = nextUrl;
    } catch (error) {
      logger.error('Theia SVG serialization failed during canvas composition.', { error });
    }
  };

  const stopCompositeFrameLoop = () => {
    if (compositeFrameTimerId) {
      window.clearInterval(compositeFrameTimerId);
      compositeFrameTimerId = null;
    }
    if (theiaSvgRefreshTimerId) {
      window.clearInterval(theiaSvgRefreshTimerId);
      theiaSvgRefreshTimerId = null;
    }
  };

  const startCompositeFrameLoop = () => {
    if (!compositeCanvas || !compositeContext) {
      logger.error('Cannot start the composite canvas loop because the composition canvas context is missing.');
      return;
    }
    stopCompositeFrameLoop();
    compositeFrameTimerId = window.setInterval(() => {
      compositeContext.clearRect(0, 0, compositeCanvas.width, compositeCanvas.height);
      if (theiaSvgTexture && theiaSvgTexture.complete) {
        const baseWidth = Number(theiaSvgTexture.naturalWidth) || 300;
        const baseHeight = Number(theiaSvgTexture.naturalHeight) || 300;
        const maxWidth = compositeCanvas.width * 0.3;
        const maxHeight = compositeCanvas.height * 0.6;
        const scale = Math.min(maxWidth / baseWidth, maxHeight / baseHeight);
        const drawWidth = baseWidth * scale;
        const drawHeight = baseHeight * scale;
        const drawX = (compositeCanvas.width - drawWidth) / 2;
        const drawY = (compositeCanvas.height - drawHeight) / 2;
        compositeContext.drawImage(theiaSvgTexture, drawX, drawY, drawWidth, drawHeight);
      }
      compositeContext.drawImage(animationCanvas, 0, 0, compositeCanvas.width, compositeCanvas.height);
    }, 33);
  };

  const stopStoryPromptUpdates = () => {
    if (storyPromptTimerId) {
      window.clearInterval(storyPromptTimerId);
      storyPromptTimerId = null;
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
    stopStoryPromptUpdates();
    if (page !== 'book' || !sentencePool.length) {
      return;
    }
    if (isSourceNarrationProvider && sourceVideoUrl) {
      const videoId = extractYouTubeVideoId(sourceVideoUrl);
      if (videoId) {
        storyPromptTimerId = window.setInterval(() => {
          const currentSeconds = youtubePlayer && typeof youtubePlayer.getCurrentTime === 'function'
            ? Number(youtubePlayer.getCurrentTime() || 0)
            : 0;
          const fallbackText = chooseRandomPromptSentence();
          void fetch(`/streams/${encodeURIComponent(streamSession.sessionId)}/story-prompt`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              videoId,
              currentSeconds,
              fallbackText,
            }),
          }).then((response) => {
            if (!response.ok) {
              logger.error('Story prompt update returned a non-2xx response.', {
                status: response.status,
                sessionId: streamSession.sessionId,
                currentSeconds,
                deltaSeconds: STORY_PROMPT_DELTA_SECONDS,
              });
            }
          }).catch((error) => {
            logger.error('Story prompt update failed in the browser.', {
              error,
              sessionId: streamSession.sessionId,
              currentSeconds,
              deltaSeconds: STORY_PROMPT_DELTA_SECONDS,
            });
          });
        }, PROMPT_UPDATE_INTERVAL_MS);
        return;
      }
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

  const prepareCompositeStream = async () => {
    setOverlay('LamiaLux live canvas', page === 'music' ? 'Visual music canvas ready for WHIP publishing.' : 'Theia canvas capture ready for WHIP publishing.');
    if (!compositeCanvas) {
      compositeCanvas = document.createElement('canvas');
      compositeCanvas.width = animationCanvas.width;
      compositeCanvas.height = animationCanvas.height;
      compositeContext = compositeCanvas.getContext('2d');
      if (!compositeContext) {
        logger.error('Failed to initialize the offscreen Theia composition canvas context.');
        throw new Error('Theia composition canvas context is unavailable.');
      }
      startTheiaSvgAnimator();
      refreshTheiaSvgTexture();
      theiaSvgRefreshTimerId = window.setInterval(refreshTheiaSvgTexture, 120);
      startCompositeFrameLoop();
    }
    if (!compositeFrameTimerId) {
      logger.error('Composite frame loop was not running while preparing the stream; restarting the canvas compositor.');
      startCompositeFrameLoop();
    }
    if (!theiaSvgRefreshTimerId) {
      logger.error('Theia SVG refresh loop was not running while preparing the stream; restarting the texture refresh timer.');
      theiaSvgRefreshTimerId = window.setInterval(refreshTheiaSvgTexture, 120);
    }
    const canvasStream = compositeCanvas.captureStream(30);
    compositeStream = new MediaStream([...canvasStream.getVideoTracks()]);
    return compositeStream;
  };

  const prepareNarrativeSourceVideo = () => {
    if (!isSourceNarrationProvider || !sourceVideoUrl) {
      return;
    }
    if (extractYouTubeVideoId(sourceVideoUrl)) {
      setStatus('YouTube source prepared. Press play to start embedded playback.');
      setOverlay('LamiaLux source narration', 'YouTube source will play through an embedded player on Play.');
      return;
    }
    if (!narrativeSourceVideo) {
      logger.error('Source narration provider requires the narrative source video element, but it is missing.', {
        narrativeModeProvider,
        sourceVideoUrl,
      });
      setStatus('Narrative source video element is missing.');
      return;
    }
    if (isUnsupportedNarrativeSourceUrl(sourceVideoUrl)) {
      logger.log('Narrative source URL is not a direct media file; browser playback will fall back to Coqui TTS.', {
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
    let localIceUfrag = '';
    let trickleBuffer = [];
    let trickleFlushTimerId = null;
    const flushTrickleBuffer = async () => {
      if (!whepResourceUrl || !trickleBuffer.length) {
        return;
      }
      const candidatesToFlush = trickleBuffer;
      trickleBuffer = [];
      for (const candidateInfo of candidatesToFlush) {
        const fragment = buildTrickleIceSdpFragment(candidateInfo.candidate, localIceUfrag, candidateInfo.mid);
        if (!fragment) {
          continue;
        }
        try {
          const patchResponse = await fetch(whepResourceUrl, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/trickle-ice-sdpfrag' },
            body: fragment,
          });
          if (!patchResponse.ok) {
            logger.error('WHEP trickle ICE PATCH returned a non-2xx response.', {
              status: patchResponse.status,
              whepResourceUrl,
            });
          }
        } catch (error) {
          logger.error('WHEP trickle ICE PATCH failed.', { error, whepResourceUrl });
        }
      }
    };
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
    viewerPc.addEventListener('icecandidate', (event) => {
      if (!event.candidate) {
        return;
      }
      trickleBuffer.push({
        candidate: event.candidate,
        mid: event.candidate.sdpMid || '0',
      });
      if (whepResourceUrl) {
        if (trickleFlushTimerId) {
          window.clearTimeout(trickleFlushTimerId);
        }
        trickleFlushTimerId = window.setTimeout(() => {
          trickleFlushTimerId = null;
          void flushTrickleBuffer();
        }, 150);
      }
    });

    const offer = await viewerPc.createOffer({ offerToReceiveAudio: true, offerToReceiveVideo: true });
    await viewerPc.setLocalDescription(offer);
    await waitForIceGatheringComplete(viewerPc);
    const finalizedOfferSdp = viewerPc.localDescription && viewerPc.localDescription.sdp
      ? viewerPc.localDescription.sdp
      : offer.sdp;
    const ufragMatch = finalizedOfferSdp.match(/^a=ice-ufrag:(.+)$/m);
    localIceUfrag = ufragMatch ? ufragMatch[1].trim() : '';

    let lastError = null;
    for (let attempt = 1; attempt <= 4; attempt += 1) {
      const response = await fetch(streamSession.whepUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: finalizedOfferSdp,
      });
      const responseSummary = await summarizeResponse(response);
      if (response.ok) {
        whepResourceUrl = parseRelativeWhepResourcePath(responseSummary.location);
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
        await flushTrickleBuffer();
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

    if (isSourceNarrationProvider) {
      if (!sourceVideoUrl) {
        logger.error('Source narration provider is enabled but no narrative source URL is available.', {
          narrativeModeProvider,
          sourceVideoUrl,
        });
        setStatus('Narrative source URL is missing for this provider.');
        return;
      }
      const youtubeVideoId = extractYouTubeVideoId(sourceVideoUrl);
      if (youtubeVideoId) {
        setStatus('Playing source narration from embedded YouTube player...');
        setOverlay('LamiaLux source narration', 'Embedded YouTube source playback started.');
        try {
          await playYouTubeSourceNarration(sourceVideoUrl);
        } catch (error) {
          logger.error('YouTube source narration playback failed to start from the embedded player.', {
            error,
            narrativeModeProvider,
            sourceVideoUrl,
          });
          setStatus('YouTube source narration playback failed; falling back to Coqui TTS playback.');
        }
      }
      if (!narrativeSourceVideo) {
        logger.error('Source narration provider requires the narrative source video element, but it is missing.', {
          narrativeModeProvider,
          sourceVideoUrl,
        });
        setStatus('Narrative source video element is missing for this provider.');
        return;
      }
      if (isUnsupportedNarrativeSourceUrl(sourceVideoUrl)) {
        logger.log('Source narration URL is not a direct media resource; falling back to Coqui TTS playback.', {
          narrativeModeProvider,
          sourceVideoUrl,
        });
        setStatus('Source narration URL is unsupported. Falling back to Coqui TTS playback.');
      } else {
        setStatus('Playing source narration video...');
        setOverlay('LamiaLux source narration', 'Playback is using the upstream source video audio.');
        try {
          connectLipAudioInput(narrativeSourceVideo);
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
      connectLipAudioInput(coquiPlayer);
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
    connectLipAudioInput(musicPlayer);
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
    stopStoryPromptUpdates();
    highlightChunk(-1);
    await stopMediaElement(coquiPlayer);
    await stopMediaElement(musicPlayer);
    await stopMediaElement(narrativeSourceVideo);
    stopYouTubeSourceNarration();
    detachLipAudioInput();
    stopCompositeFrameLoop();
    if (theiaSvgAnimatorId) {
      window.cancelAnimationFrame(theiaSvgAnimatorId);
      theiaSvgAnimatorId = null;
    }
    if (theiaSvgTextureUrl) {
      URL.revokeObjectURL(theiaSvgTextureUrl);
      theiaSvgTextureUrl = '';
    }
    setStatus('Stopped.');
    setOverlay('LamiaLux ready', page === 'music' ? 'Search a track to start visual music playback.' : 'Search a PDF to start narration playback.');
  };

  const enterFullscreen = async () => {
    try {
      if (video.requestFullscreen) {
        await video.requestFullscreen();
        return;
      }
      if (animationCanvas.requestFullscreen) {
        await animationCanvas.requestFullscreen();
      }
    } catch (error) {
      logger.error('Fullscreen request failed.', error);
      setStatus('Fullscreen mode is unavailable in this browser.');
    }
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
  if (fullscreenButton) {
    fullscreenButton.addEventListener('click', () => { void enterFullscreen(); });
  }

  prepareNarrativeSourceVideo();
  setOverlay('LamiaLux ready', page === 'music' ? 'Search a track to prepare the visual music canvas.' : 'Search a PDF to prepare the book canvas.');
})();
