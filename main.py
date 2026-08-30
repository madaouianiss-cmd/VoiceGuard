import os
import json
import hmac
import hashlib
import asyncio
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

import discord
from discord.ext import commands


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

# Logs VoiceGuard
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Twitch / Stream notification
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
TWITCH_EVENTSUB_SECRET = os.getenv("TWITCH_EVENTSUB_SECRET")
TWITCH_LOGIN = os.getenv("TWITCH_LOGIN")

STREAM_NOTIFICATION_CHANNEL_ID = int(
    os.getenv("STREAM_NOTIFICATION_CHANNEL_ID", "0")
)

PUBLIC_URL = os.getenv(
    "PUBLIC_URL",
    "https://worker-production-3aa3.up.railway.app",
).rstrip("/")

PORT = int(os.getenv("PORT", "8080"))

RIVALRIES_FILE = Path("rivalries.json")
STREAM_STATE_FILE = Path("twitch_stream_state.json")

COMMAND_PREFIX = "!"


# ============================================================
# ÉTAT WEBHOOK
# ============================================================

web_runner: Optional[web.AppRunner] = None
twitch_registration_lock = asyncio.Lock()


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
            return [
                (int(problem), int(protected))
                for problem, protected in pairs
            ]

        except Exception as error:
            raise RuntimeError(
                f"RIVALRIES_JSON invalide : {error}"
            ) from error

    try:
        with RIVALRIES_FILE.open("r", encoding="utf-8") as file:
            pairs = json.load(file)

        return [
            (int(problem), int(protected))
            for problem, protected in pairs
        ]

    except FileNotFoundError:
        return []

    except Exception as error:
        raise RuntimeError(
            f"rivalries.json invalide : {error}"
        ) from error


def save_rivalries(
    rivalries: list[tuple[int, int]]
) -> None:
    """
    Sauvegarde les paires dans rivalries.json.
    """

    temporary_file = RIVALRIES_FILE.with_suffix(".tmp")

    with temporary_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(rivalries, file, indent=2)

    temporary_file.replace(RIVALRIES_FILE)


# ============================================================
# ÉTAT TWITCH
# ============================================================

def load_last_stream_id() -> Optional[str]:
    try:
        with STREAM_STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)

        return data.get("stream_id")

    except Exception:
        return None


def save_last_stream_id(stream_id: str) -> None:
    try:
        temporary_file = STREAM_STATE_FILE.with_suffix(".tmp")

        with temporary_file.open(
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                {
                    "stream_id": stream_id,
                },
                file,
                indent=2,
            )

        temporary_file.replace(STREAM_STATE_FILE)

    except Exception as error:
        print(
            f"[TWITCH] Impossible de sauvegarder "
            f"l'état du stream : {error}"
        )


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
# OUTILS DISCORD
# ============================================================

def get_log_channel(
    guild: discord.Guild
) -> Optional[discord.TextChannel]:

    if LOG_CHANNEL_ID == 0:
        return None

    channel = guild.get_channel(LOG_CHANNEL_ID)

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


async def send_log(
    guild: discord.Guild,
    message: str,
) -> None:

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
        print(
            f"Erreur lors de l'envoi d'un log Discord : {error}"
        )


def member_name(member: discord.Member) -> str:
    return f"{member} (`{member.id}`)"


# ============================================================
# TWITCH API
# ============================================================

async def get_twitch_app_token() -> str:
    if not TWITCH_CLIENT_ID:
        raise RuntimeError(
            "TWITCH_CLIENT_ID manquant."
        )

    if not TWITCH_CLIENT_SECRET:
        raise RuntimeError(
            "TWITCH_CLIENT_SECRET manquant."
        )

    url = "https://id.twitch.tv/oauth2/token"

    params = {
        "client_id": TWITCH_CLIENT_ID,
        "client_secret": TWITCH_CLIENT_SECRET,
        "grant_type": "client_credentials",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            params=params,
        ) as response:

            data = await response.json()

            if response.status != 200:
                raise RuntimeError(
                    f"Erreur token Twitch "
                    f"{response.status}: {data}"
                )

            return data["access_token"]


