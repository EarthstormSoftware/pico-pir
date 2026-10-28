"""The MQTT client on a Pico: mqtt_as (in the product's lib/), behind the calls base.mqtt makes of a client.

base.mqtt connects again by itself when a connection drops, so mqtt_as's own reconnecting is turned off, and Wi-Fi is
left to base.pico.wlan. Its publish and subscribe wait for the connection to come back; these fail at once instead
(OSError), as base.mqtt expects.
"""
from base.compat import sleep_ms

_Managed = None


def _client_class():
    global _Managed
    if _Managed is None:
        import mqtt_as

        class Managed(mqtt_as.MQTTClient):
            async def wifi_connect(self, quick=False):
                if not self._sta_if.isconnected():
                    raise OSError('Wi-Fi not connected')

            async def _keep_connected(self):
                pass

        _Managed = Managed
    return _Managed


class Client:
    def __init__(self, client_id, host, port=1883, user='', password='', will=None, clean=True, queue_len=1):
        import mqtt_as
        config = dict(mqtt_as.config)
        config.update(client_id=client_id, server=host, port=port, user=user, password=password, clean=clean,
                      queue_len=max(1, queue_len), will=(will[0], will[1], will[2], 0) if will else None)
        self._base = mqtt_as.MQTT_base
        self._client = _client_class()(config)

    async def connect(self):
        await self._client.connect()

    async def publish(self, topic, message, retain=False):
        if not self._client.isconnected():
            raise OSError('not connected')
        await self._base.publish(self._client, topic, message, retain, 0)

    async def subscribe(self, topic, qos=1):
        if not self._client.isconnected():
            raise OSError('not connected')
        await self._base.subscribe(self._client, topic, qos)

    async def message(self):
        """The next (topic, message, retained) received, as text, or None once the connection has dropped."""
        queue = self._client.queue
        while True:
            if queue._ri != queue._wi:
                topic, message, retained = await queue.__anext__()
                try:
                    return topic.decode(), message.decode(), retained
                except UnicodeError:
                    continue  # not text: nothing here sends that
            if not self._client.isconnected():
                return None
            await sleep_ms(50)

    def close(self):
        """Closes the connection (not Wi-Fi, which mqtt_as's own close() would)."""
        client = self._client
        client._has_connected = False
        if client._isconnected:
            client._reconnect()  # stops its tasks and closes the socket
        else:
            client._close()
