# Based on the GPS/GPSD plugin from:
# - https://github.com/evilsocket
# - https://github.com/kellertk/pwnagotchi-plugin-gpsd
# - https://github.com/nothingbutlucas/pwnagotchi-plugin-gpsd
# - https://gpsd.gitlab.io/gpsd/index.html
#
# Install :
# - Install and configure gpsd
# - copy this plugin to custom plugin
#
# Config.toml:
# main.plugins.gpsd-ng.enabled = true

# Options with default settings.
# Don't add if you don't need customisation
# main.plugins.gpsd-ng.gpsdhost = "127.0.0.1"
# main.plugins.gpsd-ng.gpsdport = 2947
# main.plugins.gpsd-ng.main_device = "/dev/ttyS0" # default None
# main.plugins.gpsd-ng.use_open_elevation = true
# main.plugins.gpsd-ng.save_elevations = true
# main.plugins.gpsd-ng.view_mode = "compact" # "compact", "full", "status", "none"
# main.plugins.gpsd-ng.fields = "info,speed,altitude" # list or string of fields to display
# main.plugins.gpsd-ng.units = "metric" # "metric", "imperial"
# main.plugins.gpsd-ng.display_precision = 6 # display precision for latitude and longitude
# main.plugins.gpsd-ng.position = "127,64"
# main.plugins.gpsd-ng.show_faces = true # if false, doesn't show face. Ex if you use PNG faces
# main.plugins.gpsd-ng.lost_face_1 = "(O_o )"
# main.plugins.gpsd-ng.lost_face_2 = "( o_O)"
# main.plugins.gpsd-ng.face_1 = "(•_• )"
# main.plugins.gpsd-ng.face_2 = "( •_•)"

import base64
import io
import threading
import json
import logging
import re
import os
import subprocess
from glob import glob
from dataclasses import dataclass, field
from typing import Self, Any
import time
from copy import deepcopy
import math
from datetime import datetime, UTC
import gps
import json
import geopy.distance
import geopy.units
import requests
from flask import render_template_string, render_template

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK
from pwnagotchi.utils import StatusFile


