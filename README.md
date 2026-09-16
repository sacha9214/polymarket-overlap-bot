# Polymarket Overlap — bot Discord

Le [site](https://sacha9214.github.io/polymarket-overlap/) ne peut rien faire tant que tu ne l'ouvres pas.
Ce bot, lui, **te prévient** : il surveille les positions des meilleurs traders Polymarket et poste
dans un salon quand le smart money **entre** sur un marché ou en **sort**.

Le modèle (proba estimée, palmarès réel, désaccords, conviction) est le même que celui du site,
porté à l'identique dans `overlap.py`.

## Commandes

| Commande | Effet |
|---|---|
| `/best [nombre]` | Les meilleures entrées du moment (1 à 5) |
| `/wallet <adresse>` | Positions, palmarès réel et gains non réclamés d'une adresse |
| `/watch [seuil]` | Abonne le salon aux alertes (défaut : à partir de 10 000 $) |
| `/unwatch` | Coupe les alertes du salon |
| `/status` | État de la surveillance |

`/watch` et `/unwatch` sont réservées aux membres qui peuvent gérer le serveur.

## Deux façons de l'utiliser

| | Webhook seul | Bot complet |
|---|---|---|
| Mise en route | ~30 s, aucun jeton | création d'une application Discord |
| Alertes entrées/sorties | ✅ | ✅ |
| Commandes `/best`, `/wallet`… | ❌ | ✅ |
| Lancement | `./start_webhook.sh` | `./start_mac_linux.sh` |

### Version webhook (la plus simple)

Un webhook ne sait qu'**envoyer** des messages — ce qui suffit pour les alertes.

1. Discord → **Paramètres du serveur** → **Intégrations** → **Webhooks** → *Nouveau webhook*
2. Choisis le salon, puis **Copier l'URL du webhook**
3. Colle-la dans un fichier `webhook.txt` à côté des scripts (il est gitignoré)
4. `./start_webhook.sh`

Cette URL permet de publier dans ton salon : garde-la pour toi.

## Mise en route du bot complet

1. **Créer le bot** sur <https://discord.com/developers/applications> → *New Application* → onglet *Bot* → *Reset Token* → copier le jeton.
2. **Coller le jeton** dans un fichier `token.txt` à côté de `bot.py` (une seule ligne).
   Ne le partage avec personne : il donne le contrôle total du bot.
3. **Inviter le bot** : onglet *OAuth2 → URL Generator*, cocher `bot` + `applications.commands`,
   permissions `Send Messages` et `Embed Links`, puis ouvrir l'URL générée.
4. **Lancer** :

```bash
./start_mac_linux.sh
```

Aucun *privileged intent* n'est nécessaire : le bot ne lit pas les messages.

## Réglages (variables d'environnement, toutes optionnelles)

| Variable | Défaut | Rôle |
|---|---|---|
| `DISCORD_BOT_TOKEN` | — | Alternative à `token.txt` |
| `DISCORD_GUILD_ID` | — | Ton serveur : les commandes apparaissent tout de suite au lieu de ~1 h |
| `POLL_MINUTES` | `20` | Fréquence de vérification |
| `PRESET_SIZE` | `40` | Nombre de traders suivis (top hebdo) |

## Ce qu'il faut savoir

- **Le premier cycle ne déclenche aucune alerte** : il sert de photo de référence, sinon tu recevrais
  200 messages d'un coup. Les alertes commencent au cycle suivant.
- **Maximum 5 alertes par cycle et par salon**, pour que ce soit lisible.
- **Il doit tourner en permanence** pour surveiller. Sur ton Mac il s'arrête quand tu l'éteins :
  pour du 24/7, héberge-le (Railway, Fly.io, un petit VPS…).
- L'historique de l'API Polymarket plafonne à 500 événements par wallet, soit ~2 jours chez les gros
  traders : les palmarès sont calculés sur cette fenêtre, pas sur toute leur carrière.
- Le moteur se teste sans Discord : `./venv/bin/python overlap.py` affiche les meilleures entrées
  dans le terminal.

Pas un conseil financier. Ne mise que ce que tu peux perdre.

## Licence

[MIT](LICENSE)
