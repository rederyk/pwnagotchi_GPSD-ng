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
# main.plugins.gpsd.enabled = false
# main.plugins.gpsd.gpsdhost = "127.0.0.1"
# main.plugins.gpsd.gpsdport = 2947
# main.plugins.gpsd.compact_view = true



import threading
import json
import logging
from datetime import datetime, UTC
import gps

import pwnagotchi.plugins as plugins
import pwnagotchi.ui.fonts as fonts
from pwnagotchi.ui.components import LabeledValue
from pwnagotchi.ui.view import BLACK


class GPSD(threading.Thread):
    FIXES = {0: "No value", 1: "No fix", 2: "2D fix", 3: "3D fix"}
    DATE_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

    def __init__(self, gpsdhost, gpsdport):
        super().__init__()
        self.gpsdhost = gpsdhost
        self.gpsdport = gpsdport
        self.session = None
        self.devices = dict()
        self.last_position = None
        self.last_clean = datetime.now()
        self.lock = threading.Lock()
        self.running = True
        self.connect()

    def connect(self):
        with self.lock:
            try:
                logging.info(f"[GPSD-ng] trying to connect")
                self.session = gps.gps(
                    host=self.gpsdhost,
                    port=self.gpsdport,
                    reconnect=True,
                    mode=gps.WATCH_ENABLE | gps.WATCH_NEWSTYLE,
                )
            except Exception as e:
                logging.error(f"[GPSD-ng] Error updating GPS: {e}")
                self.session = None

    @staticmethod
    def is_old(date, max_seconds=90):
        try:
            d_time = datetime.strptime(date, self.DATE_FORMAT)
            d_time = d_time.replace(tzinfo=UTC)
        except TypeError:
            return None
        delta = datetime.now(tz=UTC) - d_time
        return delta.total_seconds() > max_seconds

    def clean(self):
        if (datetime.now() - self.last_clean).total_seconds() < 10:
            return
        self.last_clean = datetime.now()
        with self.lock:
            devices_to_clean = []
            for device in filter(lambda x: self.devices[x], self.devices):
                if self.is_old(self.devices[device]["Date"]):
                    devices_to_clean.append(device)
            for device in devices_to_clean:
                self.devices[device] = None
                logging.info(f"[GPSD-ng] Cleaning {device}")

            if self.last_position and self.is_old(self.last_position["Date"], 120):
                self.last_position = None

    def update(self):
        with self.lock:
            if self.session.fix.mode < 2:  # Remove positions without fix
                return
            self.devices[self.session.device] = dict(
                Latitude=self.session.fix.latitude,
                Longitude=self.session.fix.longitude,
                Altitude=(
                    self.session.fix.altitude if self.session.fix.mode > 2 else None
                ),
                Date=self.session.fix.time,
                Updated=self.session.fix.time,  # Wigle plugin
                Mode=self.session.fix.mode,
                Fix=self.FIXES.get(self.session.fix.mode, "Mode error"),
                Sats=len(self.session.satellites),
                Sats_Valid=self.session.satellites_used,
                Device=self.session.device,
                Accuracy=50.0,  # Wigle plugin
            )

    def run(self):
        logging.info(f"[GPSD-ng] Starting GPSD reading loop")
        while self.running:
            self.clean()
            if not self.session:
                self.connected()
            elif self.session.read() == 0 and self.session.device:
                self.update()

    def join(self, timeout=None):
        self.running = False
        super().join(timeout)

    def get_position(self):
        with self.lock:
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
                logging.info(
                    f"[GPSD-ng] No data, using last position: {self.last_position}"
                )
            return self.last_position


