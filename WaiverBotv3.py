import sqlite3
import discord
from discord.ext import commands
from discord import Embed
from discord.ext import tasks
import pytz
from datetime import datetime, timedelta
import logging
import json
import asyncio
from requests.exceptions import Timeout, RequestException

with open('config.json', 'r') as f:
    config = json.load(f)

TOKEN = config['token']

# Create a logger object
logger = logging.getLogger('discord_bot')
logger.setLevel(logging.DEBUG)

# Set up logging to a file
file_handler = logging.FileHandler(filename='discord_bot.log', encoding='utf-8', mode='a')
file_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(file_handler)

# Set up logging to the console
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
logger.addHandler(console_handler)

# Discord bot setup
intents = discord.Intents.default()
client = discord.Client(case_insensitive=True, intents=intents)
intents.message_content = True
bot = commands.Bot(command_prefix='!', case_insensitive=True, ignore_extras=True, intents=intents)

# Role and channel dictionaries
ROLES_DICT = {
    "Rookie Mentor": 712163051586977893,
    "DSFLGM": 712152408943230977,
}
TEAMS_DICT = {
    "BBB": '712159373832486972',
    "NOR": '712159741237002351',
    "KCC": '712159686991806595',
    "DAL": '712159262796939304',
    "PDX": '712159867250409572',
    "LDN": '712159867942469763',
    "MIN": '712159868668084274',
    "TIJ": '712159866369605654',
}
TEAM_NAMES_DICT = {
    "BBB": "Bondi Beach Buccaneers",
    "NOR": "Norfolk Seawolves",
    "KCC": "Kansas City Coyotes",
    "DAL": "Dallas Birddogs",
    "PDX": "Portland Pythons",
    "LDN": "London Royals",
    "MIN": "Minnesota Grey Ducks",
    "TIJ": "Tijuana Luchadores",
}
CHANNELS_DICT = {
    "announcement_channel": 712163701167226880,  # Replace with the actual channel ID
}

# SQLite3 Database Connection
DB_PATH = "waiverbot.db"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

RETRY_COUNT = 3
RETRY_DELAY = 5

is_announcements_paused = False
is_find_clearing_players_paused = False


def split_string_into_chunks(s, chunk_size=2000):
    # Splitting the string by double newlines to ensure we don't split player entries
    entries = s.split("\n\n")

    chunks = []
    current_chunk = ""

    for entry in entries:
        if len(current_chunk + entry) < chunk_size:
            current_chunk += entry + "\n\n"
        else:
            chunks.append(current_chunk)
            current_chunk = entry + "\n\n"

    chunks.append(current_chunk)
    return chunks


def get_team_priority(team_name_or_id):
    try:
        # Try to get the priority using team name
        role_id = str(TEAMS_DICT[team_name_or_id])
    except KeyError:
        # If fails, it means team_name_or_id is probably a Role ID. This reverses the dictionary to find the team name.
        reversed_teams_dict = {v: k for k, v in TEAMS_DICT.items()}
        if team_name_or_id in reversed_teams_dict:
            team_name = reversed_teams_dict[team_name_or_id]
            role_id = team_name_or_id
        else:
            logger.error(f"Error: Team or Role ID {team_name_or_id} not found in TEAMS_DICT.")
            return float('inf')  # Return a large value for priority if team not found. Should not be needed.

    # Now, fetch the priority using the role_id from the database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT Priority FROM Teams WHERE RoleID = ?", (role_id,))
    result = cursor.fetchone()
    conn.close()

    if result:
        return int(result[0])

    logger.warning(f"Team with Role ID {role_id} not found in Teams database table. Returning default priority.")
    return float('inf')  # Return a large value for priority if the team is not found


def adjust_team_priority(team_role):
    logger.info(f"Starting to adjust priority for team {team_role}")

    role_id = TEAMS_DICT[team_role]

    # Connect to the database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Fetch the current priority of the claiming team
    cursor.execute("SELECT Priority FROM Teams WHERE RoleID = ?", (role_id,))
    current_priority = cursor.fetchone()
    if not current_priority:
        logger.error(f"Could not find team {team_role} with Role ID {role_id} in Teams database table")
        return
    current_priority = int(current_priority[0])

    # Get the maximum priority value
    cursor.execute("SELECT MAX(Priority) FROM Teams")
    max_priority = int(cursor.fetchone()[0])

    # Decrement the priority of all teams with priority greater than the claiming team and less than or equal to the max
    cursor.execute("UPDATE Teams SET Priority = Priority - 1 WHERE Priority > ? AND Priority <= ?",
                   (current_priority, max_priority))

    # Increment the priority of the claiming team to the bottom (max)
    cursor.execute("UPDATE Teams SET Priority = ? WHERE RoleID = ?", (max_priority, role_id))

    # Commit the changes
    conn.commit()
    conn.close()

    logger.info(f"Successfully adjusted priority for team {team_role}")