async def get_twitch_user(
    token: str,
) -> dict:

    if not TWITCH_LOGIN:
        raise RuntimeError(
            "TWITCH_LOGIN manquant."
        )

    url = "https://api.twitch.tv/helix/users"

    headers = {
        "Authorization": f"Bearer {token}",
        "Client-Id": TWITCH_CLIENT_ID,
    }

    params = {
        "login": TWITCH_LOGIN,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=headers,
            params=params,
        ) as response:

            data = await response.json()

            if response.status != 200:
                raise RuntimeError(
                    f"Erreur récupération utilisateur Twitch "
                    f"{response.status}: {data}"
                )

            users = data.get("data", [])

            if not users:
                raise RuntimeError(
                    f"Compte Twitch '{TWITCH_LOGIN}' introuvable."
                )

            return users[0]


async def get_twitch_stream(
    broadcaster_user_id: str,
) -> Optional[dict]:

    token = await get_twitch_app_token()

    url = "https://api.twitch.tv/helix/streams"

    headers = {
        "Authorization": f"Bearer {token}",
        "Client-Id": TWITCH_CLIENT_ID,
    }

    params = {
        "user_id": broadcaster_user_id,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            headers=headers,
            params=params,
        ) as response:

            data = await response.json()

            if response.status != 200:
                print(
                    f"[TWITCH] Impossible de récupérer "
                    f"les informations du stream : {data}"
                )

                return None

            streams = data.get("data", [])

            if not streams:
                return None

            return streams[0]


# ============================================================
# NOTIFICATION TWITCH -> DISCORD
# ============================================================

async def send_stream_notification(
    event: dict,
) -> None:

    broadcaster_name = event.get(
        "broadcaster_user_name",
        TWITCH_LOGIN or "Streamer",
    )

    broadcaster_login = event.get(
        "broadcaster_user_login",
        TWITCH_LOGIN,
    )

    broadcaster_user_id = event.get(
        "broadcaster_user_id"
    )

    stream_id = str(
        event.get("id")
        or event.get("started_at")
        or ""
    )

    if not stream_id:
        print(
            "[TWITCH] Impossible de déterminer "
            "l'identifiant du stream."
        )
        return

    last_stream_id = load_last_stream_id()

    if last_stream_id == stream_id:
        print(
            f"[TWITCH] Stream {stream_id} "
            f"déjà notifié."
        )
        return

    if not broadcaster_login:
        print(
            "[TWITCH] Login du streamer introuvable."
        )
        return

    twitch_url = (
        f"https://www.twitch.tv/{broadcaster_login}"
    )

    # Petit délai pour laisser Helix exposer
    # le titre, la catégorie et la miniature.
    await asyncio.sleep(2)

    stream = None

    if broadcaster_user_id:
        try:
            stream = await get_twitch_stream(
                broadcaster_user_id
            )

        except Exception as error:
            print(
                f"[TWITCH] Erreur récupération stream : {error}"
            )

    try:
        channel = bot.get_channel(
            STREAM_NOTIFICATION_CHANNEL_ID
        )

        if channel is None:
            channel = await bot.fetch_channel(
                STREAM_NOTIFICATION_CHANNEL_ID
            )

    except discord.HTTPException as error:
        print(
            f"[TWITCH] Salon Discord introuvable : {error}"
        )
        return

    if not isinstance(
        channel,
        discord.TextChannel,
    ):
        print(
            "[TWITCH] STREAM_NOTIFICATION_CHANNEL_ID "
            "ne correspond pas à un salon textuel."
        )
        return

    title = "Le stream vient de commencer !"
    game_name = "Non renseignée"
    viewer_count = None
    thumbnail = None

    if stream:
        title = (
            stream.get("title")
            or title
        )

        game_name = (
            stream.get("game_name")
            or game_name
        )

        viewer_count = stream.get(
            "viewer_count"
        )

        raw_thumbnail = stream.get(
            "thumbnail_url"
        )

        if raw_thumbnail:
            thumbnail = (
                raw_thumbnail
                .replace("{width}", "1280")
                .replace("{height}", "720")
                + f"?t={int(asyncio.get_running_loop().time())}"
            )

    embed = discord.Embed(
        title=f"🔴 {title}",
        description=(
            f"**{broadcaster_name} est maintenant en LIVE !**\n\n"
            f"🎮 **{game_name}**\n\n"
            f"👉 [Regarder le stream sur Twitch]({twitch_url})"
        ),
        url=twitch_url,
        color=0x9146FF,
    )

    if viewer_count is not None:
        embed.add_field(
            name="Viewers",
            value=str(viewer_count),
            inline=True,
        )

    if thumbnail:
        embed.set_image(
            url=thumbnail
        )

    embed.set_footer(
        text="VoiceGuard • Twitch Live"
    )

    try:
        await channel.send(
            content=(
                f"@everyone 🔴 **{broadcaster_name} "
                f"est EN LIVE !**\n"
                f"{twitch_url}"
            ),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=True,
                users=False,
                roles=False,
            ),
        )

        save_last_stream_id(
            stream_id
        )

        print(
            f"[TWITCH] Notification Discord envoyée "
            f"pour le stream {stream_id}"
        )

    except discord.Forbidden:
        print(
            "[TWITCH] Permission refusée. "
            "VoiceGuard doit pouvoir envoyer des messages, "
            "intégrer des liens et mentionner @everyone."
        )

    except discord.HTTPException as error:
        print(
            f"[TWITCH] Erreur Discord : {error}"
        )


