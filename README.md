# GPSD-ng
Use GPSD server to retreive and save coordinates on handshake. Can use mutiple gps device (gps modules, USB dongle, phone, etc.)

![Front image of pwnagotchi](pwnagotchi.jpeg)

__Advantages with gpsd server__:
- GPS configuration independant from pwnagotchi
- Early position polling
- No position lost on bettercap/pwnagotchi restarts
- High compatibility (device, protocol, vendor, version): NMEA/ublox modules (Serial), USB modules, Android/IPhone
- Non blocking access to GPS information
- GPS hotplugin
- Compatibility with other applications like chrony
- Compatible with NTRIP/RTK/RTCM

__Exemple__:\
GPS module/dongle and/or Phone (IOS/Android) ------> GPSD ------> GPSD-ng ------> Pwnagotchi

# Features
- Client to GPSD server with multi device management
- Several customable UI modes and a Web UI
- Save position on handshake and fallback for pcap without position
- Unit option: metric or imperial
- Use open elevation and cache for 2D fix.
- Show completeness statistic (percentage of pcap files with a valid position file)
- Two hooks: ```on_position_available(coords)``` and ```on_position_lost()```
- Non blocking plugin

# Install GPSD server
- Install binaries:
  - APT method (version 3.22): ```apt-get install gpsd gpsd-clients python3-gps```
  - Download and install (version 3.24) with ```dpkg -i```: https://archive.raspberrypi.org/debian/pool/untested/g/gpsd/
  - Build from source (version 3.25): https://gpsd.gitlab.io/gpsd/building.html
- Configure GPSD (/etc/default/gpsd) and uncomment one DEVICES:
```
# Default settings for the gpsd init script and the hotplug wrapper.

# Start the gpsd daemon automatically at boot time
START_DAEMON="true"

# Use USB hotplugging to add new USB devices automatically to the daemon
USBAUTO="false" 

# Devices gpsd should collect to at boot time.
# They need to be read/writeable, either by user gpsd or the group dialout.
DEVICES=""

# Other options you want to pass to gpsd
GPSD_OPTIONS="-n" # add -D3 if you need to debug
```

## Serial GPS
- Check in raspi-config -> Interface Options -> Serial Port:
  - __Disable__ Serial Port login
  - __Enable__ Serial Port 
- Check your gps baudrate.
- Set ```DEVICES="-s BAUDRATE /dev/ttyS0"``` in /etc/default/gpsd

## USB GPS
- Set ```USBAUTO="true"``` in /etc/default/gpsd
- No need to set DEVICES

## Phone GPS
- On your phone:
  - Setup the plugin bt-tether and check you can ping your phone
  - Install a GPS app:
    - __Android__(not tested):
      - BlueNMEA: https://github.com/MaxKellermann/BlueNMEA
      - gpsdRealay: https://github.com/project-kaat/gpsdRelay
    - __IOS__: GPS2IP (tested but paid app)
      - Set "operate in background mode"
      - Set "Connection Method" -> "Socket" -> "Port Number" -> 4352
      - Set "Network selection" -> "Hotspot"
    - Both cases activate GGA messages to have "3D fix"
- Check your gpsd configuration with gpsmon or cgps
- Set ```DEVICES="tcp://PHONEIP:4352"``` in /etc/default/gpsd

## Multiple devices
You can configure several devices in DEVICES  
Ex: ```DEVICES="-s BAUDRATE /dev/ttyS0 tcp://PHONEIP:4352"```

# Install plugin
- Install GEOPY: ```apt-get install python3-geopy```
- Copy gpsd-ng.py and gpsd-ng.html to your custom plugins directory

# Configure plugin (Config.toml)
```
main.plugins.gpsd.enabled = true

# Options with default settings.
# Add only if you need customisation
main.plugins.gpsd-ng.gpsdhost = "127.0.0.1"
main.plugins.gpsd-ng.gpsdport = 2947
main.plugins.gpsd-ng.main_device = "/dev/ttyS0" # if not provided, the puglin will try to retreive the most accurate position
main.plugins.gpsd-ng.update_timeout = 120 # default 120, Delay without update before deleting the position. 0 = no timeout
main.plugins.gpsd-ng.fix_timeout = 120 # default 120, Delay without fix before deleting the position. 0 = no timeout
main.plugins.gpsd-ng.use_open_elevation = true # if true, use open-elevation API to retreive missing altitudes. Use it if you have a poor GPS signal.
main.plugins.gpsd-ng.save_elevations = true # if true, elevations cache will be saved to disk. Be carefull as it can grow fast if move a lot.
main.plugins.gpsd-ng.view_mode = "compact" # "compact", "full", "none" 
main.plugins.gpsd-ng.fields = "info,speed,altitude" # list or string of fields to display
main.plugins.gpsd-ng.units = "metric" # "metric" or "imperial"
main.plugins.gpsd-ng.display_precision = 6 # display precision for latitude and longitude
main.plugins.gpsd-ng.position = "127,64"
main.plugins.gpsd-ng.show_faces = true # if false, doesn't show face. Ex if you use PNG faces
main.plugins.gpsd-ng.lost_face_1 = "(O_o )"
main.plugins.gpsd-ng.lost_face_2 = "( o_O)"
main.plugins.gpsd-ng.face_1 = "(•_• )"
main.plugins.gpsd-ng.face_2 = "( •_•)"
```

# Usage
## Retreive GPS Position
This plugin can be used for wardriving with the wigle plugin, for example.
- __Outdoor__: GPS module/dongle works fine. 
- __Indoor__: is the GPS module/dongle doesn't work, you can use your phone.

If main_device is not set (default), the device with the most accurate (base on fix information) and most recent position, will be selected.

