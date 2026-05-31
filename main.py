import os
import json
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

VOCAL_1_ID = 1364650567103811601
VOCAL_2_ID = 1364650567103811602


def load_rivalries():
    raw = os.getenv("RIVALRIES_JSON")

    if raw:
        try:
            pairs = json.loads(raw)
            return [(int(a), int(b)) for a, b in pairs]
        except Exception as e:
            raise RuntimeError(f"RIVALRIES_JSON invalide : {e}")

    try:
        with open("rivalries.json", "r", encoding="utf-8") as f:
            pairs = json.load(f)
            return [(int(a), int(b)) for a, b in pairs]
    except FileNotFoundError:
        return []
    except Exception as e:
        raise RuntimeError(f"rivalries.json invalide : {e}")


intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Bot connecté : {bot.user}")
    print(f"Paires interdites chargées : {load_rivalries()}")
    print(f"Vocal principal : {VOCAL_1_ID}")
    print(f"Vocal de secours : {VOCAL_2_ID}")


@bot.event
async def on_voice_state_update(member, before, after):
    if after.channel is None:
        return

    if before.channel == after.channel:
        return

    current_channel = after.channel

    # Le bot agit uniquement si quelqu'un rejoint Vocal 1
    if current_channel.id != VOCAL_1_ID:
        return

    rivalries = load_rivalries()
    if not rivalries:
        print("Aucune paire interdite configurée.")
        return

    ids_in_channel = {m.id for m in current_channel.members}

    for user_a, user_b in rivalries:
        if user_a in ids_in_channel and user_b in ids_in_channel:
            try:
                destination = member.guild.get_channel(VOCAL_2_ID)

                if destination is None:
                    print("Erreur : Vocal 2 introuvable.")
                    return

                await member.move_to(destination)

                print(
                    f"{member} déplacé vers {destination.name} : "
                    f"paire interdite détectée dans {current_channel.name}"
                )

            except discord.Forbidden:
                print(
                    "Erreur : permission refusée. "
                    "Le rôle du bot doit être au-dessus des membres concernés "
                    "et avoir la permission 'Déplacer des membres'."
                )
            except discord.HTTPException as e:
                print(f"Erreur Discord : {e}")

            return


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN manquant dans les variables d'environnement Railway.")

bot.run(TOKEN)
