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
# main.plugins.gpsd-ng.compact_view = true
# main.plugins.gpsd-ng.position = "127,64"
# main.plugins.gpsd-ng.lost_face_1 = "(O_o )"
# main.plugins.gpsd-ng.lost_face_2 = "( o_O)"
# main.plugins.gpsd-ng.face_1 = "(•_• )"
# main.plugins.gpsd-ng.face_2 = "( •_•)"


import threading
import json
import logging
import re
import os
import time
import math
from datetime import datetime, UTC
import gps
import json
import geopy.distance
import requests
from flask import make_response, redirect

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue, Text
from pwnagotchi.ui.view import BLACK
from pwnagotchi.utils import StatusFile


class GPSD(threading.Thread):
    FIXES = {0: "No value", 1: "No fix", 2: "2D fix", 3: "3D fix"}
    DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

    def __init__(self):
        super().__init__()
        self.gpsdhost = None
        self.gpsdport = None
        self.session = None
        self.devices = dict()
        self.main_device = None
        self.last_position = None
        self.elevation_cache = dict()
        self.last_clean = datetime.now(tz=UTC)
        self.elevation_report = None
        self.last_elevation = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
        self.lock = threading.Lock()
        self.running = True

    def configure(self, gpsdhost, gpsdport, main_device, cache_file, save_elevations):
        self.gpsdhost = gpsdhost
        self.gpsdport = gpsdport
        self.main_device = main_device
        if save_elevations:
            self.elevation_report = StatusFile(cache_file, data_format="json")
            self.elevation_cache = self.elevation_report.data_field_or("elevations", default=dict())
        logging.info(f"[GPSD-ng] {len(self.elevation_cache)} locations already in cache")

    @property
    def configured(self):
        return self.gpsdhost and self.gpsdport

    def connect(self):
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

    def is_old(self, date, max_seconds=90):
        try:
            d_time = datetime.strptime(date, self.DATE_FORMAT)
            d_time = d_time.replace(tzinfo=UTC)
        except TypeError:
            return False
        delta = datetime.now(tz=UTC) - d_time
        return delta.total_seconds() > max_seconds

    def clean(self):
        if (datetime.now(tz=UTC) - self.last_clean).total_seconds() < 10:
            return
        self.last_clean = datetime.now(tz=UTC)
        logging.debug(f"[GPSD-ng] Start cleaning")
        with self.lock:
            devices_to_clean = []
            for device in filter(lambda x: self.devices[x], self.devices):
                if self.is_old(self.devices[device]["Date"]):
                    devices_to_clean.append(device)
            for device in devices_to_clean:
                self.devices[device] = None
                logging.debug(f"[GPSD-ng] Cleaning {device}")

            if self.last_position and self.is_old(self.last_position["Date"], 120):
                self.last_position = None
                logging.debug(f"[GPSD-ng] Cleaning last position")

    def update(self):
        with self.lock:
            if not self.session.device or self.session.fix.mode < 2:  # Remove positions without fix
                return
            if self.session.fix.mode == 3 and not math.isnan(self.session.fix.altMSL):
                altitude = self.session.fix.altMSL
                self.cache_elevation(
                    self.session.fix.latitude, self.session.fix.longitude, altitude
                )
            else:
                altitude = self.get_elevation(self.session.fix.latitude, self.session.fix.longitude)

            self.devices[self.session.device] = dict(
                Latitude=self.session.fix.latitude,
                Longitude=self.session.fix.longitude,
                Altitude=altitude,
                Speed=(self.session.fix.speed * 0.514444),  # speed in knots converted in m/s
                Date=self.session.fix.time,
                Updated=self.session.fix.time,  # Wigle plugin
                Mode=self.session.fix.mode,
                Fix=self.FIXES.get(self.session.fix.mode, "Mode error"),
                Sats=len(self.session.satellites),
                Sats_Valid=self.session.satellites_used,
                Device=self.session.device,
                Accuracy=(  # Wigle plugin, we use GPS EPE
                    50 if math.isnan(self.session.fix.sep) else self.session.fix.sep
                ),
            )

    def run(self):
        logging.info(f"[GPSD-ng] Starting loop")
        while self.running:
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

    def join(self, timeout=None):
        self.running = False
        super().join(timeout)

    def get_devices(self):
        with self.lock:
            return list(self.devices.keys())

    def get_position(self):
        if not (self.configured and self.devices):
            return None
        self.clean()
        with self.lock:
            try:
                if self.main_device and self.devices[self.main_device]:
                    return self.devices[self.main_device]
            except KeyError:
                logging.error(f"[GPSD-ng] No such device: {self.main_device}")

            # Fallback
            # Filter devices without coords
            devices = filter(lambda x: x[1], self.devices.items())
            # Sort by best positionning and most recent
            devices = sorted(
                devices,
                key=lambda x: (
                    -x[1]["Mode"],
                    -datetime.strptime(x[1]["Date"], self.DATE_FORMAT).timestamp(),
                ),
            )
            try:
                coords = devices[0][1]  # Get first and best element
                self.last_position = coords
                return coords
            except IndexError:
                logging.debug(f"[GPSD-ng] No data, using last position: {self.last_position}")
            return self.last_position

    @staticmethod
    def round_position(latitude, longitude):
        return (round(latitude, 4), round(longitude, 4))

    def elevation_key(self, latitude, longitude):
        return str(self.round_position(latitude, longitude))

    def cache_elevation(self, latitude, longitude, elevation):
        key = self.elevation_key(latitude, longitude)
        self.elevation_cache[key] = elevation

    def get_elevation(self, latitude, longitude):
        key = self.elevation_key(latitude, longitude)
        try:
            return self.elevation_cache[key]
        except KeyError:
            return None

    def save_elevation_cache(self):
        if self.elevation_report:
            self.elevation_report.update(data={"elevations": self.elevation_cache})

    def calculate_locations(self, max_dist=100):
        locations = list()

        def append_location(latitude, longitude):
            if not self.elevation_key(latitude, longitude) in self.elevation_cache:
                lat, long = self.round_position(latitude, longitude)
                locations.append({"latitude": lat, "longitude": long})

        if not (coords := self.get_position()):
            return None
        if coords["Mode"] != 2:  # No cache if we have a good Fix
            return
        append_location(coords["Latitude"], coords["Longitude"])
        center = self.round_position(coords["Latitude"], coords["Longitude"])
        for dist in range(10, max_dist + 1, 10):
            for degree in range(0, 360):
                point = geopy.distance.distance(meters=dist).destination(center, bearing=degree)
                append_location(point.latitude, point.longitude)
        seen = []
        return [l for l in locations if l not in seen and not seen.append(l)]  # remove duplicates

    def update_cache_elevation(self):
        if not (
            self.configured and (datetime.now(tz=UTC) - self.last_elevation).total_seconds() > 60
        ):
            return
        self.last_elevation = datetime.now(tz=UTC)
        logging.info(f"[GPSD-ng] Running elevation cache: {len(self.elevation_cache)} available")

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
            logging.info(f"[GPSD-ng] {len(self.elevation_cache)} elevations in cache")
        except Exception as e:
            logging.error(f"[GPSD-ng] Error with open-elevation: {e}")


