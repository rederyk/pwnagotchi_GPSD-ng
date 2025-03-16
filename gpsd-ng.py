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
# [main.plugins.gpsd-ng]
# enabled = true

# Options with default settings.
# Don't add if you don't need customisation
# [main.plugins.gpsd-ng]
# gpsdhost = "127.0.0.1"
# gpsdport = 2947
# main_device = "/dev/ttyS0" # default None
# use_open_elevation = true
# save_elevations = true
# view_mode = "compact" # "compact", "full", "status", "none"
# fields = "info,speed,altitude" # list or string of fields to display
# units = "metric" # "metric", "imperial"
# display_precision = 6 # display precision for latitude and longitude
# position = "127,64"
# show_faces = true # if false, doesn't show face. Ex if you use PNG faces
# lost_face_1 = "(O_o )"
# lost_face_2 = "( o_O)"
# face_1 = "(•_• )"
# face_2 = "( •_•)"

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
from typing import Any, Self, Optional
from copy import deepcopy
import math
import statistics
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
    Keeps data from a GPS device
    """

    device: str = field(init=True)  # Device name
    dummy: bool = False  # Wifi position is a dummy Position as it's not a real GPS device
    last_update: Optional[datetime] = None
    DATE_FORMAT: str = "%Y-%m-%dT%H:%M:%S.%fZ"
    # Position attributes
    latitude: float = field(default=float("NaN"))
    longitude: float = field(default=float("NaN"))
    altitude: float = field(default=float("NaN"))
    speed: float = field(default=float("NaN"))
    accuracy: float = field(default=float("NaN"))
    # Fix attributes
    FIXES: dict[int, str] = field(
        default_factory=lambda: {0: "No data", 1: "No fix", 2: "2D fix", 3: "3D fix"}
    )
    mode: int = 0
    last_fix: Optional[datetime] = None
    satellites: list = field(default_factory=list)

    # for logs
    header: str = ""

    def __post_init__(self) -> None:
        self.header = f"[GPSD-NG][{self.device}]"

    @property
    def seen_satellites(self) -> int:
        return len(self.satellites)

    @property
    def used_satellites(self) -> int:
        return sum(1 for s in self.satellites if s.used)

    @property
    def fix(self) -> str:
        return self.FIXES.get(self.mode, "Mode error")

    @property
    def last_update_ago(self) -> Optional[int]:
        if not self.last_update:
            return None
        return round((datetime.now(tz=UTC) - self.last_update).total_seconds())

    @property
    def last_fix_ago(self) -> Optional[int]:
        if not self.last_fix:
            return None
        return round((datetime.now(tz=UTC) - self.last_fix).total_seconds())

    def __lt__(self, other: Self) -> bool:
        if self.last_fix and other.last_fix:
            return (self.dummy, self.mode, -self.last_fix.timestamp()) < (
                other.dummy,
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
        """
        Update a Postion with the fix data
        """
        if not gps.MODE_SET & valid:
            return  # not a valid data
        now = datetime.now(tz=UTC)
        if fix.mode >= 2:  # 2D and 3D fix
            self.last_fix = now  # Don't use fix.time cause it's not reliable
            self.set_attr("latitude", fix.latitude, valid, gps.LATLON_SET)
            self.set_attr("longitude", fix.longitude, valid, gps.LATLON_SET)
            self.set_attr("speed", fix.speed, valid, gps.SPEED_SET)
            self.set_attr("mode", fix.mode, valid, gps.MODE_SET)
            self.accuracy = 50
            return
        # reset fix after 10s without fix
        if self.last_fix and (now - self.last_fix).total_seconds() < 10:
            return
        self.latitude = float("NaN")
        self.longitude = float("NaN")
        self.altitude = float("NaN")
        self.speed = float("NaN")
        self.accuracy = float("NaN")

    def update_satellites(self, satellites: list[gps.gpsdata.satellite], valid: int) -> None:
        self.set_attr("satellites", satellites, valid, gps.SATELLITE_SET)

    def update_altitude(self, altitude: int) -> None:
        self.altitude = altitude

    # ---------- VALIDATION AND TIME ----------
    def is_valid(self) -> bool:
        return gps.isfinite(self.latitude) and gps.isfinite(self.longitude) and self.mode >= 2

    def is_old(self, date: Optional[datetime], max_seconds: int) -> Optional[bool]:
        if not date:
            return None
        return (datetime.now(tz=UTC) - date).total_seconds() > max_seconds

    def is_update_old(self, max_seconds: int) -> Optional[bool]:
        return self.is_old(self.last_update, max_seconds)

    def is_fix_old(self, max_seconds: int) -> Optional[bool]:
        return self.is_old(self.last_fix, max_seconds)

    def is_fixed(self) -> bool:
        return self.mode >= 2

    # ---------- JSON DUMP ----------
    def to_dict(self) -> dict[str, int | float | datetime | Optional[str]]:
        """
        Used to save to .gps.json files
        """
        if self.last_fix:
            last_fix = self.last_fix.strftime(self.DATE_FORMAT)
        else:
            last_fix = None
        return dict(
            Latitude=self.latitude,
            Longitude=self.longitude,
            Altitude=self.altitude,
            Speed=self.speed * gps.KNOTS_TO_MPS,
            Accuracy=self.accuracy,
            Date=last_fix,
            Updated=last_fix,  # Wigle plugin
            Mode=self.mode,
            Fix=self.fix,
            Sats=self.seen_satellites,
            Sats_used=self.used_satellites,
            Device=self.device,
            Dummy=self.dummy,
        )

    # ---------- FORMAT for eink and Web UI----------
    def format_info(self) -> str:
        device = re.search(r"(^tcp|^udp|tty.*|wifi)", self.device, re.IGNORECASE)
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
            return "-"
        match units:
            case "imperial":
                return f"{round(geopy.units.feet(meters=self.altitude))}ft"
            case "metric":
                return f"{round(self.altitude)}m"
        return "error"

    def format_speed(self, units: str) -> str:
        if not gps.isfinite(self.speed):
            return "-"
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

    def generate_polar_plot(self) -> Optional[str]:
        """
        Return a polar image (base64) of seen satellites.
        Thanks to https://github.com/rai68/gpsd-easy/blob/main/gpsdeasy.py
        """
        try:
            from matplotlib.pyplot import rc, grid, figure, rcParams, savefig, close
        except ImportError:
            logging.error(
                f"{self.header} Error while importing matplotlib for generate_polar_plot()"
            )
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
    """
    Main thread:
    - Connect to gpsd server
    - Read and update/clean positions from gpsd and wifi positioning
    - cache elevations
    """

    # gpsd connection
    gpsdhost: Optional[str] = None
    gpsdport: Optional[int] = None
    session: gps.gps = None
    # Data reading
    fix_timeout: int = 120
    update_timeout: int = 120
    main_device: Optional[str] = None
    last_clean: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    positions: dict = field(default_factory=dict)  # Device:Position dictionnary
    last_position: Optional[Position] = None
    # Wifi potisioning
    wifi_positions: dict[str, dict[str, float]] = field(default_factory=dict)
    last_wifi_positioning_save: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    wifi_positioning_report: Optional[StatusFile] = None
    # Open Elevation
    elevation_data: dict = field(default_factory=dict)
    elevation_report: Optional[StatusFile] = None
    last_elevation: datetime = field(default_factory=lambda: datetime(2025, 1, 1, 0, 0, tzinfo=UTC))
    # hook
    last_hook: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    lost_position_sent: bool = False

    # Thread and logs
    lock: threading.Lock = field(default_factory=threading.Lock)
    exit: threading.Event = field(default_factory=lambda: threading.Event())
    header: str = "[GPSD-ng][Thread]"

    def __post_init__(self) -> None:
        super(GPSD, self).__init__()

    def __hash__(self) -> int:
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
        cache_filename: str,
        save_elevations: bool,
        wifi_positioning_filename: Optional[str],
    ) -> None:
        self.gpsdhost, self.gpsdport = gpsdhost, gpsdport
        self.fix_timeout, self.update_timeout = fix_timeout, update_timeout
        self.main_device = main_device
        if save_elevations:
            logging.info(f"{self.header} Reading elevation cache")
            self.elevation_report = StatusFile(cache_filename, data_format="json")
            self.elevation_data = self.elevation_report.data_field_or("elevations", default=dict())
            logging.info(f"{self.header} {len(self.elevation_data)} locations already in cache")

        if wifi_positioning_filename:
            logging.info(f"{self.header} Reading wifi position cache")
            self.wifi_positioning_report = StatusFile(wifi_positioning_filename, data_format="json")
            self.wifi_positions = self.wifi_positioning_report.data_field_or(
                "wifi_positions", default=dict()
            )
            logging.info(f"{self.header} {len(self.wifi_positions)} wifi locations in cache")
        logging.info(f"{self.header} Thread configured")

    def is_configured(self) -> bool:
        return (self.gpsdhost, self.gpsdport) != (None, None)

    def connect(self) -> bool:
        with self.lock:
            logging.info(f"{self.header} Trying to connect to {self.gpsdhost}:{self.gpsdport}")
            try:
                self.session = gps.gps(
                    host=self.gpsdhost,
                    port=self.gpsdport,
                    mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE,
                )
                logging.info(f"{self.header} Connected to {self.gpsdhost}:{self.gpsdport}")
            except Exception as e:
                logging.error(f"{self.header} Error while connecting: {e}")
                self.session = None
                return False
        return True

    def is_connected(self) -> bool:
        return self.session is not None

    def close(self) -> None:
        self.session.close()
        self.session = None
        logging.info(f"{self.header} GPSD connection closed")

    # ---------- RELOAD/ RESTART GPSD SERVER ----------
    def reload_or_restart_gpsd(self) -> None:
        try:
            logging.info(f"{self.header} Trying to reload gpsd server")
            subprocess.run(
                ["killall", "-SIGHUP", "gpsd"],
                check=True,
                timeout=5,
            )
            self.exit.wait(2)
            logging.info(f"{self.header} GPSD reloaded")
            return
        except subprocess.CalledProcessError as exp:
            logging.error(f"{self.header} Error while reloading gpsd: {exp}")
        self.restart_gpsd()

    def restart_gpsd(self) -> None:
        try:
            logging.info(f"{self.header} Trying to restart gpsd server")
            subprocess.run(
                ["systemctl", "restart", "gpsd"],
                check=True,
                timeout=20,
            )
            self.exit.wait(2)
            logging.info(f"{self.header} GPSD restarted")
        except subprocess.CalledProcessError as exp:
            logging.error(f"{self.header} Error while restarting gpsd: {exp}")

    # ---------- UPDATE AND CLEAN ----------
    def update(self) -> None:
        with self.lock:
            if not ((gps.ONLINE_SET & self.session.valid) and (device := self.session.device)):
                return  # not a TPV or SKY
            if not device in self.positions:
                self.positions[device] = Position(device=device)
                logging.info(f"{self.header} New device: {device}")

            # Update fix
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

            # update satellites
            self.positions[device].update_satellites(self.session.satellites, self.session.valid)

            # Soft reset session after reading
            self.session.valid = 0
            self.session.device = None
            self.session.fix = gps.gpsfix()
            self.session.satellites = []

    def clean(self) -> None:
        if not self.update_timeout:
            return  # keep positions forever
        if (datetime.now(tz=UTC) - self.last_clean).total_seconds() < 10:
            return
        self.last_clean = datetime.now(tz=UTC)
        with self.lock:
            for device in list(self.positions.keys()):
                if self.positions[device].is_update_old(self.update_timeout):
                    del self.positions[device]
                    logging.info(f"{self.header} Cleaning {device}")

    # ---------- WIFI POSITIONNING ----------
    def save_wifi_positions(self) -> None:
        if (datetime.now(tz=UTC) - self.last_wifi_positioning_save).total_seconds() < 60:
            return
        self.last_wifi_positioning_save = datetime.now(tz=UTC)
        if self.wifi_positioning_report and self.wifi_positions:
            logging.info(f"{self.header} Saving wifi positions")
            self.wifi_positioning_report.update(data={"wifi_positions": self.wifi_positions})

    def update_wifi_positions(self, bssid: str, lat: float, long: float, alt: float) -> None:
        if math.isnan(lat) or math.isnan(long):
            return
        self.wifi_positions[bssid] = dict(latitude=lat, longitude=long, altitude=alt)

    def update_wifi(self, bssids: list[str]) -> None:
        points = list()
        for bssid in filter(lambda b: b in self.wifi_positions, bssids):
            points.append(self.wifi_positions[bssid])
        if len(points) < 3:  # skip if not enought points
            return
        # Calculate the box containing all points
        box_min = (
            min(points, key=lambda p: p["latitude"])["latitude"],
            min(points, key=lambda p: p["longitude"])["longitude"],
        )
        box_max = (
            max(points, key=lambda p: p["latitude"])["latitude"],
            max(points, key=lambda p: p["longitude"])["longitude"],
        )
        if geopy.distance.distance(box_min, box_max).meters > 50:  # skip if the box is too large
            return
        try:  # using median rather than mean to be more representative
            latitude = statistics.median([p["latitude"] for p in points])
            longitude = statistics.median([p["longitude"] for p in points])
        except statistics.StatisticsError:
            return

        try:
            altitudes = [p["altitude"] for p in points]
            altitudes = list(filter(lambda p: not (p is None or math.isnan(p)), altitudes))
            altitude = statistics.median(altitudes)
        except statistics.StatisticsError:
            altitude = self.get_elevation(latitude, longitude)  # try to use cache if no altitude

        with self.lock:
            if "wifi" not in self.positions:
                self.positions["wifi"] = Position(accuracy=50, device="wifi", dummy=True)
                logging.info(f"{self.header} New device: wifi")

            self.positions["wifi"].latitude = latitude
            self.positions["wifi"].longitude = longitude
            self.positions["wifi"].altitude = altitude
            self.positions["wifi"].last_update = datetime.now(tz=UTC)
            self.positions["wifi"].last_fix = datetime.now(tz=UTC)
            if math.isnan(altitude):
                self.positions["wifi"].mode = 2
            else:
                self.positions["wifi"].mode = 3

    # ---------- MAIN LOOP ----------
    def plugin_hook(self) -> None:
        """
        Trigger position_available() evry 10s if a position is else position_lost() is called once
        """
        if (datetime.now(tz=UTC) - self.last_hook).total_seconds() < 10:
            return
        self.last_hook = datetime.now(tz=UTC)
        if coords := self.get_position():
            plugins.on("position_available", coords.to_dict())
            self.lost_position_sent = False
        elif not self.lost_position_sent:
            plugins.on("position_lost")
            self.lost_position_sent = True

    def loop(self) -> None:
        """
        Main thread loo. Handles gpsd connection and raw reading
        """
        logging.info(f"{self.header} Starting gpsd thread loop")
        connection_errors = 0

        while not self.exit.is_set():
            try:
                self.clean()
                if not self.session and not self.connect():
                    connection_errors += 1
                if self.session.waiting(timeout=2) and self.session.read() == 0:
                    self.update()
                    connection_errors = 0
                else:
                    self.close()
                    connection_errors += 1

                if connection_errors >= 3:
                    logging.error(f"{self.header} {connection_errors} connection errors")
                    self.restart_gpsd()
                    connection_errors = 0
                self.plugin_hook()
            except ConnectionError as exp:
                logging.error(f"{self.header} Connection Error: {exp}")
                self.restart_gpsd()
                connection_errors = 0

    def run(self) -> None:
        """
        Called by GPSD.start()
        """
        if not self.is_configured():
            logging.critical(f"{self.header} GPSD thread not configured.")
            return
        self.reload_or_restart_gpsd()
        try:
            self.loop()
        except Exception as exp:
            logging.critical(f"{self.header} Critical error during loop: {exp}")

    def join(self, timeout=None) -> None:
        """
        End the thread
        """
        self.exit.set()  # end loop
        try:
            super(GPSD, self).join(timeout)
        except Exception as e:
            logging.error(f"{self.header} Error on join(): {e}")

    # ---------- POSITION ----------
    def get_position_device(self) -> Optional[str]:
        """
        Returns the device with the best position
        """
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
                logging.debug(f"{self.header} No valid position")
            return None

    def get_position(self) -> Optional[Position]:
        """
        Returns the best position. If no position available, send the last postition within fix timout.
        """
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
            logging.info(f"{self.header}[Elevation] Saving elevation cache")
            self.elevation_report.update(data={"elevations": self.elevation_data})

    def calculate_locations(self, max_dist: int = 100) -> list[dict[str, float]]:
        """
        Calculates gps points for circles every 10m and up to 100m.
        Rounding is an efficiant way to decrease the number of points.
        """
        locations = list()

        def append_location(latitude: float, longitude: float) -> None:
            if not self.elevation_key(latitude, longitude) in self.elevation_data:
                lat, long = self.round_position(latitude, longitude)
                locations.append({"latitude": lat, "longitude": long})

        if not (coords := self.get_position()):  # No current position
            return []
        if coords.mode != 2:  # No cache if we have a no fix or good Fix
            return []
        append_location(coords.latitude, coords.longitude)  # Add current position
        center = self.round_position(coords.latitude, coords.longitude)
        for dist in range(10, max_dist + 1, 10):
            for degree in range(0, 360):
                point = geopy.distance.distance(meters=dist).destination(center, bearing=degree)
                append_location(point.latitude, point.longitude)
        seen = []
        for l in locations:  # Filter duplicates
            if not l in seen:
                seen.append(l)
        return seen

    def fetch_open_elevation(self, locations: list[dict[str, float]]) -> Optional[dict]:
        """
        Retreive elevations from open-elevation
        """
        try:
            response = requests.post(
                url="https://api.open-elevation.com/api/v1/lookup",
                headers={"Accept": "application/json", "content-type": "application/json"},
                data=json.dumps(dict(locations=locations)),
                timeout=10,
            )
            response.raise_for_status()
            return response.json()["results"]
        except requests.RequestException as e:
            logging.error(f"{self.header}[Elevation] Error with open-elevation: {e}")
        except json.JSONDecodeError:
            logging.error(f"{self.header}[Elevation] Error while reading json")
        return None

    def update_cache_elevation(self) -> None:
        """
        Use open-elevation API to cache surrounding GPS points.
        """
        if not (
            self.is_configured()
            and (datetime.now(tz=UTC) - self.last_elevation).total_seconds() > 60
        ):
            return
        self.last_elevation = datetime.now(tz=UTC)
        if not (locations := self.calculate_locations()):
            return
        logging.info(f"{self.header}[Elevation] {len(self.elevation_data)} elevations available")
        logging.info(f"{self.header}[Elevation] Trying to cache {len(locations)} locations")
        if not (results := self.fetch_open_elevation(locations)):
            return
        with self.lock:
            for item in results:
                self.cache_elevation(item["latitude"], item["longitude"], item["elevation"])
            self.save_elevation_cache()
        logging.info(f"{self.header}[Elevation] {len(self.elevation_data)} elevations in cache")


@dataclass(slots=True)
class GPSD_ng(plugins.Plugin):
    # GPSD Thread and configuration
    gpsd: GPSD = field(default_factory=lambda: GPSD())
    use_open_elevation: bool = True
    wifi_positioning: bool = False
    handshake_dir: str = ""
    #  e-ink display
    last_ui_update: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    ui_counter: int = 0
    view_mode: str = "compact"
    display_fields: list[str] = field(default_factory=list)
    units: str = "metric"
    display_precision: int = 6
    position: str = "127,64"
    linespacing: int = 10
    show_faces: bool = True
    lost_face_1: str = "(O_o )"
    lost_face_2: str = "( o_O)"
    face_1: str = "(•_• )"
    face_2: str = "( •_•)"
    # Web UI
    template: str = "Loading error"

    ready: bool = False

    __name__: str = "GPSD-ng"
    __GitHub__: str = "https://github.com/fmatray/pwnagotchi_GPSD-ng"
    __author__: str = "@fmatray"
    __version__: str = "1.9.6"
    __license__: str = "GPL3"
    __description__: str = (
        "Use GPSD server to save position on handshake. Can use mutiple gps device (serial, USB dongle, phone, etc.)"
    )
    __help__: str = (
        "Use GPSD server to save position on handshake. Can use mutiple gps device (serial, USB dongle, phone, etc.)"
    )
    __dependencies__: dict[str, Any] = field(default_factory=lambda: dict(apt=["gpsd python3-gps"]))
    __defaults__: dict[str, Any] = field(default_factory=lambda: dict(enabled=False))
    header: str = "[GPSD-ng][Plugin]"

    def __post_init__(self) -> None:
        super(plugins.Plugin, self).__init__()
        template_filename = os.path.dirname(os.path.realpath(__file__)) + "/" + "gpsd-ng.html"
        try:
            with open(template_filename, "r") as fb:
                self.template = fb.read()
        except IOError as e:
            logging.error(f"{self.header} Cannot read template file {template_filename}: {e}")

    # ----------LOAD AND CONFIGURE ----------
    def on_loaded(self) -> None:
        logging.info(f"{self.header} Plugin loaded. Version {self.__version__}")

    def on_config_changed(self, config: dict) -> None:
        logging.info(f"{self.header} Reading configuration")

        # GPSD Thread
        gpsdhost = self.options.get("gpsdhost", "127.0.0.1")
        gpsdport = int(self.options.get("gpsdport", 2947))
        main_device = self.options.get("main_device", None)
        fix_timeout = self.options.get("fix_timeout", 120)
        update_timeout = self.options.get("update_timeout", 120)
        if update_timeout < fix_timeout:
            logging.error(f"{self.header} 'update_timeout' cannot be lesser than 'fix_timeout'.")
            logging.error(f"{self.header} Setting 'update_timeout' to 'fix_timeout'.")
            update_timeout = fix_timeout
        # open-elevation
        self.handshake_dir = config["bettercap"].get("handshakes")
        self.use_open_elevation = self.options.get("use_open_elevation", self.use_open_elevation)
        save_elevations = self.options.get("save_elevations", True)
        # wifi positioning
        self.wifi_positioning = self.options.get("wifi_positioning", self.wifi_positioning)
        wifi_positioning_filename = None
        if self.wifi_positioning:
            wifi_positioning_filename = os.path.join(self.handshake_dir, ".wifi_positioning")

        self.gpsd.configure(
            gpsdhost=gpsdhost,
            gpsdport=gpsdport,
            fix_timeout=fix_timeout,
            update_timeout=update_timeout,
            main_device=main_device,
            cache_filename=os.path.join(self.handshake_dir, ".elevations"),
            save_elevations=save_elevations,
            wifi_positioning_filename=wifi_positioning_filename,
        )
        if self.wifi_positioning:
            self.read_position_files()

        try:  # Start gpsd thread
            self.gpsd.start()
        except Exception as e:
            logging.critical(f"{self.header} Error with GPSD Thread: {e}")
            logging.critical(f"{self.header} Stop plugin")
            return

        # view mode
        self.view_mode = self.options.get("view_mode", self.view_mode).lower()
        if not self.view_mode in ["compact", "full", "status", "none"]:
            logging.error(
                f"{self.header} Wrong setting for view_mode: {self.view_mode}. Using compact"
            )
            self.view_mode = "compact"
        # fields ton display
        DISPLAY_FIELDS = ["info", "altitude", "speed"]
        display_fields = self.options.get("fields", DISPLAY_FIELDS)
        if isinstance(display_fields, str):
            self.display_fields = list(map(str.strip, display_fields.split(",")))
        elif isinstance(display_fields, list):
            self.display_fields = list(map(str.strip, display_fields))
        else:
            logging.error(
                f"{self.header} Wrong setting for fields: must be a string or list. Using default"
            )
            self.display_fields = DISPLAY_FIELDS

        if "longitude" not in self.display_fields:
            self.display_fields.insert(0, "longitude")
        if "latitude" not in self.display_fields:
            self.display_fields.insert(0, "latitude")
        # units and precision. only for display
        self.units = self.options.get("units", self.units).lower()
        if not self.units in ["metric", "imperial"]:
            logging.error(f"{self.header} Wrong setting for units: {self.units}. Using metric")
            self.units = "metric"
        self.display_precision = int(self.options.get("display_precision", self.display_precision))
        # UI items
        self.position = self.options.get("position", self.position)
        self.linespacing = self.options.get("linespacing", self.linespacing)
        self.show_faces = self.options.get("show_faces", self.show_faces)
        self.lost_face_1 = self.options.get("lost_face_1", self.lost_face_1)
        self.lost_face_2 = self.options.get("lost_face_1", self.lost_face_2)
        self.face_1 = self.options.get("face_1", self.face_1)
        self.face_2 = self.options.get("face_2", self.face_2)

        logging.info(f"{self.header} Configuration done")
        self.ready = True

    def on_ready(self, agent) -> None:
        try:
            logging.info(f"{self.header} Disabling bettercap's gps module")
            agent.run("gps off")
        except Exception as e:
            logging.info(f"{self.header} Bettercap gps was already off.")

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
    def on_bluetooth_up(self, phone: dict) -> None:
        """
        Restart gpsd server on bluetooth reconnection
        """
        self.gpsd.reload_or_restart_gpsd()

    # ---------- WIFI POSITIONING ----------
    def read_position_files(self) -> None:
        """
        Read gps.json and geo.json files for wifi poistioning
        """
        files = glob(os.path.join(self.handshake_dir, "*.g*.json"))
        logging.info(f"{self.header} Reading gps/geo files ({len(files)}) for wifi positionning")
        nb_files = 0
        for file in files:
            if not self.is_gpsfile_valid(file):
                continue  # continue if the file is not valid
            try:
                bssid = re.findall(r".*_([0-9a-f]{12})\.", file)[0]
            except IndexError:
                continue
            try:
                with open(file, "r") as fb:
                    data = json.load(fb)
                    if data.get("Device", None) == "wifi":
                        continue  # remove wifi based positions
                    self.gpsd.update_wifi_positions(
                        bssid=bssid,
                        lat=data.get("Latitude", float("NaN")),
                        long=data.get("Longitude", float("NaN")),
                        alt=data.get("Altitude", float("NaN")),
                    )
                    nb_files += 1
            except (IOError, TypeError, KeyError) as e:
                logging.error(f"{self.header} Error on reading file {file}: {e}")
        logging.info(f"{self.header} {nb_files} initial files used for wifi positioning")

    def update_wifi_positions(self, aps, coords: Position) -> None:
        """
        Update wifi position based on a list for access points
        """
        if coords.device == "wifi":
            return
        for ap in aps:
            try:
                mac = ap["mac"].replace(":", "")
            except KeyError:
                continue
            self.gpsd.update_wifi_positions(mac, coords.latitude, coords.longitude, coords.altitude)
        self.gpsd.save_wifi_positions()

    # ---------- UPDATES ----------
    def update_bettercap_gps(self, agent, coords: Position) -> None:
        try:
            agent.run(f"set gps.set {coords.latitude} {coords.longitude}")
        except Exception as e:
            logging.error(f"{self.header} Cannot set bettercap GPS: {e}")

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
        logging.info(f"{self.header} Saving GPS to {gps_filename}")
        try:
            with open(gps_filename, "w+t") as fp:
                json.dump(coords.to_dict(), fp, indent=4)
        except (IOError, TypeError) as e:
            logging.error(f"{self.header} Error on saving gps coordinates: {e}")

    @staticmethod
    def is_gpsfile_valid(gps_filename: str) -> bool:
        return os.path.exists(gps_filename) and os.path.getsize(gps_filename) > 0

    def complete_missings(self, aps, coords: Position) -> None:
        for ap in aps:
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
            if self.is_gpsfile_valid(gps_filename):
                continue

            geo_filename = os.path.join(self.handshake_dir, f"{hostname}_{mac}.geo.json")
            # geo.json exist with size>0 => next
            if self.is_gpsfile_valid(geo_filename):
                continue
            logging.info(
                f"{self.header} Found pcap without gps file {os.path.basename(pcap_filename)}"
            )
            self.save_gps_file(gps_filename, coords)

    def on_unfiltered_ap_list(self, agent, aps) -> None:
        if not self.ready:
            return
        if coords := self.gpsd.get_position():
            self.update_bettercap_gps(agent, coords)
            self.complete_missings(aps, coords)
            if self.wifi_positioning:
                self.update_wifi_positions(aps, coords)
        if self.wifi_positioning:
            bssids = [ap["mac"].replace(":", "") for ap in aps]
            self.gpsd.update_wifi(bssids)

    def on_handshake(self, agent, filename: str, access_point, client_station) -> None:
        if not self.ready:
            return
        if not (coords := self.gpsd.get_position()):
            logging.info(f"{self.header} Not saving GPS: no fix")
            return
        self.update_bettercap_gps(agent, coords)
        gps_filename = filename.replace(".pcap", ".gps.json")
        if self.is_gpsfile_valid(gps_filename) and coords.device == "wifi":
            return  # not saving wifi positioning if a file already exists and is valid
        self.save_gps_file(gps_filename, coords)

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

    def get_statistics(self) -> Optional[dict[str, int | float]]:
        if not self.ready:
            return None

        pcap_filenames = glob(os.path.join(self.handshake_dir, "*.pcap"))
        nb_pcap_files = len(pcap_filenames)
        nb_position_files = 0
        for pcap_filename in pcap_filenames:
            gps_filename = pcap_filename.replace(".pcap", ".gps.json")
            geo_filename = pcap_filename.replace(".pcap", ".geo.json")
            if self.is_gpsfile_valid(gps_filename) or self.is_gpsfile_valid(geo_filename):
                nb_position_files += 1
        try:
            completeness = round(nb_position_files / nb_pcap_files * 100, 1)
        except ZeroDivisionError:
            completeness = 0.0
        return dict(
            nb_devices=len(self.gpsd.positions),
            nb_pcap_files=nb_pcap_files,
            nb_position_files=nb_position_files,
            completeness=completeness,
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

    def lost_mode(self, ui) -> None:
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
                self.lost_mode(ui)
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
        def error(message) -> str:
            return render_template("status.html", title="Error", go_back_after=10, message=message)

        if not self.ready:
            return error("Plugin not ready")
        match path:
            case None | "/":
                try:
                    return render_template_string(
                        self.template,
                        device=self.gpsd.get_position_device(),
                        current_position=deepcopy(self.gpsd.get_position()),
                        positions=deepcopy(self.gpsd.positions),
                        units=self.units,
                        statistics=self.get_statistics(),
                    )
                except Exception as e:
                    logging.error(f"{self.header} Error while rendering template: {e}")
                    return error("Rendering error")
            case "polar":
                try:
                    device = request.args["device"]
                    return self.gpsd.positions[device].generate_polar_plot()
                except KeyError:
                    return error("{self.header} Rendering with polar image")
            case "restart_gpsd":
                self.gpsd.reload_or_restart_gpsd()
                return "Done"
            case _:
                return error("{self.header} Unkown path")
