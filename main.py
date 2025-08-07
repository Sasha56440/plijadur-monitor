import os
import re
import asyncio
import logging
from datetime import datetime
from telethon import TelegramClient, events
import aiohttp
import json

# Configuration du logging pour Railway
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Variables d'environnement (Railway les injectera automatiquement)
API_ID = int(os.getenv('TELEGRAM_API_ID', '22981794'))
API_HASH = os.getenv('TELEGRAM_API_HASH', '21439f4a96b01c96be70701e350a86c1')
PHONE = os.getenv('TELEGRAM_PHONE', '+33780428027')
PLIJADUR_BOT_TOKEN = os.getenv('PLIJADUR_BOT_TOKEN', '8364336672:AAGHrkluKDrhwfRJ128pTdAY-7xGfXr3P2Q')
YOUR_CHAT_ID = os.getenv('YOUR_CHAT_ID', '5507342748')

# Session string pour éviter re-authentification
SESSION_STRING = os.getenv('TELEGRAM_SESSION', 'plijadur_session')

# Patterns de détection des alertes InPlayGuru (basés sur tes exemples du CDC)
ALERT_PATTERNS = [
    # Patterns obligatoires (au moins 2 doivent matcher)
    r'Strike Rate %:\s*\d+',                    # "Strike Rate %: 76"
    r'1X2 Pre-Match Odds:\s*[\d.]+',           # "1X2 Pre-Match Odds: 1.36 4.50 6.50"
    r'Over/Under\s+[\d.]+\s+Odds:',            # "Over/Under 6.50 Odds: 1.44 2.63"
    r'Timer:\s*\d+\'',                         # "Timer: 70'"
    r'Kickoff:\s*In\s*\d+',                    # "Kickoff: In 5 minutes"
    r'Goals:\s*\d+\s*[-–]\s*\d+',              # "Goals: 6 - 0"
    
    # Patterns de stratégies (formats de tes exemples)
    r'^BTTS\s*:\s*(yes|no)',                   # "BTTS : no #2.3.1"
    r'^DRAW\s*\[HISTO\].*\d+%',                # "DRAW [HISTO] ---- 25%"
    r'^H\s*win\s*\[HISTO\].*\d+%',             # "H win [HISTO] ---- 44%"
    r'^A\s*win\s*\[HISTO\].*\d+%',             # "A win [HISTO] ---- xx%"
    
    # Patterns de contexte
    r'Europe Friendlies|Premier League|Championship|Liga|Serie A|Bundesliga|Cosafa',
    r'\w+\s+vs\s+\w+',                         # "Vitesse U21 vs Roda JC U21"
    r'HT Score:\s*\d+-\d+',                    # "HT Score: 2-0"
    r'FT Score:\s*\d+-\d+',                    # "FT Score: 6-0"
    r'Last Goal:\s*\w+\s*at\s*\d+',           # "Last Goal: Home at 56'"
    r'Hit|Miss'                                # Résultat final
]

def is_inplayguru_alert(message_text):
    """
    Détecte si un message est une vraie alerte InPlayGuru
    Basé sur les formats du cahier des charges
    """
    if not message_text or len(message_text) < 80:
        logger.debug("Message trop court pour être une alerte")
        return False
    
    # Compter combien de patterns matchent
    pattern_matches = []
    for pattern in ALERT_PATTERNS:
        if re.search(pattern, message_text, re.IGNORECASE | re.MULTILINE):
            pattern_matches.append(pattern)
    
    matches_count = len(pattern_matches)
    
    # Critères pour considérer que c'est une alerte
    is_likely_alert = matches_count >= 3  # Au moins 3 patterns
    
    # Patterns d'exclusion (messages à ignorer)
    exclude_patterns = [
        r'^(good\s+morning|hello|hi|thanks|thank\s+you)',
        r'(question|help|how.*work|settings|admin)',
        r'^/\w+',  # Commandes bot
        r'^(welcome|bienvenue)',
        r'(subscribe|unsubscribe|subscription)',
    ]
    
    has_exclusions = any(re.search(pattern, message_text, re.IGNORECASE) 
                        for pattern in exclude_patterns)
    
    # Log pour debugging
    logger.info(f"Message Analysis: {matches_count} patterns matched, "
               f"excluded: {has_exclusions}")
    logger.debug(f"Matching patterns: {[p for p in pattern_matches]}")
    logger.debug(f"Message preview: {message_text[:150]}...")
    
    return is_likely_alert and not has_exclusions

async def send_to_plijadur_bot(message_text, session):
    """
    Envoie le message d'alerte vers le bot Plijadur pour traitement par n8n
    """
    url = f"https://api.telegram.org/bot{PLIJADUR_BOT_TOKEN}/sendMessage"
    
    # Formatage du message pour n8n
    formatted_message = f"🎯 ALERTE INPLAYGURU AUTOMATIQUE:\n\n{message_text}"
    
    payload = {
        'chat_id': YOUR_CHAT_ID,
        'text': formatted_message,
        'parse_mode': 'HTML'
    }
    
    try:
        async with session.post(url, json=payload, timeout=10) as response:
            if response.status == 200:
                result = await response.json()
                if result.get('ok'):
                    logger.info("✅ Alerte transférée avec succès vers le bot Plijadur")
                    return True
                else:
                    logger.error(f"❌ Erreur API Telegram: {result}")
                    return False
            else:
                logger.error(f"❌ Erreur HTTP: {response.status}")
                return False
    except asyncio.TimeoutError:
        logger.error("❌ Timeout lors du transfert vers le bot")
        return False
    except Exception as e:
        logger.error(f"❌ Erreur réseau: {e}")
        return False

