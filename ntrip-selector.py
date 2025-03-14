import requests
import logging
import csv
import json
import os
import glob
import re
import math
from datetime import datetime, UTC
from dataclasses import dataclass, field, asdict
import subprocess
from typing import Union, Dict, Tuple, Optional
from threading import Lock
import geopy.distance
import pwnagotchi.plugins as plugins


@dataclass
class Caster:
    host: str
    port: str
    identifier: str
    operator: str
    nmea: bool
    country: str
    latitude: float
    longitude: float
    fallback_host: str
    fallback_port: str


@dataclass
class Network:
    identifier: str
    operator: str


@dataclass
class Stream:
    mountpoint: str
    identifier: str
    format: str
    carrier: str
    network: str
    country: str
    latitude: float
    longitude: float
    nmea: bool
    auth: str


@dataclass
class SourceTable:
    url: str
    casters: dict[str, Caster] = field(default_factory=dict)
    networks: dict[str, Network] = field(default_factory=dict)
    streams: dict[str, Stream] = field(default_factory=dict)

    def add_caster(self, caster: Caster):
        self.casters[caster.operator] = caster

    def add_network(self, network: Network):
        self.networks[network.operator] = network

    def add_stream(self, stream: Stream):
        self.streams[stream.mountpoint] = stream

    @staticmethod
    def find_closest(
        objects: Dict[str, Union[Caster, Stream]], current_position: Tuple[float, float]
    ) -> Tuple[Optional[Union[Caster, Stream]], float]:
        nearest_object, nearest = None, float("inf")
        for object in objects:
            if not (abs(objects[object].latitude) <= 90 and abs(objects[object].longitude) <= 180):
                continue
            object_point = (
                objects[object].latitude,
                objects[object].longitude,
            )
            dist = geopy.distance.distance(current_position, object_point)
            if dist < nearest:
                nearest_object, nearest = object, dist
        if nearest_object:
            return objects[nearest_object], nearest
        return None, float("inf")

    def find_closest_caster(
        self, current_position: Tuple[float, float]
    ) -> Tuple[Optional[Caster], float]:
        return self.find_closest(self.casters, current_position)

    def find_closest_stream(
        self, current_position: Tuple[float, float]
    ) -> Tuple[Optional[Stream], float]:
        return self.find_closest(self.streams, current_position)

    def find_closest_ntrip_url(
        self, current_position: tuple[float, float]
    ) -> tuple[Optional[str], float]:
        stream, dist = self.find_closest_stream(current_position)
        url = None
        if stream:
            caster, _ = self.find_closest_caster(current_position)
            if caster:
                url = f"ntrip://{caster.host}:{caster.port}/{stream.mountpoint}"
            else:
                url = f"{self.url}/{stream.mountpoint}".replace("http://", "ntrip://")
        return (url, dist)


