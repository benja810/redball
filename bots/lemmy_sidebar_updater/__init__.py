"""Lemmy Sidebar Updater
by Todd Roberts
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
from datetime import datetime
import json
from mako.lookup import TemplateLookup
import mako.exceptions
import os
import pyprowl
import re
import requests
import sys
import threading
import time
import traceback
import tzlocal

import redball
from redball import logger

from plemmy import LemmyHttp

import statsapi
from ..nba_game_threads import pynbaapi
from ..nhl_game_threads import pynhlapi
from ..nfl_game_threads import mynflapi

__version__ = "1.1.2"


def run(bot, settings):
    sidebar_updater_bot = LemmySidebarUpdaterBot(bot, settings)
    sidebar_updater_bot.run()


class LemmySidebarUpdaterBot:
    def __init__(self, bot, settings):
        self.bot = bot
        self.settings = settings
        self.BOT_PATH = os.path.dirname(os.path.realpath(__file__))
        self.BOT_TEMPLATE_PATH = []
        if self.settings.get("Bot", {}).get("TEMPLATE_PATH", "") != "":
            self.BOT_TEMPLATE_PATH.append(self.settings["Bot"]["TEMPLATE_PATH"])
        self.BOT_TEMPLATE_PATH.append(os.path.join(self.BOT_PATH, "templates"))
        self.lookup = TemplateLookup(directories=self.BOT_TEMPLATE_PATH)

    def run(self):
        self.log = logger.init_logger(
            logger_name="redball.bots." + threading.current_thread().name,
            log_to_console=self.settings.get("Logging", {}).get("LOG_TO_CONSOLE", True),
            log_to_file=self.settings.get("Logging", {}).get("LOG_TO_FILE", True),
            log_path=redball.LOG_PATH,
            log_file="{}.log".format(threading.current_thread().name),
            file_log_level=self.settings.get("Logging", {}).get("FILE_LOG_LEVEL"),
            log_retention=self.settings.get("Logging", {}).get("LOG_RETENTION", 7),
            console_log_level=self.settings.get("Logging", {}).get("CONSOLE_LOG_LEVEL"),
            clear_first=True,
            propagate=False,
        )
        self.log.debug(
            f"Sidebar Updater Bot v{__version__} received settings: {self.settings}. Template path: {self.BOT_TEMPLATE_PATH}"
        )

        # Initialize Lemmy API connection
        self.init_lemmy()

        # Initialize scheduler
        if "SCHEDULER" in vars(self.bot):
            # Scheduler already exists, maybe bot restarted
            sch_jobs = self.bot.SCHEDULER.get_jobs()
            self.log.warning(
                f"Scheduler already exists on bot startup with the following job(s): {sch_jobs}"
            )
            # Remove all jobs and shut down so we can start fresh
            for x in sch_jobs:
                x.remove()
            try:
                self.bot.SCHEDULER.shutdown()
            except SchedulerNotRunningError as e:
                self.log.debug(f"Could not shut down scheduler because: {e}")

        self.bot.SCHEDULER = BackgroundScheduler(
            timezone=tzlocal.get_localzone()
            if str(tzlocal.get_localzone()) != "local"
            else "America/New_York"
        )
        self.bot.SCHEDULER.start()

        self.bot.detailedState = {
            "summary": {
                "text": "Starting up, please wait 1 minute...",
                "html": "Starting up, please wait 1 minute...",
                "markdown": "Starting up, please wait 1 minute...",
            }
        }  # Initialize detailed state to a wait message

        # Start a scheduled task to update self.bot.detailedState every minute
        self.bot.SCHEDULER.add_job(
            self.bot_state,
            "interval",
            name=f"bot-{self.bot.id}-statusUpdateTask",
            id=f"bot-{self.bot.id}-statusUpdateTask",
            minutes=1,
            replace_existing=True,
        )

        if sport := self.settings.get("Bot", {}).get("SPORT"):
            self.log.debug(f"Bot set to sport: {sport}")
            self.sport = sport
        else:
            self.log.error(
                "No sport selected! Please select a sport in the Bot > SPORT setting. Aborting..."
            )
            self.bot.STOP = True
            self.shutdown()
            return

        if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
            update_interval = self.settings["Bot"].get("UPDATE_INTERVAL", 60)
            self.log.debug(
                f"Scheduling job to update lemmy every [{update_interval}] minute(s). Job name: [bot-{self.bot.id}-lemmy_update_task]..."
            )
            self.bot.SCHEDULER.add_job(
                func=self.update_lemmy,
                trigger="interval",
                name=f"bot-{self.bot.id}-lemmy_update_task",
                id=f"bot-{self.bot.id}-lemmy_update_task",
                minutes=update_interval,
                replace_existing=True,
            )
            self.log.debug("Running the job to get things started...")
            self.update_lemmy()
        else:
            self.log.warning("Lemmy is disabled. Nothing to do!")
            self.bot.STOP = True
            self.shutdown()
            return

        while redball.SIGNAL is None and not self.bot.STOP:
            self.sleep(60)
            self.log.debug(
                f"Scheduler jobs w/ next run times: {[(x.name, x.next_run_time) for x in self.bot.SCHEDULER.get_jobs()]}"
            )

        self.shutdown()

    def bot_state(self):
        community_url = self.community['actor_id']
        community_title = self.community['title']
        bot_status = {
            "lastUpdated": datetime.today().strftime("%m/%d/%Y %I:%M:%S %p"),
            "summary": {
                "text": f"Community: {self.community['name']}",
                "html": f'Community: <a href="{community_url}" target="_blank">{community_title}</a>',
                "markdown": f"Community: [{self.community['title']}]({community_url})",
            },
        }
        bot_status["summary"][
            "text"
        ] += f"\n\nSport: {self.sport}\n\nLemmy Enabled (Standings): {self.settings.get('Lemmy', {}).get('STANDINGS_ENABLED', False)}"
        bot_status["summary"][
            "html"
        ] += f"<br /><br />Sport: {self.sport}<br /><br />Lemmy Enabled (Standings): {self.settings.get('Lemmy', {}).get('STANDINGS_ENABLED', False)}"
        bot_status["summary"][
            "markdown"
        ] += f"\n\nSport: {self.sport}\n\nLemmy Enabled (Standings): {self.settings.get('Lemmy', {}).get('STANDINGS_ENABLED', False)}"
        self.log.debug(f"Bot Status: {bot_status}")
        self.bot.detailedState = bot_status

    def error_notification(self, action):
        # Generate and send notification to Prowl for errors
        prowl_key = self.settings.get("Prowl", {}).get("ERROR_API_KEY", "")
        prowl_priority = self.settings.get("Prowl", {}).get("ERROR_PRIORITY", "")
        newline = "\n"
        if prowl_key != "" and prowl_priority != "":
            self.notify_prowl(
                api_key=prowl_key,
                event=f"{self.bot.name} - {action}!",
                description=f"{action} for bot: [{self.bot.name}]!\n\n{newline.join(traceback.format_exception(*sys.exc_info()))}",
                priority=prowl_priority,
                app_name=f"redball - {self.bot.name}",
            )

    def get_nfl_token(self):
        self.log.debug("Retrieving fresh NFL API token...")
        url = "https://api.nfl.com/v1/reroute"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "x-domain-id": "100",
        }
        body = {
            "grant_type": "client_credentials",
        }

        try:
            r = requests.post(url, data=body, headers=headers)
            content = json.loads(r.content)
        except Exception as e:
            self.log.error(f"Caught exception requesting NFL API token: {e}")
            raise

        return content

    def init_lemmy(self):
        self.log.debug(f"Initiating Lemmy API with plaw")
        with redball.REDDIT_AUTH_LOCKS[str(self.bot.redditAuth)]:
            try:
                # Check for Lemmy
                instance_name = self.settings.get("Lemmy", {}).get("INSTANCE_NAME", "")
                username = self.settings.get("Lemmy", {}).get("USERNAME", "")
                password = self.settings.get("Lemmy", {}).get("PASSWORD", "")
                community = self.settings.get("Bot", {}).get("COMMUNITY_NAME")

                if "" in [instance_name, username, password, community]:
                    self.log.warn("Lemmy not fully configured")

                self.lemmy = LemmyHttp(instance_name)
                self.lemmy.login(username, password)

                self.log.info('community: {}'.format(community))

                communityRes = self.lemmy.get_community(name=community).json()

                self.community = communityRes["community_view"]["community"]

                is_mod = False
                for x in communityRes["moderators"]:
                    if x["moderator"]["name"] == username:
                        is_mod = True
                        break

                if is_mod == False:
                    self.log.error("User is not a moderator of this community")

            except Exception as e:
                self.log.error(
                    "Error encountered attempting to initialize Lemmy: {}".format(e)
                )
                self.error_notification("Error initializing Lemmy")
                raise

    def notify_prowl(
        self, api_key, event, description, priority=0, url=None, app_name="redball"
    ):
        # Send a notification to Prowl
        p = pyprowl.Prowl(apiKey=api_key, appName=app_name)

        self.log.debug(
            f"Sending notification to Prowl with API Key: {api_key}. Event: {event}, Description: {description}, Priority: {priority}, URL: {url}..."
        )
        try:
            p.notify(
                event=event,
                description=description,
                priority=priority,
                url=url,
            )
            self.log.info("Notification successfully sent to Prowl!")
            return True
        except Exception as e:
            self.log.error("Error sending notification to Prowl: {}".format(e))
            return False

    def render_template(self, template_file_name, **kwargs):
        self.log.debug(f"Rendering template [{template_file_name}]...")
        try:
            template = self.lookup.get_template(template_file_name)
            rendered_text = template.render(**kwargs)
            self.log.debug(f"Rendered template [{template_file_name}]: {rendered_text}")
            return rendered_text
        except Exception as e:  # TODO: more specific exception(s) to handle
            self.log.error(
                f"Error rendering template [{template_file_name}]: {e}{mako.exceptions.text_error_template().render()}"
            )
            self.error_notification(f"Error rendering template [{template_file_name}]")
            return ""

    def shutdown(self):
        if "SCHEDULER" in vars(self.bot):
            sch_jobs = self.bot.SCHEDULER.get_jobs()
            # Remove all jobs and shut down the scheduler
            for x in sch_jobs:
                self.log.debug(f"Removing scheduled job [{x.name}]")
                x.remove()
            try:
                self.log.debug("Shutting down scheduler...")
                self.bot.SCHEDULER.shutdown()
            except SchedulerNotRunningError as e:
                self.log.debug(f"Could not shut down scheduler because: {e}")
        self.bot.STOP = True
        self.bot.detailedState = {
            "summary": {
                "text": "The bot has been shut down.",
                "html": "The bot has been shut down.",
                "markdown": "The bot has been shut down.",
            }
        }
        self.log.info("Shutting down...")

    def sleep(self, t):
        # t = total number of seconds to sleep before returning
        i = 0
        while redball.SIGNAL is None and not self.bot.STOP and i < t:
            i += 1
            time.sleep(1)

    def update_lemmy_standings(
        self, my_team, standings, team_subs, all_teams, current_week=None
    ):
        standings_text = self.render_template(
            self.settings["Lemmy"]["STANDINGS_TEMPLATE"]
            if self.settings["Lemmy"].get("STANDINGS_TEMPLATE", "") != ""
            else f"{self.sport.lower()}_standings.mako",
            my_team=my_team,
            standings=standings,
            team_subs=team_subs,
            num_to_show=self.settings["Lemmy"].get("STANDINGS_NUM_TO_SHOW", 99),
            all_teams=all_teams,
            current_week=current_week,
            settings=self.settings["Lemmy"],
        )
        if standings_text == "":
            self.log.warning("Standings text is blank, skipping sidebar update/insert.")
            return
        full_sidebar_text = self.community["description"]
        regex = re.compile(
            self.settings["Lemmy"]["STANDINGS_REGEX"]
            if self.settings["Lemmy"].get("STANDINGS_REGEX", "") != ""
            else "\\[]\\(\\/redball\\/standings\\).*\\[]\\(\\/redball\\/standings\\)",
            flags=re.DOTALL,
        )
        if regex.search(full_sidebar_text):
            new_sidebar_text = re.sub(regex, standings_text, full_sidebar_text)
        else:
            self.log.info(
                "Regex didn't match anything in the sidebar, so appending to the end."
            )
            new_sidebar_text = f"{full_sidebar_text}\n\n{standings_text}"
        try:
            community_id = int(self.community["id"])
            self.log.info('community_id: {}'.format(community_id))
            editRes = self.lemmy.edit_community(community_id, description=new_sidebar_text)
            
            if (editRes.status_code != 200):
                self.log.error("Failed to edit community sidebar {}".format(editRes.text))

            self.log.debug(editRes.json())

            self.log.debug("Finished updating lemmy.")
        except Exception as e:
            self.log.error(f"Error updating lemmy sidebar wiki: {e}")

    def update_lemmy(self):
        if self.sport == "MLB":
            if self.settings.get("MLB", {}).get("TEAM", "") == "":
                self.log.critical("No team selected! Set MLB > TEAM in Bot Config.")
                self.bot.STOP = True
                return
            all_teams = statsapi.get(
                "teams", {"sportIds": 1, "hydrate": "league,division"}
            ).get("teams", [])
            my_team = next(
                (
                    x
                    for x in all_teams
                    if x["id"] == int(self.settings["MLB"]["TEAM"].split("|")[1])
                ),
                None,
            )
            if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
                standings = statsapi.standings_data()
            else:
                standings = None
            team_subs = self.mlb_team_subs
            current_week = None
        elif self.sport == "NBA":
            if self.settings.get("NBA", {}).get("TEAM", "") == "":
                self.log.critical("No team selected! Set NBA > TEAM in Bot Config.")
                self.bot.STOP = True
                return
            nba = pynbaapi.nba.NBA(
                f"LemmySidebarUpdater/{__version__} (platform; redball/{redball.__version__})"
            )
            season = (
                datetime.today().strftime("%Y")
                if int(datetime.today().strftime("%m")) >= 8
                else str(int(datetime.today().strftime("%Y")) - 1)
            )
            all_teams = nba.all_teams(season)
            my_team = nba.team(int(self.settings["NBA"]["TEAM"].split("|")[1]))
            if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
                standings = nba.standings(season=season)
            else:
                standings = None
            team_subs = self.nba_team_subs
            current_week = None
        elif self.sport == "NFL":
            if self.settings.get("NFL", {}).get("TEAM", "") == "":
                self.log.critical("No team selected! Set NFL > TEAM in Bot Config.")
                self.bot.STOP = True
                return
            nfl = mynflapi.APISession(self.get_nfl_token())
            current_week = nfl.weekByDate(datetime.now().strftime("%Y-%m-%d"))
            all_teams = nfl.teams(
                current_week.get("season", datetime.now().strftime("%Y"))
            ).get("teams", [])
            my_team = next(
                (
                    x
                    for x in all_teams
                    if x["abbreviation"] == self.settings["NFL"]["TEAM"].split("|")[1]
                ),
                None,
            )
            if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
                standings = (
                    nfl.standings(
                        season=current_week["season"],
                        seasonType="REG",
                        week=current_week["week"]
                        if current_week["seasonType"] == "REG"
                        else 18
                        if current_week["seasonType"] == "POST"
                        else 1,
                    )
                    .get("weeks", [{}])[0]
                    .get("standings", [])
                )
            else:
                standings = None
            team_subs = self.nfl_team_subs
        elif self.sport == "NHL":
            if self.settings.get("NHL", {}).get("TEAM", "") == "":
                self.log.critical("No team selected! Set NHL > TEAM in Bot Config.")
                self.bot.STOP = True
                return
            nhl = pynhlapi.API()
            all_teams = nhl.teams()
            my_team = next(
                (
                    x
                    for x in all_teams
                    if x["id"] == int(self.settings["NHL"]["TEAM"].split("|")[1])
                ),
                None,
            )
            if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
                standings = nhl.standings()
            else:
                standings = None
            team_subs = self.nhl_team_subs
            current_week = None

        self.log.debug(f"{self.sport=}")
        self.log.debug(f"{my_team=}")
        self.log.debug(f"{all_teams=}")
        self.log.debug(f"{standings=}")
        self.log.debug(f"{team_subs=}")
        self.log.debug(f"{current_week=}")

        if self.settings.get("Lemmy", {}).get("STANDINGS_ENABLED"):
            self.log.debug("Updating Lemmy...")
            self.update_lemmy_standings(
                my_team,
                standings,
                team_subs,
                all_teams,
                current_week,
            )

    mlb_team_subs = {
        142: "/c/minnesotatwins@fanaticus.social",
        145: "/c/whitesox@fanaticus.social",
        116: "/c/motorcitykitties@fanaticus.social",
        118: "/c/kcroyals@fanaticus.social",
        114: "/c/clevelandguardians@fanaticus.social",
        140: "/c/texasrangers@fanaticus.social",
        117: "/c/astros@fanaticus.social",
        133: "/c/oaklandathletics@fanaticus.social",
        108: "/c/angelsbaseball@fanaticus.social",
        136: "/c/mariners@fanaticus.social",
        111: "/c/redsox@fanaticus.social",
        147: "/c/nyyankees@fanaticus.social",
        141: "/c/torontobluejays@fanaticus.social",
        139: "/c/tampabayrays@fanaticus.social",
        110: "/c/orioles@fanaticus.social",
        138: "/c/cardinals@fanaticus.social",
        113: "/c/reds@fanaticus.social",
        134: "/c/buccos@fanaticus.social",
        112: "/c/chicubs@fanaticus.social",
        158: "/c/brewers@fanaticus.social",
        137: "/c/sfgiants@fanaticus.social",
        109: "/c/azdiamondbacks@fanaticus.social",
        115: "/c/coloradorockies@fanaticus.social",
        119: "/c/dodgers@fanaticus.social",
        135: "/c/padres@fanaticus.social",
        143: "/c/phillies@fanaticus.social",
        121: "/c/newyorkmets@fanaticus.social",
        146: "/c/miamimarlins@fanaticus.social",
        120: "/c/nationals@fanaticus.social",
        144: "/c/braves@fanaticus.social",
        0: "/c/baseball@fanaticus.social",
    }

    nba_team_subs = {
        1610612737: "/r/atlantahawks",
        1610612751: "/r/gonets",
        1610612738: "/r/bostonceltics",
        1610612766: "/r/charlottehornets",
        1610612741: "/r/chicagobulls",
        1610612739: "/r/clevelandcavs",
        1610612742: "/r/mavericks",
        1610612743: "/r/denvernuggets",
        1610612765: "/r/detroitpistons",
        1610612744: "/r/warriors",
        1610612745: "/r/rockets",
        1610612754: "/r/pacers",
        1610612746: "/r/laclippers",
        1610612747: "/r/lakers",
        1610612763: "/r/memphisgrizzlies",
        1610612748: "/r/heat",
        1610612749: "/r/mkebucks",
        1610612750: "/r/timberwolves",
        1610612740: "/r/nolapelicans",
        1610612752: "/r/nyknicks",
        1610612760: "/r/thunder",
        1610612753: "/r/orlandomagic",
        1610612755: "/r/sixers",
        1610612756: "/r/suns",
        1610612757: "/r/ripcity",
        1610612758: "/r/kings",
        1610612759: "/r/nbaspurs",
        1610612761: "/r/torontoraptors",
        1610612762: "/r/utahjazz",
        1610612764: "/r/washingtonwizards",
    }

    nfl_team_subs = {
        "ARI": "/r/AZCardinals",
        "ATL": "/r/falcons",
        "BAL": "/r/ravens",
        "BUF": "/r/buffalobills",
        "CAR": "/r/panthers",
        "CHI": "/r/CHIBears",
        "CIN": "/r/bengals",
        "CLE": "/r/Browns",
        "DAL": "/r/cowboys",
        "DEN": "/r/DenverBroncos",
        "DET": "/r/detroitlions",
        "GB": "/r/GreenBayPackers",
        "HOU": "/r/Texans",
        "IND": "/r/Colts",
        "JAX": "/r/Jaguars",
        "KC": "/r/KansasCityChiefs",
        "LA": "/r/LosAngelesRams",
        "LAC": "/r/Chargers",
        "LV": "/r/raiders",
        "MIA": "/r/miamidolphins",
        "MIN": "/r/minnesotavikings",
        "NE": "/r/Patriots",
        "NO": "/r/Saints",
        "NYG": "/r/NYGiants",
        "NYJ": "/r/nyjets",
        "PHI": "/r/eagles",
        "PIT": "/r/steelers",
        "SEA": "/r/Seahawks",
        "SF": "/r/49ers",
        "TB": "/r/buccaneers",
        "TEN": "/r/Tennesseetitans",
        "WAS": "/r/Commanders",
        0: "/r/NFL",
        "nfl": "/r/NFL",
        "NFL": "/r/NFL",
    }

    nhl_team_subs = {
        1: "/r/devils",
        2: "/r/newyorkislanders",
        3: "/r/rangers",
        4: "/r/flyers",
        5: "/r/penguins",
        6: "/r/bostonbruins",
        7: "/r/sabres",
        8: "/r/habs",
        9: "/r/ottawasenators",
        10: "/r/leafs",
        12: "/r/canes",
        13: "/r/floridapanthers",
        14: "/r/tampabaylightning",
        15: "/r/caps",
        16: "/r/hawks",
        17: "/r/detroitredwings",
        18: "/r/predators",
        19: "/r/stlouisblues",
        20: "/r/calgaryflames",
        21: "/r/coloradoavalanche",
        22: "/r/edmontonoilers",
        23: "/r/canucks",
        24: "/r/anaheimducks",
        25: "/r/dallasstars",
        26: "/r/losangeleskings",
        28: "/r/sanjosesharks",
        29: "/r/bluejackets",
        30: "/r/wildhockey",
        52: "/r/winnipegjets",
        53: "/r/coyotes",
        54: "/r/goldenknights",
        55: "/r/seattlekraken",
    }
