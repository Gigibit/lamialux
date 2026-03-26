import { SvgStreamer } from "/luna/public/form.js";
/**
 * Gestisce la conversazione con un'AI audio e lo streaming del suo output video.
 */
// In main.js

class Theia {

  constructor() {
    this.conversationConnection = new RTCPeerConnection();
    this.livepeerConnection = null;
    this.playbackConnection = null;
    this.reconnectTimeoutId = null;
    this.outChannel = null;
    this.svgStreamer = new SvgStreamer('theia-form');
    this.inputStream = null;
    this.remoteAudioElement = null;
    this.audioCtx = null;
    this.currentAmplifiedStream = null;
    this.theiaVideoSender = null;
    this.sessionId = this._generateSessionId()
    this.theiaAudioSender = null;
    this.placeholderAudioElement = null; // Aggiunto per tracciare l'elemento audio del placeholder
    this.fullscreenBtn = document.getElementById('fullscreen-btn');
    this.muteBtn = document.getElementById('mute-btn');
    this.canvasTheiaIdea = document.getElementById("theia-canvas");
    this.lunaDisplayBodyButton = document.getElementById('luna-body-btn');
    this.canvasTheiaBody = document.getElementById("theia-form");
    // --- MODIFICA 1: Aggiungi il canvas dello streamer alla pagina ---
    // Questo è essenziale per far funzionare getBoundingClientRect().
    if (this.svgStreamer.canvas) {
      document.body.appendChild(this.svgStreamer.canvas);
    }
    this._setupEventListeners()
    if (this.isMobile()) {
      this.canvasTheiaBody.remove();

      // --- MODIFICATION 2: Use dynamic import instead of appending a script tag ---
      this._initializeMobileAvatar();
    }
  }

  /**
   * Dynamically imports and initializes the mobile avatar module.
   * This is the correct, modern way to handle dynamic script loading.
   */
  async _initializeMobileAvatar() {
    try {
      // Use dynamic import() which returns a promise.
      const module = await import('/luna/public/body.js');
      const LunaAvatar = module.default; // Get the default export from the module

      // Instantiate the avatar and store the instance.
      this.lunaAvatar = new LunaAvatar();
      console.log("✅ Mobile avatar loaded and initialized successfully.");

    } catch (error) {
      console.error("❌ Failed to dynamically load the mobile avatar module:", error);
    }
  }