# ============================================================
# SÉCURITÉ EVENTSUB
# ============================================================

def verify_twitch_signature(
    request: web.Request,
    body: bytes,
) -> bool:

    if not TWITCH_EVENTSUB_SECRET:
        return False

    message_id = request.headers.get(
        "Twitch-Eventsub-Message-Id"
    )

    timestamp = request.headers.get(
        "Twitch-Eventsub-Message-Timestamp"
    )

    received_signature = request.headers.get(
        "Twitch-Eventsub-Message-Signature"
    )

    if (
        not message_id
        or not timestamp
        or not received_signature
    ):
        return False

    message = (
        message_id.encode("utf-8")
        + timestamp.encode("utf-8")
        + body
    )

    digest = hmac.new(
        TWITCH_EVENTSUB_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()

    expected_signature = (
        f"sha256={digest}"
    )

    return hmac.compare_digest(
        expected_signature,
        received_signature,
    )


# ============================================================
# WEBHOOK TWITCH
# ============================================================

async def twitch_webhook(
    request: web.Request,
) -> web.Response:

    raw_body = await request.read()

    if not verify_twitch_signature(
        request,
        raw_body,
    ):
        print(
            "[TWITCH] Signature EventSub invalide."
        )

        return web.Response(
            status=403
        )

    try:
        payload = json.loads(
            raw_body.decode("utf-8")
        )

    except json.JSONDecodeError:
        return web.Response(
            status=400
        )

    message_type = request.headers.get(
        "Twitch-Eventsub-Message-Type"
    )

    # ========================================================
    # TWITCH VÉRIFIE LE WEBHOOK
    # ========================================================

    if (
        message_type
        == "webhook_callback_verification"
    ):
        challenge = payload.get(
            "challenge"
        )

        print(
            "[TWITCH] Challenge EventSub reçu."
        )

        return web.Response(
            text=challenge,
            status=200,
            content_type="text/plain",
        )

    # ========================================================
    # ABONNEMENT RÉVOQUÉ
    # ========================================================

    if message_type == "revocation":
        print(
            "[TWITCH] Abonnement EventSub révoqué :"
        )

        print(
            payload.get("subscription")
        )

        return web.Response(
            status=204
        )

    # ========================================================
    # NOTIFICATION
    # ========================================================

    if message_type == "notification":

        subscription = payload.get(
            "subscription",
            {},
        )

        event = payload.get(
            "event",
            {},
        )

        if (
            subscription.get("type")
            == "stream.online"
        ):
            print(
                "[TWITCH] Événement stream.online reçu."
            )

            # On répond tout de suite à Twitch.
            asyncio.create_task(
                send_stream_notification(
                    event
                )
            )

        return web.Response(
            status=204
        )

    return web.Response(
        status=204
    )


async def health_check(
    request: web.Request,
) -> web.Response:

    return web.json_response(
        {
            "service": "VoiceGuard",
            "discord": bot.is_ready(),
            "twitch_webhook": True,
        }
    )


async def start_web_server() -> None:
    global web_runner

    if web_runner is not None:
        return

    app = web.Application()

    app.router.add_get(
        "/",
        health_check,
    )

    app.router.add_get(
        "/health",
        health_check,
    )

    app.router.add_post(
        "/webhooks/twitch",
        twitch_webhook,
    )

    web_runner = web.AppRunner(
        app
    )

    await web_runner.setup()

    site = web.TCPSite(
        web_runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    print(
        f"[WEB] Serveur HTTP VoiceGuard actif "
        f"sur le port {PORT}"
    )

    print(
        f"[WEB] Webhook Twitch : "
        f"{PUBLIC_URL}/webhooks/twitch"
    )


# ============================================================
# ENREGISTREMENT EVENTSUB
# ============================================================

async def register_twitch_eventsub() -> None:

    async with twitch_registration_lock:

        required_values = {
            "TWITCH_CLIENT_ID": TWITCH_CLIENT_ID,
            "TWITCH_CLIENT_SECRET": TWITCH_CLIENT_SECRET,
            "TWITCH_EVENTSUB_SECRET": TWITCH_EVENTSUB_SECRET,
            "TWITCH_LOGIN": TWITCH_LOGIN,
        }

        missing = [
            name
            for name, value in required_values.items()
            if not value
        ]

        if missing:
            print(
                "[TWITCH] EventSub désactivé. "
                "Variables manquantes : "
                + ", ".join(missing)
            )
            return

        if STREAM_NOTIFICATION_CHANNEL_ID == 0:
            print(
                "[TWITCH] "
                "STREAM_NOTIFICATION_CHANNEL_ID manquant."
            )
            return

        try:
            token = await get_twitch_app_token()

            user = await get_twitch_user(
                token
            )

            broadcaster_user_id = user["id"]

            print(
                f"[TWITCH] Compte détecté : "
                f"{user['display_name']} "
                f"({broadcaster_user_id})"
            )

            callback_url = (
                f"{PUBLIC_URL}/webhooks/twitch"
            )

            headers = {
                "Authorization": (
                    f"Bearer {token}"
                ),
                "Client-Id": (
                    TWITCH_CLIENT_ID
                ),
            }

            # ------------------------------------------------
            # Vérifie si l'abonnement existe déjà
            # ------------------------------------------------

            list_url = (
                "https://api.twitch.tv/"
                "helix/eventsub/subscriptions"
            )

            async with aiohttp.ClientSession() as session:

                async with session.get(
                    list_url,
                    headers=headers,
                ) as response:

                    data = await response.json()

                    if response.status != 200:
                        raise RuntimeError(
                            f"Impossible de lire les "
                            f"subscriptions Twitch : {data}"
                        )

                subscriptions = data.get(
                    "data",
                    [],
                )

                for subscription in subscriptions:

                    condition = subscription.get(
                        "condition",
                        {},
                    )

                    transport = subscription.get(
                        "transport",
                        {},
                    )

                    if (
                        subscription.get("type")
                        == "stream.online"
                        and condition.get(
                            "broadcaster_user_id"
                        )
                        == broadcaster_user_id
                        and transport.get(
                            "callback"
                        )
                        == callback_url
                        and subscription.get(
                            "status"
                        )
                        in {
                            "enabled",
                            "webhook_callback_verification_pending",
                        }
                    ):
                        print(
                            "[TWITCH] Subscription "
                            "stream.online déjà active."
                        )

                        return

                # --------------------------------------------
                # Création abonnement
                # --------------------------------------------

                payload = {
                    "type": "stream.online",
                    "version": "1",
                    "condition": {
                        "broadcaster_user_id":
                            broadcaster_user_id,
                    },
                    "transport": {
                        "method": "webhook",
                        "callback": callback_url,
                        "secret": TWITCH_EVENTSUB_SECRET,
                    },
                }

                headers[
                    "Content-Type"
                ] = "application/json"

                async with session.post(
                    list_url,
                    headers=headers,
                    json=payload,
                ) as response:

                    result = await response.json()

                    if response.status != 202:
                        raise RuntimeError(
                            f"Création EventSub impossible "
                            f"({response.status}) : {result}"
                        )

                    print(
                        "[TWITCH] Subscription "
                        "stream.online créée."
                    )

                    print(
                        "[TWITCH] Twitch va maintenant "
                        "vérifier le webhook."
                    )

        except Exception as error:
            print(
                f"[TWITCH] Erreur EventSub : {error}"
            )


# ============================================================
# VOICEGUARD
# ============================================================

def find_available_voice_channel(
    guild: discord.Guild,
    current_channel:
        discord.VoiceChannel
        | discord.StageChannel,
    member_to_move: discord.Member,
    protected_user_id: int,
) -> Optional[discord.VoiceChannel]:

    for channel in guild.voice_channels:

        if channel.id == current_channel.id:
            continue

        if (
            guild.afk_channel
            and channel.id
            == guild.afk_channel.id
        ):
            continue

        member_ids = {
            member.id
            for member in channel.members
        }

        if protected_user_id in member_ids:
            continue

        bot_member = guild.me

        if bot_member is None:
            continue

        bot_permissions = (
            channel.permissions_for(
                bot_member
            )
        )

        target_permissions = (
            channel.permissions_for(
                member_to_move
            )
        )

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

        if (
            channel.user_limit != 0
            and len(channel.members)
            >= channel.user_limit
        ):
            continue

        return channel

    return None


async def send_move_dm(
    member: discord.Member,
    protected_member: discord.Member,
    destination:
        Optional[discord.VoiceChannel],
) -> None:

    if destination is not None:
        message = (
            f"Tu as été déplacé automatiquement vers "
            f"**{destination.name}** par VoiceGuard.\n\n"
            f"Une règle du serveur empêche actuellement "
            f"ta présence dans le même salon vocal que "
            f"**{protected_member.display_name}**.\n"
            f"Merci de rester dans un salon vocal séparé."
        )

    else:
        message = (
            "Tu as été déconnecté automatiquement "
            "du salon vocal par VoiceGuard.\n\n"
            f"Une règle du serveur empêche actuellement "
            f"ta présence dans le même salon vocal que "
            f"**{protected_member.display_name}** "
            f"et aucun autre salon vocal disponible "
            f"n'a été trouvé."
        )

    try:
        await member.send(
            message
        )

    except (
        discord.Forbidden,
        discord.HTTPException,
    ):
        pass


async def enforce_rivalries(
    guild: discord.Guild,
    current_channel:
        discord.VoiceChannel
        | discord.StageChannel,
) -> None:

    rivalries = load_rivalries()

    ids_in_channel = {
        member.id
        for member in current_channel.members
    }

    for (
        problem_user_id,
        protected_user_id,
    ) in rivalries:

        pair = {
            problem_user_id,
            protected_user_id,
        }

        if not pair.issubset(
            ids_in_channel
        ):
            continue

        problem_member = guild.get_member(
            problem_user_id
        )

        protected_member = guild.get_member(
            protected_user_id
        )

        if (
            problem_member is None
            or protected_member is None
        ):
            await send_log(
                guild,
                (
                    "⚠️ VoiceGuard a détecté une paire, "
                    "mais n'a pas réussi à récupérer "
                    "les deux membres."
                ),
            )

            return

        destination = (
            find_available_voice_channel(
                guild=guild,
                current_channel=current_channel,
                member_to_move=problem_member,
                protected_user_id=protected_user_id,
            )
        )

        try:

            if destination is None:

                await problem_member.move_to(
                    None,
                    reason=(
                        "VoiceGuard : séparation "
                        "d'une paire interdite"
                    ),
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
                        f"Membre déplacé : "
                        f"{member_name(problem_member)}\n"
                        f"Membre protégé : "
                        f"{member_name(protected_member)}\n"
                        f"Salon d'origine : "
                        f"**{current_channel.name}**\n"
                        f"Motif : aucun autre salon "
                        f"vocal disponible."
                    ),
                )

            else:

                await problem_member.move_to(
                    destination,
                    reason=(
                        "VoiceGuard : séparation "
                        "d'une paire interdite"
                    ),
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
                        f"Membre déplacé : "
                        f"{member_name(problem_member)}\n"
                        f"Membre protégé : "
                        f"{member_name(protected_member)}\n"
                        f"Salon d'origine : "
                        f"**{current_channel.name}**\n"
                        f"Destination : "
                        f"**{destination.name}**"
                    ),
                )

        except discord.Forbidden:

            await send_log(
                guild,
                (
                    "❌ **Permission refusée**\n"
                    "Le rôle du bot doit être placé "
                    "au-dessus du membre à déplacer "
                    "et posséder la permission "
                    "**Déplacer des membres**."
                ),
            )

        except discord.HTTPException as error:

            await send_log(
                guild,
                (
                    "❌ Erreur Discord pendant "
                    f"le déplacement : `{error}`"
                ),
            )

        return


