"""Reading and saving the settings (GET and POST /api/config).

Only the web UI's settings pages and the API use this, now and then, so
DeviceAPI imports it for each call and lets it go again afterwards: the
checks for every setting are a fair amount of code to keep in memory.
"""
import time
import ujson
from log import Log
from deviceconfig import DeviceConfig, is_valid_utc_offset
import clock
from passwords import hash_password
from wificrypto import encrypt_password

TAG = "API"


def view(api):
    """Return current config as dict, omitting passwords."""
    dc = api.device_config
    return {
        'device': {
            'device_name': dc.device_name,
            'log_level': dc.log_level,
            'watchdog': dc.watchdog,
            'ntp_server': dc.ntp_server,
            'utc_offset_min': dc.utc_offset_min,
            'dst': dc.dst,
        },
        'wlan': {
            'ssid': dc.wlanconfig.ssid,
            'connect_timeout': dc.wlanconfig.connect_timeout,
            'reconnect_wait': dc.wlanconfig.reconnect_wait,
            'max_retries': dc.wlanconfig.max_retries,
        },
        'mqtt': {
            'enabled': dc.mqttconfig.enabled,
            'broker_address': dc.mqttconfig.broker_address,
            'user': dc.mqttconfig.user,
            'clean': dc.mqttconfig.clean,
            'queue_len': dc.mqttconfig.queue_len,
            'ha_discovery': dc.mqttconfig.ha_discovery,
            'topic_base': api._config_cache['mqtt']['topic_base'],
            'ha_prefix': api._config_cache['mqtt']['ha_prefix'],
        },
        'web': {
            'password_set': bool(api.web_password_hash),
        },
    }


def save(api, updates):
    """
    Merge validated updates into config.json using cached config to reduce I/O.
    Only recognised fields are accepted; passwords must be non-empty strings if provided.
    Returns (success, error_message).
    """
    try:
        # Work on a copy of the cached config, so a rejected update can't leave
        # half-applied changes behind for the next save.
        cfg = ujson.loads(ujson.dumps(api._config_cache))
        for section in ('device', 'wlan', 'mqtt'):
            cfg.setdefault(section, {})

        dev = updates.get('device', {})
        wlan = updates.get('wlan', {})
        mqtt = updates.get('mqtt', {})

        # The name is kept in its own file (see set_device_name), so check it
        # here and save it once the rest has been accepted.
        name = dev.get('device_name')
        if name is not None:
            name = str(name).strip()
            if not 0 < len(name) <= 32:
                return False, 'device_name must be 1-32 characters'

        # Device section
        if 'utc_offset_min' in dev:
            if not is_valid_utc_offset(dev['utc_offset_min']):
                return False, 'utc_offset_min must be -720 to 840, in steps of 15'
            cfg['device']['utc_offset_min'] = dev['utc_offset_min']
        if 'dst' in dev:
            if dev['dst'] not in ('none', 'eu', 'us'):
                return False, 'dst must be none, eu or us'
            cfg['device']['dst'] = dev['dst']
        if 'ntp_server' in dev:
            v = str(dev['ntp_server']).strip()
            if len(v) > 64 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in v):
                return False, 'ntp_server must be a host name or address (blank: off)'
            cfg['device']['ntp_server'] = v
        if 'watchdog' in dev:
            cfg['device']['watchdog'] = bool(dev['watchdog'])
        if 'log_level' in dev:
            v = int(dev['log_level'])
            if v not in (0, 1, 2):
                return False, 'log_level must be 0, 1 or 2'
            cfg['device']['log_level'] = v

        # WLAN section (SSID comes from provisioning, not config.json)
        if 'connect_timeout' in wlan:
            v = int(wlan['connect_timeout'])
            if v < 5:
                return False, 'connect_timeout must be >= 5'
            cfg['wlan']['connect_timeout'] = v
        if 'reconnect_wait' in wlan:
            v = int(wlan['reconnect_wait'])
            if v < 5:
                return False, 'reconnect_wait must be >= 5'
            cfg['wlan']['reconnect_wait'] = v
        if 'max_retries' in wlan:
            v = int(wlan['max_retries'])
            if v < 0:
                return False, 'max_retries must be >= 0'
            cfg['wlan']['max_retries'] = v

        # MQTT section
        if 'enabled' in mqtt:
            cfg['mqtt']['enabled'] = bool(mqtt['enabled'])
        if 'broker_address' in mqtt:
            cfg['mqtt']['broker_address'] = str(mqtt['broker_address'])
        if 'user' in mqtt:
            cfg['mqtt']['user'] = str(mqtt['user'])
        if 'password' in mqtt:
            pw = str(mqtt['password'])
            if len(pw) == 0:
                return False, 'MQTT password cannot be empty'
            cfg['mqtt'].pop('password', None)
            cfg['mqtt']['password_iv'], cfg['mqtt']['password_enc'] = encrypt_password(pw)
        if 'clean' in mqtt:
            cfg['mqtt']['clean'] = bool(mqtt['clean'])
        if 'ha_discovery' in mqtt:
            cfg['mqtt']['ha_discovery'] = bool(mqtt['ha_discovery'])
        if 'queue_len' in mqtt:
            v = int(mqtt['queue_len'])
            if v < 1:
                return False, 'queue_len must be >= 1'
            cfg['mqtt']['queue_len'] = v
        if 'topic_base' in mqtt or 'ha_prefix' in mqtt:
            import mqttstate
            for key, check in (('topic_base', mqttstate.topic_base_error), ('ha_prefix', mqttstate.ha_prefix_error)):
                if key in mqtt:
                    v = str(mqtt[key]).strip()
                    error = check(v)
                    if error:
                        return False, error
                    cfg['mqtt'][key] = v

        # Web section: set a new password, or clear it.
        web_updates = updates.get('web', {})
        if web_updates.get('clear_password'):
            cfg['web'] = {'password_hash': ''}
        elif 'password' in web_updates:
            pw = str(web_updates['password'])
            if len(pw) < 4:
                return False, 'Web password must be at least 4 characters'
            cfg['web'] = {'password_hash': hash_password(pw)}

        DeviceConfig.saveConfigToFile(cfg)
        api._config_cache = cfg
        if name is not None and name != api.device_name and not api.set_device_name(name):
            return False, 'device_name could not be saved'
        new_hash = cfg.get('web', {}).get('password_hash', '')
        if new_hash != api.web_password_hash:
            api.web_password_hash = new_hash
            api.accepted_auth = None
        apply_live(api, cfg['device'])
        # A new time zone shows at once (app.py keeps it up to date after).
        api.utc_offset = Log.utc_offset = clock.offset_s(time.time(), api.device_config.utc_offset_min,
                                                          api.device_config.dst)
        api._retire_old_topics()
        Log.info(TAG, 'Config saved.' + (' Restart to apply it all.' if api.restart_required() else ''))
        return True, None

    except Exception as e:
        Log.error(TAG, f'Config save error: {e}')
        return False, str(e)


def apply_live(api, dev):
    """Apply the saved settings that don't need a restart."""
    from api import LIVE_DEVICE_KEYS  # api is loaded already: it's the caller
    dc = api.device_config
    for key in LIVE_DEVICE_KEYS:
        if key in dev:
            setattr(dc, key, dev[key])
    if Log.level != dc.log_level:
        Log.set_log_level(dc.log_level)
