class AudioFilterProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
    this.sampleRate = sampleRate;
    this.blockedWords = ["stop", "ferma", "basta", "mute"];
    this.port.onmessage = event => {
      if (event.data.blockedWords) this.blockedWords = 
event.data.blockedWords;
    };
  }

  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];

    if (input.length === 0) return true;

    const channelData = input[0];
    // Copia input su output (pass-through)
    for (let i = 0; i < channelData.length; i++) {
      output[0][i] = channelData[i];
    }

    // Accumula buffer per analisi
    this.buffer.push(...channelData);
    if (this.buffer.length > this.sampleRate * 1) { // 1 sec
      // Convertiamo in PCM16 per eventuale riconoscimento esterno
      const pcm16 = this.buffer.map(v => Math.max(-1, Math.min(1, v)) * 
32767);
      this.port.postMessage({ audioChunk: pcm16 });
      this.buffer = [];
    }

    return true;
  }
}

registerProcessor('audio-filter-processor', AudioFilterProcessor);