# ============================================================
# ÉVÉNEMENTS
# ============================================================

@bot.event
async def on_ready():

    print("=" * 60)
    print(
        f"Bot connecté : {bot.user}"
    )
    print(
        f"Identifiant du bot : "
        f"{bot.user.id if bot.user else 'inconnu'}"
    )
    print(
        f"Paires chargées : {load_rivalries()}"
    )
    print(
        f"Salon de logs configuré : "
        f"{LOG_CHANNEL_ID}"
    )
    print(
        "Surveillance active sur tous "
        "les salons vocaux."
    )
    print("=" * 60)

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="les salons vocaux",
        )
    )

    # Serveur Railway / webhook Twitch
    await start_web_server()

    # EventSub Twitch
    asyncio.create_task(
        register_twitch_eventsub()
    )


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
):

    if before.channel == after.channel:
        return

    if (
        before.channel is None
        and after.channel is not None
    ):

        await send_log(
            member.guild,
            (
                f"🟢 **Connexion vocale**\n"
                f"Membre : {member_name(member)}\n"
                f"Salon : **{after.channel.name}**"
            ),
        )

        await enforce_rivalries(
            member.guild,
            after.channel,
        )

        return

    if (
        before.channel is not None
        and after.channel is None
    ):

        await send_log(
            member.guild,
            (
                f"🔴 **Déconnexion vocale**\n"
                f"Membre : {member_name(member)}\n"
                f"Salon quitté : "
                f"**{before.channel.name}**"
            ),
        )

        return

    if (
        before.channel is not None
        and after.channel is not None
    ):

        await send_log(
            member.guild,
            (
                f"🔵 **Changement de vocal**\n"
                f"Membre : {member_name(member)}\n"
                f"Départ : **{before.channel.name}**\n"
                f"Arrivée : **{after.channel.name}**"
            ),
        )

        await enforce_rivalries(
            member.guild,
            after.channel,
        )