class InPlayGuruMonitor:
    """
    Classe principale pour surveiller le groupe InPlayGuru.com
    et transférer automatiquement les alertes vers le bot Plijadur
    """
    
    def __init__(self):
        self.client = TelegramClient(SESSION_STRING, API_ID, API_HASH)
        self.http_session = None
        self.alerts_count = 0
        self.messages_processed = 0
        
    async def start(self):
        """Démarre le système de monitoring"""
        logger.info("🚀 Démarrage du monitoring InPlayGuru...")
        logger.info(f"Configuration: API_ID={API_ID}, Phone={PHONE}")
        
        try:
            # Connexion à Telegram
            await self.client.start(phone=PHONE)
            logger.info("✅ Connecté à Telegram avec succès")
            
            # Session HTTP pour les requêtes
            self.http_session = aiohttp.ClientSession()
            
            # Vérification de l'accès au groupe InPlayGuru.com
            await self._verify_group_access()
            
            # Configuration du handler pour les nouveaux messages
            self.client.add_event_handler(self._handle_new_message, 
                                        events.NewMessage(chats='InPlayGuru.com'))
            
            logger.info("👂 Monitoring actif - Surveillance du groupe InPlayGuru.com...")
            logger.info("🤖 Les alertes détectées seront automatiquement transférées")
            
            # Message de démarrage vers le bot
            await self._send_startup_notification()
            
            # Maintenir le client actif
            await self.client.run_until_disconnected()
            
        except Exception as e:
            logger.error(f"❌ Erreur lors du démarrage: {e}")
            await self._send_error_notification(f"Erreur démarrage: {e}")
            raise
    
    async def _verify_group_access(self):
        """Vérifie l'accès au groupe InPlayGuru.com"""
        try:
            entity = await self.client.get_entity('InPlayGuru.com')
            logger.info(f"✅ Accès au groupe confirmé: {entity.title}")
            logger.info(f"📊 Groupe ID: {entity.id}, Participants: {getattr(entity, 'participants_count', 'N/A')}")
        except Exception as e:
            logger.error(f"❌ Impossible d'accéder au groupe InPlayGuru.com: {e}")
            logger.error("Vérifiez que vous êtes bien membre du groupe")
            raise
    
    async def _handle_new_message(self, event):
        """Traite chaque nouveau message du groupe"""
        try:
            message = event.message
            self.messages_processed += 1
            
            # Informations sur le message
            message_text = getattr(message, 'text', '') or ''
            sender = await message.get_sender()
            sender_name = getattr(sender, 'first_name', 'Unknown') or 'Bot'
            sender_username = getattr(sender, 'username', '') or 'no_username'
            
            logger.info(f"📨 Message #{self.messages_processed} de: {sender_name} (@{sender_username})")
            
            # Test si c'est une alerte
            if is_inplayguru_alert(message_text):
                self.alerts_count += 1
                logger.info(f"🎯 ALERTE #{self.alerts_count} DÉTECTÉE !")
                
                # Transfert vers le bot Plijadur
                success = await send_to_plijadur_bot(message_text, self.http_session)
                
                if success:
                    logger.info(f"✅ Pipeline complet réussi: InPlayGuru → Bot Plijadur → n8n")
                else:
                    logger.error(f"❌ Échec du transfert - Alerte perdue")
                    await self._send_error_notification(f"Échec transfert alerte #{self.alerts_count}")
            else:
                logger.info(f"ℹ️ Message ignoré (discussion/spam)")
        
        except Exception as e:
            logger.error(f"❌ Erreur lors du traitement du message: {e}")
    
    async def _send_startup_notification(self):
        """Envoie une notification de démarrage"""
        startup_msg = (
            f"🚀 PLIJADUR MONITOR DÉMARRÉ\n\n"
            f"✅ Surveillance active du groupe InPlayGuru.com\n"
            f"🤖 Transfert automatique vers le bot Plijadur\n"
            f"⚡ Workflow n8n prêt à traiter les alertes\n\n"
            f"📊 Statistiques:\n"
            f"Messages traités: {self.messages_processed}\n"
            f"Alertes détectées: {self.alerts_count}\n\n"
            f"Heure: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        try:
            await send_to_plijadur_bot(startup_msg, self.http_session)
        except:
            pass  # Pas grave si la notification échoue
    
    async def _send_error_notification(self, error_msg):
        """Envoie une notification d'erreur"""
        error_notification = f"🚨 ERREUR PLIJADUR MONITOR:\n\n{error_msg}\n\nVérifiez les logs Railway.app"
        
        try:
            await send_to_plijadur_bot(error_notification, self.http_session)
        except:
            pass  # Pas grave si la notification échoue
    
    async def cleanup(self):
        """Nettoyage des ressources"""
        logger.info("🔄 Nettoyage en cours...")
        
        if self.http_session:
            await self.http_session.close()
        
        if self.client.is_connected():
            await self.client.disconnect()
        
        logger.info(f"📊 Session terminée - Messages: {self.messages_processed}, Alertes: {self.alerts_count}")

async def main():
    """Fonction principale avec gestion d'erreur robuste"""
    monitor = None
    
    try:
        logger.info("🎯 PLIJADUR AUTOMATIC MONITOR - Starting...")
        monitor = InPlayGuruMonitor()
        await monitor.start()
        
    except KeyboardInterrupt:
        logger.info("🛑 Arrêt demandé par l'utilisateur")
    except Exception as e:
        logger.error(f"❌ Erreur critique: {e}")
        if monitor:
            await monitor._send_error_notification(f"Erreur critique: {e}")
    finally:
        if monitor:
            await monitor.cleanup()
        logger.info("🔚 Monitoring arrêté")

# Point d'entrée pour Railway.app
if __name__ == '__main__':
    # Configuration spéciale pour l'environnement cloud
    asyncio.run(main())
