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
# main.plugins.gpsd-ng.view_mode = "compact" # "compact", "full", "none"
# main.plugins.gpsd-ng.fields = "info,speed,altitude" # list or string of fields to display
# main.plugins.gpsd-ng.units = "metric" # "metric", "imperial"
# main.plugins.gpsd-ng.display_precision = 6 # display precision for latitude and longitude
# main.plugins.gpsd-ng.position = "127,64"
# main.plugins.gpsd-ng.show_faces = true # if false, doesn't show face. Ex if you use PNG faces
# main.plugins.gpsd-ng.lost_face_1 = "(O_o )"
# main.plugins.gpsd-ng.lost_face_2 = "( o_O)"
# main.plugins.gpsd-ng.face_1 = "(•_• )"
# main.plugins.gpsd-ng.face_2 = "( •_•)"

import threading
import json
import logging
import re
import os
from dataclasses import dataclass, field
from typing import Self
import time
from copy import deepcopy
import math
from datetime import datetime, UTC
import gps
import json
import geopy.distance
import geopy.units
import requests
from functools import total_ordering
from flask import render_template_string

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK
from pwnagotchi.utils import StatusFile


@dataclass
class Position:
    DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
    FIXES = {0: "No value", 1: "No fix", 2: "2D fix", 3: "3D fix"}

    latitude: float = 0
    longitude: float = 0
    altitude: float = 0
    speed: float = 0
    date: datetime = None
    updated: datetime = None
    mode: int = 0
    fix: int = FIXES[0]
    satellites: list = field(default_factory=list)
    viewed_satellites: int = 0
    used_satellites: int = 0
    device: str = ""
    accuracy: float = 0
    polar_plot: str = ""

    def __lt__(self, other: Self) -> bool:
        return (self.mode, self.date) < (other.mode, other.date)

    def update_fix(self, fix: gps.gpsfix) -> None:
        self.latitude = fix.latitude
        self.longitude = fix.longitude
        self.altitude = fix.altMSL
        self.speed = fix.speed * 0.514444  # speed in knots converted in m/s
        self.date = datetime.strptime(fix.time, self.DATE_FORMAT).replace(tzinfo=UTC)
        self.mode = fix.mode
        self.fix = self.FIXES.get(fix.mode, "Mode error")
        self.accuracy = 50
        if not math.isnan(fix.sep):
            self.accuracy = fix.sep

    def update_satellites(self, satellites: list[gps.gpsdata.satellite]) -> None:
        if self.mode < 2:
            self.mode = 1
            self.fix = self.FIXES[1]
        self.satellites = deepcopy(satellites)
        self.viewed_satellites = len(satellites)
        self.used_satellites = len([s for s in satellites if s.used])

    def to_json(self) -> dict:
        return {
            "Latitude": self.latitude,
            "Longitude": self.longitude,
            "Altitude": self.altitude,
            "Speed": self.speed,
            "Date": self.date.strftime(self.DATE_FORMAT),
            "Updated": self.date.strftime(self.DATE_FORMAT),  # Wigle plugin
            "Mode": self.mode,
            "Fix": self.fix,
            "Sats": self.viewed_satellites,
            "Sats_Valid": self.used_satellites,
            "Device": self.device,
            "Accuracy": self.accuracy,
        }

    def is_old(self, max_seconds: int = 90) -> bool:
        try:
            return (datetime.now(tz=UTC) - self.date).total_seconds() > max_seconds
        except TypeError:
            return False

    def format_info(self) -> str:
        dev = re.search(r"(^tcp|^udp|tty.*)", self.device, re.IGNORECASE)
        dev = f"{dev[0]}:" if dev else ""
        return f"{dev}{self.fix} ({self.used_satellites}/{self.viewed_satellites} Sats)"

    def format_lat_long(self, display_precision: int = 6) -> str:
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
        if self.altitude != None:
            if units == "metric":
                return f"{round(self.altitude)}m"
            return f"{round(geopy.units.feet(meters=self.altitude))}ft"
        return "-"

    def format_speed(self, units: str) -> str:
        if self.speed != None:
            if units == "metric":
                return f"{round(self.speed)}m/s"
            return f"{round(geopy.units.feet(meters=self.speed))}ft/s"
        return "-"

    def format(self, units: str, display_precision: int) -> tuple[str, str, str, str, str]:
        info = self.format_info()
        lat, long = self.format_lat_long(display_precision)
        alt = self.format_altitude(units)
        spd = self.format_speed(units)
        return info, lat, long, alt, spd

    def generate_polar_plot(self) -> None:
        """
        Thanks to https://github.com/rai68/gpsd-easy/blob/main/gpsdeasy.py
        """
        try:
            import base64
            import io
            from matplotlib.pyplot import rc, grid, figure, rcParams, savefig, close
        except ImportError:
            logging.error(f"[GPSD-ng] Error while importing modules for generate_polar_plot()")
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

            ax = fig.add_axes([0.1, 0.1, 0.8, 0.8], polar=True, facecolor="#d5de9c")
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
            self.polar_plot = base64.b64encode(image.getvalue()).decode("utf-8")
        except Exception as e:
            logging.error(e)
            self.polar_plot = ""


