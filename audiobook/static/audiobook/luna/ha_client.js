// ha_client.js
import fetch from "node-fetch";
import dotenv from "dotenv";

dotenv.config()


const HA_URL = "http://homeassistant.local:8123";   // <-- cambia con il tuo
const TOKEN = process.env.HA_TOKEN;

const LIGHTS = [
  "light.lampada_camera",
  "light.lampada_cucina",
  "light.led_tv",
//   'light.striscia_frigo',
//   'light.luce_media',
//   'light.luce_piccola',
  // 'light.cabina_armadio',
  'light.neon_puffo',
  'light.neon_letto',
  'light.neon_superiore',
];

export async function controlAllLights(action) {
  const endpoint =
    action === "on" ? "/api/services/light/turn_on" : "/api/services/light/turn_off";

  const res = await fetch(`${HA_URL}${endpoint}`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ entity_id: LIGHTS }),
  });
  console.log(`HA answer ${JSON.stringify(res)}`)
  if (!res.ok) throw new Error(`Errore HA: ${res.status} ${res.statusText}`);
  return res.json();
}