# ============================================================
# COMMANDES
# ============================================================

@bot.command(name="status")
async def status_command(
    ctx: commands.Context
):

    rivalries = load_rivalries()

    embed = discord.Embed(
        title="VoiceGuard est actif",
        description=(
            "La surveillance des salons vocaux "
            "est opérationnelle."
        ),
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

    twitch_status = (
        "Configuré"
        if (
            TWITCH_CLIENT_ID
            and TWITCH_CLIENT_SECRET
            and TWITCH_LOGIN
            and TWITCH_EVENTSUB_SECRET
            and STREAM_NOTIFICATION_CHANNEL_ID
        )
        else "Non configuré"
    )

    embed.add_field(
        name="Twitch",
        value=twitch_status,
        inline=True,
    )

    embed.set_footer(
        text=(
            "Premier membre d'une paire "
            "= membre déplacé"
        )
    )

    await ctx.send(
        embed=embed
    )


@bot.command(name="streamtest")
@commands.has_permissions(
    administrator=True
)
async def stream_test_command(
    ctx: commands.Context
):
    """
    Teste l'alerte Twitch sans lancer de vrai stream.
    """

    if STREAM_NOTIFICATION_CHANNEL_ID == 0:
        await ctx.send(
            "❌ STREAM_NOTIFICATION_CHANNEL_ID "
            "n'est pas configuré."
        )
        return

    login = (
        TWITCH_LOGIN
        or "twitch"
    )

    twitch_url = (
        f"https://www.twitch.tv/{login}"
    )

    channel = bot.get_channel(
        STREAM_NOTIFICATION_CHANNEL_ID
    )

    if channel is None:
        try:
            channel = await bot.fetch_channel(
                STREAM_NOTIFICATION_CHANNEL_ID
            )

        except discord.HTTPException:
            await ctx.send(
                "❌ Salon de notification introuvable."
            )
            return

    embed = discord.Embed(
        title="🔴 TEST — VoiceGuard Live",
        description=(
            "**Ceci est un test de notification Twitch.**\n\n"
            f"👉 [Regarder sur Twitch]({twitch_url})"
        ),
        url=twitch_url,
        color=0x9146FF,
    )

    await channel.send(
        content=(
            f"@everyone 🔴 **TEST STREAM VOICEGUARD**\n"
            f"{twitch_url}"
        ),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            everyone=True
        ),
    )

    await ctx.send(
        "✅ Notification Twitch de test envoyée."
    )