  _generateSessionId(length = 6) {
    let sessionId = localStorage.getItem('_THEIA')
    if (sessionId) return sessionId
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('sessionId')) return urlParams.get('sessionId');
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    let result = '';
    for (let i = 0; i < length; i++) {
      result += chars.charAt(Math.floor(Math.random() * chars.length));
    }
    sessionId = result + '_THEIA'
    localStorage.setItem('_THEIA', sessionId)
    return sessionId;
  }

  _toggleDisplayBody() {
    if (!this.canvasTheiaBody) {
      this.canvasTheiaBody = document.getElementById("theia-form");
    }
    const isCurrentlyBodyDisplayed = document.body.classList.contains('luna-body-undisplayed');
    const willBodyBeUndisplayed = !isCurrentlyBodyDisplayed;
    this.canvasTheiaBody.style.opacity = willBodyBeUndisplayed ? '0' : '1'
    window.isCurrentlyBodyDisplayed = !willBodyBeUndisplayed;
    document.body.classList.toggle('luna-body-undisplayed');

  }

  _toggleFullScreen() {
    if (!document.fullscreenElement &&    // Standard
      !document.mozFullScreenElement && // Firefox
      !document.webkitFullscreenElement && // Chrome, Safari and Opera
      !document.msFullscreenElement) {  // IE/Edge
      if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen();
      } else if (document.documentElement.mozRequestFullScreen) { /* Firefox */
        document.documentElement.mozRequestFullScreen();
      } else if (document.documentElement.webkitRequestFullscreen) { /* Chrome, Safari & Opera */
        document.documentElement.webkitRequestFullscreen();
      } else if (document.documentElement.msRequestFullscreen) { /* IE/Edge */
        document.documentElement.msRequestFullscreen();
      }
    } else {
      if (document.exitFullscreen) {
        document.exitFullscreen();
      } else if (document.mozCancelFullScreen) { /* Firefox */
        document.mozCancelFullScreen();
      } else if (document.webkitExitFullscreen) { /* Chrome, Safari and Opera */
        document.webkitExitFullscreen();
      } else if (document.msExitFullscreen) { /* IE/Edge */
        document.msExitFullscreen();
      }
    }
  }

  /**
   * Avvia la conversazione e lo streaming video.
   * @param {MediaStream} inputStream - Lo stream audio del microfono dell'utente.
   * @param {HTMLElement} targetVideoElement - L'elemento <video> in cui mostrare lo stream di Theia.
   */
  async startConversation(inputStream, targetVideoElement) {
    if (window.theiaDoesExist) return
    window.theiaDoesExist = true
    if (!inputStream) throw new Error("Input stream per Theia mancante.");
    if (!targetVideoElement) throw new Error("Elemento video di destinazione per Theia mancante.");

    this.inputStream = inputStream
    try {
      // FASE 1: Ottieni un WHIP URL per lo stream video di Theia
      const streamSessionRes = await fetch('/stream-session', {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ sessionId: this.sessionId })
      });

      if (streamSessionRes.status === 401) {
        console.error("Accesso non autorizzato. Reindirizzo al login...");
        window.location.href = '/?redirect_uri=' + encodeURIComponent('/luna'); // O la tua pagina di login
        return; // Interrompe l'esecuzione della funzione
      }
      if (!streamSessionRes.ok) throw new Error("Errore nel recuperare il whipUrl per Theia");
      const sessionData = await streamSessionRes.json();
      const whipUrl = sessionData.whipUrl;

      // FASE 1.5: Avvia lo streaming verso Livepeer e ottieni il WHEP URL
      const whepUrl = await this._startLivepeerStreamWithPlaceholder(whipUrl);
      if (whepUrl) {
        // FASE 1.6: Avvia il polling per il playback del video di Theia
        this._handlePlayback(targetVideoElement, whepUrl);
      }

    } catch (e) {
      console.error("❌ Errore in Theia.startConversation:", e.message);
    }
  }


  // This function creates a canvas and handles resizing
  createFullscreenCanvas() {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');

    // Style the canvas to fill the screen
    canvas.style.position = 'fixed';
    canvas.style.left = '0';
    canvas.style.top = '0';
    canvas.style.zIndex = '9999'; // Below your top-most UI
    canvas.style.pointerEvents = 'none'; // Clicks pass through

    // Function to handle resizing
    function resize() {
      // Use devicePixelRatio for sharp, high-res rendering
      const dpr = window.devicePixelRatio || 1;
      canvas.width = window.innerWidth * dpr;
      canvas.height = window.innerHeight * dpr;
      canvas.style.width = window.innerWidth + 'px';
      canvas.style.height = window.innerHeight + 'px';
      // Scale the context to account for the higher resolution
      ctx.scale(dpr, dpr);
    }

    window.addEventListener('resize', resize);
    resize(); // Call once to set initial size

    return { canvas, ctx };
  }


  isMobile() {
    return /Mobi|Android/i.test(navigator.userAgent);
  }
  /**
 * Merge two canvases into a new canvas that preserves their DOM positions.
 * The returned canvas supports .captureStream().
 *
 * @param {HTMLCanvasElement} canvas1 - First source canvas
 * @param {HTMLCanvasElement} canvas2 - Second source canvas
 * @returns {HTMLCanvasElement} - The merged canvas
 */
  mergeCanvases(canvas1, canvas2, options = {}) {
    const { speed = 0.08 } = options;
    const { canvas: mergedCanvas, ctx } = this.createFullscreenCanvas();

    // Helper function for smooth animation
    const lerp = (start, end, amount) => start * (1 - amount) + end * amount;

    // State for our animation
    // We'll smoothly animate the offset needed to center the content.
    let currentOffsetX = 0;
    let currentOffsetY = 0;

    // Sostituisci la funzione draw() esistente con questa
    function draw() {
      // 1. Calcola il rettangolo di contorno (bounding box) di tutto il contenuto
      const rect1 = canvas1.getBoundingClientRect();
      const rect2 = canvas2.getBoundingClientRect();

      const minX = Math.min(rect1.left, rect2.left);
      const minY = Math.min(rect1.top, rect2.top);
      const contentWidth = Math.max(rect1.right, rect2.right) - minX;
      const contentHeight = Math.max(rect1.bottom, rect2.bottom) - minY;

      // 2. Calcola la POSIZIONE TARGET per centrare il blocco
      // Questa è la coordinata (x, y) dove l'angolo in alto a sinistra del nostro blocco dovrebbe trovarsi.
      const targetX = (window.innerWidth / 2) - (contentWidth / 2);
      const targetY = (window.innerHeight / 2) - (contentHeight / 2);

      // 3. Anima la posizione corrente del blocco verso la posizione target
      // Usiamo le stesse variabili di prima, ma ora rappresentano la posizione del blocco.
      currentOffsetX = lerp(currentOffsetX, targetX, speed);
      currentOffsetY = lerp(currentOffsetY, targetY, speed);

      // 4. Pulisci l'intero canvas
      ctx.clearRect(0, 0, mergedCanvas.width, mergedCanvas.height);

      // 5. Applica la traslazione per spostare l'ORIGINE del disegno
      // Ci spostiamo all'angolo in alto a sinistra dove il nostro blocco centrato deve iniziare.
      ctx.save();
      ctx.translate(currentOffsetX, currentOffsetY);

      // 6. Disegna i canvas RELATIVAMENTE al loro blocco
      // Ora che l'origine è a posto, disegniamo ogni canvas non alla sua posizione 
      // assoluta (rect1.left), ma alla sua posizione relativa all'inizio del blocco (rect1.left - minX).
      ctx.drawImage(canvas1, rect1.left - minX, rect1.top - minY, rect1.width, rect1.height);
      ctx.drawImage(canvas2, rect2.left - minX, rect2.top - minY, rect2.width, rect2.height);

      // Ripristina la traslazione
      ctx.restore();

      // Loop
      requestAnimationFrame(draw);
    }

    draw();
    return mergedCanvas;
  }

  // --- MODIFICA 2: Cambia la logica di creazione dello stream ---
  _createSpectrogramVideoStream() {
    // return document.getElementById('video').captureStream();
    
    if (this.isMobile()) {
      // La logica per i dispositivi mobili rimane invariata
      return this.canvasTheiaIdea.captureStream(30);
    } else {
      // Per desktop, uniamo i due canvas e catturiamo lo stream dal risultato
      console.log("Merging canvases for desktop stream...");

      // Passiamo il canvas dello spettrogramma e il canvas specchio dell'SVG
      const mergedCanvas = this.mergeCanvases(this.canvasTheiaIdea, this.svgStreamer.canvas);

      // Catturiamo lo stream dal canvas appena creato che contiene l'unione
      return mergedCanvas.captureStream(30);
    }
  }


  async _startLivepeerStreamWithPlaceholder(whipUrl) {
    try {
      this.livepeerConnection = new RTCPeerConnection();
      let stream = this._createSpectrogramVideoStream()
      let streamTrack = stream.getVideoTracks()[0]

      this.theiaVideoSender = this.livepeerConnection.addTrack(streamTrack, stream);

      const offer = await this.livepeerConnection.createOffer();
      await this.livepeerConnection.setLocalDescription(offer);

      const whipResponse = await fetch(whipUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: this.livepeerConnection.localDescription.sdp
      });
      if (whipResponse.status !== 201) throw new Error(`Connessione WHIP di Theia fallita: ${whipResponse.statusText}`);

      const whepUrl = whipResponse.headers.get('livepeer-playback-url')?.replace('fra-ai-prod-livepeer-ai-gateway-0.livepeer.com', 'ai.livepeer.com');
      if (!whepUrl) throw new Error('Header livepeer-playback-url mancante per Theia.');

      const answerSdp = await whipResponse.text();
      await this.livepeerConnection.setRemoteDescription({ type: 'answer', sdp: answerSdp });
      console.log(`✅ Theia: Stream pubblico pronto per essere visualizzato.`);
      return whepUrl;
    } catch (error) {
      console.error("❌ Errore durante lo streaming di Theia a Livepeer:", error);
      return null;
    }
  }
  _createBeepAudioStream(frequency = 440, duration = 0.1, interval = 2) {
    // 1. Crea un AudioContext
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();

    // 2. Crea un OscillatorNode (onda sinusoidale)
    const oscillator = audioCtx.createOscillator();
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, audioCtx.currentTime);

    // 3. Crea un GainNode per controllare il volume
    const gainNode = audioCtx.createGain();
    gainNode.gain.setValueAtTime(0, audioCtx.currentTime); // parte silenzioso

    // 4. Collega oscillator -> gain -> MediaStreamDestination
    const dest = audioCtx.createMediaStreamDestination();
    oscillator.connect(gainNode);
    gainNode.connect(dest);

    // 5. Avvia l'oscillatore
    oscillator.start();

    // 6. Funzione per fare bip-bip
    setInterval(() => {
      // accendi volume
      gainNode.gain.setValueAtTime(0.2, audioCtx.currentTime);
      // spegni dopo "duration" secondi
      setTimeout(() => {
        gainNode.gain.setValueAtTime(0, audioCtx.currentTime);
      }, duration * 1000);
    }, interval * 1000); // ripete ogni "interval" secondi

    // 7. Restituisci lo stream
    return dest.stream;
  }

  _createSineAudioStream(frequency = 440) {
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = audioCtx.createOscillator();
    osc.type = "sine";
    osc.frequency.value = frequency;

    const gain = audioCtx.createGain();
    gain.gain.value = 1.0; // volume forte

    osc.connect(gain);
    const dest = audioCtx.createMediaStreamDestination();
    gain.connect(dest);

    osc.start();
    return dest.stream;
  }
  async callTheia() {
    const conversationSessionRes = await fetch("/theia-session", { method: "POST" });
    if (!conversationSessionRes.ok) throw new Error("Errore creazione sessione per Theia");
    if (!this.conversationConnection || this.conversationConnection.signalingState === "closed") {
      this.conversationConnection = new RTCPeerConnection();
    }
    this.inputStream.getTracks().forEach(track => this.conversationConnection.addTrack(track, this.inputStream));
    this.conversationConnection.ontrack = event => {
      const remoteStream = event.streams[0]; // Questo è l'audio di Theia
      if (this.isMobile()) {
        // Check if the avatar instance exists before trying to use it.
        if (this.lunaAvatar) {
          this.lunaAvatar.connectStream(remoteStream);
        } else {
          console.warn("Avatar not ready, cannot connect stream.");
        }
      } else {
        this.svgStreamer.start(remoteStream);
      }
      console.log("🎤 Theia: prima traccia ricevuta.");


      this.remoteAudioElement = document.createElement("audio");
      this.remoteAudioElement.srcObject = remoteStream;
      this.remoteAudioElement.autoplay = true;
      this.remoteAudioElement.style.display = 'none';
      document.body.appendChild(this.remoteAudioElement);

    };

    this._setupDataChannel();
    const offer = await this.conversationConnection.createOffer();
    await this.conversationConnection.setLocalDescription(offer);

    const offerRes = await fetch("/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sdp: this.conversationConnection.localDescription.sdp })
    });
    if (!offerRes.ok) throw new Error("Errore invio offerta SDP");
    const answer = await offerRes.json();
    await this.conversationConnection.setRemoteDescription({ type: "answer", sdp: answer.sdp });
    console.log("✅ Theia: Connessione audio con AI stabilita.");
    // --- FINE BLOCCO DISABILITATO ---

  }
  _setupEventListeners() {
    if (this.fullscreenBtn) {
      this.fullscreenBtn.addEventListener('click', this._toggleFullScreen);
    }
    if (this.muteBtn) {
      this.muteBtn.addEventListener('click', () => this._toggleMute());
    }
    if (this.lunaDisplayBodyButton) {
      this.lunaDisplayBodyButton.addEventListener('click', () => this._toggleDisplayBody());
    }
  }
  _toggleMute() {
    // Controlliamo lo stato attuale guardando se la classe 'audio-unmuted' è presente
    const isCurrentlyUnmuted = document.body.classList.contains('audio-unmuted');

    if (isCurrentlyUnmuted) this.mute()
    else this.unmute()


    // Invertiamo la classe sul body per cambiare l'icona
    document.body.classList.toggle('audio-unmuted');
  }

  _toggleFullScreen() {
    if (!document.fullscreenElement &&    // Standard
      !document.mozFullScreenElement && // Firefox
      !document.webkitFullscreenElement && // Chrome, Safari and Opera
      !document.msFullscreenElement) {  // IE/Edge
      if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen();
      } else if (document.documentElement.mozRequestFullScreen) { /* Firefox */
        document.documentElement.mozRequestFullScreen();
      } else if (document.documentElement.webkitRequestFullscreen) { /* Chrome, Safari & Opera */
        document.documentElement.webkitRequestFullscreen();
      } else if (document.documentElement.msRequestFullscreen) { /* IE/Edge */
        document.documentElement.msRequestFullscreen();
      }
      document.body.classList.add('fullscreen-active');
    } else {
      if (document.exitFullscreen) {
        document.exitFullscreen();
      } else if (document.mozCancelFullScreen) { /* Firefox */
        document.mozCancelFullScreen();
      } else if (document.webkitExitFullscreen) { /* Chrome, Safari and Opera */
        document.webkitExitFullscreen();
      } else if (document.msExitFullscreen) { /* IE/Edge */
        document.msExitFullscreen();
      }
      document.body.classList.remove('fullscreen-active');
    }
  }
  _handlePlayback(targetVideoElement, whepUrl) {
    if (!targetVideoElement || !whepUrl) return console.error("Theia Playback: argomenti invalidi.");
    this.callTheia()

    if (this.reconnectTimeoutId) clearTimeout(this.reconnectTimeoutId);
    if (this.playbackConnection) this.playbackConnection.close();

    const tryToConnect = async (pollInterval) => {
      try {
        this.reconnectTimeoutId = setTimeout(() => tryToConnect(Math.min(pollInterval + 2000, 30000)), pollInterval);
        this.playbackConnection = new RTCPeerConnection();
        this.playbackConnection.ontrack = (event) => {
          if (targetVideoElement.srcObject !== event.streams[0]) {
            targetVideoElement.srcObject = event.streams[0];
          }
          if (this.reconnectTimeoutId) { clearTimeout(this.reconnectTimeoutId); this.reconnectTimeoutId = null; }
        };
        let offer = await this.playbackConnection.createOffer({ offerToReceiveVideo: true });
        await this.playbackConnection.setLocalDescription(offer);
        const whepResponse = await fetch(whepUrl, { method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: this.playbackConnection.localDescription.sdp });
        if (!whepResponse.ok) throw new Error(`Connessione WHEP di Theia fallita: ${whepResponse.statusText}`);
        const answerSdp = await whepResponse.text();
        await this.playbackConnection.setRemoteDescription({ type: 'answer', sdp: answerSdp });
        if (!this.isMobile())
          this.canvasTheiaBody.style.opacity = '0'

      } catch (error) { console.error(`Connessione Theia Playback fallita. Riprovo tra ${pollInterval}ms`, error); }
    };
    tryToConnect(5000);
  }



  _setupDataChannel() {
    this.outChannel = this.conversationConnection.createDataChannel("oai-events");
    this.outChannel.onmessage = async ev => {
      const msg = JSON.parse(ev.data);
      //console.debug(ev.data)
      if (msg.type == 'response.audio_transcript.done') {
        let transcript = msg.transcript
        const response = await fetch('/luna-update-stream-params', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sessionId: this.sessionId, prompt: transcript })
        });
        if (!response.ok) throw new Error(`Errore dal server: ${response.statusText}`);
      }
    };

    this.outChannel.onopen = async () => {
      const res = await fetch("/theia-config", { method: "GET" });
      const m = await res.json();
      this.outChannel.send(atob(m.i));
    };
  }

  stopConversation() {
    if (this.conversationConnection) this.conversationConnection.close();
    if (this.livepeerConnection) this.livepeerConnection.close();
    if (this.playbackConnection) this.playbackConnection.close();
    if (this.reconnectTimeoutId) clearTimeout(this.reconnectTimeoutId);
    if (this.remoteAudioElement) this.remoteAudioElement.remove();
    if (this.placeholderAudioElement) {
      this.placeholderAudioElement.pause();
      this.placeholderAudioElement.remove();
    }
    console.log("🛑 Theia: Conversazione e streaming terminati.");
  }

  mute() {
    this.conversationConnection.close()
    if (this.remoteAudioElement) this.remoteAudioElement.remove();
    if (this.placeholderAudioElement) {
      this.placeholderAudioElement.pause();
      this.placeholderAudioElement.remove();
    }
  }
  unmute = this.callTheia

}


navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
  const videoEl = document.getElementById('luna')
  new Theia().startConversation(stream, videoEl)

}).catch(err => {
  console.error(err)
  document.addEventListener('click', () => {
    const stream = navigator.mediaDevices.getUserMedia({ audio: true })

    const videoEl = document.getElementById('luna')
    new Theia().startConversation(stream, videoEl)

  })
});