def send_announcement(player_row_index, playerid):
    try:
        # Check if the current time is within the allowed announcement time window.
        eastern = pytz.timezone('US/Eastern')
        current_time = datetime.now(eastern)
        if not (17 <= current_time.hour <= 22):  # Checking if the time is between 5pm and 10pm EST
            logger.warning(f"Attempted to announce player {playerid} outside of allowed time window")
            return None, None

        current_time = datetime.now()

        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Fetch the necessary data from the Players table for the announcement message
        cursor.execute("SELECT PlayerName, Position, PageURL FROM Players WHERE PlayerID = ?",
                       (playerid,))
        result = cursor.fetchone()
        if not result:
            logger.error(f"Player with ID {playerid} not found in Players database table")
            return None, None

        PlayerName, player_position, player_page = result

        # Log the intended announcement
        logger.info(f"Prepared announcement for Player {PlayerName} ({player_position}) with ID {playerid}")

        # Update the Players table
        clearing_time = (current_time + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute("""
            UPDATE Players 
            SET Status = 'Available', Announced = 'Y', TimeAnnounced = ?, TimeClearing = ? 
            WHERE PlayerID = ?
        """, (current_time.strftime('%Y-%m-%d %H:%M:%S'), clearing_time, playerid))

        # Commit the changes
        conn.commit()
        conn.close()

        # Compose the message
        announcement_message = f"ID: {playerid} - {PlayerName} - {player_position} - {player_page}"
        return announcement_message, clearing_time

    except Exception as e:
        logger.error(f"Error preparing announcement for Player with ID {playerid}: {e}")
        return None, None


async def handle_normal_claim(player_row, team_role, playerid, claim_order_pref=None):
    try:
        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Fetch the necessary data from the Players table
        cursor.execute("SELECT PlayerName FROM Players WHERE PlayerID = ?", (playerid,))
        PlayerName = cursor.fetchone()[0]

        # Insert the claim data into the Claims table
        claim_data = (playerid, TEAMS_DICT[team_role], PlayerName, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      'normal', claim_order_pref)
        cursor.execute("INSERT INTO Claims (PlayerID, TeamID, PlayerName, Time, ClaimType, ClaimOrderPreference) "
                       "VALUES (?, ?, ?, ?, ?, ?)", claim_data)

        # Commit the changes
        conn.commit()
        conn.close()

        # Log the successful claim
        logger.info(f"Team {team_role} has successfully lodged a normal claim for Player with ID {playerid}")

    except Exception as e:
        logger.error(f"Error processing normal claim for Player with ID {playerid} by Team {team_role}: {e}")
        raise e


async def handle_quick_claim(player_row, team_role, playerid):
    try:
        logger.info(f"Initiating quick claim for Player with ID {playerid} by Team {team_role}")

        # Check if the team is the highest priority
        current_priority = get_team_priority(team_role)
        logger.info(f"Retrieved priority {current_priority} for Team {team_role}")
        if current_priority != 1:  # Assuming 1 is the highest priority
            raise ValueError("Only the team with the highest priority can make a quick claim.")

        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Update the player's status to "Claimed"
        cursor.execute("UPDATE Players SET Status = 'Claimed', Cleared = 'Y', Claimed = 'Y',"
                       " SuccessfulTeamID = ? WHERE PlayerID = ?",
                       (team_role, playerid))

        # Fetch the player's name for the announcement message
        cursor.execute("SELECT PlayerName FROM Players WHERE PlayerID = ?", (playerid,))
        PlayerName = cursor.fetchone()[0]

        # Add the successful quick claim to the Claims table
        claim_data = (playerid, TEAMS_DICT[team_role], PlayerName, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      'quick', 'Y')
        cursor.execute("INSERT INTO Claims (PlayerID, TeamID, PlayerName, Time, ClaimType, Successful) VALUES"
                       " (?, ?, ?, ?, ?, ?)",
                       claim_data)

        # Mark other claims for this player as unsuccessful in the Claims table
        cursor.execute("""
               UPDATE Claims 
               SET Successful = 'N', Unsuccessful = 'Y'
               WHERE PlayerID = ? AND TeamID != ?
           """, (playerid, TEAMS_DICT[team_role]))

        # Commit the changes
        conn.commit()
        conn.close()

        # Get the role ID for the team
        role_id = TEAMS_DICT[team_role]

        # Adjust the team's priority
        adjust_team_priority(team_role)
        logger.info(f"Adjusted priority for Team {team_role}")

        # Create and return the announcement message using the role mention
        announcement_message = f"{PlayerName} with ID {playerid} has been quick claimed by <@&{role_id}>!"

        return announcement_message

    except Exception as e:
        logger.error(f"Error processing quick claim for Player with ID {playerid} by Team {team_role}: {e}")
        raise e


async def handle_free_claim(player_row, team_role, playerid):
    try:
        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if the player's status is "Free Claim"
        cursor.execute("SELECT Status FROM Players WHERE PlayerID = ?", (playerid,))
        current_status = cursor.fetchone()[0]
        if current_status != "Free Claim":
            raise ValueError(f"{team_role} attempted to free claim Player with ID {playerid} however this player is not"
                             f" available for free claim.")

        # Update the player's status to "Claimed"
        cursor.execute("UPDATE Players SET Status = 'Claimed', Cleared = 'Y', Claimed = 'Y', SuccessfulTeamID = ? "
                       "WHERE PlayerID = ?",
                       (team_role, playerid))

        # Fetch the player's name for the announcement message
        cursor.execute("SELECT PlayerName FROM Players WHERE PlayerID = ?", (playerid,))
        PlayerName = cursor.fetchone()[0]

        # Add the claim to the Claims table
        claim_data = (playerid, TEAMS_DICT[team_role], PlayerName, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'free',
                      'free', 'Y')
        cursor.execute("INSERT INTO Claims (PlayerID, TeamID, PlayerName, Time, ClaimType, ClaimOrderPreference, "
                       "Successful) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       claim_data)

        # Commit the changes
        conn.commit()
        conn.close()

        # Get the role ID for the team
        role_id = TEAMS_DICT[team_role]

        # Create and return the announcement message using the role mention
        announcement_message = f"{PlayerName} with ID {playerid} has been free claimed by <@&{role_id}>!"

        # Send the announcement message to the central channel
        central_channel = bot.get_channel(CHANNELS_DICT["announcement_channel"])
        await central_channel.send(announcement_message)

        return announcement_message

    except Exception as e:
        logger.error(f"Error processing free claim for Player with ID {playerid} by Team {team_role}: {e}")
        raise e


async def process_clearing_claims(clearing_claims, clearing_players):
    try:
        logger.info("Starting the processing of clearing claims...")

        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        if not clearing_claims:
            logger.warning("No clearing claims to process. Exiting.")
            return

        # Check if there are players in the clearing_players list who are still available
        available_players = [player for _, player in clearing_players if player[5] == "Available"]

        if not available_players:
            logger.info("No more players to clear. Exiting process.")
            return

        logger.info(f"Clearing Claims before sorting: {clearing_claims}")

        sorted_claims = sorted(clearing_claims,
                               key=lambda x: (get_team_priority(x[2]), int(x[-3])))
        logger.info(f"Sorted clearing claims by priority: {sorted_claims}")

        top_claim = sorted_claims.pop(0)
        logger.info(f"Processing top claim for Player with ID {top_claim[0]} by team {str(top_claim[2])}.")

        playerid = top_claim[1]
        player_tuple = next((player_tuple for player_tuple in clearing_players if player_tuple[1][0] == playerid),
                            None)
        if player_tuple is None:
            logger.warning(f"Player with ID {playerid} not found in clearing_players list.")
            return
        elif player_tuple[1][5] != "Available":
            logger.warning(f"Player with ID {playerid} has status {player_tuple[1][5]}.")
            return

        idx, player = player_tuple
        clearing_players.remove(player_tuple)

        team_abbreviation = next((team for team, role_id in TEAMS_DICT.items() if role_id == str(top_claim[2])), None)
        if not team_abbreviation:
            logger.error(f"Couldn't find team abbreviation for Role ID {top_claim[2]}.")
            return

        # Update player's status to "Claimed" in the Players table
        cursor.execute("""
               UPDATE Players 
               SET Status = 'Claimed', Cleared = 'Y', Claimed = 'Y', SuccessfulTeamID = ? 
               WHERE PlayerID = ?
           """, (team_abbreviation, playerid))

        # Mark the successful claim in the Claims table
        cursor.execute("""
               UPDATE Claims 
               SET Successful = 'Y', Unsuccessful = 'N'
               WHERE PlayerID = ? AND TeamID = ?
           """, (playerid, top_claim[2]))

        # Mark other claims as unsuccessful in the Claims table
        cursor.execute("""
               UPDATE Claims 
               SET Successful = 'N', Unsuccessful = 'Y'
               WHERE PlayerID = ? AND TeamID != ?
           """, (playerid, top_claim[2]))

        conn.commit()

        announcement_channel = bot.get_channel(CHANNELS_DICT["announcement_channel"])
        await announcement_channel.send(
            f"{player[1]} with ID: {playerid} has been claimed by <@&{top_claim[2]}>!")
        logger.info(f"Processed claim for {player[1]} with ID {playerid} by team {str(top_claim[2])}")

        team_name = next((team for team, role_id in TEAMS_DICT.items() if role_id == str(top_claim[2])), None)
        if team_name:
            adjust_team_priority(team_name)
        else:
            logger.warning(f"Couldn't find team name for Role ID {top_claim[2]}. Skipping priority adjustment.")

        conn.close()
        logger.info("Finished processing clearing claims.")

        # Reinvoke the find_clearing_players task (wait 3 seconds to avoid rate limiting)
        async def delayed_start():
            await asyncio.sleep(3)
            if find_clearing_players.is_running():
                find_clearing_players.restart()
            else:
                find_clearing_players.start()

        asyncio.create_task(delayed_start())

    except Exception as e:
        logger.error(f"Error in process_clearing_claims: {e}")
        raise e


async def process_announcements():
    try:
        logger.info("Fetching players from the database...")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if there are players marked as "Available" in their status.
        cursor.execute("SELECT COUNT(*) FROM Players WHERE Status = 'Available'")
        available_count = cursor.fetchone()[0]
        if available_count > 0:
            logger.warning("Attempted to announce players while there are players with status 'Available'")
            conn.close()
            return

        cursor.execute("SELECT * FROM Players WHERE Announced = 'N' OR Announced = '1'")
        player_rows = cursor.fetchall()

        logger.info(f"Fetched {len(player_rows)} player rows from the database.")

        players_to_announce = []
        cells_to_update = []

        logger.info("Iterating over the rows to find players that need to be announced...")
        for row in player_rows:
            player_id = row[0]  # assuming PlayerID is the first column in your Players table
            announcement_message, clearing_time = send_announcement(row, player_id)
            if announcement_message and clearing_time:  # Check if they're not None
                players_to_announce.append(announcement_message)
                cells_to_update.append({"column": "TimeClearing", "value": clearing_time, "row_id": player_id})

        # Batch update all cells
        if cells_to_update:
            for cell in cells_to_update:
                cursor.execute(f"UPDATE Players SET {cell['column']} = ? WHERE PlayerID = ?",
                               (cell['value'], cell['row_id']))
            conn.commit()

        if players_to_announce:
            gm_role_mention = f"<@&{ROLES_DICT['DSFLGM']}>"
            earliest_clearing_time = min([cell['value'] for cell in cells_to_update if cell['column'] == "TimeClearing"])
            timestamp = datetime.strptime(earliest_clearing_time, '%Y-%m-%d %H:%M:%S').timestamp()
            combined_message = (f"{gm_role_mention}\nThe following waivers are now available to claim and clear on "
                                f"<t:{int(timestamp)}:F>:\n\n") + "\n".join(players_to_announce)
            logger.info("Sending announcement message...")
            announcement_channel = bot.get_channel(CHANNELS_DICT["announcement_channel"])
            await announcement_channel.send(combined_message)
            logger.info("Announcement message sent successfully!")
        else:
            logger.info("No players to be announced in this iteration.")

        conn.close()

    except Exception as e:
        logger.error(f"Error in process_announcements: {e}")
        raise e


@tasks.loop(minutes=10)
async def announcement_task():
    for _ in range(RETRY_COUNT):
        try:
            logger.info("Starting announcement_task loop...")
            await process_announcements()
            logger.info("Finished announcement_task loop.")
            break  # If successful, break out of the retry loop
        except (Timeout, RequestException) as e:
            logger.warning(f"Database connection error: {e}. Retrying in {RETRY_DELAY} seconds...")
            await asyncio.sleep(RETRY_DELAY)
        except Exception as e:
            logger.error(f"Unexpected error in announcement_task: {e}")
            break  # Exit the retry loop on unexpected errors


# Loop to check for cleared players
@tasks.loop(minutes=1)
async def find_clearing_players():
    for retry in range(RETRY_COUNT):
        try:
            logger.info("Starting find_clearing_players loop...")
            current_time = datetime.now()

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM Players")
            player_rows = cursor.fetchall()
            cursor.execute("SELECT * FROM Claims")
            claim_rows = cursor.fetchall()

            logger.info(f"Fetched {len(player_rows)} player rows and {len(claim_rows)} claim rows from the database.")

            clearing_players = []
            for idx, player in enumerate(player_rows):
                if player[8]:  # If the Claimed column is not empty
                    continue

                if player[9] is not None:  # Checking TimeClearing column
                    try:
                        player_time = datetime.strptime(player[9], '%Y-%m-%d %H:%M:%S')
                        if player_time <= current_time:
                            clearing_players.append((idx, player))
                    except ValueError:
                        logger.error(
                            f"Error parsing time for player with PlayerID {player[0]}. Value encountered: {player[9]}")
                else:
                    logger.warning(f"Clearing time is None for player with PlayerID {player[0]}. Skipping...")

            clearing_claims = [claim for claim in claim_rows if
                               claim[1] in [player[0] for _, player in clearing_players]]
            logger.info(f"Identified {len(clearing_claims)} clearing claims to process.")

            # Check for players without claims and set them to "Free Claim"
            for _, player in clearing_players:
                player_id = player[0]
                matching_claims = [claim for claim in claim_rows if claim[1] == player_id]

                if not matching_claims and player[5] != "Free Claim":
                    # Player has no claims, set to "Free Claim"
                    logger.info(
                        f"Attempting to set Player with ID {player_id} to Free Claim.")
                    cursor.execute("""
                           UPDATE Players 
                           SET Status = 'Free Claim' 
                           WHERE PlayerID = ?
                       """, (player_id,))
                    conn.commit()  # Commit the changes to the database.

                    announcement_channel = bot.get_channel(CHANNELS_DICT["announcement_channel"])
                    await announcement_channel.send(f"<@&{ROLES_DICT['DSFLGM']}> {player[1]} with ID {player_id}"
                                                    f" is now available for Free Claim!")
                    logger.info(f"Set Player with ID {player_id} as Free Claim")
                else:
                    logger.info(
                        f"Skipping Player with ID {player_id} for Free Claim. Claims found: {len(matching_claims)},"
                        f" Current Status: {player[5]}")

            if clearing_claims:
                await process_clearing_claims(clearing_claims, clearing_players)

            logger.info("Finished find_clearing_players loop.")
            conn.close()
            break
        except (Timeout, RequestException) as e:
            logger.warning(f"Database connection error on attempt {retry + 1}/{RETRY_COUNT}: {e}")
            if retry < RETRY_COUNT - 1:  # Check if this is the last retry
                logger.info(f"Retrying in {RETRY_DELAY} seconds...")
                await asyncio.sleep(RETRY_DELAY)
            else:
                logger.error("All retry attempts exhausted. Moving on.")
        except Exception as e:
            logger.error(f"Unexpected error in find_clearing_players: {e}")
            break  # Exit the retry loop on unexpected errors


@bot.event
async def on_ready():
    logger.info("Bot is ready. Starting tasks...")
    announcement_task.start()
    find_clearing_players.start()
    logger.info("Tasks started successfully.")


@bot.slash_command(name="input", description="Allows RMs to input a player into the system.")
@discord.option(name='name', description="The full name of the player.", required=True)
@discord.option(name='position', description="The position of the player.", required=True,
                choices=["QB","RB", "WR", "TE", "OL", "DE", "DT", "LB", "CB", "S", "K/P"])
@discord.option(name='PageUrl', description="The URL of the player's roster page.", required=True)
async def input_player(ctx, name: str, position: str, pageurl: str):
    try:
        # Logging the attempt to add a player
        logger.info(f"{ctx.author} is starting to add Player {name} ({position})")

        # Step 1: Check if user has the Rookie Mentor role based on Role ID.
        if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
            await ctx.respond("Sorry, you do not have permission to use this command. Only Rookie Mentors "
                              "can input players.")
            logger.warning(f"{ctx.author} tried to use /input command without proper permissions")
            return

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Generate player ID based on the next available ID in the database
        cursor.execute("SELECT MAX(PlayerID) FROM Players")
        result = cursor.fetchone()
        playerid = result[0] + 1 if result and result[0] else 1  # Start from 1 if no entries found

        # Step 2: Add player details to the database.
        player_data = (
            playerid,
            name,
            position,
            pageurl,
            datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            "Pending",
            "N"
        )
        cursor.execute("""
            INSERT INTO Players (PlayerID, PlayerName, Position, PageURL, TimeEntered, Status, Announced) 
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, player_data)
        conn.commit()
        conn.close()

        logger.info(f"Successfully added Player {name} ({position}) with ID {playerid} to the database")

        # Step 3: Send confirmation message.
        await ctx.respond(f"Player {name} ({position}) with ID {playerid} has been added successfully!")
        logger.info(f"Sent confirmation message for Player {name} ({position}) with ID {playerid}")

    except Exception as e:
        logger.error(f"Error in /input command: {e}")
        await ctx.respond(f"An error occurred: {e}")
        raise e


@bot.slash_command(name="claim", description="Allows GMs to claim a player.")
@discord.option(name='player_id', description="The ID number of the player you are claiming.", type=int)
@discord.option(name='type_of_claim', description="The type of claim..", type=str,
                choices=["Quick", "Normal", "Free"])
@discord.option(name='claim_order_pref', description="Preferred claim order ranking, if applicable.", type=int,
                required=False)
async def claim_player(ctx, player_id: int, type_of_claim: str, claim_order_pref: int = None):
    await ctx.defer()
    await ctx.respond(content="WaiverBot is attempting to process your claim.")

    try:
        logger.info(f"{ctx.author} is starting to claim Player with ID {player_id} using a {type_of_claim} claim")

        # Check for valid claim_order_pref
        if claim_order_pref:
            if claim_order_pref <= 0 or claim_order_pref > 68 or not isinstance(claim_order_pref, int):
                await ctx.respond("Invalid claim order preference. Preference must be a whole number between 1 and 68. "
                                  "Nice try though Hudz.")
                return

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if user has a team role.
        team_role = None
        for team, role_id in TEAMS_DICT.items():
            if role_id in [str(role.id) for role in ctx.author.roles]:
                team_role = team
                break

        if not team_role:
            await ctx.respond("Sorry, you do not have permission to claim a player. Ensure you have a team role.")
            logger.warning(f"{ctx.author} tried to use /claim command without a team role")
            return

        # Check if the team already has a claim lodged for the player
        cursor.execute("SELECT COUNT(*) FROM Claims WHERE PlayerID=? AND TeamID=?", (player_id, TEAMS_DICT[team_role]))
        existing_claim_count = cursor.fetchone()[0]

        if existing_claim_count > 0:
            await ctx.respond(
                f"You already have a claim for player with ID {player_id}. Use the /adjust_claims command to "
                f"modify your existing claims.")
            return

        # Check if the player is set to "Pending".
        cursor.execute("SELECT * FROM Players WHERE PlayerID=?", (player_id,))
        player_row = cursor.fetchone()

        # Check if the player has been announced
        if player_row and player_row[6] != 'Y':  # Checking if the 'Announced' column is not 'Y'
            await ctx.respond(f"{player_row[1]} with ID {player_id} hasn't been announced yet and cannot be claimed.")
            return

        # Update the check to consider only "Available" and "Free Claim" statuses
        if not player_row or player_row[5] not in ["Available", "Free Claim"]:
            await ctx.respond(f"{player_row[1]} ID {player_id} is not yet available for claim.")
            return

        # Convert the type_of_claim to lowercase for case-insensitive comparison
        type_of_claim = type_of_claim.lower()

        # Based on the type of claim, call the appropriate helper function
        if type_of_claim == "normal":
            # Fetch all claims by the team for uncleared players
            cursor.execute("""
                   SELECT ClaimOrderPreference 
                   FROM Claims 
                   INNER JOIN Players ON Claims.PlayerID = Players.PlayerID 
                   WHERE TeamID = ? AND (Players.Status = 'Available')
               """, (TEAMS_DICT[team_role],))
            existing_team_claims = [int(row[0]) for row in cursor.fetchall() if
                                    row[0] is not None and (isinstance(row[0], int) or row[0].isdigit())]

            if not claim_order_pref:
                # If there are no existing claims, set the claim_order_pref to 1.
                if not existing_team_claims:
                    claim_order_pref = 1
                else:
                    # Get the current highest preference and add 1 to it.
                    current_highest_pref = max(existing_team_claims, default=0)
                    claim_order_pref = current_highest_pref + 1
            else:
                # Check if the provided claim_order_pref is already in use for another uncleared player by the same team
                if claim_order_pref in existing_team_claims:
                    await ctx.respond(
                        f"The claim order preference {claim_order_pref} is already in use for another player by your "
                        f"team. Please choose a different preference.")
                    return

            await handle_normal_claim(player_row, team_role, player_id, claim_order_pref)

        elif type_of_claim == "quick":
            announcement_message = await handle_quick_claim(player_row, team_role, player_id)

            # Send the announcement message to the announcements channel
            announcement_channel = bot.get_channel(CHANNELS_DICT["announcement_channel"])
            await announcement_channel.send(announcement_message)

        elif type_of_claim == "free":
            try:
                await handle_free_claim(player_row, team_role, player_id)
            except ValueError as ve:
                await ctx.respond(str(ve))
                return
        else:
            await ctx.respond(f"Invalid claim type: {type_of_claim}. Please use Quick, Normal or Free")
            return

        # Send confirmation message.
        await ctx.respond(
            f"{player_row[1]} with ID {player_id} has had a {type_of_claim} claim lodged successfully by {team_role}!")
        logger.info(f"{ctx.author} ({team_role}) claimed Player with ID {player_id} using a {type_of_claim}")

    except Exception as e:
        logger.error(f"Error in /claim command: {e}")
        await ctx.respond(f"An error occurred: {e}")
        raise e


@bot.slash_command(name="prioritylist", description="Displays the current priority list.")
async def priority_list(ctx):
    try:
        logger.info(f"{ctx.author} is requesting the priority list")

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get the "Teams" data
        cursor.execute("SELECT Name, Priority FROM Teams")
        teams_data = cursor.fetchall()

        # Sort teams based on priority
        sorted_teams = sorted(teams_data, key=lambda x: x[1])

        # Create the response message
        response = "**Team Priority List:**\n\n"
        for idx, (team_name, priority) in enumerate(sorted_teams, start=1):
            response += f"{idx}. {team_name}\n"

        # Create an embedded response
        embed = Embed(description=response, color=0xF39C12)  # Orange Gold embed
        await ctx.respond(embed=embed)
        logger.info(f"Sent the priority list to {ctx.author}")

    except Exception as e:
        logger.error(f"Error in /prioritylist command: {e}")
        await ctx.respond(f"An error occurred: {e}")
        raise e


@bot.slash_command(name="currentteamclaims", description="Displays the current claims for a specified team.")
@discord.option(name='team_code', description="The three letter code of the team for which to show claims.", type=str,
                choices=["BBB", "DAL", "KCC", "LDN", "MIN", "NOR", "PDX", "TIJ"])
async def current_team_claims(ctx, team_code: str):
    try:
        # Convert the team_code to uppercase for case-insensitivity
        team_code = team_code.upper()
        logger.info(f"{ctx.author} is requesting the current claims for team {team_code}")

        user_roles = [role.id for role in ctx.author.roles]
        is_rookie_mentor = ROLES_DICT["Rookie Mentor"] in user_roles
        user_roles_str = [str(role) for role in user_roles]
        user_team_role = next((role for role in user_roles_str if role in TEAMS_DICT.values()), None)

        if not is_rookie_mentor and not user_team_role:
            await ctx.respond("You don't have permission to view team claims.")
            logger.warning(f"{ctx.author} tried to use /currentteamclaims command without permission")
            return

        if not is_rookie_mentor and user_team_role != TEAMS_DICT[team_code]:
            await ctx.respond("You can only view the claims for your own team. "
                              "https://cdn.discordapp.com/emojis/808265918073012256.gif?size=96&quality=lossless")
            logger.warning(f"{ctx.author} tried to view claims for a different team")
            return

        if team_code not in TEAMS_DICT:
            await ctx.respond("Invalid team code provided. Please check and try again.")
            logger.warning(f"{ctx.author} provided an invalid team code: {team_code}")
            return

        team_id = TEAMS_DICT[team_code]
        logger.info(f"Team ID for {team_code}: {team_id}")

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM Players WHERE (Cleared IS NULL OR Cleared = 0)"
                       " AND (Claimed IS NULL OR Claimed = 0)")
        uncleared_players = cursor.fetchall()
        logger.info(f"Uncleared players: {uncleared_players}")

        uncleared_playerids = [str(row[0]) for row in uncleared_players]
        query = (f"SELECT * FROM Claims WHERE TeamID = ? AND PlayerID IN ({','.join(['?'] * len(uncleared_playerids))})"
                 f" ORDER BY ClaimOrderPreference")
        team_claims = cursor.execute(query, (team_id, *uncleared_playerids)).fetchall()

        logger.info(f"Claims for team {team_code}: {team_claims}")

        response = f"**Current claims by {team_code} for uncleared players (sorted by claim order preference):**\n\n"
        for claim in team_claims:
            playerid = claim[1]
            player_data = next((row for row in uncleared_players if row[0] == playerid), None)
            if not player_data:
                logger.info(f"No uncleared player data found for player ID: {playerid}")
                continue
            name = player_data[1]
            position = player_data[2]
            claim_type = claim[5]
            preference_order = claim[6]
            response += f"{preference_order} - **{name}** - {position} - ID: {playerid} \n\n"
            logger.info(f"Response built for {name} (ID: {playerid}): {response}")

        if not team_claims:
            logger.info(f"No claims were found for team {team_code}. Hence, the empty response.")

        # Split the response into chunks and send each chunk as an embedded message
        response_chunks = split_string_into_chunks(response)
        for chunk in response_chunks:
            embed = Embed(description=chunk, color=0x1D8348)
            await ctx.respond(embed=embed)

        logger.info(f"Sent the current claims for team {team_code} to {ctx.author}")

    except Exception as e:
        logger.error(f"Error in /currentteamclaims command for team {team_code}: {e}")
        await ctx.respond(f"An error occurred: {e}")


@bot.slash_command(name="playerlist", description="Displays the list of currently available players and their status.")
async def player_list(ctx):
    try:
        logger.info(f"{ctx.author} is requesting the list of eligible players")

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get the players from the "Players" table in the SQLite3 database
        cursor.execute("SELECT * FROM Players WHERE Announced = 'Y' AND (Status = 'Available' OR Status = 'Free Claim')")
        eligible_players = cursor.fetchall()

        # Format the player details
        if eligible_players:
            response = "**Eligible Players:**\n\n"
            for player in eligible_players:
                playerid = player[0]
                name = player[1]
                position = player[2]
                pageurl = player[3]
                status = player[5]
                clearing_time = player[9]
                timestamp = datetime.strptime(clearing_time, '%Y-%m-%d %H:%M:%S').timestamp()
                response += (f"**{name}** - {position} - ID {playerid}\nRoster Page: {pageurl}\nStatus: {status}\n"
                             f"Clearing Time: <t:{int(timestamp)}:F>\n\n")
        else:
            response = "No eligible players currently."

        # Split the response into chunks and send each chunk as an embedded message
        chunks = split_string_into_chunks(response)
        for chunk in chunks:
            embed = Embed(description=chunk, color=0x2E86C1)  #Sky Blue color
            await ctx.respond(embed=embed)

        logger.info(f"Sent the list of eligible players to {ctx.author}")

    except Exception as e:
        logger.error(f"Error in /playerlist command: {e}")
        await ctx.respond(f"An error occurred: {e}")


@bot.slash_command(name="pendingplayers", description="Displays the list of pending players, who are not yet claimable.")
async def pending_players(ctx):
    try:
        logger.info(f"{ctx.author} is requesting the list of pending players")

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Get the players from the "Players" table in the SQLite3 database where they are marked as 'Pending'
        # and not announced
        cursor.execute("SELECT * FROM Players WHERE Announced = 'N' AND Status = 'Pending'")
        pending_players = cursor.fetchall()

        # Format the player details
        if pending_players:
            response = "**Pending Players:**\n\n"
            for player in pending_players:
                playerid = player[0]  # Player ID
                name = player[1]
                position = player[2]
                pageurl = player[3]
                status = player[5]
                response += f"ID {playerid} - **{name}** - {position}\nRoster Page: {pageurl}\nStatus: {status}\n\n"
        else:
            response = "No pending players currently."

        # Send the response as an embedded message
        embed = Embed(description=response, color=0xFF6347)  # Crazy Tomato Colour
        await ctx.respond(embed=embed)

        logger.info(f"Sent the list of pending players to {ctx.author}")

    except Exception as e:
        logger.error(f"Error in /pendingplayers command: {e}")
        await ctx.respond(f"An error occurred: {e}")


@bot.slash_command(name="teamclaimhistory", description="Displays the last 10 claims history for your team.")
async def team_claims_history(ctx):
    try:
        logger.info(f"{ctx.author} is requesting the claims history")

        user_roles = [role.id for role in ctx.author.roles]

        # Check if the user has a team role
        team_role_id_str = next((str(role) for role in user_roles if str(role) in TEAMS_DICT.values()), None)
        if not team_role_id_str:
            await ctx.respond("You don't have permission to view claim history. Ensure you have a team role.")
            logger.warning(f"{ctx.author} tried to use /teamclaimhistory command without a team role")
            return

        # Establish a connection to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Fetch the team claims from the database
        cursor.execute("SELECT * FROM Claims WHERE TeamID = ? ORDER BY Time DESC LIMIT 10", (team_role_id_str,))
        team_claims = cursor.fetchall()

        # Fetch the player data from the database
        cursor.execute("SELECT * FROM Players")
        player_rows = {row[0]: row for row in cursor.fetchall()}  # Dictionary with PlayerID as key for easy lookup

        # Retrieve the team name (three-letter code)
        team_name = next((key for key, value in TEAMS_DICT.items() if value == team_role_id_str), None)
        if not team_name:
            raise ValueError(f"No team name found for the team role ID: {team_role_id_str}")

        response = f"**Claim History for {team_name}:**\n\n"
        for claim in team_claims:
            playerid = claim[1]
            player_data = player_rows.get(playerid)
            if not player_data:
                continue
            name = player_data[1]
            position = player_data[2]
            claim_time = claim[4]
            timestamp = datetime.strptime(claim_time, '%Y-%m-%d %H:%M:%S').timestamp()
            claim_type = claim[5]
            preference_order = claim[6]
            successful = claim[7]
            response += (f"**{name}** - {position}\nClaim Time: <t:{int(timestamp)}:F>\nClaim Type: {claim_type}\n"
                         f"Preference Order: {preference_order}\nSuccessful: {successful}\n\n")

        embed = Embed(description=response, color=0x5B2C6F)  # Purple Embed
        await ctx.respond(embed=embed)
        logger.info(f"Sent the claims history to {ctx.author}")

    except Exception as e:
        logger.error(f"Error in /teamclaimhistory command: {e}")
        await ctx.respond(f"An error occurred: {e}")


@bot.slash_command(name="adjustclaims", description="Allows modification of player claims.")
@discord.option(name='playerid', description="The ID of the player for which to adjust the claim.", type=int)
@discord.option(name='action', description="The action to take.", type=str,
                choices=["adjust", "withdraw"])
@discord.option(name='new_priority', description="The new preference order number for the claim, if applicable.",
                type=int, required=False)
async def adjust_claims(ctx, playerid: int, action: str, new_priority: int = None):
    try:
        logger.info(f"{ctx.author} is trying to adjust the claims for Player ID {playerid}")

        # Check for valid new_priority
        if new_priority:
            if new_priority <= 0 or new_priority > 68 or not isinstance(new_priority, int):
                await ctx.respond("Invalid new priority. It must be a whole number between 1 and 68.")
                return

        # Determine the team based on user's role
        team_role = None
        for team, role_id in TEAMS_DICT.items():
            if str(role_id) in [str(role.id) for role in ctx.author.roles]:
                team_role = team
                break

        if not team_role:
            await ctx.respond("Sorry, you do not have permission to adjust claims. Ensure you have a team role.")
            logger.warning(f"{ctx.author} tried to use /adjustclaims command without a team role")
            return

        # Connect to the SQLite3 database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Fetch the players who are yet to clear
        cursor.execute("SELECT PlayerID FROM Players WHERE (Cleared IS NULL OR Cleared='') AND Announced='Y'")
        clearing_players = [row[0] for row in cursor.fetchall()]

        # Add logging to print the clearing_players list
        logger.info(f"Clearing players: {clearing_players}")

        if playerid not in clearing_players:
            await ctx.respond(f"Player with ID {playerid} has already cleared or doesn't exist.")
            return

        # Check if team has made a claim on that player
        cursor.execute("SELECT * FROM Claims WHERE PlayerID=? AND TeamID=?", (playerid, TEAMS_DICT[team_role]))
        claim_data = cursor.fetchone()
        original_priority = claim_data[6] if claim_data else None

        if original_priority is None:
            await ctx.respond(f"Your team does not have a claim for player with ID {playerid}.")
            return

        # Adjust the claim
        if action == "adjust" and new_priority:

            if new_priority > original_priority:
                # Increase priority
                cursor.execute(
                    "UPDATE Claims SET ClaimOrderPreference = ClaimOrderPreference - 1 WHERE TeamID = ? "
                    "AND ClaimOrderPreference BETWEEN ? AND ?",
                    (TEAMS_DICT[team_role], original_priority, new_priority))

            elif new_priority < original_priority:
                # Decrease priority
                cursor.execute(
                    "UPDATE Claims SET ClaimOrderPreference = ClaimOrderPreference + 1 WHERE TeamID = ? "
                    "AND ClaimOrderPreference BETWEEN ? AND ?",
                    (TEAMS_DICT[team_role], new_priority, original_priority))

            # Update the priority of the adjusted claim
            cursor.execute("UPDATE Claims SET ClaimOrderPreference = ? WHERE PlayerID = ? AND TeamID = ?",
                           (new_priority, playerid, TEAMS_DICT[team_role]))
            conn.commit()
            await ctx.respond(f"Claim priority for player with ID {playerid} has been adjusted to {new_priority}.")

        elif action == "withdraw":

            withdrawn_priority = original_priority

            # Adjust the priority of other claims

            cursor.execute(
                "UPDATE Claims SET ClaimOrderPreference = ClaimOrderPreference - 1 WHERE TeamID = ? "
                "AND ClaimOrderPreference > ?",
                (TEAMS_DICT[team_role], withdrawn_priority))

            # Delete the withdrawn claim

            cursor.execute("DELETE FROM Claims WHERE PlayerID = ? AND TeamID = ?", (playerid, TEAMS_DICT[team_role]))

            conn.commit()

            await ctx.respond(f"Withdrew the claim for player with ID {playerid}.")

        else:
            await ctx.respond(f"Invalid action or missing new priority.")

        logger.info(
            f"{ctx.author} ({team_role}) adjusted the claim for Player with ID {playerid} with action {action}")

    except Exception as e:
        logger.error(f"Error in /adjustclaims command: {e}")
        await ctx.respond(f"An error occurred: {e}")


# @bot.slash_command(name="setpriority", description="Set priority order for all teams.")
# @discord.option(name='priority_1', description="Team for priority 1", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_2', description="Team for priority 2", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_3', description="Team for priority 3", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_4', description="Team for priority 4", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_5', description="Team for priority 5", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_6', description="Team for priority 6", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_7', description="Team for priority 7", choices=list(TEAM_NAMES_DICT.values()))
# @discord.option(name='priority_8', description="Team for priority 8", choices=list(TEAM_NAMES_DICT.values()))
# async def set_all_priorities(ctx, priority_1, priority_2, priority_3, priority_4,
#                              priority_5, priority_6, priority_7, priority_8):
#     if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
#         await ctx.respond("Only Rookie Mentors can set priorities.")
#         return
#
#     # Combine all priorities into a list
#     priorities = [priority_1, priority_2, priority_3, priority_4, priority_5, priority_6, priority_7, priority_8]
#
#     # Check for duplicates
#     if len(set(priorities)) != len(priorities):
#         await ctx.respond("Each team must be unique. Please assign different teams to each priority.")
#         return
#
#     conn = sqlite3.connect(DB_PATH)
#     cursor = conn.cursor()
#
#     # Update priorities based on the list order provided
#     for index, team_name in enumerate(priorities, start=1):
#         team_key = next(key for key, value in TEAM_NAMES_DICT.items() if value == team_name)
#         cursor.execute("UPDATE Teams SET Priority=? WHERE RoleID=?", (index, TEAMS_DICT[team_key]))
#
#     conn.commit()
#     conn.close()
#
#     await ctx.respond("Team priorities have been successfully set based on your input.")
#     logger.info(f"{ctx.author} set team priorities.")

@bot.slash_command(name="setpriority", description="Set priority order for all teams based on input order.")
@discord.option(name='priorities', description="Comma-separated list of team abbreviations in priority order.")
async def set_all_priorities(ctx, priorities: str):
    if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
        await ctx.respond("Only Rookie Mentors can set priorities.")
        return

    priority_list = priorities.split(',')
    if len(priority_list) != len(TEAM_NAMES_DICT):
        await ctx.respond("Please specify the exact number of teams in correct priority order.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Update priorities based on the list order provided
    for index, team in enumerate(priority_list, start=1):
        if team.strip().upper() in TEAM_NAMES_DICT:
            cursor.execute("UPDATE Teams SET Priority=? WHERE RoleID=?", (index, TEAMS_DICT[team.strip().upper()]))
        else:
            conn.close()
            await ctx.respond(f"Invalid team abbreviation: {team}. Please check your input.")
            return

    conn.commit()
    conn.close()

    await ctx.respond("Team priorities have been successfully set based on your input.")
    logger.info(f"{ctx.author} set team priorities based on input order.")


@bot.slash_command(name="removeplayer", description="Remove a player from the system.")
@discord.option(name='player_id', description="The ID of the player to remove.", type=int)
async def remove_player(ctx, player_id: int):
    if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
        await ctx.respond("Only Rookie Mentors can remove players.")
        return

    # Prepare a confirmation message with buttons
    view = discord.ui.View()
    confirm_button = discord.ui.Button(style=discord.ButtonStyle.red, label="Confirm Removal")
    cancel_button = discord.ui.Button(style=discord.ButtonStyle.gray, label="Cancel")

    async def confirm_interaction(interaction):
        if interaction.user != ctx.author:
            await interaction.response.send_message("You do not have permission to confirm this action.", ephemeral=True)
            return

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM Players WHERE PlayerID=?", (player_id,))
        cursor.execute("DELETE FROM Claims WHERE PlayerID=?", (player_id,))
        conn.commit()
        conn.close()

        await interaction.response.edit_message(content=f"Player ID {player_id} has been removed.", view=None)

    async def cancel_interaction(interaction):
        if interaction.user != ctx.author:
            await interaction.response.send_message("You do not have permission to cancel this action.", ephemeral=True)
            return

        await interaction.response.edit_message(content="Player removal has been cancelled.", view=None)

    confirm_button.callback = confirm_interaction
    cancel_button.callback = cancel_interaction
    view.add_item(confirm_button)
    view.add_item(cancel_button)

    await ctx.respond(f"Are you sure you want to remove the player with ID {player_id}? This action cannot be undone.", view=view)


@bot.slash_command(name="pause_tasks", description="Pauses the bots scheduled tasks.")
async def pause_tasks(ctx):
    global is_announcements_paused, is_find_clearing_players_paused
    if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
        await ctx.respond("Only Rookie Mentors can pause tasks.")
        return

    # Check if tasks are already paused
    if is_announcements_paused and is_find_clearing_players_paused:
        await ctx.respond("All tasks are already paused.")
        return

    is_announcements_paused = True
    is_find_clearing_players_paused = True
    announcement_task.cancel()
    find_clearing_players.cancel()
    await ctx.respond("All scheduled tasks have been paused.")
    logger.info(f"{ctx.author} paused all tasks.")


@bot.slash_command(name="unpause_tasks", description="Unpauses the bots scheduled tasks.")
async def unpause_tasks(ctx):
    global is_announcements_paused, is_find_clearing_players_paused
    if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
        await ctx.respond("Only Rookie Mentors can unpause tasks.")
        return

    if not is_announcements_paused and not is_find_clearing_players_paused:
        await ctx.respond("All tasks are already running.")
        return

    is_announcements_paused = False
    is_find_clearing_players_paused = False
    announcement_task.start()
    find_clearing_players.start()
    await ctx.respond("All scheduled tasks have been unpaused.")
    logger.info(f"{ctx.author} unpaused all tasks.")


# @bot.slash_command(name="trade", description="Trade the priority of two teams based on team abbreviations.")
# @discord.option(
#     name='team1',
#     description="Abbreviation of the first team.",
#     choices=["BBB", "KCC", "NOR", "PDX", "LDN", "MIN", "TIJ", "DAL"]
# )
# @discord.option(
#     name='team2',
#     description="Abbreviation of the second team.",
#     choices=["BBB", "KCC", "NOR", "PDX", "LDN", "MIN", "TIJ", "DAL"]
# )
# async def trade_teams(ctx, team1: str, team2: str):
#     if ROLES_DICT["Rookie Mentor"] not in [role.id for role in ctx.author.roles]:
#         await ctx.respond("Only Rookie Mentors can swap team priorities.")
#         return
#
#     team1 = team1.strip().upper()
#     team2 = team2.strip().upper()
#
#     if team1 == team2:
#         await ctx.respond("You cannot select the same team for both options. Please select two different teams.")
#         return
#
#     conn = sqlite3.connect(DB_PATH)
#     cursor = conn.cursor()
#
#     try:
#         cursor.execute("SELECT Priority FROM Teams WHERE RoleID=?", (TEAMS_DICT[team1],))
#         team1_priority = cursor.fetchone()
#         cursor.execute("SELECT Priority FROM Teams WHERE RoleID=?", (TEAMS_DICT[team2],))
#         team2_priority = cursor.fetchone()
#
#         if team1_priority is None or team2_priority is None:
#             await ctx.respond("One or both of the specified teams do not exist in the database.")
#             return
#
#         # Swap priorities
#         cursor.execute("UPDATE Teams SET Priority=? WHERE RoleID=?", (team2_priority[0], TEAMS_DICT[team1]))
#         cursor.execute("UPDATE Teams SET Priority=? WHERE RoleID=?", (team1_priority[0], TEAMS_DICT[team2]))
#
#         conn.commit()
#         await ctx.respond(f"Successfully swapped the priority of {team1} and {team2}.")
#         logger.info(f"{ctx.author} swapped the priority of {team1} and {team2}.")
#     except Exception as e:
#         await ctx.respond(f"An error occurred: {e}")
#         logger.error(f"An error occurred while swapping teams: {e}")
#     finally:
#         conn.close()


bot.run(TOKEN)
