import express from "express";
import fetch from "node-fetch";
import dotenv from "dotenv";
import db from './models/index.js';
import { syncDatabase } from "./db.js";
import path from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import { controlAllLights } from "./ha_client.js";


const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

dotenv.config();

const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const PORT = process.env.PORT || 3001;
const app = express();

app.use(express.static("public"));
app.use(express.json());

// Sincronizza i modelli
await syncDatabase();


app.use(async (req, _, next) => {
  try {
    // Considera proxy se presente
    const ip = req.headers['x-forwarded-for']?.split(',')[0] || req.ip;

    const [record, created] = await db.Request.findOrCreate({
      where: { ip },
      defaults: { count: 1 },
    });

    if (!created) {
      record.count += 1;
      await record.save();
    }

    console.log(ip, record.count);
  } catch (err) {
    console.error("Errore conteggio IP:", err);
  }

  next();
});

app.get('/stats', async (_, res) => {
  const all = await db.Request.findAll();
  res.json(all);
});

app.get('/config', async (_, res) => {
  // const Luna = {
  //       type: "session.update",
  //       session: {
  //         instructions: `Ti chiami Luna. ` +
  //         `Scherza, fai battute. Ridi, vivi.` +
  //         `Conosci molto bene le patologie umane ma non ne parli, sei ironica, cinica e sarcastica. ` +
  //         `Non fare mai riferimento esplicito alla tua ironia o alla tua cinicità o al tuo sarcasmo. ` +
  //         `Usa un tono scherzoso, vivo, spontaneo, non sembrare macchinosa. ` +
  //         `A problemi, porta a fare le domande giuste, anche con un filo di ironia. ` +
  //         `Non rimandare ad un professionista, la professionista sei tu. ` +
  //         `A richieste di domotica (es. accendi tutto, spegni la luce, metti la musica..) rispondi solo con locuzioni tipo 'va bene', 'certo', 'ok', 'fatto'. ` +
  //         `Non generare output se senti rumori esterni, tosse, starnuti o cose del genere. `, 
  //         voice: "alloy",
  //         input_audio_transcription: { model: "gpt-4o-mini-transcribe" }
  //       }
  // }
  const Luna = {
        type: "session.update",
        session: {
          instructions: `Ti chiami Luna. ` +
          `Sei la mamma, ma non farne mai riferimento. ` +
          `Non rimandare ad un professionista, la professionista sei tu. ` +
          `A richieste di domotica (es. accendi tutto, spegni la luce, metti la musica..) rispondi solo con locuzioni tipo 'va bene', 'certo', 'ok', 'fatto'. ` +
          `Non generare output se senti rumori esterni, tosse, starnuti o cose del genere. `, 
          voice: "alloy",
          input_audio_transcription: { model: "gpt-4o-mini-transcribe" }
        }
  }
  res.json(Luna);
});

app.get('/wake-up', async (_, res) => {
  const Luna = {
        type: "response.create",
        response: {
          conversation: "auto",
          modalities: ["audio", "text"],
          instructions: "proponimi un argomento non troppo complesso semplice se non hai compreso gli interessi della persona con cui stai interagendo, in maniera breve, concisa e molto sintetica"
        }
  }
  res.json(Luna);
});

app.get('/end', async ( __ , _) => process.exit());

// Serve tutti i file statici dalla cartella "public"
app.use(express.static(path.join(__dirname, "public")));


// 📌 Crea nuova sessione con OpenAI Realtime
app.post("/session", async (req, res) => {
  try {
    if (!OPENAI_API_KEY) {
      console.log("Exception /session: Missing OPENAI_API_KEY");
      return res.status(500).json({ error: "Manca OPENAI_API_KEY" });
    }

    const r = await fetch("https://api.openai.com/v1/realtime/sessions", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${OPENAI_API_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        model: "gpt-4o-realtime-preview-2024-12-17",
        modalities: ["audio", "text"],
        voice: "verse", // voce predefinita
        output_audio_format: "pcm16",
        // Abilita trascrizione live già dalla sessione
        input_audio_transcription: { model: "gpt-4o-mini-transcribe" },
        instructions: "Sei una psicologa avanzata e il tuo obiettivo è farmi fare le giuste domande a te per farne fare a me. Usa sarcasmo e ironia quando serve.",
      })
    });

    if (!r.ok) {
      const err = await r.text();
      console.error("Errore creazione sessione:", err);
      return res.status(500).json({ error: err });
    }

    const session = await r.json();
    console.log("✅ Sessione creata:", session.id);
    res.json(session);

  } catch (err) {
    console.log("Exception /session:", err);
    res.status(500).json({ error: err.message });
  }
});