@bot.command(name="addpair")
@commands.has_permissions(
    administrator=True
)
async def addpair_command(
    ctx: commands.Context,
    problem_member: discord.Member,
    protected_member: discord.Member,
):

    if (
        problem_member.id
        == protected_member.id
    ):
        await ctx.send(
            "❌ Tu dois indiquer deux membres différents."
        )
        return

    rivalries = load_rivalries()

    new_pair = (
        problem_member.id,
        protected_member.id,
    )

    if new_pair in rivalries:
        await ctx.send(
            "⚠️ Cette paire est déjà enregistrée."
        )
        return

    reversed_pair = (
        protected_member.id,
        problem_member.id,
    )

    if reversed_pair in rivalries:
        rivalries.remove(
            reversed_pair
        )

    rivalries.append(
        new_pair
    )

    save_rivalries(
        rivalries
    )

    await ctx.send(
        f"✅ Paire ajoutée.\n"
        f"**Membre à déplacer :** "
        f"{problem_member.mention}\n"
        f"**Membre protégé :** "
        f"{protected_member.mention}"
    )

    if ctx.guild:

        await send_log(
            ctx.guild,
            (
                f"➕ **Paire VoiceGuard ajoutée "
                f"par {ctx.author}**\n"
                f"Membre à déplacer : "
                f"{member_name(problem_member)}\n"
                f"Membre protégé : "
                f"{member_name(protected_member)}"
            ),
        )


