"""Wi-Fi: joins the saved network, and joins again when it drops.

The Wi-Fi fallback (wlan.fallback): if Wi-Fi hasn't connected once since the board started for wlan.fallback_after_min
minutes, it restarts into setup mode, which runs for wlan.fallback_for_min and then restarts on the saved settings, so a
router that was just slow to start is joined then. Off, it keeps trying.
"""
import time
import network
from base.compat import sleep_ms
from base.log import Log

TAG = 'WLAN'

# cyw43 link status 2: associated, waiting for DHCP. The network module has no constant for it.
STAT_NOIP = 2

INACTIVE, CONNECTING, CONNECTED, WAITING = 0, 1, 2, 3


class Wlan:
    def __init__(self, ssid, password, settings, led, setup_mode, hostname):
        self.ssid = ssid
        self.password = password
        self.settings = settings  # read where used: wlan.connect_timeout, reconnect_wait, max_retries, fallback...
        self.led = led
        self.setup_mode = setup_mode  # restarts into setup mode
        self.state = INACTIVE
        self.was_connected = False
        self.ever_connected = False
        self.scanned = False
        self.retries = 0
        self.started = time.time()
        self.connect_started = 0
        self.connected_at = 0
        self.disconnected_at = 0
        try:
            # The DHCP and mDNS name (<name>.local): set before the interface connects
            network.hostname(hostname)
            Log.info(TAG, 'Host name: {}'.format(network.hostname()))
        except Exception as e:
            Log.warn(TAG, 'Could not set the host name: {}'.format(e))
        self.wlan = network.WLAN(network.STA_IF)
        self.connect()

    def _get(self, key):
        return self.settings.get('wlan', key)

    def connect(self):
        self.wlan.active(True)
        # The radio's power saving dozes between router beacons, which delays or drops incoming requests now and
        # then. The board runs from mains power anyway, so keep the radio awake.
        try:
            self.wlan.config(pm=self.wlan.PM_NONE)
        except (AttributeError, ValueError, OSError) as e:
            Log.warn(TAG, 'Could not turn off Wi-Fi power saving: {}'.format(e))
        # Scan (for the log) only the first time, before the event loop starts: scan() blocks for a second or two
        if not self.scanned:
            self.scanned = True
            Log.info(TAG, 'Networks in range:')
            for found in self.wlan.scan():
                Log.info(TAG, found)
        Log.info(TAG, 'Connecting to {}'.format(self.ssid))
        self.led.set(True, True, 1000)
        self.state = CONNECTING
        self.retries += 1
        self.connect_started = time.time()
        self.wlan.connect(self.ssid, self.password)

    def _connected(self):
        self.state = CONNECTED
        if not self.was_connected:
            self.led.set(True)
            self.was_connected = self.ever_connected = True
            self.retries = 0
            self.connected_at = time.time()
            Log.info(TAG, 'Connected to {} as {}, in {} s'.format(self.ssid, self.wlan.ifconfig()[0],
                                                                   self.connected_at - self.connect_started))

    def _disconnected(self, reconnect):
        self.state = WAITING if reconnect else INACTIVE
        if not reconnect:
            self.wlan.active(False)
        self.disconnected_at = time.time()
        if self.was_connected:
            Log.info(TAG, 'The connection lasted {} s'.format(self.disconnected_at - self.connected_at))
        self.was_connected = False
        self.led.set(False)
        Log.info(TAG, 'Disconnected')

    def is_connected(self):
        return self.state == CONNECTED and self.wlan.isconnected()

    def can_retry(self):
        return self._get('max_retries') == 0 or self.retries < self._get('max_retries')

    def fallback_due(self, now):
        """Whether it's time for setup mode: the fallback on, and no connection since the board started for
        fallback_after_min minutes (or no more attempts allowed)."""
        if not self._get('fallback') or self.ever_connected:
            return False
        return now - self.started >= self._get('fallback_after_min') * 60 or not self.can_retry()

    def _failed(self, reason):
        if self.fallback_due(time.time()):
            Log.error(TAG, '{}, and no connection since the board started ({} attempts): restarting into setup '
                           'mode'.format(reason, self.retries))
            self.setup_mode()
        if self.can_retry():
            self._disconnected(True)
        else:
            self._disconnected(False)
            Log.error(TAG, '{}, and no more attempts'.format(reason))

    async def run(self):
        while True:
            now = time.time()
            if self.wlan.isconnected():
                if not self.was_connected:
                    self._connected()
            elif self.state == CONNECTING:
                status = self.wlan.status()
                if status == network.STAT_GOT_IP:
                    self._connected()
                elif status in (network.STAT_CONNECTING, STAT_NOIP):
                    if now - self.connect_started > self._get('connect_timeout'):
                        self._failed('Connecting timed out')
                else:
                    self._failed('Could not connect (status {})'.format(status))
            elif self.state == WAITING:
                if self.fallback_due(now):
                    self._failed('Still not connected')
                elif now - self.disconnected_at > self._get('reconnect_wait'):
                    if self.can_retry():
                        self.connect()
                    else:
                        self.state = INACTIVE
                        Log.warn(TAG, 'No more attempts')
            elif self.state == CONNECTED:
                self._disconnected(True)
            elif self.state == INACTIVE and self.can_retry():
                self.connect()
            await sleep_ms(200)
