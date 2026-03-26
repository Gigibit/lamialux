function isInAppBrowser() {
  // Converte lo userAgent in minuscolo per un confronto più affidabile
  const userAgent = navigator.userAgent.toLowerCase();
  
  const isInstagram = userAgent.includes("instagram");
  // Cerca "linkedin" in minuscolo, che copre più variazioni
  const isLinkedIn = userAgent.includes("linkedin") || userAgent.includes("LinkedIn"); 
  
  return isInstagram || isLinkedIn;
}

// Il resto del tuo codice rimane invariato
if (isInAppBrowser()) {
  window.guardActive = true;
  const externalBtn = document.getElementById("external-link");
  const title = document.getElementById("title");
  
  externalBtn.style.display = 'block';
  title.innerText = 'Luna, ho problemi in altre App, premi in basso a sinistra e usciamo a far due chiacchiere';
  title.style.bottom = '95px';
  title.style.width = '45%';
  title.style.fontSize = '25px';
  title.style.margin = '0 auto';

  externalBtn.addEventListener("click", function (e) {
    e.preventDefault();
    const url = "https://gigib.it/luna";
    
    // Prova schema Android
    if (/android/i.test(navigator.userAgent)) {
      window.location = "intent://" + url.replace(/^https?:\/\//, "") + "#Intent;scheme=https;package=com.android.chrome;end;";
    }
    // Prova schema iOS Safari
    else if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {
      window.location = "x-safari-" + url; // Nota: questo schema potrebbe non funzionare più su iOS recenti. Spesso si usa un link diretto.
    }
    else {
      // fallback: copia istruzioni per l’utente
      alert("Apri il menu (…) in alto e scegli 'Apri in Browser' per continuare.");
    }
  });
}