@bot.command(name="removepair")
@commands.has_permissions(
    administrator=True
)
async def removepair_command(
    ctx: commands.Context,
    member_1: discord.Member,
    member_2: discord.Member,
):

    rivalries = load_rivalries()

    direct_pair = (
        member_1.id,
        member_2.id,
    )

    reversed_pair = (
        member_2.id,
        member_1.id,
    )

    removed = False

    if direct_pair in rivalries:
        rivalries.remove(
            direct_pair
        )
        removed = True

    if reversed_pair in rivalries:
        rivalries.remove(
            reversed_pair
        )
        removed = True

    if not removed:
        await ctx.send(
            "⚠️ Cette paire n'est pas enregistrée."
        )
        return

    save_rivalries(
        rivalries
    )

    await ctx.send(
        f"✅ Paire supprimée entre "
        f"{member_1.mention} et "
        f"{member_2.mention}."
    )

    if ctx.guild:

        await send_log(
            ctx.guild,
            (
                f"➖ **Paire VoiceGuard supprimée "
                f"par {ctx.author}**\n"
                f"Membres : "
                f"{member_name(member_1)} et "
                f"{member_name(member_2)}"
            ),
        )


@bot.command(name="listpairs")
@commands.has_permissions(
    administrator=True
)
async def listpairs_command(
    ctx: commands.Context
):

    rivalries = load_rivalries()

    if not rivalries:
        await ctx.send(
            "Aucune paire VoiceGuard "
            "n'est enregistrée."
        )
        return

    lines = []

    for index, (
        problem_id,
        protected_id,
    ) in enumerate(
        rivalries,
        start=1,
    ):

        lines.append(
            f"**{index}.** "
            f"<@{problem_id}> ➜ déplacé | "
            f"<@{protected_id}> ➜ protégé"
        )

    await ctx.send(
        "**Paires surveillées "
        "par VoiceGuard :**\n\n"
        + "\n".join(lines)
    )


