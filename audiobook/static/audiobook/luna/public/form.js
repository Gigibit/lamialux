// SvgStreamer.js

export class SvgStreamer {
  // --- IMPOSTAZIONI DI ANIMAZIONE (puoi modificarle) ---
  static SILENCE_THRESHOLD = 5;
  static MOUTH_REST_POSITION = 190;
  static MOUTH_OPEN_FACTOR = 60;
  static BROW_LIFT_FACTOR = 5;
  static EYE_SQUINT_FACTOR = 0.15;
  static HEAD_BOB_FACTOR = 3;
  static SMOOTHING_FACTOR = 0.3;

  constructor(svgContainerId) {
    this.container = document.getElementById(svgContainerId);
    if (!this.container) {
      throw new Error(`[SvgStreamer] Elemento con ID "${svgContainerId}" non trovato.`);
    }

    this.svg = this.container.querySelector('svg');
    if (!this.svg) {
      throw new Error(`[SvgStreamer] Nessun SVG trovato dentro #${svgContainerId}.`);
    }

    // Trova tutti gli elementi animabili
    this.mouthUpper = this.svg.querySelector("#mouthUpper");
    this.mouthLower = this.svg.querySelector("#mouthLower");
    this.mouthInside = this.svg.querySelector("#mouthInside");
    this.leftEye = this.svg.querySelector("#leftEye");
    this.rightEye = this.svg.querySelector("#rightEye");
    this.leftBrow = this.svg.querySelector("#leftBrow");
    this.rightBrow = this.svg.querySelector("#rightBrow");
    this.faceGroup = this.svg.querySelector("#faceGroup");

    // Inizializza le variabili di stato
    this.isAnimating = false;
    this.audioCtx = null;
    this.analyser = null;
    this.dataArray = null;
    this.currentMouth = SvgStreamer.MOUTH_REST_POSITION;
    this.targetMouth = SvgStreamer.MOUTH_REST_POSITION;

    // Imposta il canvas per lo streaming
    this._setupCanvas();
  }

  /** Imposta il canvas nascosto che farà da "specchio" per l'SVG. */
// In form.js (o SvgStreamer.js)

  /** Imposta il canvas nascosto che farà da "specchio" per l'SVG. */
// In form.js (o SvgStreamer.js)

  /** Imposta il canvas che farà da "specchio" per l'SVG. */
// In form.js (o SvgStreamer.js)

  /** Imposta il canvas che farà da "specchio" per l'SVG. */
  _setupCanvas() {
    this.canvas = document.createElement('canvas');
    this.ctx = this.canvas.getContext('2d');
    
    const viewBox = this.svg.getAttribute('viewBox').split(' ');
    this.canvas.width = parseInt(viewBox[2], 10);
    this.canvas.height = parseInt(viewBox[3], 10);
    
    // --- MODIFICA CHIAVE ---
    // Stili per rendere il canvas un overlay invisibile del suo genitore.
    this.canvas.style.position = 'absolute';
    this.canvas.style.top = '30%';
    this.canvas.style.left = '0';
    this.canvas.style.right = '0';
    this.canvas.style.bottom = '0';
    
    this.canvas.style.width = '300px'; // Occupa il 100% del genitore
    this.canvas.style.height = '300px'; // Occupa il 100% del genitore
    this.canvas.style.margin = '0 auto'; // Occupa il 100% del genitore
    this.canvas.style.opacity = '0';
    this.canvas.style.pointerEvents = 'none';
    this.canvas.id = 'svg-mirror-canvas';
    
    this.renderImage = new Image();
  }

  /** Avvia l'analisi dell'audio e il loop di animazione. */
  start(audioStream) {
    if (!audioStream) {
      console.error("[SvgStreamer] È necessario un audioStream per avviare l'animazione.");
      return;
    }
    if (this.isAnimating) return; // Già in esecuzione

    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = this.audioCtx.createMediaStreamSource(audioStream);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 256;
    this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
    source.connect(this.analyser);

    this.isAnimating = true;
    this._animationLoop();
    console.log("[SvgStreamer] Animazione avviata.");
  }

