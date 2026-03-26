
const start = () => {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert("Il tuo browser non supporta SpeechRecognition!");
    return;
  }

  const recognition = new SpeechRecognition();
  recognition.lang = 'it-IT';
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  let finalTranscript = "";

  const onSentenceComplete = async (text) => {
    console.log("🎯 Frase completa:", text);

    try {
      const res = await fetch("/interpret", { 
        method: "POST", 
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({text})}
      );
      console.log(res)
    } catch (err) {
      console.log("❌ Errore fetch /interpret:", err);
    }
  };
  recognition.onresult = (event) => {
    let interimTranscript = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        finalTranscript += transcript + " ";
        onSentenceComplete(transcript.trim());
        finalTranscript = ''
      } else {
        interimTranscript += transcript;
      }
    }

  };

  recognition.onerror = (event) => {
    console.error("Errore riconoscimento vocale:", event.error);
  };

  recognition.onend = () => {
    console.log("Riconoscimento terminato. Riparto automaticamente...");
    recognition.start();
  };

  recognition.start();
  console.log("🎤 Riconoscimento avviato...");
};

window.addEventListener('load', start);