@bot.command(name="helpvoiceguard")
async def help_voiceguard_command(
    ctx: commands.Context
):

    await ctx.send(
        "**Commandes VoiceGuard**\n\n"
        "`!status` — vérifier que le bot est actif\n"
        "`!streamtest` — tester la notification Twitch\n"
        "`!addpair @membre_à_déplacer @membre_protégé` "
        "— ajouter une paire\n"
        "`!removepair @membre1 @membre2` "
        "— supprimer une paire\n"
        "`!listpairs` — afficher les paires enregistrées\n"
        "`!helpvoiceguard` — afficher cette aide\n\n"
        "Les commandes de modification "
        "sont réservées aux administrateurs."
    )


# ============================================================
# GESTION DES ERREURS
# ============================================================

@addpair_command.error
@removepair_command.error
async def pair_command_error(
    ctx: commands.Context,
    error: commands.CommandError,
):

    if isinstance(
        error,
        commands.MissingPermissions,
    ):
        await ctx.send(
            "❌ Seuls les administrateurs "
            "peuvent utiliser cette commande."
        )

    elif isinstance(
        error,
        commands.MissingRequiredArgument,
    ):
        await ctx.send(
            "❌ Il manque un membre.\n"
            "Exemple : "
            "`!addpair @membre_à_déplacer "
            "@membre_protégé`"
        )

    elif isinstance(
        error,
        commands.MemberNotFound,
    ):
        await ctx.send(
            "❌ Membre introuvable. "
            "Mentionne directement les deux membres."
        )

    else:
        await ctx.send(
            f"❌ Erreur : `{error}`"
        )


@stream_test_command.error
async def stream_test_error(
    ctx: commands.Context,
    error: commands.CommandError,
):

    if isinstance(
        error,
        commands.MissingPermissions,
    ):
        await ctx.send(
            "❌ Seuls les administrateurs "
            "peuvent tester les notifications."
        )

    else:
        await ctx.send(
            f"❌ Erreur : `{error}`"
        )


# ============================================================
# LANCEMENT
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN manquant dans "
        "les variables d'environnement Railway."
    )

bot.run(TOKEN)