@dataclass(slots=True)
class Position:
    """
    Keeps data from GPS device
    """

    DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
    FIXES = {0: "No data", 1: "No fix", 2: "2D fix", 3: "3D fix"}

    latitude: float = field(default=float("NaN"))
    longitude: float = field(default=float("NaN"))
    altitude: float = field(default=float("NaN"))
    speed: float = field(default=float("NaN"))
    last_update: datetime | None = None
    last_fix: datetime | None = None
    mode: int = 0
    satellites: list = field(default_factory=list)
    device: str = field(default="missing", init=True)
    accuracy: float = field(default=float("NaN"))

    @property
    def seen_satellites(self) -> int:
        return len(self.satellites)

    @property
    def used_satellites(self) -> int:
        return len([s for s in self.satellites if s.used])

    @property
    def fix(self) -> str:
        return self.FIXES.get(self.mode, "Mode error")

    @property
    def last_update_ago(self) -> int | None:
        if self.last_update:
            return round((datetime.now(tz=UTC) - self.last_update).total_seconds())
        return None

    @property
    def last_fix_ago(self) -> int | None:
        if self.last_fix:
            return round((datetime.now(tz=UTC) - self.last_fix).total_seconds())
        return None

    def __lt__(self, other: Self) -> bool:
        if self.last_fix and other.last_fix:
            return (self.mode, -self.last_fix.timestamp()) < (
                other.mode,
                -other.last_fix.timestamp(),
            )
        return False

    # ---------- UPDATES ----------
    def set_attr(self, attr: str, value: Any, valid: int, flag: int) -> None:
        """
        Set an attribute only if valid contains the related flag
        """
        if flag & valid:
            setattr(self, attr, value)
            self.last_update = datetime.now(tz=UTC)  # Don't use fix.time cause it's not reliable

    def update_fix(self, fix: gps.gpsfix, valid: int) -> None:
        self.set_attr("latitude", fix.latitude, valid, gps.LATLON_SET)
        self.set_attr("longitude", fix.longitude, valid, gps.LATLON_SET)
        self.set_attr("speed", fix.speed, valid, gps.SPEED_SET)
        self.set_attr("mode", fix.mode, valid, gps.MODE_SET)
        if gps.MODE_SET & valid and fix.mode >= 2:
            self.last_fix = datetime.now(tz=UTC)  # Don't use fix.time cause it's not reliable
        self.accuracy = 50

    def update_satellites(self, satellites: list[gps.gpsdata.satellite], valid: int) -> None:
        self.set_attr("satellites", satellites, valid, gps.SATELLITE_SET)

    def update_altitude(self, altitude: int) -> None:
        self.altitude = altitude

    # ---------- VALIDATION AND TIME ----------
    def is_valid(self) -> bool:
        return gps.isfinite(self.latitude) and gps.isfinite(self.longitude) and self.mode >= 2

    def is_old(self, date: datetime | None, max_seconds: int) -> bool | None:
        if not date:
            return None
        return (datetime.now(tz=UTC) - date).total_seconds() > max_seconds

    def is_update_old(self, max_seconds: int) -> bool | None:
        return self.is_old(self.last_update, max_seconds)

    def is_fix_old(self, max_seconds: int) -> bool | None:
        return self.is_old(self.last_fix, max_seconds)

    def is_fixed(self) -> bool:
        return self.mode >= 2

    # ---------- JSON DUMP ----------
    def to_dict(self) -> dict[str, int | float | datetime | str | None]:
        if self.last_fix:
            last_fix = self.last_fix.strftime(self.DATE_FORMAT)
        else:
            last_fix = None
        return dict(
            Latitude=self.latitude,
            Longitude=self.longitude,
            Altitude=self.altitude,
            Speed=self.speed * gps.KNOTS_TO_MPS,
            Date=last_fix,
            Updated=last_fix,  # Wigle plugin
            Mode=self.mode,
            Fix=self.fix,
            Sats=self.seen_satellites,
            Sats_used=self.used_satellites,
            Device=self.device,
            Accuracy=self.accuracy,
        )

    # ---------- FORMAT ----------
    def format_info(self) -> str:
        device = re.search(r"(^tcp|^udp|tty.*)", self.device, re.IGNORECASE)
        dev = f"{device[0]}:" if device else ""
        return f"{dev}{self.fix} ({self.used_satellites}/{self.seen_satellites} Sats)"

    def format_lat_long(self, display_precision: int = 9) -> tuple[str, str]:
        if not (gps.isfinite(self.latitude) and gps.isfinite(self.longitude)):
            return ("-", "-")
        if self.latitude < 0:
            lat = f"{-self.latitude:4.{display_precision}f}S"
        else:
            lat = f"{self.latitude:4.{display_precision}f}N"
        if self.longitude < 0:
            long = f"{-self.longitude:4.{display_precision}f}W"
        else:
            long = f"{self.longitude:4.{display_precision}f}E"
        return lat, long

    def format_altitude(self, units: str) -> str:
        if not gps.isfinite(self.altitude):
            return "_"
        match units:
            case "imperial":
                return f"{round(geopy.units.feet(meters=self.altitude))}ft"
            case "metric":
                return f"{round(self.altitude)}m"
        return "error"

    def format_speed(self, units: str) -> str:
        if not gps.isfinite(self.speed):
            return "_"
        match units:
            case "imperial":
                return f"{round(self.speed * 1.68781)}ft/s"
            case "metric":
                return f"{round(self.speed * gps.KNOTS_TO_MPS)}m/s"
        return "error"

    def format(self, units: str, display_precision: int) -> tuple[str, str, str, str, str]:
        info = self.format_info()
        lat, long = self.format_lat_long(display_precision)
        alt = self.format_altitude(units)
        spd = self.format_speed(units)
        return info, lat, long, alt, spd

    def generate_polar_plot(self) -> str | None:
        """
        Return a polar image (base64) of seen satellites.
        Thanks to https://github.com/rai68/gpsd-easy/blob/main/gpsdeasy.py
        """
        try:
            from matplotlib.pyplot import rc, grid, figure, rcParams, savefig, close
        except ImportError:
            logging.error(f"[GPSD-ng] Error while importing matplotlib for generate_polar_plot()")
            return None

        try:
            rc("grid", color="#316931", linewidth=1, linestyle="-")
            rc("xtick", labelsize=10)
            rc("ytick", labelsize=10)

            # force square figure and square axes looks better for polar, IMO
            width, height = rcParams["figure.figsize"]
            size = min(width, height)
            # make a square figure
            fig = figure(figsize=(size, size))
            fig.patch.set_alpha(0)

            ax = fig.add_axes((0.1, 0.1, 0.8, 0.8), polar=True, facecolor="#d5de9c")
            ax.patch.set_alpha(1)
            ax.set_theta_zero_location("N")
            ax.set_theta_direction(-1)
            for sat in self.satellites:
                fc = "green" if sat.used else "red"
                ax.annotate(
                    str(sat.PRN),
                    xy=(math.radians(sat.azimuth), 90 - sat.elevation),  # theta, radius
                    bbox=dict(boxstyle="round", fc=fc, alpha=0.4),
                    horizontalalignment="center",
                    verticalalignment="center",
                )

            ax.set_yticks(range(0, 90 + 10, 15))  # Define the yticks
            ax.set_yticklabels(["90", "", "60", "", "30", "", "0"])
            grid(True)

            image = io.BytesIO()
            savefig(image, format="png")
            close(fig)
            return base64.b64encode(image.getvalue()).decode("utf-8")
        except Exception as e:
            logging.error(e)
            return ""