  /** Ferma il loop di animazione. */
  stop() {
    this.isAnimating = false;
    if (this.audioCtx) this.audioCtx.close();
    console.log("[SvgStreamer] Animazione fermata.");
  }

  /** Il loop principale che si occupa di animare l'SVG e renderizzarlo sul canvas. */
  _animationLoop() {
    if (!this.isAnimating) return;

    if (this.analyser) {
      this.analyser.getByteFrequencyData(this.dataArray);
      const maxVal = Math.max(...this.dataArray);
      this._animateSvgParts(maxVal);
    }

    this._renderSvgToCanvas();
    requestAnimationFrame(() => this._animationLoop());
  }

  /** Aggiorna gli attributi dell'SVG in base al volume. */
  _animateSvgParts(maxVal) {
    this.targetMouth = maxVal < SvgStreamer.SILENCE_THRESHOLD ?
      SvgStreamer.MOUTH_REST_POSITION :
      SvgStreamer.MOUTH_REST_POSITION + (maxVal / 255) * SvgStreamer.MOUTH_OPEN_FACTOR;
    this.currentMouth += (this.targetMouth - this.currentMouth) * SvgStreamer.SMOOTHING_FACTOR;

    const upperY = SvgStreamer.MOUTH_REST_POSITION - (this.currentMouth - SvgStreamer.MOUTH_REST_POSITION) * 0.2;
    const lowerY = SvgStreamer.MOUTH_REST_POSITION + (this.currentMouth - SvgStreamer.MOUTH_REST_POSITION);

    this.mouthUpper.setAttribute('d', `M125 190 Q150 ${upperY} 175 190`);
    this.mouthLower.setAttribute('d', `M125 190 Q150 ${lowerY} 175 190`);
    this.mouthInside.setAttribute('d', `M125 190 Q150 ${upperY} 175 190 Q150 ${lowerY} 125 190 Z`);
    
    const intensity = maxVal / 255;
    const eyeScale = 1 - intensity * SvgStreamer.EYE_SQUINT_FACTOR;
    this.leftEye.setAttribute('ry', 6 * eyeScale);
    this.rightEye.setAttribute('ry', 6 * eyeScale);

    const browDelta = intensity * SvgStreamer.BROW_LIFT_FACTOR;
    this.leftBrow.setAttribute('d', `M115 ${135-browDelta} Q125 ${125-browDelta}, 135 ${135-browDelta}`);
    this.rightBrow.setAttribute('d', `M165 ${135-browDelta} Q175 ${125-browDelta}, 185 ${135-browDelta}`);
    
    const headOffset = Math.sin(Date.now() * 0.005) * intensity * SvgStreamer.HEAD_BOB_FACTOR;
    this.faceGroup.setAttribute('transform', `translate(0, ${headOffset})`);
  }

  /** Renderizza l'SVG sul canvas (la "foto" di ogni frame). */
// In form.js (o SvgStreamer.js)

  /** Renderizza l'SVG sul canvas (la "foto" di ogni frame). */
  _renderSvgToCanvas() {
    const svgXml = new XMLSerializer().serializeToString(this.svg);

    // --- CORREZIONE ---
    // Sostituiamo btoa() con encodeURIComponent(), che è più sicuro per gli SVG.
    // Nota che non usiamo più ';base64' nell'URL.
    const svgUrl = 'data:image/svg+xml,' + encodeURIComponent(svgXml);

    // Aggiungiamo un gestore di errori per capire se l'immagine non si carica
    this.renderImage.onerror = (e) => {
      console.error("[SvgStreamer] Errore critico: l'SVG non può essere caricato come immagine.", e);
    };

    this.renderImage.onload = () => {
      this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.ctx.drawImage(this.renderImage, 0, 0, this.canvas.width, this.canvas.height);
    };
    
    this.renderImage.src = svgUrl;
  }

  /** Metodo pubblico per ottenere lo stream video dal canvas. */
  captureStream(frameRate = 30) {
    return this.canvas.captureStream(frameRate);
  }
}