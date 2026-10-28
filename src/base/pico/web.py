"""A Pico's own routes, next to the base's (base.web):

  POST /api/name                 {"name"}: the device's name, from the next start
  GET, POST /api/wifi            {"fallback": {"enabled", "after_minutes", "for_minutes"}}: the Wi-Fi fallback, as on a Pi
  POST /api/setup-mode           {"confirm": true}: restarts into setup mode (to change the Wi-Fi network)
  POST /api/factory-reset        {"confirm": true}: forgets the settings, the name and the Wi-Fi, then setup mode
  GET /api/update                the firmware files (with SHA-256), MicroPython's .mpy version, free space and the last
                                 update's state; then POST /api/update/begin (a manifest), PUT /api/update/file/<path>
                                 for each file, and POST /api/update/commit, which restarts to apply it (see ota.py).
                                 Only with a web password set: replacing the firmware is more than changing a setting.
"""
from base.compat import asyncio
from base.log import Log
from base.microdot import Request

TAG = 'HTTP'


def routes(app, device):
    from base import pico as platform
    # Larger bodies (update files) are streamed rather than held in memory
    Request.max_body_length = 8192
    Request.max_content_length = 64 * 1024

    def body(request):
        try:
            data = request.json
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {}

    def restarting(request, what, do):
        if body(request).get('confirm') is not True:
            return {'success': False, 'error': 'confirm must be true'}, 400

        async def later():
            await asyncio.sleep(0.5)  # the answer goes first
            Log.warn(TAG, what)
            do()
        asyncio.create_task(later())
        return {'success': True}

    @app.post('/api/name')
    async def name(request):
        error = platform.rename(body(request).get('name'))
        if error:
            return {'success': False, 'error': error}, 400
        return {'success': True, 'restart_required': True}

    @app.get('/api/wifi')
    async def wifi(request):
        s = device.settings
        return {'available': True, 'fallback': {'enabled': s.get('wlan', 'fallback'),
                                                'after_minutes': s.get('wlan', 'fallback_after_min'),
                                                'for_minutes': s.get('wlan', 'fallback_for_min')}}

    @app.post('/api/wifi')
    async def set_wifi(request):
        data = body(request).get('fallback')
        if not isinstance(data, dict):
            return {'success': False, 'error': 'fallback needs enabled, after_minutes and for_minutes'}, 400
        error, _ = device.settings.update({'wlan': {'fallback': data.get('enabled') is True,
                                                    'fallback_after_min': data.get('after_minutes'),
                                                    'fallback_for_min': data.get('for_minutes')}})
        if error:
            return {'success': False, 'error': error}, 400
        return {'success': True}

    @app.post('/api/setup-mode')
    async def setup_mode(request):
        return restarting(request, 'Restarting into setup mode', platform.setup_mode)

    @app.post('/api/factory-reset')
    async def factory_reset(request):
        return restarting(request, 'Factory reset', platform.factory_reset)

    updater = []  # made on first use

    def refused():
        if not device.settings.get('web', 'password'):
            return {'success': False, 'error': 'Set a web password before updating over Wi-Fi'}, 403
        if not updater:
            from base.pico import ota
            updater.append(ota.Updater())
        return None

    @app.get('/api/update')
    async def update_status(request):
        no = refused()
        if no:
            return no
        from base.pico import ota
        return {'version': device.product.VERSION, 'mpy': ota.mpy_version(), 'free': ota.free_bytes(),
                'state': ota.state(), 'files': ota.installed_files()}

    @app.post('/api/update/begin')
    async def update_begin(request):
        no = refused()
        if no:
            return no
        error = updater[0].begin(request.json)
        if error:
            return {'success': False, 'error': error}, 400
        Log.info(TAG, 'Receiving update {}'.format(request.json['version']))
        return {'success': True}

    @app.put('/api/update/file/<path:path>')
    async def update_file(request, path):
        no = refused()
        if no:
            return no
        error = await updater[0].receive(path, request.stream, request.content_length)
        if error:
            Log.warn(TAG, 'Update file refused: {}'.format(error))
            return {'success': False, 'error': error}, 400
        return {'success': True}

    @app.post('/api/update/commit')
    async def update_commit(request):
        no = refused()
        if no:
            return no
        error = updater[0].commit()
        if error:
            return {'success': False, 'error': error}, 400
        Log.info(TAG, 'Update ready: restarting to apply it')
        asyncio.create_task(device.restart_soon())
        return {'success': True}