@dataclass(slots=True)
class GPSD(threading.Thread):
    gpsdhost: str | None = None
    gpsdport: int | None = None
    sleep_time: int = 1
    fix_timeout: int = 120
    update_timeout: int = 120
    session: gps.gps = None
    positions: dict = field(default_factory=dict)  # Device:Position dictionnary
    main_device: str | None = None
    last_position: Position | None = None
    elevation_data: dict = field(default_factory=dict)
    last_clean: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    elevation_report: StatusFile | None = None
    last_elevation: datetime = field(default_factory=lambda: datetime(2025, 1, 1, 0, 0, tzinfo=UTC))
    last_hook: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = True

    def __post_init__(self) -> None:
        super(GPSD, self).__init__()

    def __hash__(self):
        return super(GPSD, self).__hash__()

    # ---------- CONFIGURE AND CONNECTION ----------
    def configure(
        self,
        *,
        gpsdhost: str,
        gpsdport: int,
        fix_timeout: int,
        update_timeout: int,
        main_device: str,
        cache_file: str,
        save_elevations: bool,
    ) -> None:
        self.gpsdhost = gpsdhost
        self.gpsdport = gpsdport
        self.fix_timeout = fix_timeout
        self.update_timeout = update_timeout
        self.main_device = main_device
        if save_elevations:
            self.elevation_report = StatusFile(cache_file, data_format="json")
            self.elevation_data = self.elevation_report.data_field_or("elevations", default=dict())
        logging.info(f"[GPSD-ng] {len(self.elevation_data)} locations already in cache")

    def is_configured(self) -> bool:
        return self.gpsdhost != None and self.gpsdport != None

    def connect(self) -> None:
        with self.lock:
            logging.info(f"[GPSD-ng] Trying to connect to {self.gpsdhost}:{self.gpsdport}")
            try:
                self.session = gps.gps(
                    host=self.gpsdhost,
                    port=self.gpsdport,
                    mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE,
                )
                logging.info(f"[GPSD-ng] Connected to {self.gpsdhost}:{self.gpsdport}")
                self.sleep_time = 1
            except Exception as e:
                logging.error(f"[GPSD-ng] Error while connecting to GPSD: {e}")
                self.session = None
                logging.info(f"[GPS-ng] Going to sleep for {self.sleep_time}s")
                self.sleep_time = min(self.sleep_time * 2, 30)
                begin = datetime.now(tz=UTC)
                while (
                    self.running
                    and (datetime.now(tz=UTC) - begin).total_seconds() < self.sleep_time
                ):
                    time.sleep(1)

    def is_connected(self):
        return self.session != None

    # ---------- RELOAD/ RESTART GPSD SERVER ----------
    def reload_or_restart_gpsd(self):
        try:
            subprocess.run(
                ["killall", "-SIGHUP", "gpsd"],
                check=True,
                timeout=5,
            )
            time.sleep(2)
            logging.info(f"[GPSD-ng] GPSD reloaded")
            return
        except Exception as exp:
            logging.error(f"[GPSD-ng] Error while reloading gpsd: {exp}")

        try:
            subprocess.run(
                ["systemctl", "restart", "gpsd"],
                check=True,
                timeout=20,
            )
            time.sleep(2)
            logging.info(f"[GPSD-ng] GPSD restarted")
        except Exception as exp:
            logging.error(f"[GPSD-ng] Error while restarting gpsd: {exp}")

    # ---------- UPDATE AND CLEAN ----------
    def update(self) -> None:
        with self.lock:
            if not ((gps.ONLINE_SET & self.session.valid) and (device := self.session.device)):
                return
            if not device in self.positions:
                self.positions[device] = Position(device=device)
            self.positions[device].update_fix(self.session.fix, self.session.valid)
            if gps.ALTITUDE_SET & self.session.valid:  # cache altitude
                self.positions[device].update_altitude(self.session.fix.altMSL)
                self.cache_elevation(
                    self.session.fix.latitude,
                    self.session.fix.longitude,
                    self.session.fix.altMSL,
                )
            else:  # retreive altitude
                altitude = self.get_elevation(self.session.fix.latitude, self.session.fix.longitude)
                self.positions[device].update_altitude(altitude)
            self.positions[device].update_satellites(self.session.satellites, self.session.valid)
            # Soft reset session after reading
            self.session.valid = 0
            self.session.device = None
            self.session.fix = gps.gpsfix()
            self.session.satellites = []

    def clean(self) -> None:
        if not self.update_timeout:
            return
        if (datetime.now(tz=UTC) - self.last_clean).total_seconds() < 10:
            return
        self.last_clean = datetime.now(tz=UTC)
        with self.lock:
            for device in list(self.positions.keys()):
                if self.positions[device].is_update_old(self.update_timeout):
                    del self.positions[device]
                    logging.info(f"[GPSD-ng] Cleaning {device}")

    # ---------- MAIN LOOP ----------
    def plugin_hook(self) -> None:
        if (datetime.now(tz=UTC) - self.last_hook).total_seconds() < 10:
            return
        self.last_hook = datetime.now(tz=UTC)
        if coords := self.get_position():
            plugins.on("position_available", coords.to_dict())
        else:
            plugins.on("position_lost")

    def loop(self) -> None:
        logging.info(f"[GPSD-ng] Starting loop")
        while self.running:
            self.clean()
            if not self.session:
                self.connect()
            elif self.session.waiting(timeout=2) and self.session.read() == 0:
                self.update()
            else:
                logging.debug(
                    "[GPSD-ng] Closing connection to GPSD: {self.gpsdhost}:{self.gpsdport}"
                )
                self.session.close()
                self.session = None
            self.plugin_hook()

    def run(self) -> None:
        if not self.is_configured():
            logging.critical(f"[GPSD-ng] GPSD thread not configured.")
            return
        self.reload_or_restart_gpsd()
        try:
            self.loop()
        except Exception as exp:
            logging.critical(f"[GPSD-ng] Critical error during loop: {exp}")

    def join(self, timeout=None) -> None:
        self.running = False
        try:
            super(GPSD, self).join(timeout)
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on join(): {e}")

    # ---------- POSITION ----------
    def get_position_device(self) -> str | None:
        if not self.is_configured():
            return None
        with self.lock:
            if self.main_device:
                try:
                    if self.positions[self.main_device].is_valid():
                        return self.main_device
                except KeyError:
                    pass

            # Fallback
            try:
                # Filter devices without coords and sort by best positionning/most recent
                dev_pos = list(filter(lambda x: x[1].is_valid(), self.positions.items()))
                dev_pos = sorted(dev_pos, key=lambda x: x[1], reverse=True)
                return dev_pos[0][0]  # Get first and best element
            except IndexError:
                logging.debug(f"[GPSD-ng] No valid position")
            return None

    def get_position(self) -> Position | None:
        try:
            if device := self.get_position_device():
                self.last_position = self.positions[device]
                return self.positions[device]
        except KeyError:
            pass
        if (
            self.fix_timeout
            and self.last_position
            and self.last_position.is_fix_old(self.fix_timeout)
        ):
            self.last_position = None
        return self.last_position

    # ---------- OPEN ELEVATION CACHE ----------
    @staticmethod
    def round_position(latitude: float, longitude: float) -> tuple[float, float]:
        return (round(latitude, 4), round(longitude, 4))

    def elevation_key(self, latitude: float, longitude: float) -> str:
        return str(self.round_position(latitude, longitude))

    def cache_elevation(self, latitude: float, longitude: float, elevation: float) -> None:
        key = self.elevation_key(latitude, longitude)
        if not key in self.elevation_data:
            self.elevation_data[key] = elevation
            self.save_elevation_cache()

    def get_elevation(self, latitude: float, longitude: float) -> float:
        key = self.elevation_key(latitude, longitude)
        try:
            return self.elevation_data[key]
        except KeyError:
            return float("NaN")

    def save_elevation_cache(self) -> None:
        if self.elevation_report:
            logging.info("[GPSD-ng] Saving elevation cache")
            self.elevation_report.update(data={"elevations": self.elevation_data})

    def calculate_locations(self, max_dist: int = 100) -> list[dict[str, float]]:
        locations = list()

        def append_location(latitude: float, longitude: float) -> None:
            if not self.elevation_key(latitude, longitude) in self.elevation_data:
                lat, long = self.round_position(latitude, longitude)
                locations.append({"latitude": lat, "longitude": long})

        if not (coords := self.get_position()):
            return []
        if coords.mode != 2:  # No cache if we have a no fix or good Fix
            return []
        append_location(coords.latitude, coords.longitude)
        center = self.round_position(coords.latitude, coords.longitude)
        for dist in range(10, max_dist + 1, 10):
            for degree in range(0, 360):
                point = geopy.distance.distance(meters=dist).destination(center, bearing=degree)
                append_location(point.latitude, point.longitude)
        seen = []
        for l in locations:
            if not l in seen:
                seen.append(l)
        return seen

    def update_cache_elevation(self) -> None:
        if not (
            self.is_configured()
            and (datetime.now(tz=UTC) - self.last_elevation).total_seconds() > 60
        ):
            return
        self.last_elevation = datetime.now(tz=UTC)
        if not (locations := self.calculate_locations()):
            return
        logging.info(f"[GPSD-ng] Elevation cache: {len(self.elevation_data)} elevations available")
        logging.info(f"[GPSD-ng] Trying to cache {len(locations)} locations")
        try:
            logging.info("[GPSD-ng] let's request")
            res = requests.post(
                url="https://api.open-elevation.com/api/v1/lookup",
                headers={"Accept": "application/json", "content-type": "application/json"},
                data=json.dumps(dict(locations=locations)),
                timeout=10,
            )
            if not res.status_code == 200:
                logging.error(
                    f"[GPSD-ng] Error with open-elevation: {res.reason}({res.status_code})"
                )
                return
            with self.lock:
                for item in res.json()["results"]:
                    self.cache_elevation(item["latitude"], item["longitude"], item["elevation"])
                self.save_elevation_cache()
            logging.info(f"[GPSD-ng] {len(self.elevation_data)} elevations in cache")
        except Exception as e:
            logging.error(f"[GPSD-ng] Error with open-elevation: {e}")