class GPSD_ng(plugins.Plugin):
    __author__ = "@fmatray"
    __version__ = "1.0.0"
    __license__ = "GPL3"
    __description__ = (
        "Use GPSD server to save coordinates on handshake. Can use mutiple gps device (gps modules, USB dongle, phone, etc.)"
    )
    LINE_SPACING = 10
    LABEL_SPACING = 0

    def __init__(self):
        self.gpsd = None
        self.options = dict()
        self.ui_counter = 0
        self.running = False

    @staticmethod
    def check_coords(coords):
        return coords and all(  # avoid 0.000... measurements
            [coords["Latitude"], coords["Longitude"]]
        )

    def on_loaded(self):
        if not self.options["gpsdhost"]:
            logging.warning("no GPS detected")
            return
        try:
            self.gpsd = GPSD(self.options["gpsdhost"], self.options["gpsdport"])
            self.gpsd.start()
            self.running = True
            logging.info("[GPSD-ng] plugin loaded")
        except Exception as e:
            self.running = False
            logging.error(f"[GPSD-ng] Error on loading: {e}")

    def on_unload(self, ui):
        self.gpsd.join()
        with ui._lock:
            for element in ["latitude", "longitude", "altitude", "coordinates"]:
                try:
                    ui.remove_element(element)
                except KeyError:
                    pass

    def on_ready(self, agent):
        if not self.running:
            return
        try:
            logging.info(f"[GPSD-ng] Disabling bettercap's gps module")
            agent.run("gps off")
        except Exception:
            logging.info(f"[GPSD-ng] Bettercap gps was already off")

    def on_handshake(self, agent, filename, access_point, client_station):
        if not self.running:
            return
        coords = self.gpsd.get_position()
        logging.info(f"[GPSD-ng] Coordinates: {coords}")
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
        line_spacing = int(self.options.get("linespacing", self.LINE_SPACING))
        try:
            pos = self.options["position"].split(",")
            pos = [int(x.strip()) for x in pos]
            lat_pos = (pos[0] + 5, pos[1])
            lon_pos = (pos[0], pos[1] + line_spacing)
            alt_pos = (pos[0] + 5, pos[1] + (2 * line_spacing))
        except KeyError:
            if ui.is_waveshare_v2():
                lat_pos = (127, 74)
                lon_pos = (122, 84)
                alt_pos = (127, 94)
            elif ui.is_waveshare_v1():
                lat_pos = (130, 70)
                lon_pos = (125, 80)
                alt_pos = (130, 90)
            elif ui.is_inky():
                lat_pos = (127, 60)
                lon_pos = (122, 70)
                alt_pos = (127, 80)
            elif ui.is_waveshare144lcd():
                lat_pos = (67, 73)
                lon_pos = (62, 83)
                alt_pos = (67, 93)
            elif ui.is_dfrobot_v2():
                lat_pos = (127, 74)
                lon_pos = (122, 84)
                alt_pos = (127, 94)
            else:
                lat_pos = (127, 51)
                lon_pos = (122, 61)
                alt_pos = (127, 71)

        if self.options["compact_view"]:
            ui.add_element(
                "coordinates",
                LabeledValue(
                    color=BLACK,
                    label="coords",
                    value="-",
                    position=lat_pos,
                    label_font=fonts.Small,
                    text_font=fonts.Small,
                    label_spacing=0,
                ),
            )
            return
        for key, label, pos in [
            ("latitude", "lat:", lat_pos),
            ("longitude", "long:", lon_pos),
            ("altitude", "alt:", alt_pos),
        ]:
            ui.add_element(
                key,
                LabeledValue(
                    color=BLACK,
                    label=label,
                    value="-",
                    position=pos,
                    label_font=fonts.Small,
                    text_font=fonts.Small,
                    label_spacing=self.LABEL_SPACING,
                ),
            )

    def on_ui_update(self, ui):
        if not self.running:
            return
        with ui._lock:
            self.ui_counter = (self.ui_counter + 1) % 5
            coords = self.gpsd.get_position()

            if not self.check_coords(coords):
                ui.set("status", "Where am I???")
                if self.ui_counter == 1:
                    ui.set("face", "(O_o )")
                elif self.ui_counter == 2:
                    ui.set("face", "( o_O)")

                if self.options["compact_view"]:
                    ui.set("coordinates", "No Data")
                else:
                    for i in ["latitude", "longitude", "altitude"]:
                        ui.set(i, "-")
                return

            if self.ui_counter == 1:
                ui.set("face", "(•_• )")
            elif self.ui_counter == 2:
                ui.set("face", "( •_•)")

            if self.options["compact_view"]:
                if self.ui_counter == 1:
                    msg = f"{coords['Fix']} ({coords['Sats_Valid']}/{coords['Sats']} Sats)"
                    ui.set("coordinates", msg)
                    return
                elif self.ui_counter == 2:
                    ui.set("coordinates", f"GPS:{coords['Device']}")
                    return

            if coords["Latitude"] < 0:
                lat = f"{-coords['Latitude']:4.4f}S"
            else:
                lat = f"{coords['Latitude']:4.4f}N"
            if coords["Longitude"] < 0:
                long = f"{-coords['Longitude']:4.4f}W"
            else:
                long = f"{coords['Longitude']:4.4f}E"

            if self.options["compact_view"]:
                alt = ""
                if coords["Altitude"] != None:
                    alt = f" {int(coords['Altitude'])}m"
                ui.set("coordinates", f"{lat},{long}{alt}")
            else:
                # last char is sometimes not completely drawn ¯\_(ツ)_/¯
                # using an ending-whitespace as workaround on each line
                ui.set("latitude", f"{lat} ")
                ui.set("longitude", f"{long} ")
                if coords["Altitude"] != None:
                    ui.set("altitude", f"{coords['Altitude']:5.1f}m ")
                else:
                    ui.set("altitude", f"No data")