If main_device is set, the plugin will use that main device position, if available.  
If the main device is not available, it will fallback to other devices.

If the device can only get 2D positions for some reason (poor signal, wrong device orientation, bad luck, etc.), the plugin can use open-elevation API to try to ask current altitude.
To avoid many call to the API, each request asks for points every ~10m around you, in a diameter of 200m. This cache can be saved to disk.

After a delay (set by update_timeout) without data update for a device, the position will be deleted.  
If update_timeout is set to 0, positions never expire. 

After a delay (set by fix_timeout) without data fix for a device, the last position will be deleted.  
If fix_timeout is set to 0, positions fix never expire. Usefull for keeping last position when goind indoor.

## Improve positioning with RTCM (need gpsd 3.25)
If you have a GPs module or dongle with RTCM capabilities, you can activate with GPSD.
Exemple with a Ublox (firmware 34.10) and GPSD 3.25:
- ublox setup:
  - ubxtool -p MON-VER | grep PROT  -> retreive ublox version XX.YY (34.10 for me)
  - export UBXOPTS="-P XX.YY -v 2"
  - ubxtool -e RTCM3
- GPSD setup:
  - Find a local (< 30km) RTK provider (https://rtkdata.online/network)
  - You need host/port and mountpoint information
  - Check with the following command. It should stream binary data.\
curl -v -H "Ntrip-Version: Ntrip/2.0" -H "User-Agent: NTRIP theSoftware/theRevision" http://[user:pwd@]host:2101/mountpoint -o -
  - Add "ntrip://[user:pwd@]host:2101/mountpoint" to DEVICES in GPSD configuration
  - Now GPSD command should look like with ps: 'gpsd -n ntrip://host:2101/MOUNTPOINT -s 38400 /dev/ttyS0'

Of course, you can still append your phone 'gpsd -N -D3 ntrip://host:2101/MOUNTPOINT -s 38400 /dev/ttyS0 tcp://172.20.10.1:4352'
More info on: https://gpsd.gitlab.io/gpsd/ubxtool-examples.html#_survey_in_and_rtcm

## UI views
The "compact" view mode (default) option show gps informations, on one line, in rotation:
- Latitude,Longitude
- Info: Device source + Fix information
- Speed, Altitude

If you prefer a more traditionnal view, use "full" mode:
- Latitude
- Longitude
- Altitude
- Speed

You can show or not with the fields option. by default, it will display all.
If you want a clear display, use "none", nothing will be display.

## Web views
You should take a look at the Web UI with fancy graphs ;-)

# Units
You can use metric or imperial units for altitude(m or ft) and speed (m/s or ft/s).
This only changes on display, not gps.json files, as Wigle needs metric units. 
Default is metric because it's the International System. 

## Handshake
- Set gps position to bettercap (it's also done on internet_available() and on_unfiltered_ap_list())
- Saves position informations into "gps.json" (compatible with Wigle and webgpsmap)

Note: During on_unfiltered_ap_list(), if an access point whith pcap files but without gps file is detected, this plugin will save the current position for that AP. This is a fallback, if the position was not available during handshake().

## Bettercap
Gps option is set to off. Position is update by the plugin to Bettercap, on handshake, internet_available and on_unfiltered_ap_list.

## Developpers
This plugin adds two plugin hooks, triggered every 10 seconds:  
- If a position is available, the hook ```on_position_available(coords)``` is called with a dictionnary (see below)
- If no position is available, the hook ```on_position_lost() is called

The coords dictionnary:
- Latitude, Longitude (float)
- Altitude (float): Sea elevation 
- Speed (float): Horizontal speed
- Date, Updated (datetime): Last fix
- Mode (int 2 or 3), Fix (str): Fix mode (2D or 3D). 0 and 1 are removec
- Sats (int), Sats_used(int): Nb of seen satellites and used
- Device (str): GPS device (ex: /dev/ttyS0)
- Accuracy (int): default 50
All data are metric only.

## Troubleshooting: Have you tried to turn it off and on again?
### "[GPSD-ng] Error while importing matplotlib for generate_polar_plot()"
matplotlib is not up to date in /home/pi/.pwn:
- su -
- cd /home/pi/.pwn
- source bin/activate
- pip install scipy numpy matplotlib --upgrade

### "[Errno 2] No such file or directory: '/usr/local/share/pwnagotchi/custom-plugins/gpsd-ng.html'"
gpsd-ng.html is missing, just copy :-)

### "TypeError: JSONDecoder.init() got an unexpected keyword argument 'encoding'"
The gpsd python library, called "gps", is old (around 2020).  
An update will do the trick.

### "[GPSD-ng] Error while connecting to GPSD: [Errno 111] Connection refused"
- GPSD server is not running: 
 - Try to restart gpsd: sudo systemctl restart gpsd
 - Check status: sudo systemctl status gpsd
 - Check logs
- GPSD server is not configured. Check install section.
- GPSD configuration is wrong:
 - Try "cgps" or "gpsmon" to check if you have readings

### GPSD server is running and the plugin is connected but I have no position
- Check with "cgps" if gpsd can retreive data from gps modules
- The plugin filters data without fix. You can check on the plugin's webpage.

# TODO
- [ ] Run around the World!
 
# Based on:
- Pwnagotchi: 
  - https://github.com/evilsocket
  - https://github.com/jayofelony/pwnagotchi
- GPSD: https://gpsd.gitlab.io/gpsd/index.html
- Original plugin and fork: 
  - https://github.com/kellertk/pwnagotchi-plugin-gpsd
  - https://github.com/nothingbutlucas/pwnagotchi-plugin-gpsd
- Polar graph: https://github.com/rai68/gpsd-easy/blob/main/gpsdeasy.py

Have fun !