@dataclass(slots=True)
class GPSD_ng(plugins.Plugin):
    gpsd: GPSD = field(default_factory=lambda: GPSD())
    display_fields: list[str] = field(default_factory=list)
    handshake_dir: str = ""
    use_open_elevation: bool = True
    position: str = "127,64"
    linespacing: int = 10
    ui_counter: int = 0
    last_ui_update: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    view_mode: str = "compact"
    display_precision: int = 6
    units: str = "metric"
    show_faces: bool = True
    lost_face_1: str = "(O_o )"
    lost_face_2: str = "( o_O)"
    face_1: str = "(•_• )"
    face_2: str = "( •_•)"
    template: str = "Loading error"
    ready: bool = False

    __name__: str = "GPSD-ng"
    __GitHub__: str = "https://github.com/fmatray/pwnagotchi_GPSD-ng"
    __author__: str = "@fmatray"
    __version__: str = "1.8.1"
    __license__: str = "GPL3"
    __description__: str = (
        "Use GPSD server to save position on handshake. Can use mutiple gps device (serial, USB dongle, phone, etc.)"
    )

    __help__: str = (
        "Use GPSD server to save position on handshake. Can use mutiple gps device (serial, USB dongle, phone, etc.)"
    )
    __dependencies__: dict[str, Any] = field(default_factory=lambda: dict(apt=["gpsd python3-gps"]))
    __defaults__: dict[str, Any] = field(default_factory=lambda: dict(enabled=False))

    def __post_init__(self) -> None:
        super(plugins.Plugin, self).__init__()
        template_file = os.path.dirname(os.path.realpath(__file__)) + "/" + "gpsd-ng.html"
        try:
            with open(template_file, "r") as fb:
                self.template = fb.read()
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot read template file {template_file}: {e}")

    # ----------LOAD AND CONFIGURE ----------
    def on_loaded(self) -> None:
        logging.info("[GPSD-ng] plugin loaded")

    def on_config_changed(self, config: dict) -> None:
        logging.info("[GPSD-ng] Reading config")

        self.view_mode = self.options.get("view_mode", self.view_mode).lower()
        if not self.view_mode in ["compact", "full", "status", "none"]:
            logging.error(f"[GPSD-ng] Wrong setting for view_mode: {self.view_mode}. Using compact")
            self.view_mode = "compact"

        DISPLAY_FIELDS = ["info", "altitude", "speed"]
        display_fields = self.options.get("fields", DISPLAY_FIELDS)
        if isinstance(display_fields, str):
            self.display_fields = list(map(str.strip, display_fields.split(",")))
        elif isinstance(display_fields, list):
            self.display_fields = list(map(str.strip, display_fields))
        else:
            logging.error(
                f"[GPSD-ng] Wrong setting for fields: must be a string or list. Using default"
            )
            self.display_fields = DISPLAY_FIELDS

        if "longitude" not in self.display_fields:
            self.display_fields.insert(0, "longitude")
        if "latitude" not in self.display_fields:
            self.display_fields.insert(0, "latitude")

        self.handshake_dir = config["bettercap"].get("handshakes")
        gpsdhost = self.options.get("gpsdhost", "127.0.0.1")
        gpsdport = int(self.options.get("gpsdport", 2947))
        main_device = self.options.get("main_device", None)
        fix_timeout = self.options.get("fix_timeout", 120)
        update_timeout = self.options.get("update_timeout", 120)
        if update_timeout < fix_timeout:
            logging.error(f"[GPSD-ng] 'update_timeout' cannot be lesser than 'fix_timeout'.")
            logging.error(f"[GPSD-ng] Setting 'update_timeout' to 'fix_timeout'.")
            update_timeout = fix_timeout

        self.use_open_elevation = self.options.get("use_open_elevation", self.use_open_elevation)
        save_elevations = self.options.get("save_elevations", True)
        self.gpsd.configure(
            gpsdhost=gpsdhost,
            gpsdport=gpsdport,
            fix_timeout=fix_timeout,
            update_timeout=update_timeout,
            main_device=main_device,
            cache_file=os.path.join(self.handshake_dir, ".elevations"),
            save_elevations=save_elevations,
        )

        self.units = self.options.get("units", self.units).lower()
        if not self.units in ["metric", "imperial"]:
            logging.error(f"[GPSD-ng] Wrong setting for units: {self.units}. Using metric")
            self.units = "metric"
        self.display_precision = int(self.options.get("display_precision", self.display_precision))

        self.position = self.options.get("position", self.position)
        self.linespacing = self.options.get("linespacing", self.linespacing)
        self.show_faces = self.options.get("show_faces", self.show_faces)
        self.lost_face_1 = self.options.get("lost_face_1", self.lost_face_1)
        self.lost_face_2 = self.options.get("lost_face_1", self.lost_face_2)
        self.face_1 = self.options.get("face_1", self.face_1)
        self.face_2 = self.options.get("face_2", self.face_2)
        try:
            self.gpsd.start()
        except Exception as e:
            logging.critical(f"[GPSD-ng] Error with GPSD Thread: {e}")
            logging.critical(f"[GPSD-ng] Stop plugin")
            return
        self.ready = True

    def on_ready(self, agent) -> None:
        try:
            logging.info(f"[GPSD-ng] Disabling bettercap's gps module")
            agent.run("gps off")
        except Exception as e:
            logging.info(f"[GPSD-ng] Bettercap gps was already off.")

    # ---------- UNLOAD ----------
    def on_unload(self, ui) -> None:
        if not self.ready:
            return
        try:
            self.gpsd.join()
        except Exception:
            pass
        with ui._lock:
            for element in ["latitude", "longitude", "altitude", "speed", "gps", "gps_status"]:
                try:
                    ui.remove_element(element)
                except KeyError:
                    pass

    # ---------- BLUETOOTH ----------
    def on_bluetooth_up(self, phone: dict):
        self.gpsd.reload_or_restart_gpsd()

    # ---------- UPDATES ----------
    def update_bettercap_gps(self, agent, coords: Position) -> None:
        try:
            agent.run(f"set gps.set {coords.latitude} {coords.longitude}")
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot set bettercap GPS: {e}")

    def on_internet_available(self, agent) -> None:
        if not self.ready:
            return
        if self.use_open_elevation:
            self.gpsd.update_cache_elevation()

        if not (coords := self.gpsd.get_position()):
            return
        self.update_bettercap_gps(agent, coords)

    # ---------- WIFI HOOKS ----------
    def save_gps_file(self, gps_filename: str, coords: Position) -> None:
        logging.info(f"[GPSD-ng] Saving GPS to {gps_filename}")
        try:
            with open(gps_filename, "w+t") as fp:
                json.dump(coords.to_dict(), fp)
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on saving gps coordinates: {e}")

    def on_unfiltered_ap_list(self, agent, aps) -> None:
        if not self.ready:
            return
        if not (coords := self.gpsd.get_position()):
            return
        self.update_bettercap_gps(agent, coords)
        for ap in aps:  # Complete pcap files with missing gps.json
            try:
                mac = ap["mac"].replace(":", "")
                hostname = re.sub(r"[^a-zA-Z0-9]", "", ap["hostname"])
            except KeyError:
                continue

            pcap_filename = os.path.join(self.handshake_dir, f"{hostname}_{mac}.pcap")
            if not os.path.exists(pcap_filename):  # Pcap file doesn't exist => next
                continue

            gps_filename = os.path.join(self.handshake_dir, f"{hostname}_{mac}.gps.json")
            # gps.json exist with size>0 => next
            if os.path.exists(gps_filename) and os.path.getsize(gps_filename):
                continue

            geo_filename = os.path.join(self.handshake_dir, f"{hostname}_{mac}.geo.json")
            # geo.json exist with size>0 => next
            if os.path.exists(geo_filename) and os.path.getsize(geo_filename):
                continue
            logging.info(f"[GPSD-ng] Found pcap without gps file {os.path.basename(pcap_filename)}")
            self.save_gps_file(gps_filename, coords)

    def on_handshake(self, agent, filename: str, access_point, client_station) -> None:
        if not self.ready:
            return
        if not (coords := self.gpsd.get_position()):
            logging.info("[GPSD-ng] not saving GPS: no fix")
            return
        self.update_bettercap_gps(agent, coords)
        self.save_gps_file(filename.replace(".pcap", ".gps.json"), coords)

    # ---------- UI ----------
    def on_ui_setup(self, ui) -> None:
        if self.view_mode == "none":
            return
        try:
            pos = [int(x.strip()) for x in self.position.split(",")]
            lat_pos = (pos[0] + 5, pos[1])
            lon_pos = (pos[0], pos[1] + self.linespacing)
            alt_pos = (pos[0] + 5, pos[1] + (2 * self.linespacing))
            spd_pos = (pos[0] + 5, pos[1] + (3 * self.linespacing))
        except KeyError:
            if ui.is_waveshare_v2() or ui.is_waveshare_v3() or ui.is_waveshare_v4():
                lat_pos = (127, 64)
                lon_pos = (122, 74)
                alt_pos = (127, 84)
                spd_pos = (127, 94)
            elif ui.is_waveshare_v1():
                lat_pos = (130, 60)
                lon_pos = (130, 70)
                alt_pos = (130, 80)
                spd_pos = (130, 90)
            elif ui.is_inky():
                lat_pos = (127, 50)
                lon_pos = (122, 60)
                alt_pos = (127, 70)
                spd_pos = (127, 80)
            elif ui.is_waveshare144lcd():
                lat_pos = (67, 63)
                lon_pos = (67, 73)
                alt_pos = (67, 83)
                spd_pos = (67, 93)
            elif ui.is_dfrobot_v2():
                lat_pos = (127, 64)
                lon_pos = (122, 74)
                alt_pos = (127, 84)
                spd_pos = (127, 94)
            elif ui.is_waveshare2in7():
                lat_pos = (6, 120)
                lon_pos = (1, 135)
                alt_pos = (6, 150)
                spd_pos = (1, 165)
            else:
                lat_pos = (127, 41)
                lon_pos = (122, 51)
                alt_pos = (127, 61)
                spd_pos = (127, 71)

        match self.view_mode:
            case "compact":
                ui.add_element(
                    "gps",
                    Text(
                        value="Waiting for GPS",
                        color=BLACK,
                        position=lat_pos,
                        font=fonts.Small,
                    ),
                )
            case "full":
                for key, label, label_pos in [
                    ("latitude", "lat:", lat_pos),
                    ("longitude", "long:", lon_pos),
                    ("altitude", "alt:", alt_pos),
                    ("speed", "spd:", spd_pos),
                ]:
                    if key in self.display_fields:
                        ui.add_element(
                            key,
                            LabeledValue(
                                color=BLACK,
                                label=label,
                                value="-",
                                position=label_pos,
                                label_font=fonts.Small,
                                text_font=fonts.Small,
                                label_spacing=0,
                            ),
                        )
            case "status":
                ui.add_element(
                    "gps_status",
                    Text(
                        value="----",
                        color=BLACK,
                        position=lat_pos,
                        font=fonts.Small,
                    ),
                )
            case _:
                pass

    def get_statistics(self) -> dict[str, int | float] | None:
        if not self.ready:
            return None

        pcap_files = glob(os.path.join(self.handshake_dir, "*.pcap"))
        nb_pcap_files = len(pcap_files)
        nb_position_files = 0
        for pcap_file in pcap_files:
            gps_file = pcap_file.replace(".pcap", ".gps.json")
            geo_file = pcap_file.replace(".pcap", ".geo.json")
            if (os.path.exists(gps_file) and os.path.getsize(gps_file)) or (
                os.path.exists(geo_file) and os.path.getsize(geo_file)
            ):
                nb_position_files += 1
        return dict(
            nb_devices=len(self.gpsd.positions),
            nb_pcap_files=nb_pcap_files,
            nb_position_files=nb_position_files,
            completeness=round(nb_position_files / nb_pcap_files * 100, 1),
            nb_cached_elevation=len(self.gpsd.elevation_data),
        )

    def display_face(self, ui, face_1: str, face_2: str) -> None:
        if not self.show_faces:
            return
        match self.ui_counter:
            case 1:
                ui.set("face", face_1)
            case 2:
                ui.set("face", face_2)
            case _:
                pass

    def lost_mode(self, ui, coords: Position | None) -> None:
        if not self.ready:
            return

        self.display_face(ui, self.lost_face_1, self.lost_face_2)

        if not (statistics := self.get_statistics()):
            return
        if not self.gpsd.is_configured():
            status = "GPSD not configured"
        elif not self.gpsd.is_connected():
            status = "GPSD not connected"
        elif statistics["nb_devices"] == 0:
            status = "No GPS device found"
        else:
            status = "Can't get a position"
        ui.set("status", status)

        match self.view_mode:
            case "compact":
                if statistics["nb_devices"] == 0:
                    ui.set("gps", f"No GPS Device")
                else:
                    ui.set("gps", f"No GPS Fix: {statistics['nb_devices']} dev.")
            case "full":
                for i in ["latitude", "longitude", "altitude", "speed"]:
                    try:
                        ui.set(i, "-")
                    except KeyError:
                        pass
            case "status":
                ui.set("gps_status", "Lost")
            case _:
                pass

    def compact_view_mode(self, ui, coords: Position) -> None:
        info, lat, long, alt, spd = coords.format(self.units, self.display_precision)
        match self.ui_counter:
            case 0 if "info" in self.display_fields:
                ui.set("gps", info)
            case 1:
                msg = []
                if "speed" in self.display_fields:
                    msg.append(f"Spd:{spd}")
                if "altitude" in self.display_fields:
                    msg.append(f"Alt:{alt}")
                if msg:
                    ui.set("gps", " ".join(msg))
            case 2:
                if statistics := self.get_statistics():
                    ui.set("gps", f"Complet.:{statistics['completeness']}%")
            case _:
                ui.set("gps", f"{lat},{long}")

    def full_view_mode(self, ui, coords: Position) -> None:
        _, lat, long, alt, spd = coords.format(self.units, self.display_precision)
        ui.set("latitude", f"{lat} ")
        ui.set("longitude", f"{long} ")
        if "altitude" in self.display_fields:
            ui.set("altitude", f"{alt} ")
        if "speed" in self.display_fields:
            ui.set("speed", f"{spd} ")

    def status_view_mode(self, ui, coords: Position) -> None:
        if coords:
            ui.set("gps_status", f" {coords.mode}D ")
            return
        ui.set("gps_status", "Err.")

    def on_ui_update(self, ui) -> None:
        if not self.ready or self.view_mode == "none":
            return
        if (datetime.now(tz=UTC) - self.last_ui_update).total_seconds() < 10:
            return
        self.last_ui_update = datetime.now(tz=UTC)

        self.ui_counter = (self.ui_counter + 1) % 5
        with ui._lock:
            if not (coords := self.gpsd.get_position()):
                self.lost_mode(ui, coords)
                return
            self.display_face(ui, self.face_1, self.face_2)
            match self.view_mode:
                case "compact":
                    self.compact_view_mode(ui, coords)
                case "full":
                    self.full_view_mode(ui, coords)
                case "status":
                    self.status_view_mode(ui, coords)
                case _:
                    pass

    def on_webhook(self, path: str, request) -> str:
        def error(message):
            return render_template("status.html", title="Error", go_back_after=10, message=message)

        if not self.ready:
            return error("Plugin not ready")
        match path:
            case None | "/":
                try:
                    return render_template_string(
                        self.template,
                        device=self.gpsd.get_position_device(),
                        positions=deepcopy(self.gpsd.positions),
                        units=self.units,
                        statistics=self.get_statistics(),
                    )
                except Exception as e:
                    logging.error(f"[GPSD-ng] Error while rendering template: {e}")
                    return error("Rendering error")
            case "polar":
                try:
                    device = request.args["device"]
                    return self.gpsd.positions[device].generate_polar_plot()
                except KeyError:
                    return error("Rendering with polar image")
            case "restart_gpsd":
                self.gpsd.reload_or_restart_gpsd()
                return "Done"
            case _:
                return error("Unkown path")
