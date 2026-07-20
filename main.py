import os
import json
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# ID du salon textuel dans lequel VoiceGuard enverra ses logs.
# Il peut être défini dans Railway avec la variable LOG_CHANNEL_ID.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

RIVALRIES_FILE = Path("rivalries.json")

# Le préfixe des commandes sera !
COMMAND_PREFIX = "!"


# ============================================================
# CHARGEMENT ET SAUVEGARDE DES PAIRES
# ============================================================

def load_rivalries() -> list[tuple[int, int]]:
    """
    Retourne une liste de paires sous la forme :

    (utilisateur à déplacer, utilisateur protégé)
    """

    raw = os.getenv("RIVALRIES_JSON")

    if raw:
        try:
            pairs = json.loads(raw)
            return [(int(problem), int(protected)) for problem, protected in pairs]
        except Exception as error:
            raise RuntimeError(f"RIVALRIES_JSON invalide : {error}") from error

    try:
        with RIVALRIES_FILE.open("r", encoding="utf-8") as file:
            pairs = json.load(file)

        return [(int(problem), int(protected)) for problem, protected in pairs]

    except FileNotFoundError:
        return []

    except Exception as error:
        raise RuntimeError(f"rivalries.json invalide : {error}") from error


def save_rivalries(rivalries: list[tuple[int, int]]) -> None:
    """
    Sauvegarde les paires dans rivalries.json.
    """

    temporary_file = RIVALRIES_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(rivalries, file, indent=2)

    temporary_file.replace(RIVALRIES_FILE)


# ============================================================
# CONFIGURATION DISCORD
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=COMMAND_PREFIX,
    intents=intents,
    help_command=None,
)


# ============================================================
# OUTILS
# ============================================================

def get_log_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    """
    Récupère le salon textuel utilisé pour les logs.
    """

    if LOG_CHANNEL_ID == 0:
        return None

    channel = guild.get_channel(LOG_CHANNEL_ID)

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


async def send_log(guild: discord.Guild, message: str) -> None:
    """
    Envoie un message dans le salon de logs et dans les logs Railway.
    """

    print(message)

    channel = get_log_channel(guild)

    if channel is None:
        return

    try:
        await channel.send(message)

    except discord.Forbidden:
        print(
            "VoiceGuard ne peut pas écrire dans le salon de logs. "
            "Vérifie les permissions du bot."
        )

    except discord.HTTPException as error:
        print(f"Erreur lors de l'envoi d'un log Discord : {error}")


def member_name(member: discord.Member) -> str:
    """
    Retourne un nom lisible avec l'ID du membre.
    """

    return f"{member} (`{member.id}`)"


def find_available_voice_channel(
    guild: discord.Guild,
    current_channel: discord.VoiceChannel | discord.StageChannel,
    member_to_move: discord.Member,
    protected_user_id: int,
) -> Optional[discord.VoiceChannel]:
    """
    Cherche un autre salon vocal dans lequel déplacer le membre problématique.

    Le salon choisi :
    - n'est pas le salon actuel ;
    - ne contient pas l'utilisateur protégé ;
    - n'est pas plein ;
    - est visible et accessible ;
    - n'est pas le salon AFK.
    """

    for channel in guild.voice_channels:
        if channel.id == current_channel.id:
            continue

        if guild.afk_channel and channel.id == guild.afk_channel.id:
            continue

        member_ids = {member.id for member in channel.members}

        if protected_user_id in member_ids:
            continue

        bot_member = guild.me

        if bot_member is None:
            continue

        bot_permissions = channel.permissions_for(bot_member)
        target_permissions = channel.permissions_for(member_to_move)

        if not bot_permissions.view_channel:
            continue

        if not bot_permissions.connect:
            continue

        if not bot_permissions.move_members:
            continue

        if not target_permissions.view_channel:
            continue

        if not target_permissions.connect:
            continue

        if channel.user_limit != 0 and len(channel.members) >= channel.user_limit:
            continue

        return channel

    return None


async def send_move_dm(
    member: discord.Member,
    protected_member: discord.Member,
    destination: Optional[discord.VoiceChannel],
) -> None:
    """
    Explique en message privé pourquoi le membre a été déplacé ou déconnecté.
    """

    if destination is not None:
        message = (
            f"Tu as été déplacé automatiquement vers **{destination.name}** "
            f"par VoiceGuard.\n\n"
            f"Une règle du serveur empêche actuellement ta présence dans le même "
            f"salon vocal que **{protected_member.display_name}**.\n"
            f"Merci de rester dans un salon vocal séparé."
        )
    else:
        message = (
            "Tu as été déconnecté automatiquement du salon vocal par VoiceGuard.\n\n"
            f"Une règle du serveur empêche actuellement ta présence dans le même "
            f"salon vocal que **{protected_member.display_name}** et aucun autre "
            f"salon vocal disponible n'a été trouvé."
        )

    try:
        await member.send(message)

    except discord.Forbidden:
        # Les messages privés du membre sont probablement fermés.
        pass

    except discord.HTTPException:
        pass


