import sys
import base64
import json
import inspect
from importlib import util
from http.server import BaseHTTPRequestHandler

# Import relative path https://docs.python.org/3/library/importlib.html#importing-a-source-file-directly
__vc_spec = util.spec_from_file_location("__VC_HANDLER_MODULE_NAME", "./__VC_HANDLER_ENTRYPOINT")
__vc_module = util.module_from_spec(__vc_spec)
sys.modules["__VC_HANDLER_MODULE_NAME"] = __vc_module
__vc_spec.loader.exec_module(__vc_module)
__vc_variables = dir(__vc_module)


def format_headers(headers, decode=False):
    keyToList = {}
    for key, value in headers.items():
        if decode and 'decode' in dir(key) and 'decode' in dir(value):
            key = key.decode()
            value = value.decode()
        if key not in keyToList:
            keyToList[key] = []
        keyToList[key].append(value)
    return keyToList


if 'handler' in __vc_variables or 'Handler' in __vc_variables:
    base = __vc_module.handler if ('handler' in __vc_variables) else  __vc_module.Handler
    if not issubclass(base, BaseHTTPRequestHandler):
        print('Handler must inherit from BaseHTTPRequestHandler')
        print('See the docs https://vercel.com/docs/runtimes#advanced-usage/advanced-python-usage')
        exit(1)

    print('using HTTP Handler')
    from http.server import HTTPServer
    import http
    import _thread

    server = HTTPServer(('', 0), base)
    port = server.server_address[1]

    def vc_handler(event, context):
        _thread.start_new_thread(server.handle_request, ())

        payload = json.loads(event['body'])
        path = payload['path']
        headers = payload['headers']
        method = payload['method']
        encoding = payload.get('encoding')
        body = payload.get('body')

        if (
            (body is not None and len(body) > 0) and
            (encoding is not None and encoding == 'base64')
        ):
            body = base64.b64decode(body)

        request_body = body.encode('utf-8') if isinstance(body, str) else body
        conn = http.client.HTTPConnection('0.0.0.0', port)
        conn.request(method, path, headers=headers, body=request_body)
        res = conn.getresponse()

        return_dict = {
            'statusCode': res.status,
            'headers': format_headers(res.headers),
        }

        data = res.read()

        try:
            return_dict['body'] = data.decode('utf-8')
        except UnicodeDecodeError:
            return_dict['body'] = base64.b64encode(data).decode('utf-8')
            return_dict['encoding'] = 'base64'

        return return_dict

