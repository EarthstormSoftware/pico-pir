"""
The web page and JSON API, on port 80, with Microdot.

Every product has: GET / (its page, www/index.html), the base's shared style and script (/base.css, /base.js),
GET /api/info, GET and POST /api/config, POST /api/restart and POST /api/reset. A light also has POST
/api/power/on, /api/power/off, /api/power/toggle, /api/mode ({"mode", "parameters"}) and /api/brightness
({"brightness"}), as PlasmaStick's, and POST /api/alert ({"colour", "seconds", "on_ms", "off_ms", "brightness"}, each
optional, and "test": true to show one with alerts switched off) and /api/alert/stop.

An ApplPi app also has what it asks of the OS: GET /api/update (the last update's result), POST /api/update (a .tar
of the app's folder: the OS installs it on trial and restarts the app with it), GET and POST /api/clock
({"time_zone", "ntp"}, and the zones to choose from) and POST /api/reboot ({"confirm": true}: restarts the Pi). With
setup mode in the OS: GET and POST /api/wifi ({"fallback": {"enabled", "after_minutes", "for_minutes"}}: setup mode
when Wi-Fi doesn't connect), POST /api/setup-mode and /api/factory-reset (each {"confirm": true}). The product can
add its own (routes(app, device)).

Two guards before every request: a POST that a browser sends from another site's page is refused (so a malicious page
can't use the device through the browser), and with a web password set, every request needs it (HTTP Basic auth,
any username). It's sent unencrypted, so it keeps out other people on the network rather than a determined attacker.
"""
from base.compat import asyncio, a2b_base64, beside
from base.log import Log
from base.microdot import Microdot, Response
from base.passwords import check_password

TAG = 'HTTP'
BASE_WWW = beside(__file__, 'www')
TEXT = {'Content-Type': 'text/plain; charset=utf-8'}
# The largest app a Pi takes over the network (ApplPi OS takes up to 64 MiB)
UPDATE_MAX = 16 * 1024 * 1024


def origin_allowed(headers):
    """False for a POST a browser sent from another site's page. Browsers send Origin on such requests; scripts like
    curl send none and are allowed."""
    origin = headers.get('Origin')
    if not origin:
        return True
    return origin.split('://', 1)[-1].rstrip('/') == headers.get('Host', '')


def password_ok(headers, password_hash):
    """True if HTTP Basic auth in headers carries the password hashed in password_hash (any username)."""
    auth = headers.get('Authorization', '')
    if not auth.startswith('Basic '):
        return False
    try:
        decoded = a2b_base64(auth[6:].strip()).decode('utf-8')
    except (ValueError, UnicodeError):
        return False
    return check_password(decoded.partition(':')[2], password_hash)


def _file(path, content_type):
    response = Response.send_file(path, content_type=content_type)
    response.headers['Cache-Control'] = 'no-store'
    return response