class GPSD(threading.Thread):
    def __init__(self):
        super().__init__()
        self.gpsdhost = None
        self.gpsdport = None
        self.session = None
        self.positions = dict()  # Device:Position dictionnary
        self.main_device = None
        self.last_position = None
        self.elevation_data = dict()
        self.last_clean = datetime.now(tz=UTC)
        self.elevation_report = None
        self.last_elevation = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        self.lock = threading.Lock()
        self.running = True

    def configure(
        self, gpsdhost: str, gpsdport: int, main_device: str, cache_file: str, save_elevations: str
    ) -> None:
        self.gpsdhost = gpsdhost
        self.gpsdport = gpsdport
        self.main_device = main_device
        if save_elevations:
            self.elevation_report = StatusFile(cache_file, data_format="json")
            self.elevation_data = self.elevation_report.data_field_or("elevations", default=dict())
        logging.info(f"[GPSD-ng] {len(self.elevation_data)} locations already in cache")

    @property
    def configured(self) -> bool:
        return self.gpsdhost and self.gpsdport

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
            except Exception as e:
                logging.error(f"[GPSD-ng] Error while connecting to GPSD: {e}")
                self.session = None

    def clean(self) -> None:
        if (datetime.now(tz=UTC) - self.last_clean).total_seconds() < 10:
            return
        self.last_clean = datetime.now(tz=UTC)
        logging.debug(f"[GPSD-ng] Start cleaning")
        with self.lock:
            positions_to_clean = []
            for device in filter(lambda x: self.positions[x], self.positions):
                if self.positions[device].is_old():
                    positions_to_clean.append(device)
            for device in positions_to_clean:
                self.positions[device] = Position(device=device)
                logging.debug(f"[GPSD-ng] Cleaning {device}")

            if self.last_position and self.last_position.is_old(120):
                self.last_position = None
                logging.debug(f"[GPSD-ng] Cleaning last position")

    def update(self) -> None:
        with self.lock:
            if not self.session.device:
                return
            if not self.session.device in self.positions:
                self.positions[self.session.device] = Position(device=self.session.device)

            if self.session.satellites:
                self.positions[self.session.device].update_satellites(self.session.satellites)

            if self.session.fix.mode < 2:  # Remove positions without fix
                return

            if self.session.fix.mode == 3 and not math.isnan(self.session.fix.altMSL):
                self.cache_elevation(
                    self.session.fix.latitude, self.session.fix.longitude, self.session.fix.altMSL
                )
            else:
                self.session.fix.altMSL = self.get_elevation(
                    self.session.fix.latitude, self.session.fix.longitude
                )

            self.positions[self.session.device].update_fix(self.session.fix)

    def run(self) -> None:
        logging.info(f"[GPSD-ng] Starting loop")
        while self.running:
            try:  # force reinit to avoid data mixing between devices
                self.session.device = None
                self.session.satellites = []
                self.session.satellites_used = 0
                self.session.fix = gps.gpsfix()
            except AttributeError:
                pass
            if not self.configured:
                time.sleep(1)
            elif not self.session:
                self.connect()
            elif self.session.read() == 0:
                self.update()
            else:
                logging.debug(
                    "[GPSD-ng] Closing connection to GPSD: {self.gpsdhost}:{self.gpsdport}"
                )
                self.session.close()
                self.session = None
                time.sleep(1)

    def join(self, timeout=None) -> None:
        self.running = False
        try:
            super().join(timeout)
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on join(): {e}")

    def get_position(self) -> Position | None:
        if not (self.configured and self.positions):
            return None
        self.clean()
        with self.lock:
            try:
                if self.main_device and self.positions[self.main_device]:
                    return self.positions[self.main_device]
            except KeyError:
                pass

            # Fallback
            # Filter devices without coords
            devices = filter(lambda x: x[1], self.positions.items())
            # Sort by best positionning and most recent
            devices = sorted(devices)
            try:
                self.last_position = devices[0][1]  # Get first and best element
            except IndexError:
                logging.debug(f"[GPSD-ng] No data, using last position: {self.last_position}")
            return self.last_position

    # ---------- OPEN ELEVATION ----------
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

    def get_elevation(self, latitude: float, longitude: float) -> float | None:
        key = self.elevation_key(latitude, longitude)
        try:
            return self.elevation_data[key]
        except KeyError:
            return None

    def save_elevation_cache(self) -> None:
        if self.elevation_report:
            self.elevation_report.update(data={"elevations": self.elevation_data})

    def calculate_locations(self, max_dist: int = 100) -> list[tuple[float, float]] | None:
        locations = list()

        def append_location(latitude: float, longitude: float) -> None:
            if not self.elevation_key(latitude, longitude) in self.elevation_data:
                lat, long = self.round_position(latitude, longitude)
                locations.append({"latitude": lat, "longitude": long})

        if not (coords := self.get_position()):
            return None
        if coords.mode > 2:  # No cache if we have a good Fix
            return None
        append_location(coords.latitude, coords.longitude)
        center = self.round_position(coords.latitude, coords.longitude)
        for dist in range(10, max_dist + 1, 10):
            for degree in range(0, 360):
                point = geopy.distance.distance(meters=dist).destination(center, bearing=degree)
                append_location(point.latitude, point.longitude)
        seen = []
        return [l for l in locations if l not in seen and not seen.append(l)]  # remove duplicates

    def update_cache_elevation(self) -> None:
        if not (
            self.configured and (datetime.now(tz=UTC) - self.last_elevation).total_seconds() > 60
        ):
            return
        self.last_elevation = datetime.now(tz=UTC)
        logging.info(f"[GPSD-ng] Running elevation cache: {len(self.elevation_data)} available")

        if not (locations := self.calculate_locations()):
            return
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