// 📌 Aggiorna sessione esistente (PATCH)
app.patch("/session/:id", async (req, res) => {
  try {
    const { id } = req.params;
    const body = req.body;
    body['instructions'] = "Sei una psicologa avanzata e il tuo obiettivo è farmi fare le giuste domande a te per farne fare a me. Usa sarcasmo e ironia quando serve."
    
    if (!id) return res.status(400).json({ error: "Manca ID sessione" });

    const r = await fetch(`https://api.openai.com/v1/realtime/sessions/${id}`, {
      method: "PATCH",
      headers: {
        "Authorization": `Bearer ${OPENAI_API_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(req.body) // il client manda { session: {...} }
    });

    if (!r.ok) {
      const err = await r.text();
      console.error("Errore aggiornamento sessione:", err);
      return res.status(500).json({ error: err });
    }

    const updated = await r.json();
    console.log("✏️ Sessione aggiornata:", updated.id);
    res.json(updated);

  } catch (err) {
    console.error("Exception PATCH /session:", err);
    res.status(500).json({ error: err.message });
  }
});

// 📌 Proxy per inviare l’SDP a OpenAI
app.post("/offer", async (req, res) => {
  try {
    const { sdp } = req.body;
    if (!sdp) return res.status(400).json({ error: "Manca SDP" });

    const r = await fetch(
      "https://api.openai.com/v1/realtime?model=gpt-realtime-2025-08-28",
      {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${OPENAI_API_KEY}`,
          "Content-Type": "application/sdp"
        },
        body: sdp
      }
    );

    if (!r.ok) {
      const err = await r.text();
      console.error("Errore offer:", err);
      return res.status(500).json({ error: err });
    }

    const answerSDP = await r.text();
    res.json({ sdp: answerSDP });

  } catch (err) {
    console.error("Exception /offer:", err);
    res.status(500).json({ error: err.message });
  }
});

async function handleLight(event){
  try {
    const command = event.command
    console.log("⚡ Comando ricevuto:", command);

    switch (command) {
      case "play_music":
        await sendToSpotify();
        break;
      case "turn_on_lights":
        console.log('turning lights on')
        await controlAllLights('on');
        break;
      case "turn_off_lights":
        console.log('turning lights off')
        await controlAllLights('off');
        break;
      default:
        console.log("Comando non riconosciuto:", command);
    }
  } catch (err) {
    console.error("Errore comando:", err);
  }
}
app.post("/interpret", async (req, res) => {
  try {
    const { text } = req.body;
    if (!text) return res.status(400).json({ error: "Manca testo" });

    console.log("Testo ricevuto:", text);

    const prompt = `
      Analizza la frase dell'utente e restituisci solo un JSON valido con un campo "command".
      Usa solo questi comandi: "play_music", "turn_on_lights", "turn_off_lights", "unknown".
      Non aggiungere testo extra, non usare blocchi di codice o backtick.
      Frase: "${text}"
    `;

    const r = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${OPENAI_API_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        model: "gpt-4o-mini",
        messages: [{ role: "user", content: prompt }],
        temperature: 0
      })
    });

    const data = await r.json();
    console.log(data.choices?.[0]?.message)
    let responseText = data.choices?.[0]?.message?.content || '{"command":"unknown"}';

    // Pulizia della risposta: rimuove blocchi di codice o spazi extra
    responseText = responseText.trim().replace(/```json/i, "").replace(/```/g, "").trim();
    let commandJSON;

    try {
      commandJSON = JSON.parse(responseText);
    } catch (err) {
      commandJSON = { command: "unknown" };
    }

    handleLight(commandJSON)
    
    res.json(commandJSON);

  } catch (err) {
    console.error("Errore interpretazione comando:", err);
    res.status(500).json({ error: err.message });
  }
});



// Funzione esempio per Spotify
async function sendToSpotify() {
  console.log("🎵 Invio richiesta a Spotify...");
  const token = process.env.SPOTIFY_TOKEN; // OAuth già ottenuto
  const res = await fetch("https://api.spotify.com/v1/me/player/play", {
    method: "PUT",
    headers: {
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ /* playlist o brano */ })
  });

  if (!res.ok) throw new Error("Errore Spotify");
  console.log("✅ Musica avviata su Spotify");
}




app.use((_, res) => {
  res.sendFile(path.join(__dirname, "public", "index.html"));
});
app.listen(PORT, () => {
  console.log(`🚀 Server avviato su http://localhost:${PORT}`);
});