class GPSD_ng(plugins.Plugin):
    __name__ = "GPSD-ng"
    __GitHub__ = "https://github.com/fmatray/pwnagotchi_GPSD-ng"
    __author__ = "@fmatray"
    __version__ = "1.2.1"
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

    def __init__(self):
        self.gpsd = None
        self.options = dict()
        self.ui_counter = 0

    @property
    def is_ready(self):
        return self.gpsd and self.gpsd.configured

    def on_loaded(self):
        try:
            self.gpsd = GPSD()
            self.gpsd.start()
            logging.info("[GPSD-ng] plugin loaded")
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on loading. Trying later...")

    def on_ready(self, agent):
        try:
            logging.info(f"[GPSD-ng] Disabling bettercap's gps module")
            agent.run("gps off")
        except Exception as e:
            logging.info(f"[GPSD-ng] Bettercap gps was already off.")

    def on_config_changed(self, config):
        logging.info("[GPSD-ng] Reading config")
        self.compact_view = self.options.get("compact_view", False)
        self.gpsdhost = self.options.get("gpsdhost", "127.0.0.1")
        self.gpsdport = int(self.options.get("gpsdport", 2947))
        self.main_device = self.options.get("main_device", None)
        self.use_open_elevation = self.options.get("use_open_elevation", True)
        self.save_elevations = self.options.get("save_elevations", True)
        self.position = self.options.get("position", "127,64")
        self.linespacing = int(self.options.get("linespacing", 10))
        self.lost_face_1 = self.options.get("lost_face_1", "(O_o )")
        self.lost_face_2 = self.options.get("lost_face_1", "( o_O)")
        self.face_1 = self.options.get("face_1", "(•_• )")
        self.face_2 = self.options.get("face_2", "( •_•)")
        handshake_dir = config["bettercap"].get("handshakes")
        self.gpsd.configure(
            self.gpsdhost,
            self.gpsdport,
            self.main_device,
            os.path.join(handshake_dir, ".elevations"),
            self.save_elevations,
        )

    def on_unload(self, ui):
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
                "coordinates",
            ]:
                try:
                    ui.remove_element(element)
                except KeyError:
                    pass

    @staticmethod
    def check_coords(coords):
        return coords and all(  # avoid 0.000... measurements
            [coords["Latitude"], coords["Longitude"]]
        )

    # on_internet_available() is used to update GPS to bettercap.
    # Not ideal but I can't find another function to do it.
    def on_internet_available(self, agent):
        if not self.is_ready:
            return
        if self.use_open_elevation:
            self.gpsd.update_cache_elevation()
        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
            return
        try:
            agent.run(f"set gps.set {coords['Latitude']} {coords['Longitude']}")
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot set bettercap GPS: {e}")

    def on_handshake(self, agent, filename, access_point, client_station):
        if not self.is_ready:
            return
        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
            logging.info("[GPSD-ng] not saving GPS: no fix")
            return

        try:
            agent.run(f"set gps.set {coords['Latitude']} {coords['Longitude']}")
        except Exception as e:
            logging.error(f"[GPSD-ng] Cannot set bettercap GPS: {e}")

        gps_filename = filename.replace(".pcap", ".gps.json")
        logging.info(f"[GPSD-ng] saving GPS to {gps_filename} ({coords})")
        try:
            with open(gps_filename, "w+t") as fp:
                json.dump(coords, fp)
        except Exception as e:
            logging.error(f"[GPSD-ng] Error on saving gps coordinates: {e}")

    def on_ui_setup(self, ui):
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

        if self.compact_view:
            ui.add_element(
                "coordinates",
                Text(
                    value="-",
                    color=BLACK,
                    position=lat_pos,
                    font=fonts.Small,
                ),
            )
            return
        for key, label, label_pos in [
            ("latitude", "lat:", lat_pos),
            ("longitude", "long:", lon_pos),
            ("altitude", "alt:", alt_pos),
            ("speed", "spd:", spd_pos),
        ]:
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

    def lost_mode(self, ui, coords):
        with ui._lock:
            if self.ui_counter == 1:
                ui.set("status", "Where am I???")
                ui.set("face", self.lost_face_1)
            elif self.ui_counter == 2:
                ui.set("face", self.lost_face_2)

            if self.compact_view:
                ui.set("coordinates", "No GPS Data")
            else:
                for i in ["latitude", "longitude", "altitude", "speed"]:
                    ui.set(i, "-")

    @staticmethod
    def calculate_position(coords):
        if coords["Latitude"] < 0:
            lat = f"{-coords['Latitude']:4.6f}S"
        else:
            lat = f"{coords['Latitude']:4.6f}N"
        if coords["Longitude"] < 0:
            long = f"{-coords['Longitude']:4.6f}W"
        else:
            long = f"{coords['Longitude']:4.6f}E"

        alt, spd = "-", "-"
        if coords["Altitude"] != None:
            alt = f"{int(coords['Altitude'])}m"
        if coords["Speed"] != None:
            spd = f"{coords['Speed']:.1f}m/s"
        return lat, long, alt, spd

    def display_face(self, ui):
        with ui._lock:
            if self.ui_counter == 1:
                ui.set("face", self.face_1)
            elif self.ui_counter == 2:
                ui.set("face", self.face_2)

    def compact_view_mode(self, ui, coords):
        with ui._lock:
            if self.ui_counter == 0:
                dev = re.search(r"(^tcp|^udp|tty.*)", coords["Device"], re.IGNORECASE)
                dev = f"{dev[0]}:" if dev else ""
                msg = f"{dev}{coords['Fix']} ({coords['Sats_Valid']}/{coords['Sats']} Sats)"
                ui.set("coordinates", msg)
                return
            lat, long, alt, spd = self.calculate_position(coords)
            if self.ui_counter == 1:
                ui.set("coordinates", f"Speed:{spd} Alt:{alt}")
                return
            ui.set("coordinates", f"{lat},{long}")

    def full_view_mode(self, ui, coords):
        lat, long, alt, spd = self.calculate_position(coords)
        with ui._lock:
            ui.set("latitude", f"{lat} ")
            ui.set("longitude", f"{long} ")
            ui.set("altitude", f"{alt}m ")
            ui.set("speed", f"{spd}m/s ")

    def on_ui_update(self, ui):
        if not self.is_ready:
            return
        self.ui_counter = (self.ui_counter + 1) % 5
        coords = self.gpsd.get_position()

        if not self.check_coords(coords):
            self.lost_mode(ui, coords)
            return

        self.display_face(ui)
        if self.compact_view:
            self.compact_view_mode(ui, coords)
        else:
            self.full_view_mode(ui, coords)

    def on_webhook(self, path, request):
        if not self.is_ready:
            return "<html><head><title>GPSD-ng: Error</title></head><body><code>Plugin not ready</code></body></html>"

        coords = self.gpsd.get_position()
        if not self.check_coords(coords):
            return "<html><head><title>GPSD-ng: Error</title></hexad><body><code>No GPS Data</code></body></html>"
        url = f"https://www.openstreetmap.org/?mlat={coords['Latitude']}&mlon={coords['Longitude']}&zoom=18"
        response = make_response(redirect(url, code=302))
        return response
