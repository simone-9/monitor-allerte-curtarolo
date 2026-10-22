# Monitoraggio automatico allerte CFD Veneto (zona E1 - Curtarolo)

Controlla ogni 10 minuti la pagina dei bollettini del CFD, legge ogni "Bollettino" o "Avviso di allerta regionale"
(BAR_/AAR_), estrae i livelli della zona E1 per ogni tipo di rischio e li scrive su Firebase. L'app li mostra da sola.
Telegram parte per ARANCIONE e ROSSA (e al rientro), una sola volta per ogni cambiamento.
Se un documento non si legge con certezza: l'app mostra "DA VERIFICARE" e arriva un avviso. Mai verde per default.

## Installazione (circa 15 minuti)
1. **Repository privato su GitHub** con questa cartella `monitor-allerte/` e `.github/workflows/` alla radice del repository.
2. **Account di servizio Firebase**: Console Firebase > Impostazioni progetto > Account di servizio > "Genera nuova chiave privata".
   Il file JSON scaricato e' un segreto: non incollarlo in chat e non metterlo nel codice.
3. **Segreti** nel repository (Settings > Secrets and variables > Actions > New repository secret):
   - `FIREBASE_SERVICE_ACCOUNT`: l'intero contenuto del file JSON
   - `TELEGRAM_BOT_TOKEN`: il token del bot (meglio un token NUOVO: rigeneralo con BotFather con /revoke)
   - `TELEGRAM_CHAT_ID`: l'ID della chat/gruppo che deve ricevere gli avvisi (negativo per i gruppi)
4. **Regole Firestore**: aggiungi quelle indicate nella risposta (lettura di `cfd_stato` per gli utenti dell'app).
5. Scheda **Actions** del repository > "Monitoraggio allerte CFD Veneto" > **Run workflow** per la prima prova.
   Nel registro dell'esecuzione devono comparire righe "Elaborato: Bollettino di allerta regionale n. ..." .

## Note
- GitHub puo' ritardare di qualche minuto le esecuzioni programmate e disattiva quelle dei repository senza attivita'
  per 60 giorni: fai un'azione sul repository (anche un commit vuoto) ogni tanto, oppure controlla la scheda Actions.
- Se la Regione bloccasse le richieste dai server di GitHub, il controllo fallisce: dopo 3 errori consecutivi arriva un avviso Telegram
  e l'app mostra "Monitoraggio non aggiornato". In quel caso si puo' spostare lo stesso programma su un altro computer sempre acceso.
- Zona diversa: variabili `CFD_ZONA` e `CFD_NOME_ZONA`. Soglia Telegram: `SOGLIA_TELEGRAM` (default ARANCIONE).
- Prova su un PDF locale, senza Firebase: `python monitor_allerte.py --pdf BAR_261022.pdf`
