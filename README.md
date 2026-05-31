# Discord Voice Guard

Bot Discord qui empêche ces deux utilisateurs d'être ensemble dans le même salon vocal 

Quand les deux se retrouvent dans le même vocal, le bot déconnecte automatiquement le dernier arrivé.

## Fichiers

- `main.py` : code du bot
- `requirements.txt` : dépendance Python
- `Procfile` : commande de lancement Railway
- `rivalries.json` : paire interdite déjà configurée

## Configuration Discord

Dans le Discord Developer Portal :

1. Crée une application.
2. Ajoute un bot.
3. Active l'intent :
   - Server Members Intent

Permissions à cocher pour inviter le bot :

- View Channels
- Connect
- Move Members

Dans ton serveur Discord :

- Le rôle du bot doit être AU-DESSUS des membres concernés.
- Le bot doit avoir accès aux salons vocaux.
- Le bot doit avoir la permission "Déplacer des membres".

## Configuration Railway

Ajoute cette variable dans Railway :

DISCORD_TOKEN=ton_token_discord

Ensuite Railway lancera automatiquement le bot avec :

worker: python main.py

## Ajouter d'autres paires

Dans `rivalries.json`, ajoute une ligne :

[
  [1051184437490094110, 689407464608890923],
  [ID_UTILISATEUR_3, ID_UTILISATEUR_4]
]