@dataclass(slots=True)
class Ntrip(plugins.Plugin):
    __author__: str = "fmatray"
    __version__: str = "1.0.0"
    __license__: str = "GPL3"
    __description__: str = "Manage NTRIP for GPSD."
    broadcasters: list[str] = field(
        default_factory=lambda: [
            # "DE": ["http://euref-ip.net:2101"], AUTH
            "http://crtk.net:2101",  # FR
            # "IT": ["http://euref-ip.asi.it:2101"], AUTH
            "http://gnss1.tudelft.nl:2101",  # NL
        ]
    )
    handshake_dir: str = ""
    sourcetables: dict[str, SourceTable] = field(default_factory=dict)
    latitude: float = float("inf")
    longitude: float = float("inf")
    MAX_DIST: float = 30
    gpsd_pid: int = 0
    current_url: Optional[str] = None
    gpsd_positioning: bool = False
    ready: bool = False
    last_update: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    lock: Lock = field(default_factory=Lock)

    @property
    def position(self):
        return (self.latitude, self.longitude)

    def __post_init__(self) -> None:
        super(plugins.Plugin, self).__init__()
        self.gpsd_pid = self.get_gpsd_pid()

    def set_position(self, lat: float, long: float):
        if lat == None or long == None:
            self.latitude = float("inf")
            self.longitude = float("inf")
            return
        self.latitude = lat
        self.longitude = long

    def position_iset(self):
        return math.isfinite(self.latitude) and math.isfinite(self.longitude)

    def get_gpsd_pid(self) -> int:
        try:
            return int(subprocess.check_output(["pidof", "-s", "gpsd"]))
        except subprocess.CalledProcessError:
            return 0

    def on_loaded(self) -> None:
        logging.info("[NTRIP-selector] Plugin loaded")

    def on_config_changed(self, config: dict) -> None:
        self.handshake_dir = config["bettercap"].get("handshakes")
        if extra_broadcasters := self.options.get("extra_broadcasters", None):
            if isinstance(extra_broadcasters, list):
                self.broadcasters.extend(extra_broadcasters)

        self.retreive_initial_position()
        # Try to retreive the last saved position
        if not self.position_iset() and (
            position_files := glob.glob(os.path.join(self.handshake_dir, "*.g*.json"))
        ):
            self.set_position_from_file(max(position_files, key=os.path.getctime))

        self.ready = True
        logging.info("[NTRIP-selector] Plugin configured")

    def on_unload(self, ui):
        self.unset_ntrip_server()
        logging.info("[NTRIP-selector] Plugin unloaded")

    @staticmethod
    def read_caster(line: list) -> Caster:
        return Caster(
            host=line[1],
            port=line[2],
            identifier=line[3],
            operator=line[4],
            nmea=line[5],
            country=line[6],
            latitude=float(line[7]),
            longitude=float(line[8]),
            fallback_host=line[9],
            fallback_port=line[10],
        )

    @staticmethod
    def read_network(line: list) -> Network:
        return Network(identifier=line[1], operator=line[2])

    @staticmethod
    def read_stream(line: list) -> Stream:
        return Stream(
            mountpoint=line[1],
            identifier=line[2],
            format=line[3],
            carrier=line[5],
            network=line[7],
            country=line[8],
            latitude=float(line[9]),
            longitude=float(line[10]),
            nmea=line[11],
            auth=line[15],
        )

    def create_sourcetable(self, url: str, data: str) -> SourceTable:
        sourcetable = SourceTable(url=url)
        for line in csv.reader(data.split("\r\n"), delimiter=";"):
            try:
                line_type = line[0]
            except IndexError:
                continue
            match line_type:
                case "CAS":
                    sourcetable.add_caster(self.read_caster(line))
                case "NET":
                    sourcetable.add_network(self.read_network(line))
                case "STR":
                    sourcetable.add_stream(self.read_stream(line))
                case "ENDSOURCETABLE":
                    pass
                case _:
                    logging.error(f"[NTRIP-selector] Unkown type: {line_type}")
        return sourcetable

    def retrieve_source_tables(self):
        """Retrieve source tables from broadcasters in the specified region."""
        session = requests.Session()
        for broadcaster in self.broadcasters:
            try:
                response = session.get(broadcaster)
                response.raise_for_status()
                self.sourcetables[broadcaster] = self.create_sourcetable(
                    broadcaster, response.content.decode()
                )
            except requests.RequestException as e:
                logging.error(
                    f"[NTRIP-selector] Cannot retrieve sourcetables from {broadcaster}: {e}"
                )

    def retreive_initial_position(self):
        try:
            response = requests.get("http://ip-api.com/json/?fields=status,message,lat,lon,query")
            response.raise_for_status()
            position = response.json()
            if position["status"] == "success":
                logging.info(f"{position} {self.position}")
            else:
                logging.error(
                    f"[NTRIP-selector] Cannot retrieve actual position: {position['message']}"
                )
        except requests.RequestException as e:
            logging.error(f"[NTRIP-selector] Cannot retrieve actual position: {e}")

    def set_position_from_file(self, file: str) -> bool:
        try:
            with open(file, "r") as fb:
                position = json.load(fb)
                self.set_position(position["Latitude"], position["Longitude"])
                logging.info(f"[NTRIP-selector] Position set from file ({file})")
        except Exception as e:
            logging.error(f"[NTRIP-selector] Error while reading file {file}: {e}")
            return False
        return True

    def on_unfiltered_ap_list(self, agent, aps) -> None:
        if not self.ready or self.lock.locked():
            return
        if self.gpsd_positioning:
            return
        with self.lock:
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
                if (
                    os.path.exists(gps_filename)
                    and os.path.getsize(gps_filename)
                    and self.set_position_from_file(gps_filename)
                ):
                    return

                geo_filename = os.path.join(self.handshake_dir, f"{hostname}_{mac}.geo.json")
                # geo.json exist with size>0 => next
                if (
                    os.path.exists(geo_filename)
                    and os.path.getsize(geo_filename)
                    and self.set_position_from_file(gps_filename)
                ):
                    return

    def on_internet_available(self, agent):
        if not self.ready or self.lock.locked():
            return
        with self.lock:
            if not self.sourcetables:
                self.retrieve_source_tables()
            if not self.position_iset():
                self.retreive_initial_position()

    def on_position_available(self, position: dict):
        with self.lock:
            self.set_position(
                position.get("Latitude", float("inf")), position.get("Longitude", float("inf"))
            )
            self.gpsd_positioning = True

    def on_position_lost(self):
        with self.lock:
            self.set_position(float("inf"), float("inf"))
            self.gpsd_positioning = False

    def select_ntrip_server(self) -> Optional[str]:
        if not self.position_iset():
            return None
        nearest_url, nearest = None, float("inf")
        if self.position_iset() and self.sourcetables:
            for key in self.sourcetables:
                url, dist = self.sourcetables[key].find_closest_ntrip_url(self.position)
                if dist <= self.MAX_DIST and dist < nearest:
                    nearest_url, nearest = url, dist
        return nearest_url

    def unset_ntrip_server(self):
        try:
            if self.current_url:
                logging.info(f"[NTRIP-selector] Unsetting NTRIP server: {self.current_url}")
                subprocess.run(["gpsdctl", "remove", self.current_url], check=True, timeout=10)
            self.current_url = None
        except subprocess.CalledProcessError as e:
            logging.error(f"[NTRIP-selector] error while unsetting ntrip: {e}")

    def set_ntrip_server(self, url: str):
        try:
            logging.info(f"[NTRIP-selector] Setting NTRIP server: {url}")
            subprocess.run(["gpsdctl", "add", url], check=True, timeout=10)
            self.current_url = url
        except subprocess.CalledProcessError as e:
            logging.error(f"[NTRIP-selector] error while setting ntrip: {e}")

    def on_ui_update(self, ui):
        if not self.ready or self.lock.locked():
            return
        if ((now := datetime.now(tz=UTC)) - self.last_update).total_seconds() < 60:
            return
        self.last_update = now
        with self.lock:
            if (gpsd_pid := self.get_gpsd_pid()) != self.gpsd_pid:
                logging.info(f"[NTRIP-selector] GPSD restarted.")
                self.gpsd_pid = gpsd_pid
                self.set_ntrip_server(self.current_url)
            elif (new_url := self.select_ntrip_server()) != self.current_url:
                logging.info(f"[NTRIP-selector] Setting new ntrip server")
                self.unset_ntrip_server()
                self.set_ntrip_server(new_url)