async def enforce_rivalries(
    guild: discord.Guild,
    current_channel: discord.VoiceChannel | discord.StageChannel,
) -> None:
    """
    Vérifie les paires présentes dans le salon et sépare celles qui le nécessitent.

    Dans chaque paire :
    - premier ID = utilisateur à déplacer ;
    - deuxième ID = utilisateur protégé.
    """

    rivalries = load_rivalries()
    ids_in_channel = {member.id for member in current_channel.members}

    for problem_user_id, protected_user_id in rivalries:
        pair = {problem_user_id, protected_user_id}

        if not pair.issubset(ids_in_channel):
            continue

        problem_member = guild.get_member(problem_user_id)
        protected_member = guild.get_member(protected_user_id)

        if problem_member is None or protected_member is None:
            await send_log(
                guild,
                "⚠️ VoiceGuard a détecté une paire, mais n'a pas réussi à "
                "récupérer les deux membres.",
            )
            return

        destination = find_available_voice_channel(
            guild=guild,
            current_channel=current_channel,
            member_to_move=problem_member,
            protected_user_id=protected_user_id,
        )

        try:
            if destination is None:
                await problem_member.move_to(
                    None,
                    reason="VoiceGuard : séparation d'une paire interdite",
                )

                await send_move_dm(
                    member=problem_member,
                    protected_member=protected_member,
                    destination=None,
                )

                await send_log(
                    guild,
                    (
                        f"🔴 **Déconnexion VoiceGuard**\n"
                        f"Membre déplacé : {member_name(problem_member)}\n"
                        f"Membre protégé : {member_name(protected_member)}\n"
                        f"Salon d'origine : **{current_channel.name}**\n"
                        f"Motif : aucun autre salon vocal disponible."
                    ),
                )

            else:
                await problem_member.move_to(
                    destination,
                    reason="VoiceGuard : séparation d'une paire interdite",
                )

                await send_move_dm(
                    member=problem_member,
                    protected_member=protected_member,
                    destination=destination,
                )

                await send_log(
                    guild,
                    (
                        f"🛡️ **Déplacement VoiceGuard**\n"
                        f"Membre déplacé : {member_name(problem_member)}\n"
                        f"Membre protégé : {member_name(protected_member)}\n"
                        f"Salon d'origine : **{current_channel.name}**\n"
                        f"Destination : **{destination.name}**"
                    ),
                )

        except discord.Forbidden:
            await send_log(
                guild,
                (
                    "❌ **Permission refusée**\n"
                    "Le rôle du bot doit être placé au-dessus du membre à déplacer "
                    "et posséder la permission **Déplacer des membres**."
                ),
            )

        except discord.HTTPException as error:
            await send_log(
                guild,
                f"❌ Erreur Discord pendant le déplacement : `{error}`",
            )

        return


# ============================================================
# ÉVÉNEMENTS
# ============================================================

@bot.event
async def on_ready():
    print("=" * 60)
    print(f"Bot connecté : {bot.user}")
    print(f"Identifiant du bot : {bot.user.id if bot.user else 'inconnu'}")
    print(f"Paires chargées : {load_rivalries()}")
    print(f"Salon de logs configuré : {LOG_CHANNEL_ID}")
    print("Surveillance active sur tous les salons vocaux.")
    print("=" * 60)

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="les salons vocaux",
        )
    )


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):
    # Ignore les changements de mute, stream ou caméra sans changement de salon.
    if before.channel == after.channel:
        return

    # Connexion à un vocal
    if before.channel is None and after.channel is not None:
        await send_log(
            member.guild,
            (
                f"🟢 **Connexion vocale**\n"
                f"Membre : {member_name(member)}\n"
                f"Salon : **{after.channel.name}**"
            ),
        )

        await enforce_rivalries(member.guild, after.channel)
        return

    # Déconnexion d'un vocal
    if before.channel is not None and after.channel is None:
        await send_log(
            member.guild,
            (
                f"🔴 **Déconnexion vocale**\n"
                f"Membre : {member_name(member)}\n"
                f"Salon quitté : **{before.channel.name}**"
            ),
        )
        return

    # Changement de salon vocal
    if before.channel is not None and after.channel is not None:
        await send_log(
            member.guild,
            (
                f"🔵 **Changement de vocal**\n"
                f"Membre : {member_name(member)}\n"
                f"Départ : **{before.channel.name}**\n"
                f"Arrivée : **{after.channel.name}**"
            ),
        )

        await enforce_rivalries(member.guild, after.channel)


# ============================================================
# COMMANDES
# ============================================================