def create(device):
    app = Microdot()
    accepted = {'auth': None}  # the last Authorization header that passed, so the hash isn't redone per request

    @app.before_request
    async def guard(request):
        if request.method == 'POST' and not origin_allowed(request.headers):
            Log.warn(TAG, 'Refused a cross-site POST to {} from {}'.format(request.path, request.headers.get('Origin')))
            return 'Cross-site request refused', 403, TEXT
        password_hash = device.settings.get('web', 'password')
        auth = request.headers.get('Authorization')
        if password_hash and not (auth and auth == accepted['auth']):
            if password_ok(request.headers, password_hash):
                accepted['auth'] = auth
            else:
                return 'Password required', 401, dict(TEXT, **{
                    'WWW-Authenticate': 'Basic realm="{}", charset="UTF-8"'.format(device.product.NAME)})

    @app.get('/')
    async def index(request):
        return _file(device.www + '/index.html', 'text/html; charset=utf-8')

    @app.get('/base.css')
    async def base_css(request):
        return _file(BASE_WWW + '/base.css', 'text/css; charset=utf-8')

    @app.get('/base.js')
    async def base_js(request):
        return _file(BASE_WWW + '/base.js', 'text/javascript; charset=utf-8')

    @app.get('/favicon.ico')
    async def favicon(request):
        return '', 204, {}

    @app.get('/api/info')
    async def info(request):
        return device.info()

    @app.get('/api/config')
    async def get_config(request):
        view = device.settings.view()
        # Which settings restart the device when changed, so the page's Save button can say so beforehand
        view['_restart'] = ['{}.{}'.format(section, key) for section, settings in device.settings.schema.items()
                            for key, setting in settings.items() if setting.restart]
        return view

    @app.post('/api/config')
    async def set_config(request):
        error, changed = device.settings.update(_json(request))
        if error:
            return {'success': False, 'error': error}, 400
        restart = device.settings.restart_needed(changed)
        device.applied(changed)
        if restart:
            asyncio.create_task(device.restart_soon())
        return {'success': True, 'restart_required': restart}

    @app.post('/api/restart')
    async def restart(request):
        asyncio.create_task(device.restart_soon())
        return {'success': True}

    @app.post('/api/reset')
    async def reset(request):
        # Needs confirm, so a stray request can't wipe the settings
        if _json(request).get('confirm') is not True:
            return {'success': False, 'error': 'confirm must be true'}, 400
        device.reset()
        asyncio.create_task(device.restart_soon())
        return {'success': True}

    platform = device.platform
    if hasattr(platform, 'os_requests'):
        # What an ApplPi app asks of the OS. A new version comes whole, as a .tar of the app's folder
        from base.microdot import Request
        Request.max_content_length = Request.max_body_length = UPDATE_MAX

        def asked(do):
            try:
                do()
            except platform.RequestError as e:
                return {'success': False, 'error': str(e)}, 400
            return {'success': True}

        @app.get('/api/update')
        async def update_result(request):
            return {'available': platform.os_requests(), 'result': platform.update_result()}

        @app.post('/api/update')
        async def update(request):
            body = request.body or b''
            if len(body) < 512 or body[257:262] != b'ustar':
                return {'success': False, 'error': "Send a .tar of the app's folder"}, 400
            Log.info(TAG, 'An update ({} bytes): handing it to the OS'.format(len(body)))
            return asked(lambda: platform.update(body))

        @app.get('/api/clock')
        async def clock(request):
            try:
                settings = platform.clock_settings()
            except platform.RequestError as e:
                return {'success': False, 'error': str(e)}, 400
            return dict(settings, zones=platform.time_zones())

        @app.post('/api/clock')
        async def set_clock(request):
            data = _json(request)
            def do():
                if data.get('time_zone') is not None:
                    platform.set_time_zone(str(data['time_zone']))
                if data.get('ntp') is not None:
                    platform.set_ntp(str(data['ntp']).strip())
            return asked(do)

        def restarting(request, available, what, do):
            """For the requests that restart the Pi: {"confirm": true} needed, the answer sent, then done."""
            if _json(request).get('confirm') is not True:
                return {'success': False, 'error': 'confirm must be true'}, 400
            if not available:
                return {'success': False, 'error': 'Only on ApplPi OS v0.4 or later.' if do == platform.restart_device
                        else 'Only on ApplPi OS with setup mode (the release after v0.4).'}, 400

            async def later():
                await asyncio.sleep(0.5)  # the answer goes first
                Log.warn(TAG, what)
                try:
                    do()
                except platform.RequestError as e:
                    Log.error(TAG, "Couldn't: {}".format(e))
            asyncio.create_task(later())
            return {'success': True}

        @app.post('/api/reboot')
        async def reboot(request):
            return restarting(request, platform.os_requests(), 'Restarting the Pi', platform.restart_device)

        @app.get('/api/wifi')
        async def wifi(request):
            if not platform.os_setup():
                return {'available': False}
            try:
                return {'available': True, 'fallback': platform.wifi_fallback()}
            except platform.RequestError as e:
                return {'success': False, 'error': str(e)}, 400

        @app.post('/api/wifi')
        async def set_wifi(request):
            data = _json(request).get('fallback') or {}
            try:
                enabled, after, for_ = data['enabled'], int(data['after_minutes']), int(data['for_minutes'])
            except (KeyError, TypeError, ValueError):
                return {'success': False, 'error': 'fallback needs enabled, after_minutes and for_minutes'}, 400
            return asked(lambda: platform.set_wifi_fallback(enabled is True, after, for_))

        @app.post('/api/setup-mode')
        async def setup_mode(request):
            return restarting(request, platform.os_setup(), 'Restarting the Pi into setup mode', platform.setup_mode)

        @app.post('/api/factory-reset')
        async def factory_reset(request):
            return restarting(request, platform.os_setup(), 'Factory reset: the Pi forgets its settings',
                              platform.factory_reset)

    if hasattr(platform, 'routes'):
        platform.routes(app, device)  # a Pico's (base.pico.web)

    if device.light is not None:
        light = device.light

        @app.post('/api/power/on')
        async def power_on(request):
            light.set(on=True)
            return {'success': True, 'state': 'on'}

        @app.post('/api/power/off')
        async def power_off(request):
            light.set(on=False)
            return {'success': True, 'state': 'off'}

        @app.post('/api/power/toggle')
        async def power_toggle(request):
            return {'success': True, 'state': 'on' if light.toggle() else 'off'}

        @app.post('/api/mode')
        async def mode(request):
            data = _json(request)
            parameters = data.get('parameters')
            if data.get('mode') == 'Colour' and parameters is None:
                # Colour with no colour: the default (the page's Default button)
                parameters = {'Colour': device.settings.get('device', 'default_colour')}
            error = light.set(mode=data.get('mode'), parameters=parameters)
            if error:
                return {'success': False, 'error': error}, 400
            return {'success': True, 'mode': light.mode}

        @app.post('/api/brightness')
        async def brightness(request):
            error = light.set(brightness=_json(request).get('brightness'))
            if error:
                return {'success': False, 'error': error}, 400
            return {'success': True, 'brightness': light.brightness}

        @app.post('/api/alert')
        async def alert(request):
            data = _json(request)
            error = light.alert(colour=data.get('colour'), seconds=data.get('seconds'), on_ms=data.get('on_ms'),
                                off_ms=data.get('off_ms'), brightness=data.get('brightness'),
                                test=data.get('test') is True)
            if error:
                return {'success': False, 'error': error}, 400
            return {'success': True}

        @app.post('/api/alert/stop')
        async def alert_stop(request):
            light.stop_alert()
            return {'success': True}

    if hasattr(device.product, 'routes'):
        device.product.routes(app, device)

    @app.errorhandler(404)
    async def not_found(request):
        return 'Not Found', 404, TEXT

    @app.errorhandler(500)
    async def internal_error(request):
        return 'Internal Server Error', 500, TEXT

    return app


def _json(request):
    try:
        data = request.json
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}