class GPSD_ng(plugins.Plugin):
    __name__ = "GPSD-ng"
    __GitHub__ = "https://github.com/fmatray/pwnagotchi_GPSD-ng"
    __author__ = "@fmatray"
    __version__ = "1.5.0"
    __license__ = "GPL3"
    __description__ = "Use GPSD server to save coordinates on handshake. Can use mutiple gps device (gps modules, USB dongle, phone, etc.)"
    __help__ = "Use GPSD server to save coordinates on handshake. Can use mutiple gps device (gps modules, USB dongle, phone, etc.)"
    __dependencies__ = {
        "apt": ["gpsd python3-gps"],
    }
    __defaults__ = {
        "enabled": False,
    }

    LABEL_SPACING = 0
    FIELDS = ["info", "altitude", "speed"]

    def __init__(self) -> None:
        self.gpsd = GPSD()
        self.options = dict()
        self.ui_counter = 0
        template_file = os.path.dirname(os.path.realpath(__file__)) + "/" + "gpsd-ng.html"
        self.template = "Loading error"
        try:
            with open(template_file, "r") as fb:
                self.template = fb.read()
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot read template file {template_file}: {e}")

    @property
    def is_ready(self) -> bool:
        return self.gpsd and self.gpsd.configured

    def on_loaded(self) -> None:
        try:
            self.gpsd.start()
            logging.info("[GPSD-ng] plugin loaded")
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on loading. Trying later...")

    def on_config_changed(self, config: dict) -> None:
        logging.info("[GPSD-ng] Reading config")

        self.view_mode = self.options.get("view_mode", "compact").lower()
        if not self.view_mode in ["compact", "full", "none"]:
            logging.error(f"[GPSD-ng] Wrong setting for view_mode: {self.view_mode}. Using compact")
            self.view_mode = "compact"
        self.fields = self.options.get("fields", self.FIELDS)
        if isinstance(self.fields, str):
            self.fields = self.fields.split(",")
        if not isinstance(self.fields, list):
            logging.error(f"[GPSD-ng] Wrong setting for fields: must be a list. Using default")
            self.fields = self.FIELDS
        else:
            self.fields = [i.strip() for i in self.fields]
            for field in self.fields:
                if not field in self.fields:
                    logging.error(f"[GPSD-ng] Wrong setting for fields: {field}.")
        if "longitude" not in self.fields:
            self.fields.insert(0, "longitude")
        if "latitude" not in self.fields:
            self.fields.insert(0, "latitude")

        self.gpsdhost = self.options.get("gpsdhost", "127.0.0.1")
        self.gpsdport = int(self.options.get("gpsdport", 2947))
        self.main_device = self.options.get("main_device", None)
        self.use_open_elevation = self.options.get("use_open_elevation", True)
        self.save_elevations = self.options.get("save_elevations", True)
        self.units = self.options.get("units", "metric").lower()
        if not self.units in ["metric", "imperial"]:
            logging.error(f"[GPSD-ng] Wrong setting for units: {self.units}. Using metric")
            self.units = "metric"
        self.display_precision = int(self.options.get("display_precision", 6))
        self.position = self.options.get("position", "127,64")
        self.linespacing = int(self.options.get("linespacing", 10))
        self.show_faces = self.options.get("show_faces", True)
        self.lost_face_1 = self.options.get("lost_face_1", "(O_o )")
        self.lost_face_2 = self.options.get("lost_face_1", "( o_O)")
        self.face_1 = self.options.get("face_1", "(•_• )")
        self.face_2 = self.options.get("face_2", "( •_•)")
        self.handshake_dir = config["bettercap"].get("handshakes")
        self.gpsd.configure(
            self.gpsdhost,
            self.gpsdport,
            self.main_device,
            os.path.join(self.handshake_dir, ".elevations"),
            self.save_elevations,
        )

    def on_ready(self, agent) -> None:
        try:
            logging.info(f"[GPSD-ng] Disabling bettercap's gps module")
            agent.run("gps off")
        except Exception as e:
            logging.info(f"[GPSD-ng] Bettercap gps was already off.")

    def on_unload(self, ui) -> None:
        try:
            self.gpsd.join()
        except Exception:
            pass
        with ui._lock:
            for element in [
                "latitude",
                "longitude",
                "altitude",
                "speed",
                "gps",
            ]:
                try:
                    ui.remove_element(element)
                except KeyError:
                    pass

    @staticmethod
    def check_coords(coords: Position) -> bool:
        return coords and all([coords.latitude, coords.longitude])  # avoid 0.000... measurements

    def update_bettercap_gps(self, agent, coords: Position) -> None:
        try:
            agent.run(f"set gps.set {coords.latitude} {coords.longitude}")
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot set bettercap GPS: {e}")

    def on_internet_available(self, agent) -> None:
        if not self.is_ready:
            return
        if self.use_open_elevation:
            self.gpsd.update_cache_elevation()

        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
            return
        self.update_bettercap_gps(agent, coords)

    def save_gps_file(self, gps_filename: str, coords: Position) -> None:
        logging.info(f"[GPSD-ng] Saving GPS to {gps_filename}")
        try:
            with open(gps_filename, "w+t") as fp:
                json.dump(coords.to_json(), fp)
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on saving gps coordinates: {e}")

    def on_unfiltered_ap_list(self, agent, aps) -> None:
        if not self.is_ready:
            return
        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
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
        if not self.is_ready:
            return
        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
            logging.info("[GPSD-ng] not saving GPS: no fix")
            return
        self.update_bettercap_gps(agent, coords)
        self.save_gps_file(filename.replace(".pcap", ".gps.json"), coords)

    def on_ui_setup(self, ui) -> None:
        if self.view_mode == "none":
            return
        try:
            pos = self.position.split(",")
            pos = [int(x.strip()) for x in pos]
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
                    if key in self.fields:
                        ui.add_element(
                            key,
                            LabeledValue(
                                color=BLACK,
                                label=label,
                                value="-",
                                position=label_pos,
                                label_font=fonts.Small,
                                text_font=fonts.Small,
                                label_spacing=self.LABEL_SPACING,
                            ),
                        )
            case _:
                pass

    def set_face(self, ui, face: str) -> None:
        if self.show_faces and face:
            ui.set("face", face)

    def lost_mode(self, ui, coords: Position) -> None:
        with ui._lock:
            if self.ui_counter == 1:
                ui.set("status", "Where am I???")
                self.set_face(ui, self.lost_face_1)
            elif self.ui_counter == 2:
                self.set_face(ui, self.lost_face_2)

            match self.view_mode:
                case "compact":
                    ui.set("gps", "No GPS Data")
                case "full":
                    for i in ["latitude", "longitude", "altitude", "speed"]:
                        try:
                            ui.set(i, "-")
                        except KeyError:
                            pass
                case _:
                    pass

    def display_face(self, ui) -> None:
        with ui._lock:
            if self.ui_counter == 1:
                self.set_face(ui, self.face_1)
            elif self.ui_counter == 2:
                self.set_face(ui, self.face_2)

    def compact_view_mode(self, ui, coords: Position) -> None:
        with ui._lock:
            info, lat, long, alt, spd = coords.format(self.units, self.display_precision)
            if self.ui_counter == 0 and "info" in self.fields:
                ui.set("gps", info)
                return
            if self.ui_counter == 1:
                msg = []
                if "speed" in self.fields:
                    msg.append(f"Spd:{spd}")
                if "altitude" in self.fields:
                    msg.append(f"Alt:{alt}")
                if msg:
                    ui.set("gps", " ".join(msg))
                    return
            ui.set("gps", f"{lat},{long}")

    def full_view_mode(self, ui, coords: Position) -> None:
        _, lat, long, alt, spd = coords.format(self.units, self.display_precision)
        with ui._lock:
            ui.set("latitude", f"{lat} ")
            ui.set("longitude", f"{long} ")
            if "altitude" in self.fields:
                ui.set("altitude", f"{alt} ")
            if "speed" in self.fields:
                ui.set("speed", f"{spd} ")

    def on_ui_update(self, ui) -> None:
        if not self.is_ready or self.view_mode == "none":
            return

        self.ui_counter = (self.ui_counter + 1) % 5
        coords = self.gpsd.get_position()

        if not self.check_coords(coords):
            self.lost_mode(ui, coords)
            return

        self.display_face(ui)
        match self.view_mode:
            case "compact":
                self.compact_view_mode(ui, coords)
            case "full":
                self.full_view_mode(ui, coords)
            case _:
                pass

    def on_webhook(self, path: str, request) -> str:
        if not self.is_ready:
            return "<html><head><title>GPSD-ng: Error</title></head><body><code>Plugin not ready</code></body></html>"

        if path is None or path == "/":
            for device in self.gpsd.positions:
                self.gpsd.positions[device].generate_polar_plot()
            try:
                return render_template_string(
                    self.template, positions=self.gpsd.positions, units=self.units
                )
            except Exception as e:
                logging.error(f"[GPSD-ng] Error while rendering template: {e}")
                return "<html><head><title>GPSD-ng: Error</title></head><body><code>Rendering error</code></body></html>"