@bot.command(name="status")
async def status_command(ctx: commands.Context):
    """
    Affiche l'état actuel de VoiceGuard.
    """

    rivalries = load_rivalries()

    embed = discord.Embed(
        title="VoiceGuard est actif",
        description="La surveillance des salons vocaux est opérationnelle.",
    )

    embed.add_field(
        name="Latence",
        value=f"{round(bot.latency * 1000)} ms",
        inline=True,
    )

    embed.add_field(
        name="Paires surveillées",
        value=str(len(rivalries)),
        inline=True,
    )

    embed.add_field(
        name="Serveurs surveillés",
        value=str(len(bot.guilds)),
        inline=True,
    )

    embed.set_footer(
        text="Premier membre d'une paire = membre déplacé"
    )

    await ctx.send(embed=embed)


@bot.command(name="addpair")
@commands.has_permissions(administrator=True)
async def addpair_command(
    ctx: commands.Context,
    problem_member: discord.Member,
    protected_member: discord.Member,
):
    """
    Exemple :
    !addpair @MembreADeplacer @MembreProtege
    """

    if problem_member.id == protected_member.id:
        await ctx.send("❌ Tu dois indiquer deux membres différents.")
        return

    rivalries = load_rivalries()
    new_pair = (problem_member.id, protected_member.id)

    if new_pair in rivalries:
        await ctx.send("⚠️ Cette paire est déjà enregistrée.")
        return

    # Supprime également l'éventuelle paire inversée.
    reversed_pair = (protected_member.id, problem_member.id)

    if reversed_pair in rivalries:
        rivalries.remove(reversed_pair)

    rivalries.append(new_pair)
    save_rivalries(rivalries)

    await ctx.send(
        f"✅ Paire ajoutée.\n"
        f"**Membre à déplacer :** {problem_member.mention}\n"
        f"**Membre protégé :** {protected_member.mention}"
    )

    await send_log(
        ctx.guild,
        (
            f"➕ **Paire VoiceGuard ajoutée par {ctx.author}**\n"
            f"Membre à déplacer : {member_name(problem_member)}\n"
            f"Membre protégé : {member_name(protected_member)}"
        ),
    )


@bot.command(name="removepair")
@commands.has_permissions(administrator=True)
async def removepair_command(
    ctx: commands.Context,
    member_1: discord.Member,
    member_2: discord.Member,
):
    """
    Exemple :
    !removepair @Membre1 @Membre2

    L'ordre des deux membres n'a pas d'importance pour la suppression.
    """

    rivalries = load_rivalries()

    direct_pair = (member_1.id, member_2.id)
    reversed_pair = (member_2.id, member_1.id)

    removed = False

    if direct_pair in rivalries:
        rivalries.remove(direct_pair)
        removed = True

    if reversed_pair in rivalries:
        rivalries.remove(reversed_pair)
        removed = True

    if not removed:
        await ctx.send("⚠️ Cette paire n'est pas enregistrée.")
        return

    save_rivalries(rivalries)

    await ctx.send(
        f"✅ Paire supprimée entre {member_1.mention} et {member_2.mention}."
    )

    await send_log(
        ctx.guild,
        (
            f"➖ **Paire VoiceGuard supprimée par {ctx.author}**\n"
            f"Membres : {member_name(member_1)} et {member_name(member_2)}"
        ),
    )


@bot.command(name="listpairs")
@commands.has_permissions(administrator=True)
async def listpairs_command(ctx: commands.Context):
    """
    Affiche toutes les paires enregistrées.
    """

    rivalries = load_rivalries()

    if not rivalries:
        await ctx.send("Aucune paire VoiceGuard n'est enregistrée.")
        return

    lines = []

    for index, (problem_id, protected_id) in enumerate(rivalries, start=1):
        lines.append(
            f"**{index}.** <@{problem_id}> ➜ déplacé | "
            f"<@{protected_id}> ➜ protégé"
        )

    await ctx.send(
        "**Paires surveillées par VoiceGuard :**\n\n"
        + "\n".join(lines)
    )


@bot.command(name="helpvoiceguard")
async def help_voiceguard_command(ctx: commands.Context):
    await ctx.send(
        "**Commandes VoiceGuard**\n\n"
        "`!status` — vérifier que le bot est actif\n"
        "`!addpair @membre_à_déplacer @membre_protégé` — ajouter une paire\n"
        "`!removepair @membre1 @membre2` — supprimer une paire\n"
        "`!listpairs` — afficher les paires enregistrées\n"
        "`!helpvoiceguard` — afficher cette aide\n\n"
        "Les commandes de modification sont réservées aux administrateurs."
    )


# ============================================================
# GESTION DES ERREURS DE COMMANDES
# ============================================================

@addpair_command.error
@removepair_command.error
async def pair_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(
            "❌ Seuls les administrateurs peuvent utiliser cette commande."
        )

    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            "❌ Il manque un membre.\n"
            "Exemple : `!addpair @membre_à_déplacer @membre_protégé`"
        )

    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(
            "❌ Membre introuvable. Mentionne directement les deux membres."
        )

    else:
        await ctx.send(f"❌ Erreur : `{error}`")


# ============================================================
# LANCEMENT
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN manquant dans les variables d'environnement Railway."
    )

bot.run(TOKEN)