elif 'app' in __vc_variables:
    if (
        not inspect.iscoroutinefunction(__vc_module.app) and
        not inspect.iscoroutinefunction(__vc_module.app.__call__)
    ):
        print('Web Server Gateway Interface (WSGI) not support')
        exit(1)
    else:
        print('using Asynchronous Server Gateway Interface (ASGI)')
        # Originally authored by Jordan Eremieff and included under MIT license:
        # https://github.com/erm/mangum/blob/07ce20a0e2f67c5c2593258a92c03fdc66d9edda/mangum/__init__.py
        # https://github.com/erm/mangum/blob/07ce20a0e2f67c5c2593258a92c03fdc66d9edda/LICENSE
        import asyncio
        import enum
        from urllib.parse import urlparse
        # from werkzeug.datastructures import Headers

        def get_event_loop():
            try:
                return asyncio.get_running_loop()
            except RuntimeError:
                if sys.version_info < (3, 10):
                    return asyncio.get_event_loop()
                else:
                    return asyncio.get_event_loop_policy().get_event_loop()

        class ASGICycleState(enum.Enum):
            REQUEST = enum.auto()
            RESPONSE = enum.auto()


        class ASGICycle:
            def __init__(self, scope):
                self.scope = scope
                self.body = b''
                self.state = ASGICycleState.REQUEST
                self.app_queue = None
                self.response = {}

            def __call__(self, app, body):
                """
                Receives the application and any body included in the request, then builds the
                ASGI instance using the connection scope.
                Runs until the response is completely read from the application.
                """
                loop = get_event_loop()
                self.app_queue = asyncio.Queue(**({'loop': loop} if sys.version_info < (3, 10) else {}))
                self.put_message({'type': 'http.request', 'body': body, 'more_body': False})

                asgi_instance = app(self.scope, self.receive, self.send)

                asgi_task = loop.create_task(asgi_instance)
                loop.run_until_complete(asgi_task)
                return self.response

            def put_message(self, message):
                self.app_queue.put_nowait(message)

            async def receive(self):
                """
                Awaited by the application to receive messages in the queue.
                """
                message = await self.app_queue.get()
                return message

            async def send(self, message):
                """
                Awaited by the application to send messages to the current cycle instance.
                """
                message_type = message['type']

                if self.state is ASGICycleState.REQUEST:
                    if message_type != 'http.response.start':
                        raise RuntimeError(
                            f"Expected 'http.response.start', received: {message_type}"
                        )

                    status_code = message['status']
                    headers = {k: v for k, v in message.get("headers", [])}

                    self.on_request(headers, status_code)
                    self.state = ASGICycleState.RESPONSE

                elif self.state is ASGICycleState.RESPONSE:
                    if message_type != 'http.response.body':
                        raise RuntimeError(
                            f"Expected 'http.response.body', received: {message_type}"
                        )

                    body = message.get('body', b'')
                    more_body = message.get('more_body', False)

                    # The body must be completely read before returning the response.
                    self.body += body

                    if not more_body:
                        self.on_response()
                        self.put_message({'type': 'http.disconnect'})

            def on_request(self, headers, status_code):
                self.response['statusCode'] = status_code
                # self.response['headers'] = format_headers(headers, decode=True)
                self.response["headers"] = {k.decode(): v.decode() for k, v in headers.items()}

            def on_response(self):
                if self.body:
                    self.response['body'] = base64.b64encode(self.body).decode('utf-8')
                    self.response['encoding'] = 'base64'

        class Lifespan:
            startup_event = asyncio.Event()
            shutdown_event = asyncio.Event()
            app_queue = asyncio.Queue()

            def __init__(self, app):
                self.app = app
                
            async def run(self):
                try:
                    await self.app({"type": "lifespan"}, self.receive, self.send)
                finally:
                    self.startup_event.set()
                    self.shutdown_event.set()

            async def send(self, message):
                assert message["type"] in (
                    "lifespan.startup.complete",
                    "lifespan.shutdown.complete",
                )

                if message["type"] == "lifespan.startup.complete":
                    self.startup_event.set()
                elif message["type"] == "lifespan.shutdown.complete":
                    self.shutdown_event.set()
                else:  # pragma: no cover
                    raise RuntimeError(
                        f"Expected lifespan message type, received: {message['type']}"
                    )

            async def receive(self):
                message = await self.app_queue.get()
                return message

            async def wait_startup(self):
                await self.app_queue.put({"type": "lifespan.startup"})
                await self.startup_event.wait()

            async def wait_shutdown(self):
                await self.app_queue.put({"type": "lifespan.shutdown"})
                await self.shutdown_event.wait()

        lifespan = None
        def vc_handler(event, context):
            global lifespan
            payload = json.loads(event['body'])

            headers = payload.get('headers', {})

            body = payload.get('body', b'')
            if payload.get('encoding') == 'base64':
                body = base64.b64decode(body)
            elif not isinstance(body, bytes):
                body = body.encode()

            url = urlparse(payload['path'])
            query = url.query.encode()
            path = url.path

            scope = {
                'server': (headers.get('host', 'lambda'), headers.get('x-forwarded-port', 80)),
                'client': (headers.get(
                    'x-forwarded-for', headers.get(
                        'x-real-ip', payload.get(
                            'true-client-ip', ''))), 0),
                'scheme': headers.get('x-forwarded-proto', 'http'),
                'root_path': '',
                'query_string': query,
                'headers': [[k.lower().encode(), v.encode()] for k, v in headers.items()],
                'type': 'http',
                'http_version': '1.1',
                'method': payload['method'],
                'path': path,
                'raw_path': path.encode(),
            }
            if not lifespan:
                lifespan = Lifespan(__vc_module.app)
                loop = get_event_loop()
                loop.create_task(lifespan.run())
                loop.run_until_complete(lifespan.wait_startup())

            asgi_cycle = ASGICycle(scope)
            response = asgi_cycle(__vc_module.app, body)
            if lifespan:
                loop = get_event_loop()
                loop.run_until_complete(lifespan.wait_shutdown())
            return response

else:
    print('Missing variable `handler` or `app` in file "__VC_HANDLER_ENTRYPOINT".')
    print('See the docs https://vercel.com/docs/runtimes#advanced-usage/advanced-python-usage')
    exit(1)
